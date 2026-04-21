from types import SimpleNamespace

from game_parser import parse_message


def _msg(text: str, buttons: list[list[str]]):
    rows = [[SimpleNamespace(text=label) for label in row] for row in buttons]
    return SimpleNamespace(message=text, buttons=rows)


def test_fishing_start_detected_in_fishing_context():
    msg = _msg(
        "🎣 Рыбалка\nДля начала рыбалки нажми кнопку ниже.",
        [["🛠Начать", "✖️Отмена"]],
    )
    state = parse_message(msg, fish_hook_sub="Подсечь", fish_cast_sub="Закинуть")
    assert state.stage == "fishing_start"


def test_dismantle_start_is_not_fishing_start():
    msg = _msg(
        "📿⁵ Дубовая удочка (0/60)\nНачать разбор?",
        [["🛠Начать", "✖️Отмена"]],
    )
    state = parse_message(msg, fish_hook_sub="Подсечь", fish_cast_sub="Закинуть")
    assert state.stage != "fishing_start"
