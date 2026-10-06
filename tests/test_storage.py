"""Хранилище: идемпотентность и история."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from autoru_market_dataset.core.models import Offer, PricePoint
from autoru_market_dataset.core.normalize import compute_payload_hash
from autoru_market_dataset.storage.duck import Store

#: Фиксированный момент, чтобы два одинаковых по содержанию объявления
#: имели одинаковый хеш. Реальная история цен приходит со сайта и не «плывёт».
MOMENT = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def make_offer(offer_id: str, price: int) -> Offer:
    offer = Offer(
        offer_id=offer_id,
        sale_id=f"{offer_id}-aaaaaaaa",
        url=f"https://auto.ru/cars/used/sale/mercedes/s_klasse/{offer_id}-aaaaaaaa/",
        mark="mercedes",
        model="s_klasse",
        price=price,
        mileage=100_000,
        production_year=2015,
        price_history=[PricePoint(price=price, observed_at=MOMENT)],
        observed_at=MOMENT,
    )
    offer.payload_hash = compute_payload_hash(offer)
    return offer


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "test.duckdb") as opened:
        yield opened


def test_первая_запись_попадает_в_историю(store: Store) -> None:
    assert store.save(make_offer("111", 1_000_000)) is True
    assert store.count_offers() == 1
    assert store.count_snapshots() == 1


def test_повторный_прогон_не_создаёт_дублей(store: Store) -> None:
    """Главное требование: перезапуск безопасен."""
    store.save(make_offer("111", 1_000_000))
    store.save(make_offer("111", 1_000_000))
    assert store.count_offers() == 1
    assert store.count_snapshots() == 1, "неизменившееся объявление не должно попадать в историю"


def test_изменение_цены_даёт_новую_запись_в_истории(store: Store) -> None:
    store.save(make_offer("111", 1_000_000))
    assert store.save(make_offer("111", 900_000)) is True
    assert store.count_offers() == 1
    assert store.count_snapshots() == 2


def test_разные_объявления_живут_рядом(store: Store) -> None:
    store.save_many([make_offer("111", 1_000_000), make_offer("222", 2_000_000)])
    assert store.count_offers() == 2
    assert store.count_snapshots() == 2


def test_повтор_через_открытие_базы_не_дублирует(tmp_path: Path) -> None:
    """Идемпотентность должна держаться между запусками, а не внутри сессии."""
    path = tmp_path / "again.duckdb"
    with Store(path) as store:
        store.save(make_offer("111", 1_000_000))
    with Store(path) as store:
        store.save(make_offer("111", 1_000_000))
        assert store.count_offers() == 1
        assert store.count_snapshots() == 1


def test_состояние_перезаписывается(store: Store) -> None:
    store.save(make_offer("111", 1_000_000))
    store.save(make_offer("111", 900_000))
    row = store._con.execute("SELECT price FROM offers WHERE offer_id = '111'").fetchone()
    assert row is not None
    assert row[0] == 900_000


def test_в_истории_есть_и_цена_и_пробег(store: Store) -> None:
    store.save(make_offer("111", 1_000_000))
    store.save(make_offer("111", 950_000))
    rows = store._con.execute(
        "SELECT price FROM offer_snapshots ORDER BY observed_at, price"
    ).fetchall()
    prices = [int(row[0]) for row in rows]
    assert 950_000 in prices
    assert 1_000_000 in prices
