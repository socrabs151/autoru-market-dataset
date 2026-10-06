"""Момент времени в базе не должен зависеть от машины.

Две ошибки, обе найдены на CI и обе выглядели бы как «иногда падает».

Первая: DuckDB читает колонки ``TIMESTAMPTZ`` через ``pytz``. Без него любой
запрос к истории цен и списку объявлений падает. Проверяется тем, что
``pytz`` объявлен в зависимостях.

Вторая, опаснее: если часовой пояс базы не задан, DuckDB берёт его из системы.
Одна и та же запись на двух машинах прочитается с разным часом, а история цен
существует ради сравнения по датам. Здесь проверяется, что время всегда в UTC.
"""

from datetime import UTC, datetime
from pathlib import Path

from autoru_market_dataset.core.models import Offer, PricePoint, now_utc
from autoru_market_dataset.storage.duck import Store

MOMENT = datetime(2026, 10, 6, 21, 18, 31, tzinfo=UTC)


def make_offer(offer_id: str, observed: datetime) -> Offer:
    return Offer(
        offer_id=offer_id,
        sale_id=f"{offer_id}-aaaaaaaa",
        url=f"https://auto.ru/cars/used/sale/a/b/{offer_id}-aaaaaaaa/",
        mark="BMW",
        model="X5",
        price=1_000_000,
        price_history=[PricePoint(price=1_000_000, observed_at=observed)],
        observed_at=observed,
    )


def test_момент_читается_в_utc(tmp_path: Path) -> None:
    """Записанный UTC должен прочитаться как UTC, а не как местное время."""
    with Store(tmp_path / "utc.duckdb") as store:
        store.save(make_offer("1", MOMENT))
        stored = store.connection.execute(
            "SELECT observed_at FROM offers WHERE offer_id = '1'"
        ).fetchone()[0]

    assert stored.tzinfo is not None, "время прочиталось без часового пояса"
    assert stored.utcoffset() == UTC.utcoffset(stored), f"ожидался UTC, получено {stored.tzinfo}"
    assert stored == MOMENT, f"ожидалось {MOMENT}, получено {stored}"


def test_поля_времени_в_истории_тоже_utc(tmp_path: Path) -> None:
    """История читается отдельно от объявлений, и она тоже должна быть в UTC."""
    with Store(tmp_path / "история.duckdb") as store:
        store.save(make_offer("2", MOMENT))
        rows = store.connection.execute(
            "SELECT observed_at FROM offer_snapshots WHERE offer_id = '2'"
        ).fetchall()

    assert rows, "в истории нет ни одной записи"
    for (value,) in rows:
        assert value == MOMENT, f"ожидалось {MOMENT}, получено {value}"


def test_пояс_базы_не_берётся_из_системы(tmp_path: Path) -> None:
    """Пояс в самой базе задан явно, а не унаследован от машины."""
    with Store(tmp_path / "пояс.duckdb") as store:
        setting = store.connection.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    assert str(setting) == "UTC", f"пояс базы = {setting}"


def test_pytz_объявлен_в_зависимостях() -> None:
    """Зависимость должна быть объявлена, а не принесена кем-то ещё.

    Именно так это и сломалось: ``pytz`` ставили ``pandas`` и ``babel``, и на
    машине без них всё работало, пока CI не поставил пакет в чистое окружение.
    """
    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8")
    required = text.split("[project.optional-dependencies]")[0]
    assert "pytz" in required, "pytz не объявлен среди обязательных зависимостей"


def test_приклад_тоже_даёт_время_с_поясом() -> None:
    """Моменты наблюдения по определению в UTC, иначе история не сравнима."""
    assert now_utc().tzinfo is not None
