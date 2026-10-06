"""Запросы по истории цен.

История — главная причина, по которой проект затевался: у сайта своя
история цен неполная (замерено: одна-две записи), поэтому временные ряды
накапливаются ежедневными прогонами.

Все запросы читают базу и ничего не меняют.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class PriceChange:
    """Одно изменение цены."""

    offer_id: str
    mark: str | None
    model: str | None
    production_year: int | None
    mileage: int | None
    old_price: int
    new_price: int

    @property
    def delta(self) -> int:
        return self.new_price - self.old_price

    @property
    def percent(self) -> float:
        return round(100.0 * self.delta / self.old_price, 1) if self.old_price else 0.0


def price_history(con: duckdb.DuckDBPyConnection, offer_id: str) -> list[tuple[str, int]]:
    """История цен одного объявления: ``(когда, цена)``, по возрастанию."""
    rows = con.execute(
        """
        SELECT observed_at, price
        FROM offer_snapshots
        WHERE offer_id = ? AND price IS NOT NULL
        ORDER BY observed_at
        """,
        [offer_id],
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


def current_price_change(con: duckdb.DuckDBPyConnection, offer_id: str) -> PriceChange | None:
    """Изменилась ли цена объявления с прошлого прогона."""
    row = con.execute(
        """
        WITH ordered AS (
            SELECT price, row_number() OVER (ORDER BY observed_at DESC) AS rn
            FROM offer_snapshots
            WHERE offer_id = ? AND price IS NOT NULL
        )
        SELECT
            (SELECT price FROM ordered WHERE rn = 1),
            (SELECT price FROM ordered WHERE rn = 2)
        """,
        [offer_id],
    ).fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    new_price, old_price = int(row[0]), int(row[1])
    if new_price == old_price:
        return None
    info = con.execute(
        "SELECT mark, model, production_year, mileage FROM offers WHERE offer_id = ?",
        [offer_id],
    ).fetchone()
    return PriceChange(
        offer_id=offer_id,
        mark=info[0] if info else None,
        model=info[1] if info else None,
        production_year=int(info[2]) if info and info[2] is not None else None,
        mileage=int(info[3]) if info and info[3] is not None else None,
        old_price=old_price,
        new_price=new_price,
    )


def price_changes(
    con: duckdb.DuckDBPyConnection,
    limit: int = 50,
    only_changed: bool = True,
) -> list[PriceChange]:
    """Изменения цен по всем объявлениям, свежие сверху.

    Считается по таблице состояния, а не по снапшотам: снапшотов может быть
    много, а вопрос «что изменилось с прошлого прогона» — это сравнение двух
    последних записей. Соединение окна даёт и то, и другое разом.
    """
    sql = """
        WITH ordered AS (
            SELECT
                offer_id,
                price,
                observed_at,
                lag(price) OVER (PARTITION BY offer_id ORDER BY observed_at) AS prev_price
            FROM offer_snapshots
            WHERE price IS NOT NULL
        ),
        latest AS (
            SELECT offer_id, price, prev_price
            FROM ordered
            WHERE prev_price IS NOT NULL
        )
        SELECT o.offer_id, o.mark, o.model, o.production_year, o.mileage,
               l.prev_price, l.price
        FROM latest l
        JOIN offers o USING (offer_id)
        {where}
        ORDER BY o.offer_id
        LIMIT ?
    """
    where = "WHERE l.price <> l.prev_price" if only_changed else ""
    rows = con.execute(sql.format(where=where), [limit]).fetchall()
    return [
        PriceChange(
            offer_id=str(row[0]),
            mark=row[1],
            model=row[2],
            production_year=int(row[3]) if row[3] is not None else None,
            mileage=int(row[4]) if row[4] is not None else None,
            old_price=int(row[5]),
            new_price=int(row[6]),
        )
        for row in rows
    ]


def hash_consistency(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Объявления, у которых менялся ``sale_hash`` — проверка гипотезы.

    Гипотеза из решения 5: хеш в адресе может зависеть от цены. Если она
    верна, у одного объявления при неизменном ``id`` встретится несколько
    разных ``sale_hash``.

    Проверять это иначе нечем: два прогона в один день не дадут разницы,
    а данные между прогонами меняются. Поэтому проверка запускается на
    накопленной истории и возвращает список подозрительных объявлений.
    Пустой список означает, что гипотеза пока не подтвердилась, а не что
    она опровергнута.
    """
    rows = con.execute(
        """
        SELECT
            s.offer_id,
            count(DISTINCT json_extract_string(s.payload, '$.sale_hash')) AS hashes,
            count(DISTINCT json_extract_string(s.payload, '$.price')) AS prices,
            count(*) AS versions
        FROM offer_snapshots s
        WHERE s.payload IS NOT NULL
        GROUP BY s.offer_id
        HAVING count(DISTINCT json_extract_string(s.payload, '$.sale_hash')) > 1
        ORDER BY s.offer_id
        """
    ).fetchall()
    return [
        f"id={row[0]}: разных хешей {row[1]}, разных цен {row[2]}, версий {row[3]}" for row in rows
    ]


def dataset_summary(con: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Короткая сводка по накопленному: сколько, какие годы, какой разброс цен."""
    row = con.execute(
        """
        SELECT
            count(*),
            count(DISTINCT mark),
            count(DISTINCT model),
            count(DISTINCT production_year),
            min(production_year),
            max(production_year),
            min(price),
            max(price),
            min(mileage),
            max(mileage),
            count(*) FILTER (WHERE seller_type = 'PRIVATE'),
            count(*) FILTER (WHERE seller_type IS NOT NULL
                             AND seller_type <> 'PRIVATE')
        FROM offers
        WHERE price IS NOT NULL
        """
    ).fetchone()
    if not row or row[0] == 0:
        return {"объявлений": 0}
    return {
        "объявлений": int(row[0]),
        "марок": int(row[1]),
        "моделей": int(row[2]),
        "годов": int(row[3]),
        "год_от": int(row[4]) if row[4] is not None else None,
        "год_до": int(row[5]) if row[5] is not None else None,
        "цена_от": int(row[6]) if row[6] is not None else None,
        "цена_до": int(row[7]) if row[7] is not None else None,
        "пробег_от": int(row[8]) if row[8] is not None else None,
        "пробег_до": int(row[9]) if row[9] is not None else None,
        "частных": int(row[10]),
        "салонов": int(row[11]),
    }


def yearly_prices(
    con: duckdb.DuckDBPyConnection,
) -> list[tuple[int, float, int, int, int]]:
    """Сводка по годам: средняя и медианная цена, машин, средний пробег.

    Среднее и медиана показываются вместе не для redundancy: среднее тянут
    вверх единичные дорогие машины и несколько внедорожников, а медиана
    показывает настоящий уровень рынка. Разница между ними и есть
    показатель неоднородности выборки.
    """
    rows = con.execute(
        """
        SELECT
            production_year,
            round(avg(price))::BIGINT,
            quantile_cont(price, 0.5)::BIGINT,
            count(*),
            round(avg(mileage))::BIGINT
        FROM offers
        WHERE price IS NOT NULL AND production_year IS NOT NULL
        GROUP BY production_year
        ORDER BY production_year
        """
    ).fetchall()
    return [
        (int(r[0]), float(r[1]), int(r[2]), int(r[3]), int(r[4]) if r[4] is not None else 0)
        for r in rows
    ]


__all__ = [
    "PriceChange",
    "current_price_change",
    "dataset_summary",
    "hash_consistency",
    "price_changes",
    "price_history",
    "yearly_prices",
]
