from __future__ import annotations

import json
import logging
import re
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


def _http_json(url: str, *, timeout_sec: float, method: str = "GET", body: dict | None = None) -> dict:
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def list_lmstudio_models(base_url: str, timeout_sec: float) -> list[str]:
    payload = _http_json(base_url.rstrip("/") + "/models", timeout_sec=timeout_sec)
    out: list[str] = []
    for item in payload.get("data", []) or []:
        model_id = str(item.get("id", "")).strip()
        if model_id:
            out.append(model_id)
    return out


def resolve_chat_model(base_url: str, configured_model: str, timeout_sec: float) -> str:
    configured = (configured_model or "").strip()
    models = list_lmstudio_models(base_url, timeout_sec)
    if not models:
        # Fallback to configured value when model listing is unavailable.
        return configured
    if configured and configured in models:
        return configured

    # Prefer non-embedding models when auto-selecting.
    candidates = [m for m in models if "embedding" not in m.lower()]
    base = (candidates or models)
    # Prefer Qwen family if present (current default runtime model line).
    qwen = [m for m in base if "qwen" in m.lower()]
    selected = (qwen or base)[0]
    if configured and configured not in models:
        log.warning("🕸 LM Studio model '%s' not found; using '%s'", configured, selected)
    return selected


def looks_like_dungeon_prompt(text: str, buttons: Iterable[str]) -> bool:
    low = (text or "").lower().replace("ё", "е")
    # Do not trigger on generic informational mentions like
    # "Чтобы отправиться в подземелье...". We only treat text as a dungeon
    # choice prompt when there is evidence of an immediate branching decision.
    has_dungeon_context = any(h in low for h in DUNGEON_HINTS)
    has_room_list = bool(re.search(r"(?:^|\n)\s*[123]\.\s+", text or "", flags=re.MULTILINE))

    btns = [((b or "").lower().replace("ё", "е")) for b in (buttons or [])]
    if len(btns) < 2:
        return False

    directional = sum(1 for b in btns if any(k in b for k in ("налево", "направо", "прямо", "дальше", "вперед", "вперёд")))
    tactical = sum(1 for b in btns if any(k in b for k in ("атак", "тихо", "осмотр", "отступ", "обойти", "открыть")))
    has_branching_buttons = directional >= 2 or tactical >= 2

    if has_dungeon_context and (has_room_list or has_branching_buttons):
        return True

    # Fallback heuristic when text is short/edited but buttons clearly indicate branching.
    return has_branching_buttons


def _extract_choice(payload: dict) -> str | None:
    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception:
        return None
    if not content:
        return None

    content = str(content).strip()
    # Support markdown fenced JSON output.
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(content)
        c = str(parsed.get("choice", "")).strip()
        return c or None
    except Exception:
        pass

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
    final_model = resolve_chat_model(base_url, model, timeout_sec)
    if not final_model:
        return None

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

    payload = _http_json(
        endpoint,
        timeout_sec=timeout_sec,
        method="POST",
        body={
            "model": final_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    return _extract_choice(payload)


__all__ = [
    "ask_lmstudio_choice",
    "list_lmstudio_models",
    "looks_like_dungeon_prompt",
    "resolve_chat_model",
]
