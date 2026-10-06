"""HTTP-сервис: постановка прогонов, прогресс, отмена, данные.

Тесты не ходят в сеть: клиент подменяется заглушкой, которая отдаёт
заранее заданные страницы.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoru_parser.core.errors import BlockedBySite, CaptchaRequired
from autoru_parser.core.models import Filters, Offer, Page, PricePoint
from autoru_parser.core.normalize import compute_payload_hash
from autoru_parser.frontends.autoru.urls import build_url
from autoru_parser.services.crawl import CrawlConfig, Crawler
from autoru_parser.storage.duck import Store
from autoru_parser.transport.api import create_app

PAGE_SIZE = 37


def make_offer(index: int, price: int) -> Offer:
    offer_id = str(1000 + index)
    offer = Offer(
        offer_id=offer_id,
        sale_id=f"{offer_id}-aaaaaaaa",
        url=f"https://auto.ru/cars/used/sale/a/b/{offer_id}-aaaaaaaa/",
        mark="BMW",
        model="X5",
        price=price,
        mileage=10_000 * index,
        production_year=2020 + (index % 5),
        seller_type="PRIVATE" if index % 2 else "COMMERCIAL",
        price_history=[PricePoint(price=price, observed_at="2026-10-06T12:00:00+00:00")],  # type: ignore[arg-type]
        observed_at="2026-10-06T12:00:00+00:00",  # type: ignore[arg-type]
    )
    offer.payload_hash = compute_payload_hash(offer)
    return offer


class FakeClient:
    """Клиент, отдающий заранее приготовленные страницы."""

    def __init__(self, pages: dict[str, Page] | None = None, error: Exception | None = None):
        self.pages = pages or {}
        self._error = error
        self.calls: list[str] = []
        self.closed = False

    def url_for(self, filters: Filters) -> str:
        return build_url(filters)

    async def fetch(self, url: str) -> Page:
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        page = self.pages.get(url)
        if page is None:
            from autoru_parser.core.errors import NoResults

            raise NoResults(f"нет данных: {url}")
        return page

    async def aclose(self) -> None:
        """Заглушка отмечает закрытие: настоящий клиент закрывает соединения."""
        self.closed = True


def small_page(url: str, total: int = 3) -> Page:
    return Page(
        url=url,
        offers=[make_offer(i, 1_000_000 + i) for i in range(total)],
        total_offers=total,
        total_pages=1,
        page_size=PAGE_SIZE,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "api.duckdb"


def make_factory(client: FakeClient):
    def factory(store: Store, run, config: CrawlConfig) -> Crawler:
        # Клиент подменяется, а всё остальное — настоящее: тест проверяет
        # сервис и оркестрацию, а не выдуманную заглушку целиком.
        return Crawler(client, store, config)  # type: ignore[arg-type]

    return factory


def app_with(db_path: Path, client: FakeClient):
    return create_app(db_path, make_factory(client))


FINAL_STATES = ("done", "failed", "cancelled", "blocked")


def start_run(client_obj: TestClient, payload: dict) -> str:
    """Поставить прогон и вернуть его идентификатор."""
    created = client_obj.post("/runs", json=payload)
    assert created.status_code == 202, created.text
    return str(created.json()["id"])


def wait_run(client_obj: TestClient, run_id: str, timeout: float = 60.0) -> dict:
    """Дождаться конца прогона, опрашивая состояние.

    Опрос идёт через сам API, а не через внутренности менеджера: ``TestClient``
    живёт в своём потоке со своим циклом событий, иawait чужой задачи из
    тестового потока приводит к ошибке. Побочная выгода — проверяется ровно то,
    чем пользуется внешняя система.
    """
    deadline = time.monotonic() + timeout
    while True:
        body = client_obj.get(f"/runs/{run_id}").json()
        if body["state"] in FINAL_STATES or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


# --- служебное ---------------------------------------------------------------


def test_служба_отвечает(db_path: Path) -> None:
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.get("/health").json() == {"status": "ok", "runs": 0}


def test_схема_описания_генерируется(db_path: Path) -> None:
    with TestClient(app_with(db_path, FakeClient())) as c:
        paths = c.get("/openapi.json").json()["paths"]
    for expected in ("/runs", "/offers", "/summary", "/changes", "/check-hash"):
        assert expected in paths


# --- постановка прогона -----------------------------------------------------


def test_прогон_ставится_и_выполняется(db_path: Path) -> None:
    client = FakeClient()
    filters = Filters(geo="moskva", mark="bmw", model="x5")
    client.pages[build_url(filters)] = small_page(build_url(filters), 3)

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": filters.model_dump()})
        run = wait_run(c, run_id)

    assert run["state"] == "done", run
    assert run["progress"]["offers_seen"] == 3
    assert run["progress"]["requests"] == 1
    assert client.calls == [build_url(filters)], "ожидался ровно один запрос"


def test_неизвестный_прогон_даёт_404(db_path: Path) -> None:
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.get("/runs/нет-такого").status_code == 404


def test_плохой_фильтр_отвергается(db_path: Path) -> None:
    """Неверное значение фильтра отвергается на входе, а не в ходе сбора."""
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.post("/runs", json={"filters": {"year": "не число"}}).status_code == 422
        assert c.post("/runs", json={"filters": {"mark": "bmw"}, "rps": 0}).status_code == 422
        assert (
            c.post("/runs", json={"filters": {"mark": "bmw"}, "max_depth": 99}).status_code == 422
        )


# --- отмена ------------------------------------------------------------------


def test_отмена_останавливает_сбор(db_path: Path) -> None:
    """Долгий обход останавливается по запросу и не идёт до конца."""
    client = FakeClient()
    filters = Filters(geo="moskva", mark="bmw", model="x5", year=2020)
    client.pages[build_url(filters)] = small_page(build_url(filters), total=200)
    for year in range(1990, 2027):
        child = Filters(geo="moskva", mark="bmw", model="x5", year=year)
        client.pages.setdefault(build_url(child), small_page(build_url(child), 1))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": filters.model_dump(), "full": True})
        assert c.post(f"/runs/{run_id}/cancel").status_code == 200
        run = wait_run(c, run_id)

    assert run["state"] == "cancelled", run
    assert run["finished_at"] is not None, "отмена должна фиксировать время конца"


def test_отменённый_прогон_сохраняет_уже_собранное(db_path: Path) -> None:
    """Данные, собранные до отмены, не пропадают: они в базе."""
    client = FakeClient()
    filters = Filters(geo="moskva", mark="bmw", model="x5", year=2020)
    client.pages[build_url(filters)] = small_page(build_url(filters), total=200)
    for year in range(1990, 2027):
        child = Filters(geo="moskva", mark="bmw", model="x5", year=year)
        client.pages.setdefault(build_url(child), small_page(build_url(child), 1))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": filters.model_dump(), "full": True})
        c.post(f"/runs/{run_id}/cancel")
        wait_run(c, run_id)
        items = c.get("/offers").json()["items"]

    assert items, "собранное до отмены должно остаться в базе"


def test_отмена_неизвестного_прогона_даёт_404(db_path: Path) -> None:
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.post("/runs/нет-такого/cancel").status_code == 404


def test_повторная_отмена_не_ломает(db_path: Path) -> None:
    """Повторная отмена уже завершённого прогона — не ошибка."""
    client = FakeClient()
    filters = Filters(mark="bmw")
    client.pages[build_url(filters)] = small_page(build_url(filters))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": filters.model_dump()})
        wait_run(c, run_id)
        assert c.post(f"/runs/{run_id}/cancel").json()["state"] == "done"
        assert c.post(f"/runs/{run_id}/cancel").status_code == 200


# --- состояния ---------------------------------------------------------------


def test_проверка_даёт_отдельное_состояние(db_path: Path) -> None:
    """Запрос проверки — не успех и не сбой, а отдельное состояние."""
    client = FakeClient(error=CaptchaRequired("нужна проверка"))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": Filters(mark="bmw").model_dump()})
        run = wait_run(c, run_id)

    assert run["state"] == "blocked", run
    assert "проверка" in run["error"]


def test_отказ_сайта_это_сбой(db_path: Path) -> None:
    client = FakeClient(error=BlockedBySite("403"))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": Filters(mark="bmw").model_dump()})
        run = wait_run(c, run_id)

    assert run["state"] == "failed", run
    assert "403" in run["error"]


def test_пустой_результат_это_успех(db_path: Path) -> None:
    """Ноль объявлений по фильтру — результат, а не сбой."""
    client = FakeClient()

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": Filters(mark="bmw").model_dump()})
        run = wait_run(c, run_id)

    assert run["state"] == "done", run


def test_список_прогонов_накапливается(db_path: Path) -> None:
    """Внешняя система видит историю постановок, а не только текущий прогон."""
    client = FakeClient()
    filters = Filters(mark="bmw")
    client.pages[build_url(filters)] = small_page(build_url(filters))

    with TestClient(app_with(db_path, client)) as c:
        first = start_run(c, {"filters": filters.model_dump()})
        wait_run(c, first)
        second = start_run(c, {"filters": filters.model_dump()})
        wait_run(c, second)
        runs = c.get("/runs").json()

    assert len(runs) == 2
    assert {r["id"] for r in runs} == {first, second}


# --- данные ------------------------------------------------------------------


def test_соединения_закрываются_после_прогона(db_path: Path) -> None:
    """Клиент закрывается: иначе соединения висели бы до конца процесса."""
    client = FakeClient()
    filters = Filters(mark="bmw")
    client.pages[build_url(filters)] = small_page(build_url(filters))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": filters.model_dump()})
        wait_run(c, run_id)

    assert client.closed, "клиент должен закрываться по завершении прогона"


def test_соединения_закрываются_даже_при_сбое(db_path: Path) -> None:
    """Ошибка сбора не должна оставлять соединение открытым."""
    client = FakeClient(error=BlockedBySite("403"))

    with TestClient(app_with(db_path, client)) as c:
        run_id = start_run(c, {"filters": Filters(mark="bmw").model_dump()})
        wait_run(c, run_id)

    assert client.closed


def test_список_объявлений(db_path: Path) -> None:
    with Store(db_path) as store:
        store.save_many([make_offer(i, 1_000_000 + i * 10) for i in range(5)])
    with TestClient(app_with(db_path, FakeClient())) as c:
        response = c.get("/offers", params={"limit": 3})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    assert items[0]["price"] > items[1]["price"], "ожидалась сортировка по цене"


def test_локальный_фильтр_по_пробегу_в_api(db_path: Path) -> None:
    with Store(db_path) as store:
        store.save_many([make_offer(i, 1_000_000) for i in range(4)])
    with TestClient(app_with(db_path, FakeClient())) as c:
        items = c.get("/offers", params={"mileage_to": 1}).json()["items"]
    assert len(items) == 1, "ожидалось только первое объявление"


def test_в_api_нет_персональных_данных(db_path: Path) -> None:
    """Проверка на выходе из сервиса: то же требование, что и для выгрузки."""
    with Store(db_path) as store:
        store.save_many([make_offer(i, 1_000_000) for i in range(2)])
    with TestClient(app_with(db_path, FakeClient())) as c:
        items = c.get("/offers").json()["items"]
    forbidden = {"name", "phone", "address", "user_ref", "salon", "vin", "coord"}
    for item in items:
        assert not set(item) & forbidden


def test_история_цен_одного_объявления(db_path: Path) -> None:
    with Store(db_path) as store:
        store.save(make_offer(1, 1_000_000))
        offer = make_offer(1, 900_000)
        store.save(offer)
    with TestClient(app_with(db_path, FakeClient())) as c:
        response = c.get("/offers/1001/prices")
    assert response.status_code == 200
    body = response.json()
    assert len(body["history"]) >= 1
    assert body["mark"] == "BMW"


def test_неизвестное_объявление_даёт_404(db_path: Path) -> None:
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.get("/offers/нет/prices").status_code == 404


def test_сводка_и_изменения(db_path: Path) -> None:
    with Store(db_path) as store:
        store.save_many([make_offer(i, 1_000_000 + i) for i in range(3)])
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.get("/summary").status_code == 200
        assert c.get("/changes").status_code == 200
        assert "suspicious" in c.get("/check-hash").json()


def test_лимит_страницы_ограничен(db_path: Path) -> None:
    with TestClient(app_with(db_path, FakeClient())) as c:
        assert c.get("/offers", params={"limit": 0}).status_code == 422
        assert c.get("/offers", params={"limit": 10_000}).status_code == 422


def test_путь_к_базе_доходит_до_хранилища(db_path: Path) -> None:
    """Сервис обязан работать с той базой, которую ему передали."""
    app = create_app(db_path, make_factory(FakeClient()))
    assert app.state.manager.db_path == db_path
