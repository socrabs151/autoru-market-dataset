"""Построение адресов: фильтры превращаются в чистый путь."""

from __future__ import annotations

import pytest

from autoru_market_dataset.core.errors import ParserError
from autoru_market_dataset.core.models import Filters
from autoru_market_dataset.frontends.autoru.urls import (
    build_path,
    mark_model_from_url,
    offer_id_from_url,
)


def test_минимальный_путь() -> None:
    assert build_path(Filters()) == "/cars/used/"


def test_путь_с_гео_маркой_и_моделью() -> None:
    filters = Filters(geo="moskva", mark="mercedes", model="s_klasse")
    assert build_path(filters) == "/moskva/cars/mercedes/s_klasse/used/"


def test_путь_с_годом_и_ценой() -> None:
    filters = Filters(geo="moskva", mark="mercedes", year=2022, price_to=3_000_000)
    assert build_path(filters) == "/moskva/cars/mercedes/2022-year/used/do-3000000/"


def test_на_заказ_выражается_путём() -> None:
    filters = Filters(geo="moskva", mark="audi", model="a6", availability="order")
    assert build_path(filters).endswith("/used/for-order/")


def test_в_наличии_ничего_не_добавляет() -> None:
    filters = Filters(mark="audi", availability="in_stock")
    assert build_path(filters) == "/cars/audi/used/"


def test_остальные_фильтры_становятся_сегментами() -> None:
    filters = Filters(
        mark="kia",
        body="sedan",
        color="belyj",
        drive="forward_wheel",
        transmission="automatic",
        engine="benzin",
        seats=5,
        origin="korean",
    )
    path = build_path(filters)
    assert "body-sedan" in path
    assert "color-belyj" in path
    assert "drive-forward_wheel" in path
    assert "transmission-automatic" in path
    assert "engine-benzin" in path
    assert "seats-5" in path
    assert "origin-korean" in path


def test_в_адресе_нет_знака_вопроса() -> None:
    """Роботы сайта запрещают любые параметры в адресе."""
    filters = Filters(
        geo="moskva",
        mark="bmw",
        model="x5",
        year=2022,
        price_to=2_000_000,
        availability="order",
        body="allroad",
    )
    assert "?" not in build_path(filters)
    assert "=" not in build_path(filters)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("body", "sedan_xyz"),
        ("drive", "backward"),
        ("transmission", "robot_manual"),
        ("engine", "hydrogen"),
        ("origin", "martian"),
    ],
)
def test_неизвестное_значение_ловится_сразу(field: str, value: str) -> None:
    """Опечатка не должна молча давать адрес с нулём объявлений."""
    with pytest.raises(ParserError, match=field):
        build_path(Filters(**{field: value}))


@pytest.mark.parametrize("field", ["mark", "model", "geo"])
def test_слаг_с_недопустимыми_символами_отвергается(field: str) -> None:
    with pytest.raises(ParserError, match=field):
        build_path(Filters(**{field: "../../etc/passwd"}))


def test_идентификатор_берётся_из_адреса() -> None:
    url = "https://auto.ru/cars/used/sale/bmw/5er/1160014886-541f8e71/"
    assert offer_id_from_url(url) == "1160014886"


def test_без_идентификатора_возвращается_пусто() -> None:
    assert offer_id_from_url("https://auto.ru/moskva/cars/bmw/used/") is None


def test_марка_и_модель_берутся_из_адреса() -> None:
    url = "https://auto.ru/cars/used/sale/mercedes/s_klasse/1132598485-6179cd82/"
    assert mark_model_from_url(url) == ("mercedes", "s_klasse")
