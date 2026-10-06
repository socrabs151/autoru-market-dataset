"""Экран в терминале.

Зачем он, если есть командная строка. Одна причина: сбор может идти долго,
а человеку нужно видеть, что происходит, и иметь возможность остановить.
В терминале это делается без внешних зависимостей и без окна.

Экран не содержит логики сбора. Он только спрашивает фильтры, запускает
тот же ``Crawler``, что и командная строка, и показывает прогресс.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Log, Static

from autoru_market_dataset.core.errors import (
    BlockedBySite,
    Cancelled,
    CaptchaRequired,
    ParserError,
)
from autoru_market_dataset.core.models import DEFAULT_DB, Filters
from autoru_market_dataset.frontends.autoru.captcha import explain, report
from autoru_market_dataset.frontends.autoru.client import AutoRuClient
from autoru_market_dataset.services.crawl import CrawlConfig, Crawler, build_http
from autoru_market_dataset.storage.duck import Store

FIELDS = (
    ("geo", "Город или регион", "moskva"),
    ("mark", "Марка", "bmw"),
    ("model", "Модель", "x5"),
    ("year", "Год (необязательно)", ""),
    ("price_to", "Цена до (необязательно)", ""),
)

TOGGLES = (("full", "Собрать выборку целиком", True),)


#: Клавиши экрана.
#:
#: Базовый класс объявляет BINDINGS как список смешанного типа, а список
#: только из троек такой тип не принимает: списки в проверке типов
#: инвариантны. Обойти это нельзя ни через класс, ни через экземпляр,
#: поэтому подавление объявлено явно — интеграция со сторонним фреймворком,
#: а не ошибка здесь.
KEYS: list[Binding | tuple[str, str] | tuple[str, str, str]] = [
    ("q", "quit", "Выход"),
    ("s", "start", "Старт"),
    ("x", "stop", "Стоп"),
]


#: Как делается клиент для прогона. Подменяется в тестах, чтобы экран
#: проверялся без сети.
ClientFactory = Callable[[CrawlConfig], AutoRuClient]


def default_client(config: CrawlConfig) -> AutoRuClient:
    """Обычный клиент: httpx, без браузера (решение 1)."""
    return AutoRuClient(build_http(config))


class ParserApp(App[None]):
    """Сбор объявлений с прогрессом и остановкой."""

    BINDINGS = KEYS

    CSS = """
    #form { height: auto; padding: 1 2; }
    .row { height: 3; }
    .label { width: 28; padding: 1 1 0 0; }
    Input { width: 40; }
    #actions { height: auto; padding: 0 2 1 2; }
    Button { margin-right: 2; }
    #status { padding: 0 2; color: $text-muted; }
    #log { height: 1fr; border: round $primary; }
    """

    def __init__(
        self,
        db: Path | None = None,
        client_factory: ClientFactory | None = None,
        rps: float = 0.5,
        full: bool = False,
    ) -> None:
        super().__init__()
        self._db = db or Path(DEFAULT_DB)
        self._client_factory = client_factory or default_client
        self._rps = rps
        self._full = full
        self._crawler: Crawler | None = None
        # Имя не ``_task``: это поле есть у самого Textual, оно держит задачу
        # приложения, и перекрытие выдавало бы «сбор уже идёт» на пустом месте.
        self._crawl_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="form"):
            for name, title, placeholder in FIELDS:
                with Horizontal(classes="row"):
                    yield Label(title, classes="label")
                    yield Input(placeholder=placeholder, id=f"f-{name}")
            with Horizontal(id="actions"):
                yield Button("Собрать", variant="primary", id="btn-start")
                yield Button("Остановить", variant="error", id="btn-stop")
                yield Button("Выход", id="btn-quit")
        yield Static("Готов к работе", id="status", markup=False)
        yield Log(id="log", auto_scroll=True)
        yield Footer()

    def _read_filters(self) -> Filters:
        def value(name: str) -> str | None:
            raw = self.query_one(f"#f-{name}", Input).value.strip()
            return raw or None

        def number(name: str) -> int | None:
            raw = value(name)
            if raw is None:
                return None
            if not raw.isdigit():
                raise ValueError(f"в поле {name} ожидается число, получено {raw!r}")
            return int(raw)

        return Filters(
            geo=value("geo"),
            mark=value("mark"),
            model=value("model"),
            year=number("year"),
            price_to=number("price_to"),
        )

    def _write(self, message: str) -> None:
        # Разметка выключена намеренно: в сообщения попадают тексты ошибок, а
        # они содержат квадратные скобки. Разметка решила бы, что это команда,
        # и уронила бы экран ровно тогда, когда человеку нужнее всего видеть,
        # что именно сломалось.
        self.query_one("#log", Log).write_line(message)
        self.query_one("#status", Static).update(message)

    @on(Button.Pressed, "#btn-start")
    def action_start(self) -> None:
        """Запустить сбор."""
        if self._crawl_task is not None and not self._crawl_task.done():
            self._write("Сбор уже идёт. Сначала остановите его: кнопка «Стоп» или X.")
            return
        try:
            filters = self._read_filters()
        except ValueError as exc:
            self._write(f"Ошибка в полях: {exc}")
            return

        config = CrawlConfig(db_path=self._db, rps=self._rps, max_requests=100)
        self._crawl_task = asyncio.create_task(self._run(filters, config))
        self._write(f"Запущено: {filters.model_dump(exclude_none=True)}")

    async def _run(self, filters: Filters, config: CrawlConfig) -> None:
        try:
            with Store(config.db_path) as store:
                crawler = Crawler(self._client_factory(config), store, config)
                self._crawler = crawler
                if self._full:
                    progress = await crawler.crawl_full(filters)
                else:
                    progress = await crawler.crawl(filters)
                await crawler.aclose()
        except Cancelled:
            self._write("Остановлено человеком.")
        except CaptchaRequired as exc:
            # Раньше BlockedBySite, потому что проверка — его подкласс: иначе
            # человек увидел бы «сайт не отдал данные» вместо того, что делать.
            report(exc)
            self._write(explain())
        except BlockedBySite as exc:
            self._write(f"Сайт не отдал данные: {exc}")
        except ParserError as exc:
            self._write(f"Сбой: {exc}")
        except Exception as exc:
            self._write(f"Непредвиденная ошибка: {type(exc).__name__}: {exc}")
        else:
            self._write(
                f"Готово: страниц {progress.pages_done}, объявлений "
                f"{progress.offers_stored}, запросов {progress.requests}"
            )
        finally:
            self._crawler = None

    @on(Button.Pressed, "#btn-stop")
    def action_stop(self) -> None:
        """Остановить сбор."""
        if self._crawler is None:
            self._write("Сбор не идёт.")
            return
        self._crawler.cancel()
        self._write("Останавливаю…")

    @on(Button.Pressed, "#btn-quit")
    def action_quit_button(self) -> None:
        self.action_stop()
        self.exit()


def run(db: Path | None = None) -> None:
    """Запустить экран."""
    ParserApp(db).run()


__all__ = ["ClientFactory", "ParserApp", "default_client", "run"]
