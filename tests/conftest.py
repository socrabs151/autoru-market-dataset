"""Общие приспособления для тестов.

Тесты не ходят в сеть: используются офлайн-фикстуры. Фикстура — это
настоящее SSR-состояние auto.ru, из которого вычищены персональные данные
и которое заново нарезано на фрагменты.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from autoru_market_dataset.frontends.autoru.ris import assemble, clear_cache, split_chunks

FIXTURES = Path(__file__).parent / "fixtures" / "autoru"
LISTING = FIXTURES / "listing_moscow_bmw_x5_used.html"


#: Адреса, куда тестам ходить можно: сам компьютер. Нужен не для проверки
#: данных, а потому что так устроены служебные соединения — цикл событий,
#: терминальный экран, клиент тестов.
LOCAL = {"localhost", "127.0.0.1", "::1", "", None}


def _is_local(address: Any) -> bool:
    """Относится ли адрес к самому компьютеру."""
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
        return str(host) in LOCAL or str(host).startswith("127.")
    return str(address) in LOCAL


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    """Запретить тестам обращаться к внешним адресам.

    Тесты не должны ходить на auto.ru: обращения к чужому сайту во время
    прогона CI — это и нагрузка на сайт, и нестабильность из-за сети. Правило
    держится не соглашением, а проверкой: тест, который полезет в сеть, упадёт
    с внятным сообщением вместо того, чтобы тихо скачать что-нибудь.

    Локальные соединения разрешены: к ним прибегает сам тестовый обвязкой —
    цикл событий и терминальный экран. Запрет касается только внешней сети.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(address):
            raise RuntimeError(
                f"тест попытался обратиться к внешнему адресу {address!r}. "
                "Тесты работают на фикстурах из tests/fixtures; обращения к "
                "сайту здесь недопустимы."
            )
        return original_connect(self, address, *args, **kwargs)

    def connect_ex(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(address):
            raise RuntimeError(f"тест попытался обратиться к внешнему адресу {address!r}")
        return original_connect_ex(self, address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]


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
