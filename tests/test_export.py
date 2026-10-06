"""Выгрузка в CSV и JSON, включая локальные фильтры.

Локальные фильтры — главное, что здесь проверяется: именно они закрывают
пробел, который сайт закрыть не позволяет. Фильтр по пробегу через адрес
запрещён правилами сайта, поэтому он применяется к собранным данным.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from autoru_parser.core.errors import ParserError
from autoru_parser.core.models import Offer, PricePoint
from autoru_parser.core.normalize import compute_payload_hash
from autoru_parser.storage import export
from autoru_parser.storage.duck import Store

MOMENT = "2026-10-06T12:00:00+00:00"


def make_offer(offer_id: str, price: int, mileage: int, year: int, seller: str) -> Offer:
    offer = Offer(
        offer_id=offer_id,
        sale_id=f"{offer_id}-aaaaaaaa",
        url=f"https://auto.ru/cars/used/sale/mercedes/s_klasse/{offer_id}-aaaaaaaa/",
        mark="MERCEDES",
        model="S_KLASSE",
        sale_hash="aaaaaaaa",
        price=price,
        mileage=mileage,
        production_year=year,
        seller_type=seller,
        price_history=[PricePoint(price=price, observed_at=MOMENT)],  # type: ignore[arg-type]
        observed_at=MOMENT,  # type: ignore[arg-type]
    )
    offer.payload_hash = compute_payload_hash(offer)
    return offer


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "t.duckdb") as opened:
        opened.save_many(
            [
                make_offer("111", 1_000_000, 20_000, 2023, "PRIVATE"),
                make_offer("222", 2_000_000, 150_000, 2020, "COMMERCIAL"),
                make_offer("333", 3_000_000, 300_000, 2015, "PRIVATE"),
            ]
        )
        yield opened


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_выгрузка_без_фильтра_отдаёт_всё(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "all.csv"
    rows = export.export_offers(store.connection, out)
    assert rows == 3
    assert len(read_csv(out)) == 3


def test_в_csv_есть_заголовок(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "a.csv"
    export.export_offers(store.connection, out)
    rows = read_csv(out)
    assert "offer_id" in rows[0]
    assert "price" in rows[0]


def test_фильтр_по_пробегу_работает_локально(store: Store, tmp_path: Path) -> None:
    """Такой фильтр сайт через адрес не поддерживает: только по данным."""
    out = tmp_path / "fresh.csv"
    rows = export.export_offers(store.connection, out, local_filter=export.mileage_to(30_000))
    assert rows == 1
    assert read_csv(out)[0]["offer_id"] == "111"


def test_диапазон_пробега(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "mid.csv"
    rows = export.export_offers(
        store.connection,
        out,
        local_filter=export.and_(export.mileage_from(50_000), export.mileage_to(200_000)),
    )
    assert rows == 1
    assert read_csv(out)[0]["offer_id"] == "222"


def test_фильтр_по_типу_продавца(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "private.csv"
    rows = export.export_offers(store.connection, out, local_filter=export.seller_type("PRIVATE"))
    assert rows == 2


def test_фильтр_по_марке_не_чувствителен_к_регистру(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "bmw.csv"
    rows = export.export_offers(store.connection, out, local_filter=export.mark_is("mercedes"))
    assert rows == 3, "сравнение должно быть без учёта регистра"


def test_несуществующая_марка_даёт_пустую_выгрузку(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "none.csv"
    rows = export.export_offers(store.connection, out, local_filter=export.mark_is("LADA"))
    assert rows == 0


def test_несколько_условий_складываются(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "narrow.csv"
    spec = export.and_(
        export.mileage_to(200_000),
        export.year_from(2018),
        export.seller_type("PRIVATE"),
    )
    rows = export.export_offers(store.connection, out, local_filter=spec)
    assert rows == 1
    assert read_csv(out)[0]["offer_id"] == "111"


def test_диапазон_цен(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "price.csv"
    rows = export.export_offers(
        store.connection, out, local_filter=export.price_between(1_500_000, 2_500_000)
    )
    assert rows == 1


def test_выгрузка_в_json(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "a.json"
    rows = export.export_offers(store.connection, out, fmt="json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert rows == 3
    assert isinstance(data, list), "ожидался обычный JSON-массив"
    assert len(data) == 3
    assert data[0]["offer_id"]


def test_выгрузка_в_ndjson(store: Store, tmp_path: Path) -> None:
    """Построчный формат: одна строка — один объект."""
    out = tmp_path / "a.ndjson"
    rows = export.export_offers(store.connection, out, fmt="ndjson")
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert rows == 3
    assert len(lines) == 3
    assert all(json.loads(line)["offer_id"] for line in lines)


def test_выгрузка_в_parquet(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "a.parquet"
    rows = export.export_offers(store.connection, out, fmt="parquet")
    assert rows == 3
    assert out.stat().st_size > 0


def test_неизвестный_формат_отвергается(store: Store, tmp_path: Path) -> None:
    with pytest.raises(ParserError, match="формат"):
        export.export_offers(store.connection, tmp_path / "a.xls", fmt="xls")


def test_в_выгрузке_нет_персональных_данных(store: Store, tmp_path: Path) -> None:
    """Проверка на выходе: в файле не должно быть колонок о продавце.

    Сравнение идёт по точным именам колонок, а не по подстроке в тексте:
    иначе срабатывало бы на ``mark_name``, который персональных данных
    не содержит.
    """
    out = tmp_path / "safe.csv"
    export.export_offers(store.connection, out)
    columns = set(read_csv(out)[0])
    forbidden = {"name", "phone", "phones", "address", "user_ref", "salon", "vin", "coord"}
    assert not columns & forbidden, f"в выгрузке лишние колонки: {sorted(columns & forbidden)}"


def test_путь_с_кавычками_не_ломает_запрос(store: Store, tmp_path: Path) -> None:
    """Путь приходит из командной строки: кавычки в нём обезвреживаются."""
    out = tmp_path / "o'brien.csv"
    rows = export.export_offers(store.connection, out)
    assert rows == 3
    assert out.exists()


def test_создаёт_недостающую_папку(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "a.csv"
    rows = export.export_offers(store.connection, out)
    assert rows == 3
    assert out.exists()


def test_выгрузка_истории(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "snap.csv"
    rows = export.export_snapshots(store.connection, out)
    assert rows == 3


def test_история_с_даты_отсекается(store: Store, tmp_path: Path) -> None:
    out = tmp_path / "recent.csv"
    rows = export.export_snapshots(store.connection, out, since="2030-01-01")
    assert rows == 0


def test_повторная_выгрузка_не_меняет_счётчик(store: Store, tmp_path: Path) -> None:
    export.export_offers(store.connection, tmp_path / "a.csv")
    export.export_offers(store.connection, tmp_path / "b.csv")
    assert store.count_offers() == 3
