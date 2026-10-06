"""Запрещённые поля и проверка на их отсутствие.

Нормализация собирает объявление по белому списку: перечислено разрешённое,
остальное не копируется вообще. Поэтому персональные данные не попадают
в хранилище, а не вычищаются на выходе.

Список нужен ещё и для тестов: тест проверяет, что в результате нет
ни одного запрещённого ключа — тем же списком.
"""

from __future__ import annotations

from typing import Any

#: Ключи, которые не должны появляться ни в одном сохранённом объекте.
#: Проверено по реальным ответам: в них лежат имя, телефон, точный адрес,
#: координаты, станции метро, идентификатор пользователя и счётчики.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "address",
        "alias",
        "author",
        "avg_rating_counter",
        "callback_info",
        "call_hours",
        "card_view_chat_conversion_all",
        "chats_all",
        "chats_enabled",
        "coord",
        "coords",
        "email",
        "encrypted_user_id",
        "geo_ids",
        "latitude",
        "license_plate",
        "login",
        "longitude",
        "metro",
        "name",
        "phones",
        "redirect_phones",
        "salon",
        "user_ref",
        "vin",
    }
)

#: Ключи, которые встречаются и в безобидном смысле: их нельзя запрещать
#: целиком, потому что потеряются технические характеристики.
#: Значения таких полей проверяются отдельно — см. ``normalize``.


def find_forbidden(obj: Any, path: str = "") -> list[str]:
    """Возвращает пути к запрещённым ключам внутри ``obj``.

    Рекурсивный обход списков и словарей. Нужен для тестов: он доказывает,
    что запрещённого в сохранённом объекте нет.
    """
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key) in FORBIDDEN_KEYS:
                found.append(here)
            found.extend(find_forbidden(value, here))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_forbidden(item, f"{path}[]"))
    return found


__all__ = ["FORBIDDEN_KEYS", "find_forbidden"]
