from __future__ import annotations

import json
import logging
from typing import Iterable
from urllib import request

log = logging.getLogger("autopilot")


DUNGEON_HINTS = (
    "данж",
    "данже",
    "подзем",
    "комнат",
    "двер",
    "коридор",
    "ловуш",
    "сундук",
    "босс",
)


def looks_like_dungeon_prompt(text: str, buttons: Iterable[str]) -> bool:
    low = (text or "").lower().replace("ё", "е")
    if any(h in low for h in DUNGEON_HINTS):
        return True
    btns = [((b or "").lower().replace("ё", "е")) for b in (buttons or [])]
    if len(btns) < 2:
        return False
    directional = sum(1 for b in btns if any(k in b for k in ("налево", "направо", "прямо", "дальше", "вперед", "вперёд")))
    tactical = sum(1 for b in btns if any(k in b for k in ("атак", "тихо", "осмотр", "отступ", "обойти", "открыть")))
    return directional >= 2 or tactical >= 2


def _extract_choice(payload: dict) -> str | None:
    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception:
        return None
    if not content:
        return None

    content = str(content).strip()
    # Preferred format: strict JSON {"choice":"..."}
    try:
        parsed = json.loads(content)
        c = str(parsed.get("choice", "")).strip()
        return c or None
    except Exception:
        pass

    # Fallback: model answered plain text.
    line = content.splitlines()[0].strip().strip('"')
    return line or None


def ask_lmstudio_choice(
    *,
    text: str,
    buttons: list[str],
    base_url: str,
    model: str,
    timeout_sec: float,
    temperature: float,
    max_tokens: int,
) -> str | None:
    if not buttons:
        return None

    endpoint = base_url.rstrip("/") + "/chat/completions"
    sys_prompt = (
        "Ты управляешь игровым ботом в Telegram и выбираешь одну кнопку. "
        "Ответь строго JSON-объектом: {\"choice\":\"<точный текст кнопки>\"}. "
        "Ничего больше не добавляй."
    )
    user_prompt = (
        "Текст экрана:\n"
        f"{text}\n\n"
        "Кнопки:\n"
        + "\n".join(f"- {b}" for b in buttons)
        + "\n\nВыбери СТРОГО одну кнопку из списка."
    )

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    req = request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    return _extract_choice(payload)


__all__ = [
    "ask_lmstudio_choice",
    "looks_like_dungeon_prompt",
]
