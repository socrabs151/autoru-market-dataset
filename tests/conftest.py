"""Общие приспособления для тестов.

Тесты не ходят в сеть: используются офлайн-фикстуры. Фикстура — это
настоящее SSR-состояние auto.ru, из которого вычищены персональные данные
и которое заново нарезано на фрагменты.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoru_market_dataset.frontends.autoru.ris import assemble, clear_cache, split_chunks

FIXTURES = Path(__file__).parent / "fixtures" / "autoru"
LISTING = FIXTURES / "listing_moscow_bmw_x5_used.html"


@pytest.fixture(autouse=True)
def _clean_order_cache() -> None:
    """Кэш порядка не должен протекать между тестами."""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(scope="session")
def listing_html() -> str:
    return LISTING.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def listing_state(listing_html: str) -> dict[str, Any]:
    chunks = split_chunks(listing_html)
    assert chunks, "фикстура не содержит фрагментов RIS"
    state, _order = assemble(chunks)
    return state


@pytest.fixture(scope="session")
def listing_offers(listing_state: dict[str, Any]) -> list[dict[str, Any]]:
    offers = listing_state["listing"]["data"]["offers"]
    assert offers, "в фикстуре нет объявлений"
    return offers


@pytest.fixture
def shuffled_chunks(listing_html: str) -> dict[int, str]:
    """Те же фрагменты, но переставленные и с перемешанными id.

    Смысл: если сборка полагалась бы на порядок в документе или на сортировку
    по id, такой тетст её бы сломал.
    """
    chunks = split_chunks(listing_html)
    renumbered = {9000 + index: body for index, body in enumerate(chunks.values())}
    return dict(reversed(list(renumbered.items())))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
