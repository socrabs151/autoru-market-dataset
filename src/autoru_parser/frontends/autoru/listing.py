"""Разбор страницы листинга auto.ru.

Данные объявлений лежат в SSR-разметке страницы листинга, в состоянии
``listing.data.offers``. В карточке объявления это поле пустое: там данные
приходят отдельным внутренним запросом, а его подпись вычисляется на стороне
сайта. Поэтому источник данных у нас — листинг, и только он (решение 1).

За одна страницу приходит до 37 объявлений. Всего по выборке бывает больше
тысячи, поэтому дальше нужен переход по страницам, а он запрещён: полноту
достигаем сужением фильтра (решение 10).
"""

from __future__ import annotations

import re
from typing import Any

from autoru_parser.core.errors import (
    BlockedBySite,
    CaptchaRequired,
    EmptyResultError,
    ParseError,
)
from autoru_parser.frontends.autoru.ris import assemble, split_chunks

BLOCK_MARKERS = ("Нет доступа", "Доступ ограничен", "Ошибка 403")
CAPTCHA_MARKERS = ("SmartCaptcha", "showcaptcha", "Подтвердите, что запросы отправляли вы")


def check_block(html: str) -> None:
    """Отличить отказ сайта от пустого результата.

    :raises CaptchaRequired: требуется ручная проверка.
    :raises BlockedBySite: сайт не отдал страницу.
    """
    if any(marker in html for marker in CAPTCHA_MARKERS):
        raise CaptchaRequired("auto.ru требует подтверждения запроса")
    if any(marker in html for marker in BLOCK_MARKERS):
        raise BlockedBySite("auto.ru не отдал страницу: отказ или ограничение")


def extract_offers(state: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Достать сырые объявления и пагинацию из разобранного состояния.

    :raises ParseError: состояние собрано, но блока листинга нет или
        в нём нет поля ``offers``. Это сбой разбора, а не пустой результат.
    :raises EmptyResultError: объявлений нет, хотя сайт их показывает.
    """
    listing = state.get("listing")
    if not isinstance(listing, dict):
        raise ParseError("в состоянии нет блока listing")
    data = listing.get("data")
    if not isinstance(data, dict):
        raise ParseError("в блоке listing нет data")

    if "offers" not in data:
        raise ParseError("в listing.data нет поля offers: разбор сломан")

    offers = data.get("offers")
    if not isinstance(offers, list):
        raise ParseError("listing.data.offers имеет неверный тип")

    pagination = data.get("pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    total = _as_int(pagination.get("total_offers_count"))
    total_pages = _as_int(pagination.get("total_page_count"))

    if not offers and total and total > 0:
        raise EmptyResultError(
            f"сайт показывает {total} объявлений, а в ответе их нет: канал данных перестал работать"
        )

    return [o for o in offers if isinstance(o, dict)], total or len(offers), total_pages or 1


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_listing(html: str) -> tuple[list[dict[str, Any]], int, int]:
    """Полный разбор страницы листинга: HTML → сырые объявления.

    :raises BlockedBySite: отказ или требование проверки.
    :raises ParseError: страница есть, а состояние собрать не удалось.
    """
    check_block(html)
    chunks = split_chunks(html)
    if not chunks:
        raise ParseError("на странице нет фрагментов RIS")
    state, _order = assemble(chunks)
    return extract_offers(state)


def strip_tracking_params(url: str) -> str:
    """Убрать метки из адреса. Здесь только для ясности: мы их и не строим."""
    return re.sub(r"[?&](utm_\w+|yclid|from|source)=[^&]*", "", url)


__all__ = [
    "BLOCK_MARKERS",
    "CAPTCHA_MARKERS",
    "check_block",
    "extract_offers",
    "parse_listing",
    "strip_tracking_params",
]
