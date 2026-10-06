"""HTTP-сервис парсера.

Сервис нужен системе управления парсерами: она ставит задачу, следит за
прогрессом и останавливает сбор. Ядро при этом ничего не знает про HTTP —
обмен идёт через тот же ``services.crawl``, что и у командной строки.

Состояние прогонов живёт в памяти процесса. Это осознанно: прогоны короткие,
переживать перезапуск незачем, а постоянное хранилище задач заметно
усложнило бы и код, и интерфейс. Собранные данные лежат в DuckDB, и
переживают перезапуск они.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from autoru_market_dataset.core.errors import (
    BlockedBySite,
    Cancelled,
    CaptchaRequired,
    LimitReached,
    NoResults,
    ParserError,
)
from autoru_market_dataset.core.models import DEFAULT_DB, Filters, Progress
from autoru_market_dataset.frontends.autoru.client import AutoRuClient
from autoru_market_dataset.services.crawl import CrawlConfig, Crawler, build_http
from autoru_market_dataset.services.history import (
    dataset_summary,
    hash_consistency,
    price_changes,
    price_history,
)
from autoru_market_dataset.storage.duck import Store

log = structlog.get_logger(__name__)

RunState = Literal["pending", "running", "done", "failed", "cancelled", "blocked"]


class RunRequest(BaseModel):
    """Запрос на запуск сбора."""

    filters: Filters = Field(default_factory=Filters)
    full: bool = Field(default=False, description="собрать выборку целиком")
    max_requests: int = Field(default=100, gt=0)
    max_depth: int = Field(default=2, ge=0, le=4)
    rps: float = Field(default=0.5, gt=0)


class RunInfo(BaseModel):
    """Состояние одного прогона."""

    id: str
    state: RunState
    filters: Filters
    full: bool
    progress: Progress = Field(default_factory=Progress)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


#: Как собирается клиент для прогона. Подменяется в тестах, чтобы они не
#: ходили в сеть.
CrawlerFactory = Callable[[Store, RunInfo, CrawlConfig], Crawler]


class RunManager:
    """Прогоны, их состояние и отмена.

    Реестр в памяти и очередь фоновых задач. Ограничение по числу
    одновременных прогонов одно и намеренное: темп и так ограничен, а
    параллельные обходы ничего, кроме риска сработать по защите сайта, не
    дают.
    """

    def __init__(
        self,
        db_path: Path,
        factory: CrawlerFactory | None = None,
        max_concurrent: int = 1,
    ) -> None:
        self._db_path = db_path
        self._factory = factory or default_crawler_factory
        self._runs: dict[str, RunInfo] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._crawlers: dict[str, Crawler] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)

    @property
    def db_path(self) -> Path:
        """Путь к базе. Нужен обработчикам запросов."""
        return self._db_path

    @property
    def count(self) -> int:
        """Сколько прогонов известно."""
        return len(self._runs)

    def get(self, run_id: str) -> RunInfo:
        run = self._runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"прогон {run_id} не найден")
        return run

    def listing(self) -> list[RunInfo]:
        return sorted(self._runs.values(), key=lambda r: r.started_at or datetime.now(UTC))

    async def start(self, request: RunRequest) -> RunInfo:
        """Поставить прогон в очередь."""
        run = RunInfo(
            id=uuid.uuid4().hex[:12],
            state="pending",
            filters=request.filters,
            full=request.full,
        )
        self._runs[run.id] = run
        config = CrawlConfig(
            rps=request.rps,
            max_requests=request.max_requests,
            max_depth=request.max_depth,
            db_path=self._db_path,
        )
        self._tasks[run.id] = asyncio.create_task(self._execute(run.id, config))
        log.info("run.queued", run=run.id, url_filters=run.filters.model_dump(exclude_none=True))
        return run

    async def _execute(self, run_id: str, config: CrawlConfig) -> None:
        run = self._runs[run_id]
        async with self._semaphore:
            if run.state == "cancelled":
                return
            run.state = "running"
            run.started_at = datetime.now(UTC)
            crawler: Crawler | None = None
            try:
                with Store(self._db_path) as store:
                    crawler = self._factory(store, run, config)
                    self._crawlers[run_id] = crawler
                    if run.full:
                        await crawler.crawl_full(run.filters)
                    else:
                        await crawler.crawl(run.filters)
                    run.progress = crawler.progress
                run.state = "done"
            except Cancelled as exc:
                run.state = "cancelled"
                run.error = str(exc)
            except CaptchaRequired as exc:
                # Сбор не довёл: нужна проверка человека. Состояние отдельное,
                # чтобы его нельзя было спутать с успешным.
                run.state = "blocked"
                run.error = f"требуется проверка: {exc}"
                log.warning("run.captcha", run=run_id)
            except LimitReached as exc:
                run.state = "done"
                run.error = str(exc)
                log.info("run.limit", run=run_id)
            except NoResults as exc:
                run.state = "done"
                run.error = str(exc)
            except (BlockedBySite, ParserError) as exc:
                run.state = "failed"
                run.error = str(exc)
                log.warning("run.failed", run=run_id, error=str(exc))
            except Exception as exc:
                run.state = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
                log.exception("run.crashed", run=run_id)
            finally:
                run.finished_at = datetime.now(UTC)
                self._crawlers.pop(run_id, None)
                if crawler is not None:
                    # Клиент создала фабрика, но закрыть его должен тот, кто
                    # знает, что прогон больше не пойдёт. Без этого соединения
                    # к auto.ru оставались бы висеть до конца процесса.
                    await crawler.aclose()

    def cancel(self, run_id: str) -> RunInfo:
        run = self.get(run_id)
        if run.state in ("pending", "running"):
            crawler = self._crawlers.get(run_id)
            if crawler is not None:
                crawler.cancel()
            run.state = "cancelled"
            task = self._tasks.get(run_id)
            if task is not None:
                task.cancel()
            log.info("run.cancelled", run=run_id)
        return run

    async def wait(self, run_id: str, timeout: float = 120.0) -> RunInfo:
        """Дождаться завершения прогона. Для тестов и проверок."""
        task = self._tasks.get(run_id)
        if task is None:
            return self.get(run_id)
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self.get(run_id)


def default_crawler_factory(store: Store, run: RunInfo, config: CrawlConfig) -> Crawler:
    """Обычный клиент: httpx, без браузера (решение 1)."""
    http = build_http(config)
    return Crawler(AutoRuClient(http), store, config)


def create_app(
    db_path: Path | None = None,
    factory: CrawlerFactory | None = None,
) -> FastAPI:
    """Собрать приложение.

    :param db_path: путь к базе. По умолчанию — обычный рабочий файл.
    :param factory: подмена клиента для тестов.
    """
    manager = RunManager(db_path or Path(DEFAULT_DB), factory)
    app = FastAPI(
        title="autoru-market-dataset",
        version="0.4.0",
        description=(
            "Сбор объявлений auto.ru. Сервис ставит задачи, следит за прогрессом "
            "и отдаёт собранные данные. Данные о продавцах не собираются."
        ),
    )
    app.state.manager = manager

    def get_manager() -> RunManager:
        return manager

    @app.get("/health", tags=["служебное"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "runs": manager.count}

    @app.post("/runs", response_model=RunInfo, status_code=202, tags=["прогоны"])
    async def start_run(request: RunRequest, mgr: RunManager = Depends(get_manager)) -> RunInfo:
        return await mgr.start(request)

    @app.get("/runs", response_model=list[RunInfo], tags=["прогоны"])
    def list_runs(mgr: RunManager = Depends(get_manager)) -> list[RunInfo]:
        return mgr.listing()

    @app.get("/runs/{run_id}", response_model=RunInfo, tags=["прогоны"])
    def get_run(run_id: str, mgr: RunManager = Depends(get_manager)) -> RunInfo:
        return mgr.get(run_id)

    @app.post("/runs/{run_id}/cancel", response_model=RunInfo, tags=["прогоны"])
    def cancel_run(run_id: str, mgr: RunManager = Depends(get_manager)) -> RunInfo:
        return mgr.cancel(run_id)

    @app.get("/offers", tags=["данные"])
    def list_offers(
        mgr: RunManager = Depends(get_manager),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        mileage_to: int | None = Query(default=None, description="пробег не больше, км"),
        price_to: int | None = Query(default=None, description="цена не больше, руб"),
        mark: str | None = Query(default=None),
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[object] = []
        if mileage_to is not None:
            clauses.append("mileage <= ?")
            params.append(mileage_to)
        if price_to is not None:
            clauses.append("price <= ?")
            params.append(price_to)
        if mark:
            clauses.append("upper(mark) = ?")
            params.append(mark.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with Store(mgr.db_path) as store:
            rows = store.connection.execute(
                f"SELECT * FROM offers {where} ORDER BY price DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            cols = [d[0] for d in store.connection.description]
        items = [dict(zip(cols, row, strict=True)) for row in rows]
        return {"items": items, "limit": limit}

    @app.get("/offers/{offer_id}/prices", tags=["данные"])
    def offer_prices(offer_id: str, mgr: RunManager = Depends(get_manager)) -> dict[str, Any]:
        with Store(mgr.db_path) as store:
            rows = price_history(store.connection, offer_id)
            conn = store.connection
            info = conn.execute(
                "SELECT mark, model, production_year, mileage, price, sale_hash "
                "FROM offers WHERE offer_id = ?",
                [offer_id],
            ).fetchone()
        if info is None and not rows:
            raise HTTPException(status_code=404, detail="объявление не найдено")
        return {
            "offer_id": offer_id,
            "mark": info[0] if info else None,
            "model": info[1] if info else None,
            "sale_hash": info[5] if info else None,
            "history": [{"observed_at": when, "price": price} for when, price in rows],
        }

    @app.get("/summary", tags=["данные"])
    def summary(mgr: RunManager = Depends(get_manager)) -> dict[str, Any]:
        with Store(mgr.db_path) as store:
            return dataset_summary(store.connection)

    @app.get("/changes", tags=["данные"])
    def changes(
        mgr: RunManager = Depends(get_manager),
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> dict[str, Any]:
        with Store(mgr.db_path) as store:
            rows = price_changes(store.connection, limit)
        return {
            "items": [
                {
                    "offer_id": c.offer_id,
                    "mark": c.mark,
                    "model": c.model,
                    "old_price": c.old_price,
                    "new_price": c.new_price,
                    "delta": c.delta,
                    "percent": c.percent,
                }
                for c in rows
            ]
        }

    @app.get("/check-hash", tags=["данные"])
    def check_hash(mgr: RunManager = Depends(get_manager)) -> dict[str, Any]:
        with Store(mgr.db_path) as store:
            return {"suspicious": hash_consistency(store.connection)}

    return app


app = create_app()


__all__ = [
    "RunInfo",
    "RunManager",
    "RunRequest",
    "RunState",
    "app",
    "create_app",
    "default_crawler_factory",
]
