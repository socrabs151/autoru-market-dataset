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


def extract_offers(state: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int, int]:
    """Достать сырые объявления, пагинацию и размер страницы.

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
    page_size = _as_int(pagination.get("page_size")) or len(offers)

    if not offers and total and total > 0:
        raise EmptyResultError(
            f"сайт показывает {total} объявлений, а в ответе их нет: канал данных перестал работать"
        )

    return (
        [o for o in offers if isinstance(o, dict)],
        total or len(offers),
        total_pages or 1,
        page_size,
    )


def extract_price_ranges(state: dict[str, Any]) -> list[tuple[int, int]]:
    """Границы ценовых диапазонов, которые сайт насчитал сам.

    В ответе приходит список ``(потолок цены, сколько объявлений до него)``.
    Это позволяет понять глубину выборки, не делая дополнительных запросов:
    если даже самый узкий потолок даёт больше объявлений, чем помещается в
    страницу, цену дробить бессмысленно и нужно уходить в другие измерения.
    """
    raw = _get(state, "listingPriceRanges", "data")
    if not isinstance(raw, list):
        return []
    result: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ceiling = _as_int(item.get("price_to"))
        count = _as_int(item.get("offers_count"))
        if ceiling and count is not None:
            result.append((ceiling, count))
    return result


def _get(state: dict[str, Any], *path: str) -> Any:
    current: Any = state
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_listing(html: str) -> tuple[list[dict[str, Any]], int, int, int]:
    """Полный разбор страницы листинга: HTML → сырые объявления и пагинация.

    :raises BlockedBySite: отказ или требование проверки.
    :raises ParseError: страница есть, а состояние собрать не удалось.
    """
    check_block(html)
    chunks = split_chunks(html)
    if not chunks:
        raise ParseError("на странице нет фрагментов RIS")
    state, _order = assemble(chunks)
    return extract_offers(state)


__all__ = [
    "BLOCK_MARKERS",
    "CAPTCHA_MARKERS",
    "check_block",
    "extract_offers",
    "extract_price_ranges",
    "parse_listing",
]
