"""История цен: изменения, сводки и проверка гипотезы о хеше."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from autoru_parser.core.models import Offer
from autoru_parser.core.normalize import compute_payload_hash
from autoru_parser.services.history import (
    dataset_summary,
    hash_consistency,
    price_changes,
    price_history,
    yearly_prices,
)
from autoru_parser.storage.duck import Store


def at(day: int) -> datetime:
    return datetime(2026, 10, day, 12, 0, tzinfo=UTC)


def make_offer(
    offer_id: str,
    price: int,
    *,
    year: int = 2020,
    mileage: int = 100_000,
    seller: str = "PRIVATE",
    sale_hash: str = "aaaaaaaa",
    observed: datetime | None = None,
) -> Offer:
    offer = Offer(
        offer_id=offer_id,
        sale_id=f"{offer_id}-{sale_hash}",
        url=f"https://auto.ru/cars/used/sale/mercedes/s_klasse/{offer_id}-{sale_hash}/",
        mark="MERCEDES",
        model="S_KLASSE",
        sale_hash=sale_hash,
        price=price,
        mileage=mileage,
        production_year=year,
        seller_type=seller,
        observed_at=observed or at(1),
    )
    offer.payload_hash = compute_payload_hash(offer)
    return offer


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "h.duckdb") as opened:
        yield opened


# --- история одного объявления ---------------------------------------------


def test_история_пуста_у_нового_объявления(store: Store) -> None:
    assert price_history(store.connection, "111") == []


def test_история_собирается_по_прогонам(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, observed=at(1)))
    store.save(make_offer("111", 1_100_000, observed=at(2)))
    store.save(make_offer("111", 950_000, observed=at(3)))
    rows = price_history(store.connection, "111")
    assert [price for _when, price in rows] == [1_000_000, 1_100_000, 950_000]
    assert rows == sorted(rows, key=lambda item: item[0]), "история должна идти по времени"


def test_изменение_цены_считается(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, observed=at(1)))
    store.save(make_offer("111", 900_000, observed=at(2)))
    changes = price_changes(store.connection)
    assert len(changes) == 1
    change = changes[0]
    assert change.old_price == 1_000_000
    assert change.new_price == 900_000
    assert change.delta == -100_000
    assert change.percent == -10.0


def test_неизменная_цена_не_считается_изменением(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, observed=at(1)))
    store.save(make_offer("222", 2_000_000, observed=at(2)))
    store.save(make_offer("111", 1_000_000, observed=at(3)))
    assert price_changes(store.connection, only_changed=True) == []


def test_с_флагом_показываются_и_неизменные(store: Store) -> None:
    """Неизменной считается цена; сам объявление при этом может обновиться.

    Поэтому меняется пробег, а не цена: если бы менялась цена, запись была бы
    изменённой, и разница между режимами не была бы видна.
    """
    store.save(make_offer("111", 1_000_000, mileage=100_000, observed=at(1)))
    store.save(make_offer("111", 1_000_000, mileage=99_000, observed=at(2)))
    assert price_changes(store.connection, only_changed=True) == []
    assert len(price_changes(store.connection, only_changed=False)) == 1


def test_одного_прогона_недостаточно(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, observed=at(1)))
    assert price_changes(store.connection) == []


# --- сводки -----------------------------------------------------------------


def test_сводка_на_пустой_базе(store: Store) -> None:
    assert dataset_summary(store.connection) == {"объявлений": 0}


def test_сводка_считает_объёмы(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, year=2023, mileage=20_000, seller="PRIVATE"))
    store.save(make_offer("222", 2_000_000, year=2020, mileage=200_000, seller="COMMERCIAL"))
    data = dataset_summary(store.connection)
    assert data["объявлений"] == 2
    assert data["год_от"] == 2020
    assert data["год_до"] == 2023
    assert data["цена_от"] == 1_000_000
    assert data["цена_до"] == 2_000_000
    assert data["частных"] == 1
    assert data["салонов"] == 1


def test_сводка_по_годам_различает_среднее_и_медиану(store: Store) -> None:
    """Среднее тянут вверх единичные дорогие машины, медиана показывает уровень."""
    store.save(make_offer("1", 1_000_000, year=2020))
    store.save(make_offer("2", 1_000_000, year=2020))
    store.save(make_offer("3", 1_000_000, year=2020))
    store.save(make_offer("4", 10_000_000, year=2020))
    rows = yearly_prices(store.connection)
    assert len(rows) == 1
    year, avg, median, count, _mileage = rows[0]
    assert year == 2020
    assert count == 4
    assert median == 1_000_000
    assert avg > median, "среднее должно быть выше медианы из-за выброса"


# --- проверка гипотезы о хеше ----------------------------------------------


def test_без_истории_подозрительных_нет(store: Store) -> None:
    assert hash_consistency(store.connection) == []


def test_одинаковый_хеш_считается_нормальным(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, observed=at(1)))
    store.save(make_offer("111", 900_000, observed=at(2)))
    assert hash_consistency(store.connection) == []


def test_смена_цены_без_смены_хеша_не_подозрительно(store: Store) -> None:
    """Главный сценарий: цена поменялась, адрес остался прежним."""
    store.save(make_offer("111", 1_000_000, sale_hash="aaaaaaaa", observed=at(1)))
    store.save(make_offer("111", 900_000, sale_hash="aaaaaaaa", observed=at(2)))
    assert hash_consistency(store.connection) == []


def test_смена_хеша_при_том_же_id_подозрительна(store: Store) -> None:
    """Гипотеза из решения 5: хеш зависит от цены."""
    store.save(make_offer("111", 1_000_000, sale_hash="aaaaaaaa", observed=at(1)))
    store.save(make_offer("111", 900_000, sale_hash="bbbbbbbb", observed=at(2)))
    suspicious = hash_consistency(store.connection)
    assert suspicious, "разные хеши у одного идентификатора должны попасть в список"
    assert "111" in suspicious[0]
    assert "разных хешей 2" in suspicious[0]


def test_разные_объявления_не_считаются_подозрительными(store: Store) -> None:
    store.save(make_offer("111", 1_000_000, sale_hash="aaaaaaaa", observed=at(1)))
    store.save(make_offer("222", 2_000_000, sale_hash="bbbbbbbb", observed=at(2)))
    assert hash_consistency(store.connection) == []
