import unittest

from tg_client import _parse_effects_state, _effect_group_is_active


class EffectsLogicTests(unittest.TestCase):
    def test_parse_negative_effects_detected(self):
        txt = """Енот
Временные эффекты:
🐋Живучесть +9 (3 ч.)
🫟Потеряшливость -20 Разведка (23 ч.)
"""
        st = _parse_effects_state(txt)
        self.assertTrue(st["has_negative"])
        self.assertTrue(_effect_group_is_active(st["active_norm"], "vitality"))
        self.assertFalse(_effect_group_is_active(st["active_norm"], "regen"))

    def test_parse_positive_effects_only(self):
        txt = """Временные эффекты:
Бонус 🌙 +50% (12 ч.)
Броня +36 (16 ч.)
Атака +3 (16 ч.)
"""
        st = _parse_effects_state(txt)
        self.assertFalse(st["has_negative"])
        self.assertTrue(_effect_group_is_active(st["active_norm"], "wealth"))
        self.assertTrue(_effect_group_is_active(st["active_norm"], "armor"))
        self.assertTrue(_effect_group_is_active(st["active_norm"], "power"))


if __name__ == "__main__":
    unittest.main()
