import json
import unittest
from unittest.mock import patch

import dungeon_lm


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


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

    def test_resolve_chat_model_prefers_non_embedding(self):
        models_payload = {
            "data": [
                {"id": "text-embedding-nomic-embed-text-v1.5"},
                {"id": "google/gemma-4-e4b"},
            ],
            "object": "list",
        }
        with patch("dungeon_lm.request.urlopen", return_value=_Resp(models_payload)):
            model = dungeon_lm.resolve_chat_model(
                "http://127.0.0.1:1234/v1",
                configured_model="missing-model",
                timeout_sec=5,
            )
        self.assertEqual(model, "google/gemma-4-e4b")

    def test_ask_lmstudio_choice(self):
        models_payload = {
            "data": [{"id": "google/gemma-4-e4b"}],
            "object": "list",
        }
        chat_payload = {
            "choices": [
                {"message": {"content": '{"choice":"Осмотреться"}'}}
            ]
        }

        def _fake_urlopen(req, timeout=0):
            url = getattr(req, "full_url", str(req))
            if str(url).endswith("/models"):
                return _Resp(models_payload)
            return _Resp(chat_payload)

        with patch("dungeon_lm.request.urlopen", side_effect=_fake_urlopen):
            out = dungeon_lm.ask_lmstudio_choice(
                text="Комната данжа",
                buttons=["Осмотреться", "Идти дальше"],
                base_url="http://127.0.0.1:1234/v1",
                model="google/gemma-4-e4b",
                timeout_sec=5,
                temperature=0.1,
                max_tokens=64,
            )
        self.assertEqual(out, "Осмотреться")


if __name__ == "__main__":
    unittest.main()
