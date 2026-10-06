"""Экран в терминале: фильтры, запуск, остановка.

Textual умеет запускать приложение без терминала, поэтому проверки идут
через тот же цикл событий, что и настоящий экран, но с подменённым клиентом:
сеть в тестах не нужна.
"""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Log, Static

from autoru_market_dataset.core.errors import BlockedBySite, Cancelled, CaptchaRequired
from autoru_market_dataset.core.models import DEFAULT_DB, Filters, Offer, Page, PricePoint
from autoru_market_dataset.core.normalize import compute_payload_hash
from autoru_market_dataset.frontends.autoru.urls import build_url
from autoru_market_dataset.services.crawl import CrawlConfig
from autoru_market_dataset.storage.duck import Store
from autoru_market_dataset.ui.tui import KEYS, ParserApp


def sync(fn: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """Запустить асинхронный тест из обычного.

    Отдельная зависимость ради этого не нужна: у Textual есть свой цикл
    событий внутри ``run_test``, и одного ``asyncio.run`` достаточно.
    ``wraps`` обязателен — без него pytest перестаёт видеть фикстуры в
    аргументах и выдаёт «missing 1 required positional argument».
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(fn(*args, **kwargs))

    return wrapper


def make_offer(index: int, price: int) -> Offer:
    offer = Offer(
        offer_id=str(2000 + index),
        sale_id=f"{2000 + index}-aaaaaaaa",
        url=f"https://auto.ru/cars/used/sale/a/b/{2000 + index}-aaaaaaaa/",
        mark="BMW",
        model="X5",
        price=price,
        mileage=1000 * index,
        production_year=2020,
        seller_type="PRIVATE",
        price_history=[PricePoint(price=price, observed_at="2026-10-06T12:00:00+00:00")],  # type: ignore[arg-type]
        observed_at="2026-10-06T12:00:00+00:00",  # type: ignore[arg-type]
    )
    offer.payload_hash = compute_payload_hash(offer)
    return offer


class FakeClient:
    """Клиент с заранее заданными страницами и без сети."""

    def __init__(self, offers: int = 2, error: Exception | None = None, hang: bool = False):
        self._offers = offers
        self._error = error
        self._hang = hang
        self.closed = False
        self.calls = 0

    def url_for(self, filters: Filters) -> str:
        return build_url(filters)

    async def fetch(self, url: str) -> Page:
        import asyncio

        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._hang:
            await asyncio.sleep(3600)
        return Page(
            url=url,
            offers=[make_offer(i, 1_000_000 + i) for i in range(self._offers)],
            total_offers=self._offers,
            total_pages=1,
            page_size=37,
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tui.duckdb"


def log_text(app: ParserApp) -> str:
    return "\n".join(str(line) for line in app.query_one("#log", Log).lines)


def status_text(app: ParserApp) -> str:
    return str(app.query_one("#status", Static).render())


async def settle(pilot, predicate, tries: int = 100) -> None:
    """Подождать, пока экран дойдёт до нужного состояния.

    Пауза пилота уступает управление циклу событий, но не течёт времени
    ожидания: ограничитель темпа спит по-настоящему. Поэтому ждём короткими
    паузами и с запасом по числу попыток.
    """
    for _ in range(tries):
        if predicate(pilot.app):
            return
        await asyncio.sleep(0.02)
        await pilot.pause()


# --- устройство экрана -------------------------------------------------------


@sync
async def test_экран_собирается(db_path: Path) -> None:
    async with ParserApp(db_path).run_test() as pilot:
        await pilot.pause()
        app = pilot.app
        assert app.query(Button)
        for name in ("geo", "mark", "model", "year", "price_to"):
            assert app.query_one(f"#f-{name}", Input)
        assert status_text(app) == "Готов к работе"


@sync
async def test_у_экрана_есть_клавиши_старта_и_остановки(db_path: Path) -> None:
    """Экран должен быть пригоден с клавиатуры: мышь не всегда доступна."""
    actions = {key for _, key, _ in KEYS}
    assert {"start", "stop", "quit"} <= actions


# --- запуск ------------------------------------------------------------------


@sync
async def test_сбор_по_кнопке(db_path: Path) -> None:
    client = FakeClient(offers=3)

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-mark", Input).value = "bmw"
        await pilot.click("#btn-start")
        await settle(pilot, lambda app: "Готово" in status_text(app))
        assert "Готово" in status_text(pilot.app), log_text(pilot.app)
        assert "3" in status_text(pilot.app)

    with Store(db_path) as store:
        assert store.count_offers() == 3, "экран должен писать в ту же базу"


@sync
async def test_неверное_число_в_поле_ловится(db_path: Path) -> None:
    """Опечатка в поле не должна оборачиваться падением экрана."""
    client = FakeClient()

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-year", Input).value = "двадцать"
        await pilot.click("#btn-start")
        await pilot.pause()

        assert "Ошибка в полях" in status_text(pilot.app)
        assert client.calls == 0, "сбор не должен был начинаться"


@sync
async def test_сбой_сайта_показывается_человеку(db_path: Path) -> None:
    client = FakeClient(error=BlockedBySite("403"))

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-mark", Input).value = "bmw"
        await pilot.click("#btn-start")
        await settle(pilot, lambda app: "Сайт не отдал данные" in status_text(app))

        assert "Сайт не отдал данные" in status_text(pilot.app), log_text(pilot.app)


@sync
async def test_проверка_сайта_объясняется_человеку(db_path: Path) -> None:
    """На требовании проверки экран должен объяснять, что делать.

    CaptchaRequired — подкласс BlockedBySite, поэтому проверяется отдельно:
    иначе человек увидел бы «сайт не отдал данные» и не понял бы, что нужно.
    """
    client = FakeClient(error=CaptchaRequired("auto.ru требует подтверждения запроса"))

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-mark", Input).value = "bmw"
        await pilot.click("#btn-start")
        await settle(pilot, lambda app: "проверк" in status_text(app).lower())

        assert "проверк" in status_text(pilot.app).lower(), status_text(pilot.app)
        assert "Сайт не отдал данные" not in status_text(pilot.app)


@sync
async def test_клиент_закрывается_после_сбора(db_path: Path) -> None:
    client = FakeClient()

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-mark", Input).value = "bmw"
        await pilot.click("#btn-start")
        await settle(pilot, lambda _a: client.closed)

    assert client.closed


# --- остановка ---------------------------------------------------------------


@sync
async def test_остановка_останавливает_сбор(db_path: Path) -> None:
    client = FakeClient(hang=True)

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-mark", Input).value = "bmw"
        await pilot.click("#btn-start")
        await settle(pilot, lambda _a: bool(client.calls))
        await pilot.click("#btn-stop")
        assert "Останавливаю" in status_text(pilot.app)


@sync
async def test_повторный_старт_не_создаёт_второй_сбор(db_path: Path) -> None:
    """Двойное нажатие «Собрать» не должно запустить обход дважды."""
    client = FakeClient(hang=True)

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        pilot.app.query_one("#f-mark", Input).value = "bmw"
        await pilot.click("#btn-start")
        # Обработчики выполняются через очередь сообщений, поэтому состояние
        # не появляется мгновенно: ждём его, а не фиксированную паузу.
        await settle(pilot, lambda app: "Запущено" in status_text(app))
        # Второй запуск — с клавиатуры: так проверяется и защита от двойного
        # сбора, и то, что экран пригоден без мыши.
        await pilot.press("s")
        await settle(pilot, lambda app: "Сбор уже идёт" in status_text(app))
        assert "Сбор уже идёт" in status_text(pilot.app), status_text(pilot.app)
        assert client.calls == 1, "второй сбор не должен был начаться"


@sync
async def test_стоп_без_сбора_не_ошибка(db_path: Path) -> None:
    client = FakeClient()

    async with ParserApp(db_path, client_factory=lambda _c: client, rps=1000).run_test() as pilot:
        await pilot.pause()
        await pilot.click("#btn-stop")
        assert "Сбор не идёт" in status_text(pilot.app)


# --- настройки ---------------------------------------------------------------


def test_экран_берёт_путь_к_базе() -> None:
    app = ParserApp(Path("data/другая.duckdb"))
    assert app._db == Path("data/другая.duckdb")
    assert ParserApp()._db == Path(DEFAULT_DB)


def test_клиент_по_умолчанию_httpx() -> None:
    """Обычный клиент строится на httpx, без браузера."""
    from autoru_market_dataset.frontends.autoru.client import AutoRuClient
    from autoru_market_dataset.ui.tui import default_client

    client = default_client(CrawlConfig())
    assert isinstance(client, AutoRuClient)


def test_отмена_снаружи_доходит_до_обхода() -> None:
    """Флаг отмены проверяется обходом, а не только экраном."""
    import asyncio

    from autoru_market_dataset.services.crawl import Crawler

    async def scenario() -> None:
        client = FakeClient(hang=True)
        crawler = Crawler(client, Store(Path("data/проверка.duckdb")), CrawlConfig())
        crawler.cancel()
        with pytest.raises(Cancelled):
            await crawler.crawl(Filters(mark="bmw"))

    asyncio.run(scenario())
