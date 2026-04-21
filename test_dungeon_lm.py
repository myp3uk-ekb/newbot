import json
import unittest
from unittest.mock import patch

import dungeon_lm


class DungeonLMTests(unittest.TestCase):
    def test_looks_like_dungeon_prompt_by_text(self):
        self.assertTrue(
            dungeon_lm.looks_like_dungeon_prompt(
                "Вы вошли в подземелье. Перед вами две двери.",
                ["Налево", "Направо"],
            )
        )

    def test_extracts_choice_from_json_content(self):
        payload = {
            "choices": [
                {"message": {"content": json.dumps({"choice": "Направо"}, ensure_ascii=False)}}
            ]
        }
        self.assertEqual(dungeon_lm._extract_choice(payload), "Направо")

    def test_ask_lmstudio_choice(self):
        fake_response = {
            "choices": [
                {"message": {"content": '{"choice":"Осмотреться"}'}}
            ]
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(fake_response).encode("utf-8")

        with patch("dungeon_lm.request.urlopen", return_value=_Resp()):
            out = dungeon_lm.ask_lmstudio_choice(
                text="Комната данжа",
                buttons=["Осмотреться", "Идти дальше"],
                base_url="http://127.0.0.1:1234/v1",
                model="qwen",
                timeout_sec=5,
                temperature=0.1,
                max_tokens=64,
            )
        self.assertEqual(out, "Осмотреться")


if __name__ == "__main__":
    unittest.main()
