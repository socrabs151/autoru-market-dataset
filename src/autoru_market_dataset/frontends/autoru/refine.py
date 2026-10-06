"""Измерения для дробления выборки — только проверенные.

Обычная гипотеза «список значений делит выборку на непересекающиеся части»
оказалась неверной, и проверка на живых данных это показала. Замерено на
X5 2023 года в Москве, 50 объявлений:

===================  ========  ==========================================
измерение            сумма     вывод
===================  ========  ==========================================
``body``             100       **не делит**: ``allroad`` и
                               ``allroad_5_doors`` возвращают одно и то же
``engine``           100       **не делит**: ``turbo`` вложен в ``benzin``
``transmission``     50        делит
``drive``            50        делит
``seats``            50        делит
``origin``           0         **не годится**: на латинице всегда пусто
===================  ========  ==========================================

Причины две, и обе важны.

1. **Часть значений вложена, а не параллельна.** ``body-allroad`` — это группа,
   внутрь которой входит ``body-allroad_5_doors``. ``engine-turbo`` — не
   отдельный тип топлива, а признак наддува поверх ``benzin``. Если брать такие
   значения как равноправные части, части перекрываются: одна машина попадёт в
   две выборки, обход вернёт больше объявлений, чем их есть, и никто этого не
   заметит.
2. **Сайт понимает не те слаги, что мы думаем.** Наш ``origin-german`` молча
   даёт ноль: похоже, ожидается составной код вроде ``vendor-german``, который
   встречается в sitemap, а не ``origin-``. Пустой результат отличить от
   «фильтр не поддерживается» по одному ответу нельзя.

Поэтому список измерений ниже короткий и проверен: год, коробка, привод, число
мест. Остальные помечены как непригодные, с указанием причины, чтобы их не
попробовали снова молча.

Ещё одно следствие проверки: молчаливый ноль опасен. Измерение, которое даёт
ноль на всех значениях, ничего не сообщает о себе. Поэтому в отчёте о полноте
пустые части считаются отдельно, а «разделить нечем» — это явное сообщение,
а не тихий недобор.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from autoru_market_dataset.core.models import Filters, Offer
from autoru_market_dataset.frontends.autoru.urls import (
    DRIVES,
    TRANSMISSIONS,
    build_url,
)

#: Горизонт лет. Новые машины появляются с опережением, поэтому верхняя
#: граница на год вперёд.
FIRST_YEAR = 1990
YEAR_MARGIN = 4

#: Число мест: замерено, значения делят выборку.
SEAT_VALUES: tuple[int, ...] = (5, 7)


@dataclass(frozen=True)
class _Observed:
    """Что видно из уже полученных объявлений.

    Диапазон годов нужен, чтобы построить разбиение по годам. Набор
    поколений нужен, чтобы разбить по ним: идентификаторы поколений известны
    только из самих объявлений, угадать их нельзя.
    """

    years: tuple[int, ...] = ()
    generations: tuple[str, ...] = ()


Dimension = Callable[[Filters, _Observed], list[Filters]]


def _years(filters: Filters, seen: _Observed) -> list[Filters]:
    """Разбить по году.

    Год — единственное измерение, где множество заведомо полное: это просто
    целые числа в известном диапазоне. Диапазон берётся вокруг того, что видно
    в уже полученных объявлениях, с запасом по краям: самые старые машины
    попадают на последние страницы выдачи, и в первых сорока их может не быть.
    """
    if filters.year is not None or not seen.years:
        return []
    low = max(FIRST_YEAR, min(seen.years) - YEAR_MARGIN)
    high = min(datetime.now().year + 1, max(seen.years) + YEAR_MARGIN)
    if high < low:
        return []
    return [filters.model_copy(update={"year": year}) for year in range(low, high + 1)]


def _categorical(field: str, values: tuple[str, ...]) -> Dimension:
    """Измерение из проверенного списка значений.

    Если значение уже задано, разбить по нему нельзя: единственный вариант —
    снять ограничение, а это вернёт к родителю и зациклит обход.
    """

    def dimension(filters: Filters, seen: _Observed) -> list[Filters]:
        if getattr(filters, field) is not None:
            return []
        return [filters.model_copy(update={field: value}) for value in values]

    return dimension


#: Признаки, которые не могут сочетаться между собой.
#:
#: Коробка и привод описывают одно и то же разными словами: ``ALL_WHEEL_DRIVE``
#: в поле привода и ``AUTOMATIC`` в поле коробки часто идут в паре на
#: полноприводных автоматах. Если применять их вместе, получаются адреса,
#: которых не существует: сайт отвечает на них пустотой, а обход тратит на
#: них запросы.
#:
#: Замерено на X5 2023: ``drive-4x4_wheel/transmission-automatic`` дал пустой
#: ответ, хотя отдельно и привод, и коробка давали по 50. Именно поэтому
#: измерения коробки и привода не применяются одновременно: при заданном
#: приводе коробка не перебирается, и наоборот.
def _by_transmission(filters: Filters, seen: _Observed) -> list[Filters]:
    if filters.transmission is not None or filters.drive is not None:
        return []
    return [filters.model_copy(update={"transmission": value}) for value in sorted(TRANSMISSIONS)]


def _by_drive(filters: Filters, seen: _Observed) -> list[Filters]:
    if filters.drive is not None or filters.transmission is not None:
        return []
    return [filters.model_copy(update={"drive": value}) for value in sorted(DRIVES)]


def _by_generation(filters: Filters, seen: _Observed) -> list[Filters]:
    """Разбить по поколению.

    Поколение — числовой идентификатор, и сайт принимает его прямо в пути.
    Значения берутся из уже полученных объявлений: поле ``super_gen.id``
    есть у каждого. Измерение непересекающееся: объявление принадлежит ровно
    одному поколению.
    """
    if filters.super_gen is not None:
        return []
    return [filters.model_copy(update={"super_gen": gen}) for gen in seen.generations]


def _seats(filters: Filters, seen: _Observed) -> list[Filters]:
    if filters.seats is not None:
        return []
    return [filters.model_copy(update={"seats": value}) for value in SEAT_VALUES]


#: Порядок важен: разбиваемся по одной оси за раз, и ось выбирается так, чтобы
#: поделить выборку на части близкого размера. Год режет сильнее всего.
DIMENSIONS: tuple[tuple[str, Dimension], ...] = (
    ("year", _years),
    ("generation", _by_generation),
    ("drive", _by_drive),
    ("transmission", _by_transmission),
    ("seats", _seats),
)

#: Измерения, забракованные проверкой на живых данных. Оставлены здесь
#: с указанием причины, чтобы их не вернули по памяти.
REJECTED: dict[str, str] = {
    "body": (
        "значения вложены: body-allroad содержит body-allroad_5_doors, "
        "сумма частей вдвое больше родителя"
    ),
    "engine": (
        "engine-turbo вложен в engine-benzin, а не параллелен ему; "
        "сумма частей вдвое больше родителя"
    ),
    "origin": "наш слаг всегда даёт ноль; в sitemap встречается vendor-german",
    "color": "значения не проверялись на непересекаемость",
}


def split(
    filters: Filters,
    offers: list[Offer],
    depth: int = 0,
    max_depth: int = 2,
    exhausted: frozenset[str] = frozenset(),
) -> tuple[list[Filters], str]:
    """Разбить запрос на части.

    Возвращает части и имя измерения, по которому они получены. Имя нужно
    обходу: увидев, что добавление этого фильтра не изменило число
    объявлений, он помечает измерение как бесполезное здесь и больше не
    перебирает его значения. У BMW X5 2023 все 50 объявлений с автоматом,
    поэтому ``transmission-automatic`` вернул те же 50 — и перебирать
    оставшиеся коробки бессмысленно, размер не изменится.

    Пустой список означает «делить нечем».

    :param offers: объявления, уже полученные для этого запроса: по ним
        понимается диапазон годов.
    :param max_depth: насколько глубоко можно дробить. Трёх измерений
        хватает для типичных запросов; дальше дробление даёт всё меньше
        отдачи на каждый запрос.
    """
    if depth >= max_depth:
        return [], ""
    observed = _Observed(
        years=tuple(o.production_year for o in offers if o.production_year),
        generations=tuple(dict.fromkeys(o.super_gen_id for o in offers if o.super_gen_id)),
    )
    for name, dimension in DIMENSIONS:
        if name in exhausted:
            continue
        children = _without_parent(dimension(filters, observed), filters)
        if children:
            return children, name
    return [], ""


def _without_parent(children: list[Filters], parent: Filters) -> list[Filters]:
    """Убрать вариант, не отличающийся от родителя.

    Иначе получился бы бесконечный обход: запрос снял бы ограничение и
    вернулся к самому себе.
    """
    parent_url = build_url(parent)
    unique: dict[str, Filters] = {}
    for child in children:
        url = build_url(child)
        if url == parent_url:
            continue
        unique.setdefault(url, child)
    return list(unique.values())


def splittable(filters: Filters) -> bool:
    """Есть ли ещё измерение, по которому можно разбить."""
    return any(dimension(filters, _Observed()) for _name, dimension in DIMENSIONS)


__all__ = [
    "DIMENSIONS",
    "REJECTED",
    "SEAT_VALUES",
    "split",
    "splittable",
]
