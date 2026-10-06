"""Командная строка: что происходит, когда не хватает пакета.

Экран и сервис вынесены в необязательные наборы. Значит, установка без них —
обычная ситуация, а не поломка: человек должен получить слова и код выхода,
а не трассировку стека.

Проверка без пакетов идёт в отдельном интерпретаторе. В том же процессе
подделать отсутствие пакета нельзя: модуль, который уже импортирован, лежит
в ``sys.modules``, и подмена импорта его не достанет. Отсюда и отдельный
процесс — иначе тест врал бы только в одиночном прогоне.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from autoru_parser.ui import cli

SRC = Path(__file__).resolve().parents[1] / "src"
OPTIONAL = ("textual", "fastapi", "uvicorn")

#: Скрипт подделывает отсутствие необязательных пакетов и зовёт команду.
BLOCK_AND_RUN = """
import builtins, sys

BLOCKED = {optional!r}
real = builtins.__import__


def fake(name, *args, **kwargs):
    if name.split(".")[0] in BLOCKED:
        raise ImportError("No module named " + repr(name))
    return real(name, *args, **kwargs)


builtins.__import__ = fake

from typer.testing import CliRunner
from autoru_parser.ui.cli import app

result = CliRunner().invoke(app, [{command!r}])
print("EXIT", result.exit_code)
print(result.output)
if result.exception and not isinstance(result.exception, SystemExit):
    print("EXC", type(result.exception).__name__)
"""


def run_without_optional(command: str) -> tuple[int, str]:
    """Выполнить команду в чистом интерпретаторе без необязательных пакетов."""
    script = BLOCK_AND_RUN.format(optional=list(OPTIONAL), command=command)
    # Окружение копируется, а не собирается с нуля: с пустым PATH на Windows
    # не поднимается сокетная подсистема, и подпроцесс падает не там, где
    # проверяют.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=120,
        check=False,
    )
    output = completed.stdout + completed.stderr
    code = 0
    for line in output.splitlines():
        if line.startswith("EXIT "):
            code = int(line.split()[1])
    return code, output


# --- подсказка ---------------------------------------------------------------


@pytest.mark.parametrize("command", ["tui", "serve"])
def test_команда_без_пакета_объясняет(command: str) -> None:
    code, output = run_without_optional(command)

    assert code == 7, output
    assert "Traceback" not in output
    assert "EXC" not in output, output
    assert "autoru-parser[" in output
    assert "Остальные команды работают" in output


@pytest.mark.parametrize(
    ("command", "extra"),
    [("tui", "[ui]"), ("serve", "[service]")],
)
def test_подсказка_называет_нужный_набор(command: str, extra: str) -> None:
    """Подсказка должна быть точной: не тот набор приведёт к тому же вопросу."""
    _code, output = run_without_optional(command)
    assert extra in output, output


def test_ядро_работает_без_пакетов() -> None:
    """`autoru summary` не должен требовать ни Textual, ни FastAPI."""
    code, output = run_without_optional("summary")
    assert code in (0, 2), output
    assert "Traceback" not in output


# --- устройство установки ---------------------------------------------------


def test_необязательные_пакеты_не_в_ядре() -> None:
    """Сервис и экран не должны быть среди обязательных зависимостей."""
    import tomllib

    manifest = tomllib.loads((SRC.parent / "pyproject.toml").read_text(encoding="utf-8"))
    project = manifest["project"]
    required = " ".join(project["dependencies"]).lower()
    for package in OPTIONAL:
        assert package not in required, f"{package} попал в обязательные зависимости"
    extras = project["optional-dependencies"]
    assert "textual" in " ".join(extras["ui"]).lower()
    assert "fastapi" in " ".join(extras["service"]).lower()
    assert "uvicorn" in " ".join(extras["service"]).lower()


def test_ядро_не_импортирует_необязательные_пакеты() -> None:
    """Сервис и экран не должны попадать в импорт ядра.

    Проверяется текстом файлов, а не импортом: если модуль начнёт тянуть
    FastAPI на верхнем уровне, обычный ``autoru crawl`` замедлится и начнёт
    требовать лишний пакет.
    """
    import re

    for relative in (
        "ui/cli.py",
        "core/models.py",
        "core/normalize.py",
        "services/crawl.py",
        "storage/duck.py",
    ):
        source = (SRC / "autoru_parser" / relative).read_text(encoding="utf-8")
        path = f"src/autoru_parser/{relative}"
        for line in source.splitlines():
            if line.startswith(("import ", "from ")) and not line.startswith(" "):
                assert not re.match(
                    r"^(import|from)\s+(textual|fastapi|uvicorn)\b",
                    line.strip(),
                ), f"{path}: необязательный пакет импортируется на верхнем уровне: {line}"


def test_список_команд_доступен_без_пакетов() -> None:
    """`autoru --help` показывает и те команды, что требуют пакета."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "tui" in result.output
    assert "serve" in result.output


def test_наборы_согласованы_с_кодом() -> None:
    """Каждый набор из подсказки должен существовать в манифесте."""
    import tomllib

    manifest = tomllib.loads((SRC.parent / "pyproject.toml").read_text(encoding="utf-8"))
    extras = manifest["project"]["optional-dependencies"]
    for extra in cli.EXTRAS.values():
        name = extra[1].split("[")[1].split("]")[0]
        assert name in extras, f"подсказка зовёт набор {name}, которого нет в манифесте"


def test_указание_набора_разбирается() -> Any:
    """Все три набора из подсказок есть и в extras, и в all."""
    import tomllib

    manifest = tomllib.loads((SRC.parent / "pyproject.toml").read_text(encoding="utf-8"))
    extras = manifest["project"]["optional-dependencies"]
    everything = " ".join(extras["all"]).lower()
    for name in ("textual", "fastapi", "uvicorn"):
        assert name in everything, f"{name} не попал в набор all"
