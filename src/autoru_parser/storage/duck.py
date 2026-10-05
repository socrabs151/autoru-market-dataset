"""Хранение: DuckDB поверх Parquet.

Две таблицы с разным смыслом (решение 6):

- ``offers`` — текущее состояние каждого объявления, обновляется по ключу.
  Именно она делает повторный прогон безопасным: дубли не появляются.
- ``offer_snapshots`` — история. Строка добавляется только если содержимое
  объявления действительно изменилось, поэтому в истории нет тысяч
  одинаковых записей.

Ключ — пара ``(source, offer_id)``. Хеш из адреса в ключ не входит: есть
основания полагать, что он меняется вместе с ценой, и тогда ключ по нему
породил бы «новое» объявление с тем же самым автомобилем (решение 5).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from autoru_parser.core.models import Offer, PricePoint

SCHEMA_VERSION = 2

_OFFERS_DDL = """
CREATE TABLE IF NOT EXISTS offers (
    source VARCHAR,
    offer_id VARCHAR,
    sale_id VARCHAR,
    url VARCHAR,
    sale_hash VARCHAR,
    category VARCHAR,
    section VARCHAR,
    sub_category VARCHAR,
    title VARCHAR,
    mark VARCHAR,
    mark_name VARCHAR,
    model VARCHAR,
    model_name VARCHAR,
    super_gen_id VARCHAR,
    super_gen_name VARCHAR,
    super_gen_year_from INTEGER,
    super_gen_year_to INTEGER,
    origin VARCHAR,
    body_type VARCHAR,
    body_type_human VARCHAR,
    auto_class VARCHAR,
    doors_count INTEGER,
    seats INTEGER[],
    engine VARCHAR,
    engine_type VARCHAR,
    displacement INTEGER,
    power_hp INTEGER,
    power_kwt INTEGER,
    acceleration_to DOUBLE,
    nameplate VARCHAR,
    transmission VARCHAR,
    drive VARCHAR,
    color VARCHAR,
    color_hex VARCHAR,
    tech_param_id VARCHAR,
    production_year INTEGER,
    mileage INTEGER,
    condition VARCHAR,
    status VARCHAR,
    availability VARCHAR,
    price BIGINT,
    price_eur BIGINT,
    price_usd BIGINT,
    currency VARCHAR,
    with_nds BOOLEAN,
    owners_number INTEGER,
    pts VARCHAR,
    pts_original BOOLEAN,
    custom_cleared BOOLEAN,
    vin_resolution VARCHAR,
    owners_resolution VARCHAR,
    legal_resolution VARCHAR,
    accidents_resolution VARCHAR,
    not_registered_in_russia BOOLEAN,
    seller_type VARCHAR,
    geo_id VARCHAR,
    federal_subject_id VARCHAR,
    photos_count INTEGER,
    transport_tax INTEGER,
    tags VARCHAR[],
    creation_date TIMESTAMPTZ,
    update_date TIMESTAMPTZ,
    days_in_stock INTEGER,
    days_on_sale INTEGER,
    price_history JSON,
    payload_hash VARCHAR,
    observed_at TIMESTAMPTZ,
    PRIMARY KEY (source, offer_id)
);
"""

_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS offer_snapshots (
    source VARCHAR,
    offer_id VARCHAR,
    observed_at TIMESTAMPTZ,
    payload_hash VARCHAR,
    price BIGINT,
    mileage INTEGER,
    payload JSON
);
"""

_META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR
);
"""

_COLUMNS: tuple[str, ...] = (
    "source",
    "offer_id",
    "sale_id",
    "url",
    "sale_hash",
    "category",
    "section",
    "sub_category",
    "title",
    "mark",
    "mark_name",
    "model",
    "model_name",
    "super_gen_id",
    "super_gen_name",
    "super_gen_year_from",
    "super_gen_year_to",
    "origin",
    "body_type",
    "body_type_human",
    "auto_class",
    "doors_count",
    "seats",
    "engine",
    "engine_type",
    "displacement",
    "power_hp",
    "power_kwt",
    "acceleration_to",
    "nameplate",
    "transmission",
    "drive",
    "color",
    "color_hex",
    "tech_param_id",
    "production_year",
    "mileage",
    "condition",
    "status",
    "availability",
    "price",
    "price_eur",
    "price_usd",
    "currency",
    "with_nds",
    "owners_number",
    "pts",
    "pts_original",
    "custom_cleared",
    "vin_resolution",
    "owners_resolution",
    "legal_resolution",
    "accidents_resolution",
    "not_registered_in_russia",
    "seller_type",
    "geo_id",
    "federal_subject_id",
    "photos_count",
    "transport_tax",
    "tags",
    "creation_date",
    "update_date",
    "days_in_stock",
    "days_on_sale",
    "price_history",
    "payload_hash",
    "observed_at",
)


def _row(offer: Offer) -> tuple[object, ...]:
    return (
        offer.source,
        offer.offer_id,
        offer.sale_id,
        offer.url,
        offer.sale_hash,
        offer.category,
        offer.section,
        offer.sub_category,
        offer.title,
        offer.mark,
        offer.mark_name,
        offer.model,
        offer.model_name,
        offer.super_gen_id,
        offer.super_gen_name,
        offer.super_gen_year_from,
        offer.super_gen_year_to,
        offer.origin,
        offer.body_type,
        offer.body_type_human,
        offer.auto_class,
        offer.doors_count,
        list(offer.seats),
        offer.engine,
        offer.engine_type,
        offer.displacement,
        offer.power_hp,
        offer.power_kwt,
        offer.acceleration_to,
        offer.nameplate,
        offer.transmission,
        offer.drive,
        offer.color,
        offer.color_hex,
        offer.tech_param_id,
        offer.production_year,
        offer.mileage,
        offer.condition,
        offer.status,
        offer.availability,
        offer.price,
        offer.price_eur,
        offer.price_usd,
        offer.currency,
        offer.with_nds,
        offer.owners_number,
        offer.pts,
        offer.pts_original,
        offer.custom_cleared,
        offer.vin_resolution,
        offer.owners_resolution,
        offer.legal_resolution,
        offer.accidents_resolution,
        offer.not_registered_in_russia,
        offer.seller_type,
        offer.geo_id,
        offer.federal_subject_id,
        offer.photos_count,
        offer.transport_tax,
        list(offer.tags),
        offer.creation_date,
        offer.update_date,
        offer.days_in_stock,
        offer.days_on_sale,
        _history_json(offer.price_history),
        offer.payload_hash,
        offer.observed_at,
    )


def _history_json(points: list[PricePoint]) -> str:
    import json

    return json.dumps(
        [{"price": p.price, "observed_at": p.observed_at.isoformat()} for p in points],
        ensure_ascii=False,
    )


class Store:
    """Хранилище объявлений и истории."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if self._path.parent != Path():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self._path))
        self._init_schema()

    def _init_schema(self) -> None:
        for ddl in (_OFFERS_DDL, _SNAPSHOTS_DDL, _META_DDL):
            self._con.execute(ddl)
        self._con.execute(
            "INSERT INTO meta VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [str(SCHEMA_VERSION)],
        )

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def known_payload_hashes(self) -> dict[tuple[str, str], str]:
        """Известные хеши содержимого: что нужно для решения о записи в историю."""
        rows = self._con.execute("SELECT source, offer_id, payload_hash FROM offers").fetchall()
        return {(str(r[0]), str(r[1])): str(r[2]) for r in rows}

    def save(self, offer: Offer) -> bool:
        """Записать объявление.

        Возвращает ``True``, если содержимое изменилось и в историю добавлена
        новая запись, ``False`` — если состояние совпало с прежним.
        """
        previous = self._con.execute(
            "SELECT payload_hash FROM offers WHERE source = ? AND offer_id = ?",
            [offer.source, offer.offer_id],
        ).fetchone()
        previous_hash = str(previous[0]) if previous else None

        placeholders = ", ".join("?" * len(_COLUMNS))
        self._con.execute(
            "DELETE FROM offers WHERE source = ? AND offer_id = ?", [offer.source, offer.offer_id]
        )
        self._con.execute(
            f"INSERT INTO offers ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            list(_row(offer)),
        )

        if previous_hash == offer.payload_hash:
            return False

        import json

        self._con.execute(
            "INSERT INTO offer_snapshots "
            "(source, offer_id, observed_at, payload_hash, price, mileage, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                offer.source,
                offer.offer_id,
                offer.observed_at,
                offer.payload_hash,
                offer.price,
                offer.mileage,
                json.dumps(offer.model_dump(mode="json"), ensure_ascii=False),
            ],
        )
        return True

    def save_many(self, offers: list[Offer]) -> int:
        """Записать список. Возвращает, сколько записей добавлено в историю."""
        return sum(1 for offer in offers if self.save(offer))

    def _count(self, table: str) -> int:
        row = self._con.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def count_offers(self) -> int:
        return self._count("offers")

    def count_snapshots(self) -> int:
        return self._count("offer_snapshots")


__all__ = ["SCHEMA_VERSION", "Store"]
