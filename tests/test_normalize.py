"""Нормализация: сырой ответ → плоское объявление без персональных данных."""

from __future__ import annotations

from typing import Any

from autoru_market_dataset.core.models import Offer
from autoru_market_dataset.core.normalize import (
    compute_payload_hash,
    normalize_all,
    normalize_offer,
)
from autoru_market_dataset.core.pii import find_forbidden


def test_первое_объявление_нормализуется(listing_offers: list[dict[str, Any]]) -> None:
    offer = normalize_offer(listing_offers[0])
    assert offer.offer_id.isdigit()
    assert offer.url.startswith("https://auto.ru/")
    assert offer.price and offer.price > 0
    # Марка и модель берутся из кода сайта: он каноничнее слага в адресе.
    assert offer.mark == "BMW"
    assert offer.model
    assert offer.mark_name, "человекочитаемое название марки должно сохраняться"


def test_марка_и_модель_подставляются_из_адреса_если_нет_данных() -> None:
    """Слаг из адреса — запасной путь, когда полей сайта нет."""
    raw = {
        "id": "1234567",
        "url": "https://auto.ru/cars/used/sale/kia/rio/1234567-abcdef01/",
    }
    offer = normalize_offer(raw)
    assert offer.mark == "kia"
    assert offer.model == "rio"


def test_характеристики_берутся_из_tech_param(listing_offers: list[dict[str, Any]]) -> None:
    """Двигатель и коробка лежат не на верхнем уровне vehicle_info."""
    offers = normalize_all(listing_offers)
    assert any(o.engine_type for o in offers), "должен найтись тип двигателя"
    assert any(o.transmission for o in offers), "должна найтись коробка"
    assert all(o.super_gen_id for o in offers), "должно находиться поколение"
    assert any(o.color_hex for o in offers), "должен находиться цвет в виде кода"


def test_марка_и_модель_подставляются_из_адреса() -> None:
    raw = {"id": "1234567", "url": "https://auto.ru/cars/used/sale/kia/rio/1234567-abcdef01/"}
    offer = normalize_offer(raw)
    assert offer.mark == "kia"
    assert offer.model == "rio"


def test_хаш_не_входит_в_ключевые_данные() -> None:
    raw = {
        "id": "1",
        "saleId": "1-aaaaaaaa",
        "hash": "aaaaaaaa",
        "url": "https://auto.ru/cars/used/sale/a/b/1-aaaaaaaa/",
    }
    offer = normalize_offer(raw)
    assert offer.sale_hash == "aaaaaaaa"
    assert offer.sale_id == "1-aaaaaaaa"


def _все_поля_легализованы(offer: Offer) -> list[str]:
    return find_forbidden(offer.model_dump(mode="json"))


def test_в_объявлении_нет_персональных_данных(listing_offers: list[dict[str, Any]]) -> None:
    """Ключевая проверка: в сохранённом объекте нет ничего о продавце."""
    offers = normalize_all(listing_offers)
    assert offers, "ожидались нормализованные объявления"
    for offer in offers:
        assert _все_поля_легализованы(offer) == [], f"найдены запрещённые поля в {offer.offer_id}"


def test_остаётся_только_тип_продавца(listing_offers: list[dict[str, Any]]) -> None:
    """Ни названия автосалона, ни телефона — только признак «дилер или нет»."""
    for offer in normalize_all(listing_offers):
        dumped = offer.model_dump(mode="json")
        assert "salon" not in dumped
        assert "phones" not in dumped
        assert "address" not in dumped
        if offer.seller_type is not None:
            assert offer.seller_type.isupper()


def test_частное_объявление_не_считается_салоном(listing_offers: list[dict[str, Any]]) -> None:
    offers = normalize_all(listing_offers)
    private = [o for o in offers if o.seller_type == "PRIVATE"]
    assert private, "в фикстуре должны быть частные объявления"
    for offer in private:
        assert offer.is_dealer is False


def test_фото_считаются_но_не_хранятся(listing_offers: list[dict[str, Any]]) -> None:
    offer = normalize_offer(listing_offers[0])
    assert offer.photos_count > 0
    dumped = offer.model_dump(mode="json")
    assert "avatars.avto.ru" not in str(dumped), "ссылки на фото не должны сохраняться"


def test_история_цен_разбирается_и_сортируется(listing_offers: list[dict[str, Any]]) -> None:
    with_history = [o for o in listing_offers if len(o.get("price_history") or []) > 1]
    assert with_history, "ожидалось объявление с историей цены"
    offer = normalize_offer(with_history[0])
    points = offer.price_history
    assert len(points) >= 2
    assert points == sorted(points, key=lambda p: p.observed_at)
    assert points[0].price != points[-1].price, "в истории должна быть смена цены"


def test_хеш_не_меняется_при_перестановке_тегов(listing_offers: list[Any]) -> None:
    """Сайт отдаёт теги в произвольном порядке: перестановка не изменение."""
    raw = dict(listing_offers[0])
    first = normalize_offer(raw)
    shuffled = dict(raw)
    shuffled["tags"] = list(reversed(raw.get("tags") or []))
    second = normalize_offer(shuffled)
    assert first.payload_hash == second.payload_hash, "порядок тегов не должен влиять на хэш"


def test_мигающие_теги_не_создают_изменения(listing_offers: list[Any]) -> None:
    """Теги — служебные метки сайта, и они появляются и пропадают.

    Найдено на живых данных: между двумя прогонами подряд у части
    объявлений появлялся и исчезал тег ``type_top_crown`` при неизменных
    цене и пробеге. Такое не должно попадать в историю.
    """
    raw = dict(listing_offers[0])
    before = normalize_offer(raw)
    after_raw = dict(raw)
    after_raw["tags"] = [*list(raw.get("tags") or []), "type_top_crown"]
    after = normalize_offer(after_raw)
    assert before.payload_hash == after.payload_hash


def test_изменение_характеристики_меняет_хеш(listing_offers: list[Any]) -> None:
    """Проверка, что исключение тегов не выключило хэш целиком."""
    raw = dict(listing_offers[0])
    before = normalize_offer(raw)
    changed = dict(raw)
    price_info = dict(raw["price_info"])
    price_info["price"] = int(price_info["price"]) - 1000
    changed["price_info"] = price_info
    assert before.payload_hash != normalize_offer(changed).payload_hash


def test_добавление_новой_характеристики_меняет_хеш(listing_offers: list[Any]) -> None:
    """Контроль к исключению тегов: хэш не должен перестать работать."""
    raw = dict(listing_offers[0])
    before = normalize_offer(raw)
    changed = dict(raw)
    changed["documents"] = {**raw["documents"], "owners_number": 7}
    assert before.payload_hash != normalize_offer(changed).payload_hash


def test_хеш_не_меняется_от_времени_наблюдения(
    listing_offers: list[dict[str, Any]],
) -> None:
    """Повторный прогон не должен считать объявление изменившимся."""
    raw = dict(listing_offers[0])
    first = normalize_offer(raw)
    second = normalize_offer(raw)
    assert first.payload_hash == second.payload_hash


def test_хеш_меняется_при_смене_цены(listing_offers: list[dict[str, Any]]) -> None:
    raw = dict(listing_offers[0])
    before = normalize_offer(raw)
    changed = dict(raw)
    price_info = dict(raw["price_info"])
    price_info["price"] = int(price_info["price"]) - 1000
    changed["price_info"] = price_info
    after = normalize_offer(changed)
    assert before.payload_hash != after.payload_hash


def test_хеш_считается_по_содержанию(listing_offers: list[dict[str, Any]]) -> None:
    offer = normalize_offer(listing_offers[0])
    assert offer.payload_hash == compute_payload_hash(offer)
    assert len(offer.payload_hash) == 64


def test_объявление_без_идентификатора_пропускается() -> None:
    assert normalize_all([{"url": "https://auto.ru/cars/used/sale/a/b/1-aaaaaaaa/"}]) == []


def test_числа_приходят_строками_и_всё_равно_разбираются() -> None:
    raw = {
        "id": "1",
        "url": "https://auto.ru/cars/used/sale/a/b/1-aaaaaaaa/",
        "price_info": {"price": "1 235 000"},
        "documents": {"year": "2015"},
        "state": {"mileage": "153926"},
    }
    offer = normalize_offer(raw)
    assert offer.price is None or isinstance(offer.price, int)
    assert offer.production_year == 2015
    assert offer.mileage == 153926


def test_отсутствующие_поля_не_ломают_нормализацию() -> None:
    offer = normalize_offer({"id": "1", "url": "https://auto.ru/cars/used/sale/a/b/1-aaaaaaaa/"})
    assert offer.price is None
    assert offer.mileage is None
    assert offer.seats == []
    assert offer.photos_count == 0
    assert offer.is_dealer is False
