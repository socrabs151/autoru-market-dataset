"""Проверки со стороны человека.

Здесь важно не то, что обойти нельзя, а то, что человек понимает, что
произошло, и знает, что делать. Поэтому проверяются тексты и то, что
парсер честно останавливается, а не делает вид, что всё в порядке.
"""

import structlog

from autoru_parser.core.errors import CaptchaRequired
from autoru_parser.frontends.autoru import captcha
from autoru_parser.frontends.autoru.listing import check_block

CAPTCHA_HTML = "<div>Подтвердите, что запросы отправляли вы</div>"


def test_текст_объясняет_что_делать() -> None:
    text = captcha.explain()
    assert "проверк" in text.lower()
    assert "браузер" in text.lower()
    assert "--rps" in text, "нужно сказать про низкий темп: причина обычно в нём"


def test_текст_не_обещает_автоматического_обхода() -> None:
    """Текст не должен намекать, что обход программный."""
    text = captcha.explain().lower()
    for forbidden in ("автомат", "обойд", "распозна", "тольф"):
        assert forbidden not in text, f"в объяснении не должно быть слова «{forbidden}»"


def test_факт_проверки_попадает_в_лог() -> None:
    with structlog.testing.capture_logs() as logs:
        captcha.report(CaptchaRequired("нужна проверка"))
    events = [entry for entry in logs if entry["event"] == "captcha.required"]
    assert events, f"в логе нет отметки о проверке: {[e['event'] for e in logs]}"
    assert events[0]["action"] == "wait_for_human"


def test_проверка_распознаётся_и_останавливает_сбор() -> None:
    """Страница с требованием проверки обязана поднимать CaptchaRequired.

    Отдельный класс нужен именно потому, что это не поломка: сбор на этом
    останавливается, а не продолжает биться в защиту.
    """
    try:
        check_block(CAPTCHA_HTML)
    except CaptchaRequired:
        pass
    else:
        raise AssertionError("проверка не была распознана")


def test_проверка_не_путается_с_отказом() -> None:
    """Обычный отказ и требование проверки — разные исходы."""
    from autoru_parser.core.errors import BlockedBySite

    try:
        check_block("<html>Доступ ограничен</html>")
    except CaptchaRequired as exc:
        raise AssertionError(f"обычный отказ принят за проверку: {exc}") from exc
    except BlockedBySite:
        return
    raise AssertionError("отказ не распознан")
