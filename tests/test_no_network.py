"""Проверка того, что тесты действительно не ходят в сеть.

Правило «тесты работают на фикстурах» держится в ``conftest``, который
запрещает внешние соединения. Само правило бесполезно, если никто не
проверяет, что оно вообще работает: обход запрета нашёлся бы позже и тихо.

Здесь проверка обратная: подключение к внешнему адресу обязано падать, а
подключение к локальному — нет.
"""

import socket

import pytest


def test_внешнее_соединение_запрещено() -> None:
    with pytest.raises(RuntimeError, match="внешн"):
        socket.socket().connect(("93.184.216.34", 80))


def test_внешнее_соединение_через_ex_тоже_запрещено() -> None:
    with pytest.raises(RuntimeError, match="внешн"):
        socket.socket().connect_ex(("93.184.216.34", 80))


def test_локальное_соединение_разрешено() -> None:
    """Локальный обход служебной надобности: так устроен тестовый обвязкой."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.socket()
    try:
        client.connect(("127.0.0.1", port))
    finally:
        client.close()
        listener.close()


def test_проверка_адреса_понимает_формы() -> None:
    from conftest import _is_local

    assert _is_local(("127.0.0.1", 80))
    assert _is_local(("localhost", 80))
    assert _is_local(("::1", 80, 0, 0))
    assert _is_local("127.0.0.1")
    assert not _is_local(("93.184.216.34", 80))
    assert not _is_local("auto.ru")
