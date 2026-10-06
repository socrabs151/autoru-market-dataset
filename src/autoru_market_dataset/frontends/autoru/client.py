"""HTTP-клиент страниц листинга auto.ru."""

from __future__ import annotations

import httpx
import structlog

from autoru_market_dataset.core.errors import BlockedBySite, CaptchaRequired, NoResults
from autoru_market_dataset.core.models import Filters, Page
from autoru_market_dataset.core.normalize import normalize_all
from autoru_market_dataset.frontends.autoru.listing import check_block, extract_offers
from autoru_market_dataset.frontends.autoru.ris import assemble, split_chunks
from autoru_market_dataset.frontends.autoru.urls import BASE_URL, build_url

log = structlog.get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class AutoRuClient:
    """Загрузка и разбор страниц листинга auto.ru."""

    def __init__(self, http: httpx.AsyncClient, base_url: str = BASE_URL) -> None:
        self._http = http
        self._base_url = base_url

    def url_for(self, filters: Filters) -> str:
        return build_url(filters)

    async def aclose(self) -> None:
        """Закрыть сетевые соединения."""
        await self._http.aclose()

    async def fetch(self, url: str) -> Page:
        """Загрузить страницу и вернуть нормализованные объявления.

        :raises NoResults: по адресу нет объявлений (404). Нормальный исход
            при переборе значений фильтра, а не поломка.
        :raises BlockedBySite: сайт отказал или потребовал проверки.
        :raises ParseError: страница есть, а состояние собрать не удалось.
        :raises EmptyResultError: сайт показывает объявления, а их нет.
        """
        log.info("fetch.start", url=url)
        response = await self._http.get(url)
        html = response.text
        log.info("fetch.response", status=response.status_code, bytes=len(response.content))

        # Пустой ответ — не сбой. При переборе значений фильтра часть адресов
        # обязательно оказывается пустой, и это штатный исход.
        #
        # Признак проверяется по содержимому, а не по коду ответа: сайт
        # отдаёт пустую выборку и с кодом 200, и с кодом 404, причём в обоих
        # случаях это одна и та же страница без фрагментов состояния.
        # Ориентироваться на код было бы ошибкой: один и тот же факт
        # пришлось бы обрабатывать двумя разными ветками.
        if not split_chunks(html):
            if response.status_code == 404:
                raise NoResults(f"по адресу нет объявлений: {url}")
            raise NoResults(f"выборка пуста: {url}")
        if response.status_code >= 500:
            raise BlockedBySite(f"auto.ru ответил {response.status_code}")
        check_block(html)

        chunks = split_chunks(html)
        state, order = assemble(chunks)
        raw_offers, total, pages, page_size = extract_offers(state)

        page = Page(
            url=url,
            offers=normalize_all(raw_offers),
            total_offers=total,
            total_pages=pages,
            page_size=page_size,
        )
        log.info(
            "fetch.page",
            offers=len(page.offers),
            total_offers=total,
            total_pages=pages,
            fragments=len(chunks),
            order=order,
        )
        return page


__all__ = ["DEFAULT_HEADERS", "USER_AGENT", "AutoRuClient", "CaptchaRequired"]
