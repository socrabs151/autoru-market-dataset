"""Разбор страницы листинга: от HTML к объявлениям."""

from __future__ import annotations

from typing import Any

import pytest

from autoru_market_dataset.core.errors import (
    BlockedBySite,
    CaptchaRequired,
    EmptyResultError,
    ParseError,
)
from autoru_market_dataset.frontends.autoru.listing import (
    check_block,
    extract_offers,
    extract_price_ranges,
    parse_listing,
)


def test_из_фикстуры_читаются_объявления(listing_html: str) -> None:
    raw, total, pages, page_size = parse_listing(listing_html)
    assert raw, "ожидались объявления"
    assert total >= len(raw), "всего на сайте должно быть не меньше, чем отдано"
    assert pages >= 1
    assert page_size >= len(raw), "отдано не больше, чем помещается в страницу"


def test_размер_страницы_известен(listing_html: str) -> None:
    """Знание page_size нужно, чтобы понять, пора ли дробить выборку."""
    _raw, _total, _pages, page_size = parse_listing(listing_html)
    assert page_size == 37


def test_выдаётся_полная_страница(listing_offers: list[dict[str, Any]]) -> None:
    """За один ответ приходит до 37 объявлений — столько и должно быть."""
    assert len(listing_offers) == 37


def test_в_объявлении_есть_идентификатор_и_цена(listing_offers: list[dict[str, Any]]) -> None:
    first = listing_offers[0]
    assert first["id"].isdigit()
    assert first["price_info"]["price"] > 0
    assert first["url"].endswith("/")


def test_история_цен_есть_в_ответе(listing_offers: list[dict[str, Any]]) -> None:
    with_history = [o for o in listing_offers if o.get("price_history")]
    assert with_history, "хотя бы у части объявлений должна быть история цены"


def test_пустое_состояние_это_ошибка_разбора() -> None:
    with pytest.raises(ParseError, match="listing"):
        extract_offers({})


def test_отсутствие_поля_offers_это_ошибка_разбора() -> None:
    state = {"listing": {"data": {"pagination": {"total_offers_count": 5}}}}
    with pytest.raises(ParseError, match="offers"):
        extract_offers(state)


def test_пустая_выдача_при_ненулевом_итоге_это_не_результат() -> None:
    """Сайт показывает объявления, а их нет — значит канал сломался."""
    state = {
        "listing": {
            "data": {
                "offers": [],
                "pagination": {"total_offers_count": 120, "total_page_count": 4},
            }
        }
    }
    with pytest.raises(EmptyResultError, match="120"):
        extract_offers(state)


def test_поистине_пустая_выборка_допустима() -> None:
    """Ноль объявлений и ноль на сайте — это результат, а не сбой."""
    state = {"listing": {"data": {"offers": [], "pagination": {"total_offers_count": 0}}}}
    raw, total, pages, _size = extract_offers(state)
    assert raw == []
    assert total == 0
    assert pages == 1


def test_сайт_отдаёт_ценовые_диапазоны(listing_state: dict[str, Any]) -> None:
    """Ответ содержит границы цен и счётчики — это подсказка без доп. запросов."""
    ranges = extract_price_ranges(listing_state)
    assert ranges, "ожидались ценовые диапазоны"
    assert all(ceiling > 0 and count >= 0 for ceiling, count in ranges)


def test_отказ_сайта_распознаётся() -> None:
    with pytest.raises(BlockedBySite):
        check_block("<html><title>Ошибка 403! Нет доступа.</title></html>")


def test_капча_распознаётся_отдельно_от_отказа() -> None:
    with pytest.raises(CaptchaRequired):
        check_block("<html><script>SmartCaptcha.show()</script></html>")


def test_пустой_ответ_не_выглядит_как_сбой_разбора() -> None:
    """Пустой ответ по фильтру — это результат, а не поломка разбора.

    Различение существенно: при переборе значений фильтра часть адресов
    обязательно пуста, и если считать это ошибкой, обход остановится.
    """
    with pytest.raises(ParseError, match="RIS"):
        parse_listing("<html>страница есть, а фрагментов нет</html>")


def test_обычная_страница_не_вызывает_ошибок() -> None:
    check_block("<html><body>объявления</body></html>")


def test_ошибка_404_распознаётся() -> None:
    check_block("<html><title>Ошибка 404! Страница не найдена. - AUTO.RU</title></html>")
