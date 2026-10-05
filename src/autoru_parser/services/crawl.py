"""Сбор одной выборки.

Правила, которые здесь соблюдаются и не должны нарушаться:

- не более 0.5 запроса в секунду, с разбросом задержек (решение 7);
- плановый лимит запросов на прогон, который агент не повышает молча;
- отказ сайта и пустая выборка — разные вещи, и обе не молчат (решение 13).
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, Field

from autoru_parser.core.errors import LimitReached
from autoru_parser.core.models import DEFAULT_DB, Filters, Progress
from autoru_parser.frontends.autoru.client import DEFAULT_HEADERS, AutoRuClient
from autoru_parser.storage.duck import Store

log = structlog.get_logger(__name__)


class CrawlConfig(BaseModel):
    """Настройки прогона."""

    rps: float = Field(default=0.5, gt=0, description="запросов в секунду")
    jitter: float = Field(default=0.4, ge=0, le=1, description="разброс задержки, доля интервала")
    max_requests: int = Field(default=100, gt=0, description="плановый лимит запросов на прогон")
    db_path: Path = Field(default=Path(DEFAULT_DB))
    timeout: float = Field(default=90.0, gt=0)


class RateLimiter:
    """Ограничитель темпа. Держит интервал с разбросом."""

    def __init__(self, rps: float, jitter: float = 0.4) -> None:
        self._interval = 1.0 / rps
        self._jitter = jitter
        self._next_allowed = 0.0
        self._random = random.Random()

    def wait(self) -> float:
        """Подождать до следующего допустимого запроса. Возвращает паузу."""
        delay = self._next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(delay)
            pause = delay
        else:
            pause = 0.0
            self._next_allowed = time.monotonic()
        spread = self._interval * self._jitter
        self._next_allowed += self._interval + self._random.uniform(-spread, spread)
        return pause


class Crawler:
    """Собирает страницу выборки и складывает результат в хранилище."""

    def __init__(self, client: AutoRuClient, store: Store, config: CrawlConfig) -> None:
        self._client = client
        self._store = store
        self._config = config
        self._limiter = RateLimiter(config.rps, config.jitter)
        self.progress = Progress()

    async def crawl(self, filters: Filters) -> Progress:
        """Собрать первую страницу выборки по фильтру."""
        url = self._client.url_for(filters)
        log.info("crawl.start", url=url, max_requests=self._config.max_requests)

        if self.progress.requests >= self._config.max_requests:
            raise LimitReached(f"плановый лимит {self._config.max_requests} запросов исчерпан")

        self._limiter.wait()
        self.progress.requests += 1
        page = await self._client.fetch(url)
        self.progress.pages_done += 1
        self.progress.offers_seen += len(page.offers)
        self.progress.offers_stored += self._store.save_many(page.offers)

        log.info(
            "crawl.done",
            offers=len(page.offers),
            total_offers=page.total_offers,
            total_pages=page.total_pages,
            changed=self.progress.offers_stored,
            requests=self.progress.requests,
        )
        return self.progress


def build_http(config: CrawlConfig) -> httpx.AsyncClient:
    """HTTP-клиент с настройками проекта."""
    return httpx.AsyncClient(
        timeout=config.timeout,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
    )


__all__ = ["CrawlConfig", "Crawler", "RateLimiter", "build_http"]
