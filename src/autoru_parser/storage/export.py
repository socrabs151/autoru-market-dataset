"""Выгрузка данных в табличные форматы.

Формат по умолчанию — Parquet внутри DuckDB, поэтому CSV и JSON здесь
побочные: их читают в других программах, и для этого нужен обычный текст,
а не аналитическая база.

Отдельная ценность выгрузки в том, что часть фильтров применяется
**локально**. Сайт не умеет фильтровать по пробегу, типу продавца и
состоянию через адрес — такие адреса запрещены правилами сайта, и их
параметры существуют только как query-строка (решение 10). Зато в собранных
данных всё это есть, и фильтр превращается в обычное условие запроса.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from autoru_parser.core.errors import ParserError

#: Таблицы, которые имеет смысл выгружать.
TABLE_OFFERS = "offers"
TABLE_SNAPSHOTS = "offer_snapshots"

#: Условия, которые нельзя задать на стороне сайта.
#: Названия полей совпадают с колонками таблицы ``offers``.
LOCAL_FILTER_FIELDS = frozenset(
    {
        "mileage",
        "price",
        "production_year",
        "seller_type",
        "pts",
        "mark",
        "model",
        "super_gen_id",
        "transmission",
        "drive",
        "body_type",
        "owner_expenses",
    }
)


@dataclass
class LocalFilter:
    """Фильтр по собранным данным.

    Один класс на все поля: условия однотипные, а разводить их по классам
    означало бы плодить одинаковый код. Диапазон с двумя границами —
    тоже один случай, а не два.
    """

    #: Условия вида ``поле -> SQL``. Хранятся строкой, потому что это
    #: готовый SQL, а параметры передаются отдельно.
    where: str = ""
    params: list[object] = field(default_factory=list)

    def render(self) -> str:
        return self.where


def mileage_to(km: int) -> LocalFilter:
    """Пробег не больше заданного. Сайт такой фильтр не поддерживает."""
    return LocalFilter(where="mileage IS NOT NULL AND mileage <= ?", params=[km])


def mileage_from(km: int) -> LocalFilter:
    return LocalFilter(where="mileage >= ?", params=[km])


def price_between(low: int, high: int) -> LocalFilter:
    return LocalFilter(
        where="price IS NOT NULL AND price >= ? AND price <= ?",
        params=[low, high],
    )


def year_from(year: int) -> LocalFilter:
    return LocalFilter(where="production_year >= ?", params=[year])


def year_to(year: int) -> LocalFilter:
    return LocalFilter(where="production_year <= ?", params=[year])


def seller_type(value: str) -> LocalFilter:
    """Частные объявления или салоны. Сайт такой фильтр не поддерживает."""
    return LocalFilter(where="upper(seller_type) = ?", params=[value.upper()])


def mark_is(value: str) -> LocalFilter:
    return LocalFilter(where="upper(mark) = ?", params=[value.upper()])


def and_(*filters: LocalFilter) -> LocalFilter:
    """Объединить условия."""
    parts = [f for f in filters if f.render()]
    if not parts:
        return LocalFilter()
    params: list[object] = []
    for item in filters:
        params.extend(item.params)
    return LocalFilter(where=" AND ".join(f"({p.render()})" for p in parts), params=params)


def _copy_query(
    con: duckdb.DuckDBPyConnection,
    table: str,
    where: str,
    params: list[object],
    target: Path,
    fmt: str,
) -> int:
    if not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    clauses = f"WHERE {where}" if where else ""
    select = f"SELECT * FROM {table} {clauses}"

    if fmt == "csv":
        statement = f"COPY ({select}) TO '{_escape(target)}' (FORMAT CSV, HEADER)"
    elif fmt == "json":
        # ARRAY true — обычный JSON-массив, его понимают все парсеры.
        statement = f"COPY ({select}) TO '{_escape(target)}' (FORMAT JSON, ARRAY true)"
    elif fmt == "ndjson":
        # По одному объекту на строку. Формат проверен: ARRAY false даёт
        # именно построчный вывод, а не массив. Удобен, когда файл велик и
        # читать его целиком в память незачем.
        statement = f"COPY ({select}) TO '{_escape(target)}' (FORMAT JSON, ARRAY false)"
    elif fmt == "parquet":
        statement = f"COPY ({select}) TO '{_escape(target)}' (FORMAT PARQUET)"
    else:
        raise ParserError(f"формат {fmt!r} не поддерживается: csv, json, ndjson или parquet")

    con.execute(statement, params)
    return _count_rows(con, select, params)


def _escape(path: Path) -> str:
    """Экранировать путь для подстановки в SQL.

    Путь приходит из командной строки, поэтому кавычки в нём должны быть
    обезврежены: иначе значение влияет на запрос.
    """
    return str(path).replace("'", "''")


def _count_rows(con: duckdb.DuckDBPyConnection, select: str, params: list[object]) -> int:
    row = con.execute(f"SELECT count(*) FROM ({select})", params).fetchone()
    return int(row[0]) if row else 0


def export_offers(
    con: duckdb.DuckDBPyConnection,
    target: Path,
    fmt: str = "csv",
    local_filter: LocalFilter | None = None,
) -> int:
    """Выгрузить текущее состояние объявлений. Возвращает число строк."""
    spec = local_filter or LocalFilter()
    return _copy_query(con, TABLE_OFFERS, spec.render(), spec.params, target, fmt)


def export_snapshots(
    con: duckdb.DuckDBPyConnection,
    target: Path,
    fmt: str = "csv",
    since: str | None = None,
) -> int:
    """Выгрузить историю изменений.

    :param since: дата в формате ``ГГГГ-ММ-ДД``, от которой брать историю.
    """
    where = "observed_at >= ?" if since else ""
    params: list[object] = [since] if since else []
    return _copy_query(con, TABLE_SNAPSHOTS, where, params, target, fmt)


__all__ = [
    "LOCAL_FILTER_FIELDS",
    "TABLE_OFFERS",
    "TABLE_SNAPSHOTS",
    "LocalFilter",
    "and_",
    "export_offers",
    "export_snapshots",
    "mark_is",
    "mileage_from",
    "mileage_to",
    "price_between",
    "seller_type",
    "year_from",
    "year_to",
]
