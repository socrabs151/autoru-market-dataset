"""Сбор одной выборки.

Правила, которые здесь соблюдаются и не должны нарушаться:

- не более 0.5 запроса в секунду, с разбросом задержек (решение 7);
- плановый лимит запросов на прогон, который агент не повышает молча;
- отказ сайта и пустая выборка — разные вещи, и обе не молчат (решение 13);
- полнота проверяется, а не предполагается: если обход не довёл до конца,
  это попадает в отчёт словами, а не остаётся незамеченным (решение 10).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, Field

from autoru_market_dataset.core.errors import Cancelled, LimitReached, NoResults
from autoru_market_dataset.core.models import Coverage, Filters, Page, Progress
from autoru_market_dataset.frontends.autoru.client import DEFAULT_HEADERS, AutoRuClient
from autoru_market_dataset.frontends.autoru.refine import split, splittable
from autoru_market_dataset.storage.duck import Store

log = structlog.get_logger(__name__)


@dataclass
class _Node:
    """Узел обхода: запрос, глубина и того, какие измерения уже отпали."""

    filters: Filters
    depth: int
    exhausted: frozenset[str] = frozenset()
    #: Сколько объявлений сайт насчитал в родителе. По этому числу видно,
    #: режет ли измерение выборку.
    parent_total: int = 0
    #: Каким измерением этот узел получен. Нужно, чтобы понять, что
    #: измерение не сузило выборку, и не пробовать его снова.
    dimension: str = ""


class CrawlConfig(BaseModel):
    """Настройки прогона."""

    rps: float = Field(default=0.5, gt=0, description="запросов в секунду")
    jitter: float = Field(default=0.4, ge=0, le=1, description="разброс задержки, доля интервала")
    max_requests: int = Field(default=100, gt=0, description="плановый лимит запросов на прогон")
    max_depth: int = Field(default=2, ge=0, le=4, description="насколько дробить выборку")
    db_path: Path = Field(default=Path("data/autoru.duckdb"))
    timeout: float = Field(default=90.0, gt=0)


class RateLimiter:
    """Ограничитель темпа. Держит интервал с разбросом.

    Пауза асинхронная. Раньше здесь стоял блокирующий ``time.sleep``, что
    не годится: в HTTP-сервисе сбор идёт в фоне вместе с обслуживанием
    запросов, и блокировка event loop останавливала бы оба сразу.
    """

    def __init__(self, rps: float, jitter: float = 0.4) -> None:
        self._interval = 1.0 / rps
        self._jitter = jitter
        self._next_allowed = 0.0
        self._random = random.Random()

    async def wait(self) -> float:
        """Подождать до следующего допустимого запроса. Возвращает паузу."""
        loop = asyncio.get_running_loop()
        delay = self._next_allowed - loop.time()
        pause = delay if delay > 0 else 0.0
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            self._next_allowed = loop.time()
        spread = self._interval * self._jitter
        self._next_allowed += self._interval + self._random.uniform(-spread, spread)
        return pause


class Crawler:
    """Собирает выборку и складывает результат в хранилище."""

    def __init__(self, client: AutoRuClient, store: Store, config: CrawlConfig) -> None:
        self._client = client
        self._store = store
        self._config = config
        self._limiter = RateLimiter(config.rps, config.jitter)
        self._seen: set[str] = set()
        self._cancelled = False
        self.progress = Progress()

    def cancel(self) -> None:
        """Остановить сбор.

        Флаг, а не отмена задачи: сбор делает сетевые запросы по одному, и
        между ними нужно успеть заметить просьбу. Немедленное прерывание
        посреди загрузки страницы оставило бы данные в непредсказуемом виде.
        """
        self._cancelled = True

    def reset_progress(self) -> None:
        """Сбросить счётчики прогона.

        Нужно перед повторным обходом: иначе полнота второго прогона
        считалась бы от накопленного и показывала бы несуществующее
        перевыполнение.
        """
        self._seen.clear()
        self._cancelled = False
        self.progress = Progress()

    async def aclose(self) -> None:
        """Освободить сетевые ресурсы.

        Обход не заводит соединений сам — он берёт готовый клиент, — но
        закрыть его обязан, если он достанется именно этому объекту. Поэтому
        закрытие есть у клиента, а не здесь: место, где клиент создан, и
        место, где он закрыт, должны совпадать.
        """
        closer = getattr(self._client, "aclose", None)
        if closer is not None:
            await closer()

    async def _spend_request(self) -> None:
        if self._cancelled:
            raise Cancelled("сбор остановлен")
        if self.progress.requests >= self._config.max_requests:
            raise LimitReached(f"плановый лимит {self._config.max_requests} запросов исчерпан")
        await self._limiter.wait()
        if self._cancelled:
            raise Cancelled("сбор остановлен")
        self.progress.requests += 1

    async def _visit(self, filters: Filters, depth: int) -> Page:
        await self._spend_request()
        page = await self._client.fetch(self._client.url_for(filters))
        page.depth = depth
        self.progress.pages_done += 1
        self.progress.offers_seen += len(page.offers)
        self.progress.offers_stored += self._store.save_many(page.offers)
        self._seen.update(offer.offer_id for offer in page.offers)
        return page

    async def crawl(self, filters: Filters) -> Progress:
        """Собрать первую страницу выборки."""
        url = self._client.url_for(filters)
        log.info("crawl.start", url=url, max_requests=self._config.max_requests)
        page = await self._visit(filters, 0)
        self.progress.coverage = Coverage(
            expected=page.total_offers,
            covered=len(self._seen),
            leaves=1,
            complete=page.total_offers <= max(page.page_size, 1),
            reason=None
            if page.total_offers <= max(page.page_size, 1)
            else f"собрана одна страница из {page.total_pages}",
        )
        log.info(
            "crawl.done",
            offers=len(page.offers),
            total_offers=page.total_offers,
            requests=self.progress.requests,
        )
        return self.progress

    async def crawl_full(self, filters: Filters) -> Progress:
        """Собрать выборку целиком, дробя её на непересекающиеся части.

        Обход в ширину: каждый запрос проверяется, и если объявлений больше,
        чем помещается в страницу, он заменяется своими частями. Когда части
        кончились или закончился лимит запросов, остаток фиксируется в отчёте.

        Полнота считается по **различным** собранным объявлениям против того,
        что сайт назвал в корне. Считать по сумме размеров частей нельзя:
        части перекрываются, и объявление из пересечения засчиталось бы
        дважды — первая попытка так и дала 76 при 50 существующих.
        """
        log.info(
            "crawl.full.start",
            url=self._client.url_for(filters),
            max_requests=self._config.max_requests,
            max_depth=self._config.max_depth,
        )
        root = await self._visit(filters, 0)
        expected = root.total_offers
        page_size = max(root.page_size, 1)

        queue: list[_Node] = []
        leaves = 0
        empty_parts = 0
        incomplete: str | None = None

        if root.total_offers <= page_size:
            leaves = 1
        else:
            children, dimension = split(filters, root.offers, 0, self._config.max_depth)
            if not children:
                incomplete = "выборку нечем разделить: не осталось измерений"
                leaves = 1
            else:
                queue = [
                    _Node(
                        filters=child,
                        depth=1,
                        parent_total=root.total_offers,
                        dimension=dimension,
                    )
                    for child in children
                ]

        while queue:
            if self.progress.requests >= self._config.max_requests:
                incomplete = (
                    f"плановый лимит {self._config.max_requests} запросов исчерпан, "
                    f"осталось {len(queue)} частей"
                )
                break
            node = queue.pop(0)
            try:
                page = await self._visit(node.filters, node.depth)
            except LimitReached as exc:
                incomplete = str(exc)
                break
            except NoResults:
                # Часть фильтров при переборе всегда оказывается пустой.
                # Это не сбой и не повод останавливать обход.
                empty_parts += 1
                log.debug("crawl.part.empty", url=self._client.url_for(node.filters))
                continue

            # Измерение, не изменившее число объявлений, помечаем как
            # бесполезное **в этом узле**: все объявления и так попадают под
            # это значение.
            #
            # Помечать нужно именно узел, а не измерение целиком. Раньше
            # метка была общей, и это ломало обход: у X5 2023 все 50 машин на
            # автомате, поэтому transmission помечался бесполезным, и обход
            # переставал пробовать привод — хотя 49 из 50 объявлений были
            # именно полноприводными, и разбиение по приводу сузило бы
            # выборку. Пропущенные 12 объявлений были следствием этой метки.
            exhausted = node.exhausted
            if node.dimension and node.parent_total and page.total_offers >= node.parent_total:
                exhausted = exhausted | {node.dimension}
                log.debug(
                    "crawl.dimension.exhausted",
                    dimension=node.dimension,
                    parent_total=node.parent_total,
                    total=page.total_offers,
                )

            # Проверка «поместилось ли» идёт по ожидаемому числу объявлений.
            # Раньше здесь стояло «влезло ли в страницу», и это давало две
            # ошибки сразу: полнота считалась по страницам вместо выборки,
            # а объявления с промежуточных узлов попадали в счёт повторно.
            if page.total_offers <= page_size:
                leaves += 1
                continue

            children, dimension = split(
                node.filters,
                page.offers,
                node.depth,
                self._config.max_depth,
                exhausted,
            )
            if children:
                queue.extend(
                    _Node(
                        filters=child,
                        depth=node.depth + 1,
                        parent_total=page.total_offers,
                        exhausted=exhausted,
                        dimension=dimension,
                    )
                    for child in children
                )
                continue

            # Измерения кончились: страница не влезает, а разбить её нечем.
            # Это честный «недобор», и он обязан быть виден в отчёте.
            leaves += 1
            incomplete = (
                f"часть {self._client.url_for(node.filters)}: "
                f"{page.total_offers} объявлений, а разделить уже нечем"
            )
            log.warning(
                "crawl.part.unsplittable",
                url=node.filters,
                total=page.total_offers,
                page_size=page_size,
                exhausted=sorted(exhausted),
            )

        # Части перекрываются не всегда, а сайт между прогонами меняет
        # счётчики. Поэтому «больше, чем ожидалось» — это не ошибка счёта,
        # а повод сказать правду: полнота оценена сверху.
        covered_count = len(self._seen)
        if incomplete is None and covered_count < expected:
            # Части кончились, но недобора никто не зафиксировал: значит,
            # причина в том, что обход посчитал узел листом ошибочно.
            incomplete = (
                f"собрано {covered_count} из {expected}: "
                f"части перечислены, но полнота не достигнута"
            )
        self.progress.coverage = Coverage(
            expected=expected,
            covered=covered_count,
            leaves=leaves,
            empty_parts=empty_parts,
            complete=covered_count >= expected and incomplete is None,
            reason=incomplete,
        )
        log.info(
            "crawl.full.done",
            requests=self.progress.requests,
            leaves=leaves,
            empty_parts=empty_parts,
            expected=expected,
            covered=len(self._seen),
            complete=self.progress.coverage.complete,
            reason=incomplete,
        )
        if not splittable(filters):
            log.info("crawl.note", note="фильтр задан целиком, делить нечего")
        return self.progress


def build_http(config: CrawlConfig) -> httpx.AsyncClient:
    """HTTP-клиент с настройками проекта."""
    return httpx.AsyncClient(
        timeout=config.timeout,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
    )


__all__ = ["CrawlConfig", "Crawler", "RateLimiter", "build_http"]
