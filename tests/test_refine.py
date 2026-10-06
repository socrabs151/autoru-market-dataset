"""Дробление выборки: части не пересекаются, обход не зацикливается."""

from __future__ import annotations

import pytest

from autoru_market_dataset.core.models import Filters, Offer
from autoru_market_dataset.frontends.autoru.refine import (
    DIMENSIONS,
    REJECTED,
    split,
    splittable,
)
from autoru_market_dataset.frontends.autoru.urls import build_url


def offers_with_years(years: list[int]) -> list[Offer]:
    return [
        Offer(
            offer_id=str(1000 + index),
            sale_id=str(1000 + index),
            url=f"https://auto.ru/cars/used/sale/a/b/{1000 + index}-aaaaaaaa/",
            production_year=year,
        )
        for index, year in enumerate(years)
    ]


def offers_with_generations(gens: list[str]) -> list[Offer]:
    return [
        Offer(
            offer_id=str(1000 + index),
            sale_id=str(1000 + index),
            url=f"https://auto.ru/cars/used/sale/a/b/{1000 + index}-aaaaaaaa/",
            production_year=2020,
            super_gen_id=gen,
        )
        for index, gen in enumerate(gens)
    ]


def observed(years: list[int], generations: list[str] | None = None) -> object:
    """Наблюдения для измерений: годы и, если заданы, поколения."""
    from autoru_market_dataset.frontends.autoru.refine import _Observed

    return _Observed(
        years=tuple(years),
        generations=tuple(generations or ()),
    )


def urls_of(filters_list: list[Filters]) -> set[str]:
    return {build_url(f) for f in filters_list}


# --- ключевое требование: части не пересекаются -----------------------------


def test_части_по_году_не_пересекаются() -> None:
    children, _dimension = split(
        Filters(geo="moskva", mark="bmw"), offers_with_years([2015, 2020, 2024])
    )
    urls = urls_of(children)
    assert len(urls) == len(children), "адреса частей должны быть различны"


def test_каждый_год_даёт_свой_сегмент() -> None:
    children, _dimension = split(Filters(geo="moskva"), offers_with_years([2019, 2020]))
    years = sorted(f.year for f in children if f.year is not None)
    assert len(years) == len(set(years))
    assert 2019 in years
    assert 2020 in years


def test_соседние_годы_не_сливаются_в_один_запрос() -> None:
    """Объединение было бы той же исходной выборкой, то есть без пользы."""
    children, _dimension = split(Filters(geo="moskva"), offers_with_years([2019, 2020]))
    parent = build_url(Filters(geo="moskva"))
    assert parent not in urls_of(children)


def test_если_год_уже_задан_идём_к_следующему_измерению() -> None:
    filters = Filters(geo="moskva", year=2020)
    children, _dimension = split(filters, offers_with_years([2020]))
    assert children
    assert all(f.year == 2020 for f in children)
    assert all(f.drive is not None for f in children), "ожидалось разбиение по приводу"


# --- забракованные измерения ------------------------------------------------
#
# Гипотеза «список значений делит выборку» проверялась на живых данных и не
# подтвердилась. Тесты ниже фиксируют именно вывод проверки, чтобы измерение
# не вернули по памяти.


def test_кузов_не_используется_для_дробления() -> None:
    """Значения кузова вложены: allroad содержит allroad_5_doors."""
    fields = {name for name, _dimension in DIMENSIONS}
    assert "body" not in fields
    assert "body" in REJECTED
    assert "вложен" in REJECTED["body"]


def test_двигатель_не_используется_для_дробления() -> None:
    """engine-turbo вложен в engine-benzin, а не параллелен."""
    fields = {name for name, _dimension in DIMENSIONS}
    assert "engine" not in fields
    assert "вложен" in REJECTED["engine"]


def test_измерения_для_дробления_только_проверенные() -> None:
    """Каждое измерение обязано быть проверено на непересекаемость."""
    used = {name for name, _dimension in DIMENSIONS}
    assert used == {"year", "generation", "drive", "transmission", "seats"}
    assert not used & set(REJECTED)


def test_разбиение_возвращает_имя_измерения() -> None:
    """Обходу нужно знать, какое измерение применилось, чтобы помечать его
    бесполезным, если число объявлений не изменилось."""
    children, dimension = split(Filters(geo="moskva"), offers_with_years([2020]))
    assert children
    assert dimension in {name for name, _ in DIMENSIONS}


def test_помеченное_измерение_больше_не_используется() -> None:
    """У X5 2023 все объявления на автомате: перебирать коробки бессмысленно."""
    filters = Filters(geo="moskva", year=2020)
    _children, dimension = split(filters, offers_with_years([2020]))
    assert dimension == "drive"

    skipped, next_dimension = split(
        filters, offers_with_years([2020]), exhausted=frozenset({dimension})
    )
    assert skipped, "нужно перейти к следующему измерению"
    assert next_dimension != dimension
    assert next_dimension == "transmission"


def test_дробление_не_использует_забракованные_значения() -> None:
    """Проверяем на год без ограничений: ни одна часть не должна быть
    построена через вложенные значения кузова или двигателя."""
    children, _dimension = split(Filters(geo="moskva"), offers_with_years([2020]))
    for child in children:
        url = build_url(child)
        assert "body-" not in url
        assert "engine-" not in url


# --- защита от зацикливания --------------------------------------------------


def test_разбиение_всегда_меняет_адрес() -> None:
    """Иначе обход повторил бы сам себя и не остановился."""
    filters = Filters(geo="moskva", mark="bmw", model="x5", body="sedan", drive="forward_wheel")
    children, _dimension = split(filters, offers_with_years([2020]))
    if children:
        assert build_url(filters) not in urls_of(children)


def test_полностью_заданный_фильтр_нечем_делить() -> None:
    filters = Filters(
        geo="moskva",
        mark="bmw",
        model="x5",
        year=2020,
        body="sedan",
        drive="forward_wheel",
        transmission="automatic",
        engine="benzin",
        origin="german",
        color="belyj",
        seats=5,
    )
    assert split(filters, offers_with_years([2020])) == ([], "")
    assert splittable(filters) is False


def test_вспомогательные_фильтры_не_мешают_дроблению() -> None:
    """Кузов и двигатель задавать можно: они не мешают дроблению по годам.

    Забракованы они только как измерения дробления, а не как фильтры.
    """
    filters = Filters(geo="moskva", body="sedan", engine="benzin")
    children, _dimension = split(filters, offers_with_years([2020]))
    assert children
    assert all(f.year is not None for f in children)


def test_без_наблюдений_год_не_используется_но_следующее_измерение_да() -> None:
    """Диапазон годов без наблюдений неизвестен, но кузов дробить можно.

    Обход переходит к следующему измерению, а не останавливается: полноту
    выборки терять нельзя из-за того, что первый запрос вернул мало данных.
    """
    children, _dimension = split(Filters(geo="moskva"), [])
    assert children, "ожидалось дробление по какому-то измерению"
    assert all(f.year is None for f in children)
    assert all(f.drive is not None for f in children), "ожидалось дробление по приводу"


# --- ограничения ------------------------------------------------------------


def test_глубина_дробления_ограничена() -> None:
    """На пределе глубины дробления прекращаются, а не идут бесконечно."""
    filters = Filters(geo="moskva")
    offers = offers_with_years([2019, 2020, 2021])
    assert split(filters, offers, depth=0, max_depth=2)
    assert split(filters, offers, depth=1, max_depth=1) == ([], "")


def test_глубина_в_пределе_даёт_пусто() -> None:
    assert split(Filters(geo="moskva"), offers_with_years([2020]), depth=2, max_depth=2) == ([], "")


@pytest.mark.parametrize(("name", "dimension"), DIMENSIONS)
def test_каждое_измерение_даёт_различные_адреса(name: str, dimension) -> None:
    base = Filters(geo="moskva", mark="bmw")
    children = dimension(base, observed([2015, 2025]))
    urls = urls_of(children)
    assert len(urls) == len(children), f"измерение {name} дало повторяющиеся адреса"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transmission", "automatic"),
        ("drive", "forward_wheel"),
        ("seats", 5),
        ("super_gen", "21307931"),
    ],
)
def test_измерение_не_снимает_уже_заданное_значение(field: str, value) -> None:
    """Снятие ограничения вернуло бы к родителю и зациклило обход."""
    for _name, dimension in DIMENSIONS:
        children = dimension(Filters(**{field: value}), observed([2020]))
        if children:
            assert all(getattr(child, field) is not None for child in children)


# --- границы года -----------------------------------------------------------


def test_год_не_уходит_за_разумные_пределы() -> None:
    children, _dimension = split(Filters(geo="moskva"), offers_with_years([2015, 2015]))
    years = [f.year for f in children if f.year is not None]
    assert years, "ожидались годы"
    assert min(years) >= 1990
    assert max(years) <= 2027


def test_старые_машины_не_теряются_на_пустом_годе() -> None:
    """Запас по краям нужен: первые объявления выдачи не самые старые."""
    children, _dimension = split(Filters(geo="moskva"), offers_with_years([2020]))
    years = [f.year for f in children if f.year is not None]
    assert min(years) <= 2016, "ожидался запас вниз от первого увиденного года"
