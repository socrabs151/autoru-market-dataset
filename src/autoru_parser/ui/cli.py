"""Командная строка.

Textual-экран (этап 4) появится отдельной командой ``autoru tui``. Здесь
только то, что нужно для запуска и автоматизации: команды, флаги, вывод.

Набор опций фильтра объявлен один раз на уровне модуля, чтобы команды
``url`` и ``crawl`` не могли разойтись. Типы при этом остаются явными:
собирать фильтр из ``**kwargs`` нельзя — проверка типов такие вызовы не
проверяет.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog
import typer

from autoru_parser.core.errors import (
    BlockedBySite,
    LimitReached,
    NoResults,
    ParseError,
    ParserError,
)
from autoru_parser.core.models import DEFAULT_DB, Filters, Progress
from autoru_parser.frontends.autoru.client import AutoRuClient
from autoru_parser.frontends.autoru.urls import (
    AVAILABILITIES,
    BODY_TYPES,
    CATEGORIES,
    DRIVES,
    ENGINES,
    ORIGINS,
    SECTIONS,
    TRANSMISSIONS,
    build_url,
)
from autoru_parser.services.crawl import CrawlConfig, Crawler, build_http
from autoru_parser.services.history import (
    dataset_summary,
    hash_consistency,
    price_changes,
    price_history,
    yearly_prices,
)
from autoru_parser.storage import export as export_mod
from autoru_parser.storage.duck import Store

app = typer.Typer(
    help="Сбор объявлений auto.ru для личного датасета и анализа рынка.",
    no_args_is_help=True,
    add_completion=False,
)


def _choices(field: str, values: frozenset[str]) -> str:
    """Подсказка со списком допустимых значений — чтобы словарь сайта
    не пришлось искать по исходникам."""
    return ", ".join(sorted(values))


_GEO = typer.Option(None, help="город или регион слагами, например moskva")
_MARK = typer.Option(None, help="марка слагом, например bmw")
_MODEL = typer.Option(None, help="модель слагом, например x5")
_YEAR = typer.Option(None, help="год выпуска")
_PRICE_TO = typer.Option(None, help="цена до, рублей")
_CATEGORY = typer.Option("cars", help=f"категория: {_choices('c', CATEGORIES)}")
_SECTION = typer.Option("used", help=f"состояние: {_choices('s', SECTIONS)}")
_AVAILABILITY = typer.Option("any", help=f"наличие: {_choices('a', AVAILABILITIES)}")
_BODY = typer.Option(None, help=f"кузов: {_choices('b', BODY_TYPES)}")
_COLOR = typer.Option(None, help="цвет слагом, например belyj")
_DRIVE = typer.Option(None, help=f"привод: {_choices('d', DRIVES)}")
_TRANSMISSION = typer.Option(None, help=f"коробка: {_choices('t', TRANSMISSIONS)}")
_ENGINE = typer.Option(None, help=f"двигатель: {_choices('e', ENGINES)}")
_SEATS = typer.Option(None, help="число мест")
_ORIGIN = typer.Option(None, help=f"происхождение марки: {_choices('o', ORIGINS)}")


def _filters(
    geo: str | None,
    mark: str | None,
    model: str | None,
    year: int | None,
    price_to: int | None,
    category: str,
    section: str,
    availability: str,
    body: str | None,
    color: str | None,
    drive: str | None,
    transmission: str | None,
    engine: str | None,
    seats: int | None,
    origin: str | None,
) -> Filters:
    return Filters(
        geo=geo,
        mark=mark,
        model=model,
        year=year,
        price_to=price_to,
        category=category,
        section=section,
        availability=availability,
        body=body,
        color=color,
        drive=drive,
        transmission=transmission,
        engine=engine,
        seats=seats,
        origin=origin,
    )


def _fail(message: str, code: int) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _configure_logging(verbose: bool) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(10 if verbose else 20),
        cache_logger_on_first_use=True,
    )


@app.command()
def url(
    geo: str | None = _GEO,
    mark: str | None = _MARK,
    model: str | None = _MODEL,
    year: int | None = _YEAR,
    price_to: int | None = _PRICE_TO,
    category: str = _CATEGORY,
    section: str = _SECTION,
    availability: str = _AVAILABILITY,
    body: str | None = _BODY,
    color: str | None = _COLOR,
    drive: str | None = _DRIVE,
    transmission: str | None = _TRANSMISSION,
    engine: str | None = _ENGINE,
    seats: int | None = _SEATS,
    origin: str | None = _ORIGIN,
) -> None:
    """Показать адрес выборки по фильтру. Ничего не скачивает."""
    filters = _filters(
        geo,
        mark,
        model,
        year,
        price_to,
        category,
        section,
        availability,
        body,
        color,
        drive,
        transmission,
        engine,
        seats,
        origin,
    )
    try:
        typer.echo(build_url(filters))
    except ParserError as exc:
        _fail(f"Некорректный фильтр: {exc}", 2)


@app.command()
def crawl(
    geo: str | None = _GEO,
    mark: str | None = _MARK,
    model: str | None = _MODEL,
    year: int | None = _YEAR,
    price_to: int | None = _PRICE_TO,
    category: str = _CATEGORY,
    section: str = _SECTION,
    availability: str = _AVAILABILITY,
    body: str | None = _BODY,
    color: str | None = _COLOR,
    drive: str | None = _DRIVE,
    transmission: str | None = _TRANSMISSION,
    engine: str | None = _ENGINE,
    seats: int | None = _SEATS,
    origin: str | None = _ORIGIN,
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
    rps: float = typer.Option(0.5, help="запросов в секунду"),
    max_requests: int = typer.Option(100, help="плановый лимит запросов на прогон"),
    full: bool = typer.Option(False, "--full", help="собрать выборку целиком, а не одну страницу"),
    max_depth: int = typer.Option(2, help="насколько дробить выборку при сборе целиком"),
    as_json: bool = typer.Option(False, "--json", help="вывести итог в JSON"),
    verbose: bool = typer.Option(False, "-v", help="подробный лог"),
) -> None:
    """Собрать выборку и записать в базу.

    По умолчанию берётся одна страница: это быстро и предсказуемо.
    С ``--full`` выборка дробится на непересекающиеся части, пока каждая
    не поместится в страницу, — так достигается полнота без пагинации.
    """
    _configure_logging(verbose)
    filters = _filters(
        geo,
        mark,
        model,
        year,
        price_to,
        category,
        section,
        availability,
        body,
        color,
        drive,
        transmission,
        engine,
        seats,
        origin,
    )
    config = CrawlConfig(rps=rps, max_requests=max_requests, max_depth=max_depth, db_path=db)

    async def run() -> Progress:
        with Store(config.db_path) as store:
            async with build_http(config) as http:
                crawler = Crawler(AutoRuClient(http), store, config)
                crawler.reset_progress()
                if full:
                    return await crawler.crawl_full(filters)
                return await crawler.crawl(filters)

    try:
        result = asyncio.run(run())
    except LimitReached as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2) from exc
    except NoResults as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=5) from exc
    except BlockedBySite as exc:
        _fail(f"Сайт не отдал данные: {exc}", 3)
        return
    except ParseError as exc:
        _fail(f"Не удалось разобрать ответ: {exc}", 4)
        return
    except ParserError as exc:
        _fail(f"Некорректные параметры: {exc}", 2)
        return

    if as_json:
        typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    coverage = result.coverage
    typer.echo(
        f"Страниц: {result.pages_done}, объявлений: {result.offers_seen}, "
        f"записано в базу: {result.offers_stored}, запросов: {result.requests}"
    )
    if coverage.expected:
        empty = f", пустых частей {coverage.empty_parts}" if coverage.empty_parts else ""
        typer.echo(
            f"Полнота: {coverage.covered} из {coverage.expected} "
            f"({coverage.percent}%), частей {coverage.leaves}{empty}"
        )
        if not coverage.complete:
            typer.secho(
                f"Выборка собрана не полностью: {coverage.reason}",
                fg=typer.colors.YELLOW,
                err=True,
            )


@app.command("count")
def count(
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
) -> None:
    """Показать, сколько объявлений и записей истории в базе."""
    with Store(db) as store:
        typer.echo(f"объявлений: {store.count_offers()}")
        typer.echo(f"записей истории: {store.count_snapshots()}")


@app.command("export")
def export_cmd(
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
    out: Path = typer.Option(Path("data/offers.csv"), help="куда выгружать"),
    fmt: str = typer.Option("csv", help="csv | json | parquet"),
    table: str = typer.Option("offers", help="offers | snapshots"),
    since: str = typer.Option(None, help="для snapshots: дата ГГГГ-ММ-ДД"),
    mileage_to: int = typer.Option(None, help="пробег не больше, км"),
    mileage_from: int = typer.Option(None, help="пробег не меньше, км"),
    price_from: int = typer.Option(None, help="цена не меньше, руб"),
    price_to: int = typer.Option(None, help="цена не больше, руб"),
    year_from: int = typer.Option(None, help="год не меньше"),
    year_to: int = typer.Option(None, help="год не больше"),
    seller_type: str = typer.Option(None, help="PRIVATE или DEALER"),
    mark: str = typer.Option(None, help="марка, например BMW"),
) -> None:
    """Выгрузить данные в файл.

    Фильтры ``--mileage-*``, ``--seller-type`` и ``--year-*`` работают по
    собранным данным, а не на стороне сайта: сайт такие фильтры через адрес
    не поддерживает, их параметры запрещены правилами сайта.
    """
    filters = [
        f
        for f in (
            export_mod.mileage_to(mileage_to) if mileage_to is not None else None,
            export_mod.mileage_from(mileage_from) if mileage_from is not None else None,
            export_mod.price_between(price_from, price_to)
            if price_from is not None and price_to is not None
            else None,
            export_mod.year_from(year_from) if year_from is not None else None,
            export_mod.year_to(year_to) if year_to is not None else None,
            export_mod.seller_type(seller_type) if seller_type else None,
            export_mod.mark_is(mark) if mark else None,
        )
        if f is not None
    ]
    local = export_mod.and_(*filters) if filters else None

    with Store(db) as store:
        if table == "snapshots":
            rows = export_mod.export_snapshots(store.connection, out, fmt, since)
        elif table == "offers":
            rows = export_mod.export_offers(store.connection, out, fmt, local)
        else:
            _fail(f"неизвестная таблица {table!r}: подходит offers или snapshots", 2)
            return

    typer.echo(f"выгружено строк: {rows} -> {out}")


@app.command("summary")
def summary(
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
    as_json: bool = typer.Option(False, "--json", help="вывести в JSON"),
) -> None:
    """Сводка по накопленному: объёмы, годы, разброс цен."""
    with Store(db) as store:
        data = dataset_summary(store.connection)
        if as_json:
            typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
            return
        if data.get("объявлений", 0) == 0:
            typer.echo("в базе пока нет объявлений с ценой")
            return
        for key, value in data.items():
            typer.echo(f"{key:>12}: {value}")


@app.command("prices")
def prices(
    offer_id: str = typer.Argument(..., help="идентификатор объявления"),
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
) -> None:
    """История цен одного объявления."""
    with Store(db) as store:
        rows = price_history(store.connection, offer_id)
    if not rows:
        typer.echo(f"по объявлению {offer_id} истории цен нет")
        raise typer.Exit(code=1)
    typer.echo(f"история цен объявления {offer_id}:")
    previous: int | None = None
    for when, price in rows:
        if previous is None:
            typer.echo(f"  {when[:19]}  {price:>10}")
        else:
            delta = price - previous
            mark = f"{delta:+d}" + (f" ({100.0 * delta / previous:+.1f}%)" if previous else "")
            typer.echo(f"  {when[:19]}  {price:>10}  {mark}")
        previous = price


@app.command("changes")
def changes(
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
    limit: int = typer.Option(30, help="сколько показать"),
    all_changes: bool = typer.Option(False, "--all", help="показывать и записи без изменения цены"),
) -> None:
    """Изменения цен по всем объявлениям."""
    with Store(db) as store:
        rows = price_changes(store.connection, limit, only_changed=not all_changes)
    if not rows:
        typer.echo("изменений цен пока нет: нужно минимум два прогона по одному объявлению")
        raise typer.Exit(code=1)
    typer.echo(f"{'id':>12}  {'машина':<22} {'старая':>10} {'новая':>10}  {'изм.':>8}")
    for change in rows:
        car = " ".join(part for part in (change.mark, change.model) if part)
        typer.echo(
            f"{change.offer_id:>12}  {car[:22]:<22} {change.old_price:>10} "
            f"{change.new_price:>10}  {change.percent:>7}%"
        )


@app.command("check-hash")
def check_hash(
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
) -> None:
    """Проверить гипотезу: меняется ли хеш в адресе при смене цены.

    Если у одного идентификатора встретилось несколько разных хешей, значит
    хеш зависит от содержимого объявления и использовать его как часть
    ключа нельзя. Пустой ответ означает лишь, что данных пока мало.
    """
    with Store(db) as store:
        suspicious = hash_consistency(store.connection)
        snapshots = store.count_snapshots()
    if snapshots < 2:
        typer.echo("нужно минимум два прогона: история ещё не накопилась")
        return
    if not suspicious:
        typer.echo(f"подозрительных нет: у всех {snapshots} записей один хеш на идентификатор")
        return
    typer.secho(f"хеш менялся у {len(suspicious)} объявлений:", fg=typer.colors.YELLOW)
    for line in suspicious:
        typer.echo(f"  {line}")


@app.command("by-year")
def by_year(
    db: Path = typer.Option(Path(DEFAULT_DB), help="файл базы"),
) -> None:
    """Сводка цен по годам выпуска."""
    with Store(db) as store:
        rows = yearly_prices(store.connection)
    if not rows:
        typer.echo("данных с ценой и годом пока нет")
        return
    typer.echo(f"{'год':>6} {'машин':>7} {'средняя':>11} {'медиана':>11} {'ср.пробег':>11}")
    for year, avg, median, count, avg_mileage in rows:
        typer.echo(f"{year:>6} {count:>7} {int(avg):>11} {median:>11} {avg_mileage:>11}")


if __name__ == "__main__":
    app()
