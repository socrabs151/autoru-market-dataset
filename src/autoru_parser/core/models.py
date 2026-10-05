"""Доменные объекты: объявление, точка истории, фильтры, ссылка на объявление.

Все объекты плоские. Вложенности исходного ответа здесь нет намеренно:
аналитике удобнее колонки, а не разбор ``json``-строки (решение 3).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from autoru_parser import SOURCE

DEFAULT_DB = "data/autoru.duckdb"

#: Значения ``seller_type``, которые означают перекупщика или салон.
#: Наблюдались в ответах: ``PRIVATE`` и ``COMMERCIAL``. Набор расширяем по
#: мере появления новых: неизвестное значение считаем частным объявлением,
#: потому что перепутать в эту сторону безопаснее, чем назвать салон частным.
DEALER_SELLER_TYPES: frozenset[str] = frozenset(
    {"COMMERCIAL", "DEALER", "TRADEIN", "COMPANY", "REPRESENTATIVE"}
)


def now_utc() -> datetime:
    """Текущее время в UTC."""
    return datetime.now(UTC)


class Filters(BaseModel):
    """Фильтр выборки.

    Хранит только те фильтры, которые auto.ru выражает сегментом пути.
    Фильтры вроде пробега сюда не входят: их нет на стороне сайта,
    они применяются к собранным данным (решение 10).

    Значения полей — обычные строки, а не ``Literal``. Словари допустимых
    значений принадлежат сайту и живут рядом с построением адреса
    (``frontends.autoru.urls``), где и проверяются: так ошибка в фильтре
    сообщается сразу при попытке собрать адрес, а не молча при создании
    объекта.
    """

    category: str = "cars"
    section: str = "used"
    geo: str | None = Field(default=None, description="город или регион слагами")
    mark: str | None = None
    model: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    price_to: int | None = Field(default=None, gt=0)
    availability: str = "any"
    body: str | None = None
    color: str | None = None
    drive: str | None = None
    transmission: str | None = None
    engine: str | None = None
    seats: int | None = Field(default=None, ge=1, le=20)
    origin: str | None = Field(default=None, description="происхождение марки")


class PricePoint(BaseModel):
    """Одна точка истории цены, сообщённая сайтом."""

    price: int
    observed_at: datetime


class Offer(BaseModel):
    """Нормализованное объявление. Плоское, без персональных данных."""

    source: str = SOURCE
    offer_id: str
    sale_id: str
    url: str
    sale_hash: str | None = None

    category: str | None = None
    section: str | None = None
    sub_category: str | None = None
    title: str | None = None

    mark: str | None = None
    mark_name: str | None = None
    model: str | None = None
    model_name: str | None = None
    super_gen_id: str | None = None
    super_gen_name: str | None = None
    super_gen_year_from: int | None = None
    super_gen_year_to: int | None = None
    origin: str | None = Field(default=None, description="происхождение марки, как на сайте")
    body_type: str | None = None
    body_type_human: str | None = None
    auto_class: str | None = None
    doors_count: int | None = None
    seats: list[int] = Field(default_factory=list)
    engine: str | None = None
    engine_type: str | None = None
    displacement: int | None = None
    power_hp: int | None = None
    power_kwt: int | None = None
    acceleration_to: float | None = None
    nameplate: str | None = Field(default=None, description="модификация, например 40i")
    transmission: str | None = None
    drive: str | None = None
    color: str | None = None
    color_hex: str | None = None
    tech_param_id: str | None = None

    production_year: int | None = None
    mileage: int | None = None
    condition: str | None = None
    status: str | None = None
    availability: str | None = None

    price: int | None = None
    price_eur: int | None = None
    price_usd: int | None = None
    currency: str | None = None
    with_nds: bool | None = None

    owners_number: int | None = None
    pts: str | None = None
    pts_original: bool | None = None
    custom_cleared: bool | None = None
    vin_resolution: str | None = None
    owners_resolution: str | None = None
    legal_resolution: str | None = None
    accidents_resolution: str | None = None
    not_registered_in_russia: bool | None = None

    seller_type: str | None = None
    geo_id: str | None = None
    federal_subject_id: str | None = None

    photos_count: int = 0
    transport_tax: int | None = None
    tags: list[str] = Field(default_factory=list)

    creation_date: datetime | None = None
    update_date: datetime | None = None
    days_in_stock: int | None = None
    days_on_sale: int | None = None

    price_history: list[PricePoint] = Field(default_factory=list)

    observed_at: datetime = Field(default_factory=now_utc)
    payload_hash: str = ""

    @property
    def is_dealer(self) -> bool:
        """Перекупщик или автосалон ли продавец.

        Основано на списке известных значений, а не на «всё, что не PRIVATE».
        Причина: в ответах встречается ``COMMERCIAL``, о котором заранее
        не знаешь, и правило «не PRIVATE» назвало бы частное объявление
        салоном.
        """
        return (self.seller_type or "").upper() in DEALER_SELLER_TYPES


class OfferRef(BaseModel):
    """Ссылка на объявление: найдена, но ещё не загружена."""

    offer_id: str
    url: str


class Page(BaseModel):
    """Страница выборки: что отдал сайт."""

    url: str
    offers: list[Offer]
    total_offers: int
    total_pages: int


class Progress(BaseModel):
    """Состояние прогона — для отчёта и для внешней системы."""

    pages_done: int = 0
    offers_seen: int = 0
    offers_stored: int = 0
    requests: int = 0


__all__ = [
    "DEALER_SELLER_TYPES",
    "DEFAULT_DB",
    "Filters",
    "Offer",
    "OfferRef",
    "Page",
    "PricePoint",
    "Progress",
    "now_utc",
]
