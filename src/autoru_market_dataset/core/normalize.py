"""Приведение ответа auto.ru к нормализованному объявлению.

Собираем по белому списку: перечислено разрешённое, остальное не копируется
вообще. Персональные данные поэтому не попадают в хранилище, а не вычищаются
на выходе: в ответе их много, и новые поля у сайта появляются постоянно
(решение 4).

Сырой ответ целиком не сохраняется (решение 3): здесь ровно тот набор
колонок, который нужен для анализа.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from autoru_market_dataset.core.models import Offer, PricePoint, now_utc
from autoru_market_dataset.frontends.autoru.urls import mark_model_from_url


def _get(node: Any, *path: str, default: Any = None) -> Any:
    """Достать вложенное значение. Отсутствие и ``None`` не путаем."""
    current = node
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current if current is not None else default


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        number = _as_int(item)
        if number is not None:
            out.append(number)
    return out


def _ms_to_dt(value: Any) -> datetime | None:
    """Единицы времени на сайте — миллисекунды epoch, иногда строкой."""
    number = _as_int(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _price_history(raw: Any) -> list[PricePoint]:
    if not isinstance(raw, list):
        return []
    points: list[PricePoint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        price = _as_int(item.get("RUR")) or _as_int(item.get("price"))
        moment = _ms_to_dt(item.get("create_timestamp"))
        if price is None or moment is None:
            continue
        points.append(PricePoint(price=price, observed_at=moment))
    points.sort(key=lambda p: p.observed_at)
    return points


#: Поля, которые не влияют на запись в истории.
#:
#: ``tags`` — служебные метки сайта, а не свойства автомобиля, и они
#: мигают: при двух прогонах подряд у части объявлений появлялся и пропадал
#: тег ``type_top_crown``, при неизменных цене, пробеге и датах. Если бы
#: теги входили в хэш, каждый прогон создавал бы записи в истории без
#: всякого смысла, и таблица наполнилась бы мусором.
#:
#: Время наблюдения исключено по другой причине: оно всегда новое.
_HASH_IGNORED: frozenset[str] = frozenset({"observed_at", "payload_hash", "tags"})


def compute_payload_hash(offer: Offer) -> str:
    """Хэш содержания объявления — основа для записи в историю.

    Порядок элементов в списках не учитывается: сайт отдаёт их в
    произвольном порядке, и при сравнении списков он менялся от прогона к
    прогону при полностью одинаковом содержании.

    Это найдено на живых данных, а не придумано: второй прогон подряд
    добавлял записи в историю, хотя содержание объявлений не менялось.

    ВИстория должна отвечать на вопрос «что изменилось в объявлении», а не
    «что изменилось на странице сайта». Всё, что не является свойством
    объявления, из хэша исключено.
    """
    data = offer.model_dump(mode="json", exclude=set(_HASH_IGNORED))
    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, list):
            items = [item for item in value if not isinstance(item, (list, dict))]
            if len(items) == len(value):
                normalized[key] = sorted(items, key=str)
                continue
        normalized[key] = value
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_offer(raw: dict[str, Any]) -> Offer:
    """Собрать ``Offer`` из сырого объекта ответа.

    :raises ValueError: у объекта нет идентификатора — нормализовать нечего.
    """
    offer_id = _as_str(raw.get("id"))
    if not offer_id:
        raise ValueError("в объявлении нет поля id")

    url = _as_str(raw.get("url")) or ""
    # Слаги из адреса — надёжный запасной вариант: в самом ответе названия
    # приходят в человеческом виде, который меняется вместе с вёрсткой.
    url_mark, url_model = mark_model_from_url(url)

    vehicle = raw.get("vehicle_info")
    config = _get(vehicle, "configuration", default={})
    # Двигатель и коробка в листинге лежат не на верхнем уровне vehicle_info,
    # а в tech_param. Это выяснено по реальному ответу, а не предположено.
    tech = _get(vehicle, "tech_param", default={})
    super_gen = _get(vehicle, "super_gen", default={})
    seller_loc = _get(raw, "seller", "location", default={})

    photos = _get(raw, "state", "image_urls", default=[])

    offer = Offer(
        offer_id=offer_id,
        sale_id=_as_str(raw.get("saleId")) or _as_str(raw.get("idHash")) or offer_id,
        url=url,
        sale_hash=_as_str(raw.get("hash")),
        category=_as_str(raw.get("category")),
        section=_as_str(raw.get("section")),
        sub_category=_as_str(raw.get("sub_category")),
        title=_as_str(raw.get("title")),
        mark=_as_str(_get(vehicle, "mark_info", "code")) or url_mark,
        mark_name=_as_str(_get(vehicle, "mark_info", "ru_name")),
        model=_as_str(_get(vehicle, "model_info", "code"))
        or _as_str(_get(vehicle, "model_info", "nameplate"))
        or url_model,
        model_name=_as_str(_get(vehicle, "model_info", "ru_name")),
        super_gen_id=_as_str(_get(super_gen, "id")),
        super_gen_name=_as_str(_get(super_gen, "ru_name")),
        super_gen_year_from=_as_int(_get(super_gen, "year_from")),
        super_gen_year_to=_as_int(_get(super_gen, "year_to")),
        origin=_as_str(_get(vehicle, "vendor")),
        body_type=_as_str(_get(config, "body_type")) or _as_str(_get(config, "body_type_group")),
        body_type_human=_as_str(_get(config, "human_name")),
        auto_class=_as_str(_get(config, "auto_class")),
        doors_count=_as_int(_get(config, "doors_count")),
        seats=_as_int_list(_get(config, "seats")),
        engine=_as_str(_get(vehicle, "engine")) or _as_str(_get(tech, "human_name")),
        engine_type=_as_str(_get(tech, "engine_type")),
        displacement=_as_int(_get(tech, "displacement")),
        power_hp=_as_int(_get(tech, "power")),
        power_kwt=_as_int(_get(tech, "power_kvt")),
        acceleration_to=_as_float(_get(tech, "acceleration")),
        nameplate=_as_str(_get(tech, "nameplate")),
        # В tech_param привод называется gear_type, а коробка — transmission.
        # Путать их нельзя: в списке X5 почти все полноприводные, и при
        # перепутанных полях привод выглядел бы как коробка.
        transmission=_as_str(_get(vehicle, "transmission")) or _as_str(_get(tech, "transmission")),
        drive=_as_str(_get(vehicle, "drive")) or _as_str(_get(tech, "gear_type")),
        color=_as_str(_get(vehicle, "color")),
        color_hex=_as_str(raw.get("color_hex")),
        tech_param_id=_as_str(_get(vehicle, "tech_param_id")),
        production_year=_as_int(_get(raw, "documents", "year")),
        mileage=_as_int(_get(raw, "state", "mileage")),
        condition=_as_str(_get(raw, "state", "condition")),
        status=_as_str(raw.get("status")),
        availability=_as_str(raw.get("availability")),
        price=_as_int(_get(raw, "price_info", "price")) or _as_int(_get(raw, "price_info", "RUR")),
        price_eur=_as_int(_get(raw, "price_info", "EUR")),
        price_usd=_as_int(_get(raw, "price_info", "USD")),
        currency=_as_str(_get(raw, "price_info", "currency")),
        with_nds=_as_bool(_get(raw, "price_info", "with_nds")),
        owners_number=_as_int(_get(raw, "documents", "owners_number")),
        pts=_as_str(_get(raw, "documents", "pts")),
        pts_original=_as_bool(_get(raw, "documents", "pts_original")),
        custom_cleared=_as_bool(_get(raw, "documents", "custom_cleared")),
        vin_resolution=_as_str(_get(raw, "documents", "vin_resolution")),
        owners_resolution=_as_str(_get(raw, "documents", "owners_resolution")),
        legal_resolution=_as_str(_get(raw, "documents", "legal_resolution")),
        accidents_resolution=_as_str(_get(raw, "documents", "accidents_resolution")),
        not_registered_in_russia=_as_bool(_get(raw, "documents", "not_registered_in_russia")),
        seller_type=_as_str(raw.get("seller_type")),
        geo_id=_as_str(_get(seller_loc, "geobase_id")),
        federal_subject_id=_as_str(_get(seller_loc, "federal_subject_id")),
        photos_count=len(photos) if isinstance(photos, list) else 0,
        transport_tax=_as_int(_get(raw, "owner_expenses", "transport_tax", "tax_by_year")),
        tags=[t for t in raw.get("tags", []) if isinstance(t, str)]
        if isinstance(raw.get("tags"), list)
        else [],
        creation_date=_ms_to_dt(_get(raw, "additional_info", "creation_date")),
        update_date=_ms_to_dt(_get(raw, "additional_info", "update_date")),
        days_in_stock=_as_int(_get(raw, "additional_info", "days_in_stock")),
        days_on_sale=_as_int(_get(raw, "additional_info", "days_on_sale")),
        price_history=_price_history(raw.get("price_history")),
        observed_at=now_utc(),
    )
    offer.payload_hash = compute_payload_hash(offer)
    return offer


def normalize_all(raw_offers: list[dict[str, Any]]) -> list[Offer]:
    """Нормализовать список. Объявления без ``id`` пропускаются и считаются."""
    offers: list[Offer] = []
    for raw in raw_offers:
        try:
            offers.append(normalize_offer(raw))
        except ValueError:
            continue
    return offers


__all__ = ["compute_payload_hash", "normalize_all", "normalize_offer"]
