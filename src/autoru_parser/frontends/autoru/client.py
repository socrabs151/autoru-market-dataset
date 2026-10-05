"""HTTP-клиент страниц листинга auto.ru."""

from __future__ import annotations

import httpx
import structlog

from autoru_parser.core.errors import BlockedBySite, CaptchaRequired
from autoru_parser.core.models import Filters, Page
from autoru_parser.core.normalize import normalize_all
from autoru_parser.frontends.autoru.listing import check_block, extract_offers
from autoru_parser.frontends.autoru.ris import assemble, split_chunks
from autoru_parser.frontends.autoru.urls import BASE_URL, build_url

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

    async def fetch(self, url: str) -> Page:
        """Загрузить страницу и вернуть нормализованные объявления.

        :raises BlockedBySite: сайт отказал или потребовал проверки.
        :raises ParseError: страница есть, а состояние собрать не удалось.
        :raises EmptyResultError: сайт показывает объявления, а их нет.
        """
        log.info("fetch.start", url=url)
        response = await self._http.get(url)
        html = response.text
        log.info("fetch.response", status=response.status_code, bytes=len(response.content))

        if response.status_code >= 500:
            raise BlockedBySite(f"auto.ru ответил {response.status_code}")
        check_block(html)

        chunks = split_chunks(html)
        state, order = assemble(chunks)
        raw_offers, total, pages = extract_offers(state)

        page = Page(
            url=url,
            offers=normalize_all(raw_offers),
            total_offers=total,
            total_pages=pages,
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
