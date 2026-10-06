"""Сборка SSR-состояния auto.ru из фрагментов.

Состояние приходит в нескольких ``<script class="RIS">``, и это куски одного
JSON-объекта, причём в произвольном порядке. Ни порядок документа, ни
сортировка по ``chunk-id`` верным не являются: на одной странице одно
совпало случайно, на другой перестало работать (решение 12).

Поэтому порядок не угадывается, а подбирается перебором с проверкой разбора.
Найденный порядок кэшируется по набору фрагментов, потому что между запросами
он не меняется.

Если ни один порядок не подошёл — бросается ``ParseError``. Возвращать пустое
состояние нельзя: это выглядело бы как «фильтр ничего не нашёл» и стоило нам
одного прогона разведки.
"""

from __future__ import annotations

import itertools
import json
import re
from typing import Any

from autoru_market_dataset.core.errors import ParseError

_CHUNK_RE = re.compile(
    r'<script[^>]*class="RIS"[^>]*data-webpack-chunk-id="(\d+)"[^>]*>(.*?)</script>',
    re.DOTALL,
)

#: Максимум фрагментов, для которого ещё имеет смысл перебор.
#: При 8 фрагментах остаётся 7! = 5040 вариантов, при 12 уже 39 млн.
MAX_PERMUTABLE = 8

#: Ключи, которые обязаны быть в корне разобранного состояния.
#: Без этой проверки можно принять за состояние любой случайный валидный JSON.
_REQUIRED_ROOT_KEYS = ("listing", "router")

#: Найденные порядки: ключ — подпись набора фрагментов, значение — порядок.
_ORDER_CACHE: dict[tuple[tuple[int, int], ...], tuple[int, ...]] = {}


def split_chunks(html: str) -> dict[int, str]:
    """Вытащить фрагменты состояния: ``{chunk_id: текст}``.

    Порядок появления в HTML не сохраняется — он не логический.
    """
    return {int(cid): body for cid, body in _CHUNK_RE.findall(html)}


def signature(chunks: dict[int, str]) -> tuple[tuple[int, int], ...]:
    """Подпись набора фрагментов: по ней кэшируется найденный порядок."""
    return tuple(sorted((cid, len(body)) for cid, body in chunks.items()))


def clear_cache() -> None:
    """Сбросить кэш порядка. Нужен в тестах."""
    _ORDER_CACHE.clear()


def _looks_valid(obj: Any) -> bool:
    return isinstance(obj, dict) and any(key in obj for key in _REQUIRED_ROOT_KEYS)


def _try_order(chunks: dict[int, str], first: int, rest: list[int]) -> dict[str, Any] | None:
    blob = chunks[first] + "".join(chunks[cid] for cid in rest)
    try:
        parsed = json.loads(blob.strip())
    except (ValueError, TypeError):
        return None
    return parsed if _looks_valid(parsed) else None


def assemble(chunks: dict[int, str]) -> tuple[dict[str, Any], list[int]]:
    """Собрать состояние из фрагментов.

    Возвращает разобранное состояние и найденный порядок фрагментов.

    :raises ParseError: фрагментов нет, начальный не единственный,
        перебор слишком дорог или ни один порядок не подошёл.
    """
    if not chunks:
        raise ParseError("в ответе нет ни одного фрагмента RIS")

    firsts = [cid for cid, body in chunks.items() if body.lstrip().startswith("{")]
    if len(firsts) != 1:
        raise ParseError(
            f"фрагментов, начинающихся с '{{': {len(firsts)}, ожидался ровно один; "
            f"всего фрагментов {len(chunks)}"
        )
    first = firsts[0]
    rest = [cid for cid in chunks if cid != first]

    sig = signature(chunks)
    cached = _ORDER_CACHE.get(sig)
    if cached is not None:
        parsed = _try_order(chunks, first, list(cached))
        if parsed is not None:
            return parsed, [first, *cached]

    candidates: list[list[int]] = [rest, sorted(rest, reverse=True)]
    for cand in candidates:
        parsed = _try_order(chunks, first, cand)
        if parsed is not None:
            _ORDER_CACHE[sig] = tuple(cand)
            return parsed, [first, *cand]

    if len(rest) + 1 > MAX_PERMUTABLE:
        raise ParseError(
            f"фрагментов {len(rest) + 1}, перебор недопустим: "
            f"формат изменился, нужно переписывать сборку"
        )

    for perm in itertools.permutations(rest):
        parsed = _try_order(chunks, first, list(perm))
        if parsed is not None:
            _ORDER_CACHE[sig] = perm
            return parsed, [first, *perm]

    raise ParseError(f"ни один порядок из {len(chunks)} фрагментов не дал валидное состояние")


__all__ = ["MAX_PERMUTABLE", "assemble", "clear_cache", "signature", "split_chunks"]
