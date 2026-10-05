"""Построение адресов auto.ru из фильтра.

Адреса строятся **только путём, без query-строки**: в ``robots.txt`` для
``User-agent: *`` стоит ``Disallow: /*?``, то есть любой параметр в адресе
запрещён. Поэтому пагинации ``?page=N`` не существует, а полнота выборки
достигается сужением фильтра (решение 10).

Словари значений взяты из sitemap: только эти слаги сервер понимает.
Проверка по словарям нужна, чтобы опечатка в фильтре не превращалась в
адрес, который молча вернёт ноль объявлений.
"""

from __future__ import annotations

import re

from autoru_parser.core.errors import ParserError
from autoru_parser.core.models import Filters

BASE_URL = "https://auto.ru"

_SLUG_RE = re.compile(r"^[a-z0-9_]+$")

BODY_TYPES: frozenset[str] = frozenset(
    {
        "sedan",
        "hatchback",
        "hatchback_3_doors",
        "hatchback_5_doors",
        "allroad",
        "allroad_3_doors",
        "allroad_5_doors",
        "wagon",
        "minivan",
        "liftback",
        "coupe",
        "cabrio",
        "pickup",
        "van",
    }
)
DRIVES: frozenset[str] = frozenset({"forward_wheel", "4x4_wheel", "rear_wheel"})
TRANSMISSIONS: frozenset[str] = frozenset({"automatic", "mechanical", "robot", "variator"})
ENGINES: frozenset[str] = frozenset({"benzin", "turbo", "dizel", "gibrid", "electro", "lpg"})
ORIGINS: frozenset[str] = frozenset(
    {
        "domestic",
        "foreign",
        "german",
        "european",
        "japanese",
        "korean",
        "chinese",
        "american",
    }
)
CATEGORIES: frozenset[str] = frozenset({"cars", "moto", "lcv", "trucks"})
SECTIONS: frozenset[str] = frozenset({"used", "new", "all"})
AVAILABILITIES: frozenset[str] = frozenset({"any", "in_stock", "order"})


def _check_slug(value: str, field: str) -> str:
    if not _SLUG_RE.match(value):
        raise ParserError(
            f"{field}: ожидается слаг латиницей в нижнем регистре, получено {value!r}"
        )
    return value


def _check_choice(value: str | None, allowed: frozenset[str], field: str) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ParserError(
            f"{field}: значение {value!r} сервер не понимает. "
            f"Допустимые: {', '.join(sorted(allowed))}"
        )
    return value


def build_path(filters: Filters) -> str:
    """Собрать путь выборки.

    Порядок сегментов повторяет те, что встречаются в sitemap: сначала
    гео и категория, затем марка и модель, год, состояние, цена, остальные
    фильтры.

    :raises ParserError: значение фильтра неизвестно серверу.
    """
    parts: list[str] = []

    category = _check_choice(filters.category, CATEGORIES, "category") or "cars"
    section = _check_choice(filters.section, SECTIONS, "section") or "used"
    availability = _check_choice(filters.availability, AVAILABILITIES, "availability") or "any"

    if filters.geo:
        parts.append(_check_slug(filters.geo, "geo"))
    parts.append(category)
    if filters.mark:
        parts.append(_check_slug(filters.mark, "mark"))
    if filters.model:
        parts.append(_check_slug(filters.model, "model"))
    if filters.year is not None:
        parts.append(f"{filters.year}-year")
    parts.append(section)
    if filters.price_to is not None:
        parts.append(f"do-{filters.price_to}")
    if availability == "order":
        parts.append("for-order")

    optional: list[tuple[str, str | None]] = [
        ("body", _check_choice(filters.body, BODY_TYPES, "body")),
        ("color", _check_slug(filters.color, "color") if filters.color else None),
        ("drive", _check_choice(filters.drive, DRIVES, "drive")),
        ("transmission", _check_choice(filters.transmission, TRANSMISSIONS, "transmission")),
        ("engine", _check_choice(filters.engine, ENGINES, "engine")),
        ("origin", _check_choice(filters.origin, ORIGINS, "origin")),
    ]
    for field, value in optional:
        if value:
            parts.append(f"{field}-{value}")
    if filters.seats is not None:
        parts.append(f"seats-{filters.seats}")

    return "/" + "/".join(parts) + "/"


def build_url(filters: Filters) -> str:
    """Полный адрес страницы выборки."""
    return BASE_URL + build_path(filters)


def offer_id_from_url(url: str) -> str | None:
    """Достать числовой идентификатор объявления из его адреса.

    В адресе объявления есть пара ``{id}-{hash}``. Идентификатор устойчив,
    а хеш может меняться вместе с ценой, поэтому ключом служит только ``id``.
    """
    match = re.search(r"/([0-9]{6,})-[0-9a-f]{6,}/", url)
    return match.group(1) if match else None


def mark_model_from_url(url: str) -> tuple[str | None, str | None]:
    """Достать марку и модель из адреса объявления.

    Разбирает путь вида ``/{geo}/cars/{mark}/{model}/{id}-{hash}/``.
    Надёжнее полей в ответе: в них названия приходят в разной форме.
    """
    tail = url.split("auto.ru/", 1)[-1].rstrip("/")
    parts = tail.split("/")
    if "sale" in parts:
        idx = parts.index("sale")
        after = parts[idx + 1 :]
    else:
        after = parts
    mark = after[0] if len(after) >= 2 and re.match(r"^[a-z0-9_]+$", after[0]) else None
    model = after[1] if len(after) >= 3 and re.match(r"^[a-z0-9_]+$", after[1]) else None
    return mark, model


__all__ = [
    "AVAILABILITIES",
    "BASE_URL",
    "BODY_TYPES",
    "CATEGORIES",
    "DRIVES",
    "ENGINES",
    "ORIGINS",
    "SECTIONS",
    "TRANSMISSIONS",
    "build_path",
    "build_url",
    "mark_model_from_url",
    "offer_id_from_url",
]
