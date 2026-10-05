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


if __name__ == "__main__":
    app()
