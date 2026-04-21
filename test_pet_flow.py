import unittest

from tg_client import (
    _pet_extract_inventory_pet_cmds,
    _pet_extract_pet_cmds,
    _pet_parse_selector_cmd,
    _pet_parse_terrarium_no_from_cmd,
    _pet_parse_terrarium_no_from_screen,
)


class PetFlowParsingTests(unittest.TestCase):
    def test_extract_pet_cmds_keeps_order(self):
        text = "Питомцы: /t8_1 🐸 /t8_2 🐸 /t8_1 🐸"
        self.assertEqual(_pet_extract_pet_cmds(text), ["/t8_1", "/t8_2"])

    def test_parse_terrarium_number_from_command(self):
        self.assertEqual(_pet_parse_terrarium_no_from_cmd("/f_8"), 8)
        self.assertIsNone(_pet_parse_terrarium_no_from_cmd("/t8_1"))

    def test_parse_terrarium_number_from_screen(self):
        text = "🕰⁵ Дубовый террариум 8  🪹 Кормушка³ Еда: 109/450"
        self.assertEqual(_pet_parse_terrarium_no_from_screen(text), 8)
        self.assertIsNone(_pet_parse_terrarium_no_from_screen("Дом без номера"))

    def test_parse_pet_selector(self):
        self.assertEqual(_pet_parse_selector_cmd("/t8_12"), (8, 12))
        self.assertIsNone(_pet_parse_selector_cmd("/f_8"))

    def test_extract_inventory_pet_cmds(self):
        text = (
            "/i_h Голова: шлем\n"
            "/i_p Питомец: Рыболов [5]\n"
            "/i_12 Помощник садовника (1)\n"
            "/i_44 Зелье лечения (3)\n"
            "/i_12 Помощник садовника (1)\n"
        )
        self.assertEqual(_pet_extract_inventory_pet_cmds(text), ["/i_p", "/i_12"])


    def test_extract_inventory_pet_cmds_skips_empty_pet_slot(self):
        text = (
            "/i_h Голова: шлем\n"
            "/i_p Питомец: слот пуст\n"
            "/i_12 Помощник садовника (1)\n"
        )
        self.assertEqual(_pet_extract_inventory_pet_cmds(text), ["/i_12"])

    def test_extract_inventory_pet_cmds_from_single_line_dump(self):
        text = "/i_h Голова: шлем /i_p Питомец: Рыболов [5] /i_12 Помощник садовника (1) /i_44 Зелье лечения (3)"
        self.assertEqual(_pet_extract_inventory_pet_cmds(text), ["/i_p", "/i_12"])



if __name__ == "__main__":
    unittest.main()
