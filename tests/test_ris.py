"""Сборка SSR-состояния из фрагментов."""

from __future__ import annotations

import json

import pytest

from autoru_parser.core.errors import ParseError
from autoru_parser.frontends.autoru import ris
from autoru_parser.frontends.autoru.ris import assemble, clear_cache, split_chunks


def test_фрагменты_находятся_в_странице(listing_html: str) -> None:
    chunks = split_chunks(listing_html)
    assert len(chunks) >= 2, "ожидалось несколько фрагментов"


def test_порядок_в_документе_не_используется(shuffled_chunks: dict[int, str]) -> None:
    """Переставленные фрагменты с новыми id обязаны собраться."""
    state, order = assemble(shuffled_chunks)
    assert "listing" in state, "собранное состояние должно содержать listing"
    assert len(order) == len(shuffled_chunks), "использованы не все фрагменты"


def test_в_собранном_состоянии_есть_объявления(shuffled_chunks: dict[int, str]) -> None:
    state, _order = assemble(shuffled_chunks)
    assert state["listing"]["data"]["offers"]


def test_сборка_не_зависит_от_исходного_порядка(listing_html: str) -> None:
    """Результат не должен зависеть от того, в каком порядке лежат фрагменты."""
    chunks = split_chunks(listing_html)
    first_state, _first_order = assemble(chunks)
    clear_cache()
    reordered = {cid: chunks[cid] for cid in sorted(chunks)}
    second_state, _second_order = assemble(reordered)
    assert json.dumps(first_state, sort_keys=True) == json.dumps(second_state, sort_keys=True)


def test_найденный_порядок_кэшируется(shuffled_chunks: dict[int, str]) -> None:
    assemble(shuffled_chunks)
    assert ris._ORDER_CACHE, "порядок должен был попасть в кэш"


def test_кэш_позволяет_собрать_повторно(shuffled_chunks: dict[int, str]) -> None:
    """После сброса кэша порядок находится заново и даёт тот же результат."""
    first_state, _order = assemble(shuffled_chunks)
    clear_cache()
    second_state, _order2 = assemble(shuffled_chunks)
    assert json.dumps(first_state, sort_keys=True) == json.dumps(second_state, sort_keys=True)


def test_без_фрагментов_это_ошибка_разбора() -> None:
    with pytest.raises(ParseError, match="нет ни одного фрагмента"):
        assemble({})


def test_битые_фрагменты_дают_ошибку_а_не_пустоту() -> None:
    """Главный тест: сломанная сборка обязана быть заметна.

    Именно отсутствие такого поведения стоило нам прогона разведки: страница
    пришла, объявлений ноль, и это выглядело как «фильтр ничего не нашёл».
    """
    broken = {1: '{"a":1,"b":', 2: '  "c":', 3: '"d"'}
    with pytest.raises(ParseError):
        assemble(broken)


def test_валидный_но_чужой_json_не_принимается() -> None:
    """Состояние без listing и router — не наше состояние."""
    with pytest.raises(ParseError):
        assemble({1: '{"something":"else"}'})


def test_несколько_начальных_фрагментов_дают_ошибку() -> None:
    with pytest.raises(ParseError, match="начинающихся"):
        assemble({1: '{"a":1}', 2: '{"b":2}'})


def test_слишком_много_фрагментов_не_перебираем_без_конца(monkeypatch) -> None:
    monkeypatch.setattr(ris, "MAX_PERMUTABLE", 3)
    chunks: dict[int, str] = {0: '{"listing":{},"a":1'}
    chunks.update({index: f',"k{index}":1' for index in range(1, 6)})
    with pytest.raises(ParseError, match="перебор"):
        assemble(chunks)
