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
    # Some game messages serialize all room options on one line:
    # "Уровень 5... 1. ... 2. ... 3. ...". Accept both multiline and inline lists.
    has_room_list = bool(re.search(r"(?:^|\n|\s)[123]\.\s+", text or "", flags=re.MULTILINE))

    btns = [((b or "").lower().replace("ё", "е")) for b in (buttons or [])]
    if len(btns) < 2:
        return False

    directional = sum(1 for b in btns if any(k in b for k in ("налево", "направо", "прямо", "дальше", "вперед", "вперёд")))
    tactical = sum(1 for b in btns if any(k in b for k in ("атак", "тихо", "осмотр", "отступ", "обойти", "открыть")))
    numeric = sum(1 for b in btns if re.search(r"\b[123]\b", b))
    has_branching_buttons = directional >= 2 or tactical >= 2 or numeric >= 2

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


def choose_dungeon_room_by_priority(text: str) -> int | None:
    """Choose dungeon room number using deterministic priority rules.

    Priority:
    1) Boss/monster room (enemy has explicit tier like [1]..[10]); pick highest tier.
    2) Room with strange plants.
    3) Room with alchemy table.
    4) Room with campfire.
    5) Room with chest.
    6) Fallback: first parsed room.
    """
    txt = text or ""
    room_blocks: dict[int, str] = {}
    room_tier: dict[int, int] = {}
    room_priority: dict[int, int] = {}

    for room in (1, 2, 3):
        m = re.search(
            rf"(?:^|\n|\s){room}\.\s*(.*?)(?=(?:\n|\s)[123]\.\s|$)",
            txt,
            re.S | re.I,
        )
        block = (m.group(1) if m else "") or ""
        if not block.strip():
            continue
        room_blocks[room] = block
        low = block.lower().replace("ё", "е")

        tiers = [int(x) for x in re.findall(r"\[(\d{1,2})\]", block)]
        tier_1_10 = [t for t in tiers if 1 <= t <= 10]
        room_tier[room] = max(tier_1_10) if tier_1_10 else 0

        pri = 0
        if "странные растения" in low:
            pri = max(pri, 5)
        if ("алхимическии стол" in low) or ("алхимический стол" in low):
            pri = max(pri, 4)
        if "костер" in low:
            pri = max(pri, 3)
        if "сундук" in low:
            pri = max(pri, 2)
        room_priority[room] = pri

    if not room_blocks:
        return None

    boss_rooms = [r for r in room_blocks if room_tier.get(r, 0) > 0]
    if boss_rooms:
        return sorted(boss_rooms, key=lambda r: (-room_tier[r], r))[0]

    best_util = sorted(room_blocks, key=lambda r: (-room_priority.get(r, 0), r))[0]
    if room_priority.get(best_util, 0) > 0:
        return best_util
    return sorted(room_blocks)[0]


__all__ = [
    "ask_lmstudio_choice",
    "choose_dungeon_room_by_priority",
    "list_lmstudio_models",
    "looks_like_dungeon_prompt",
    "resolve_chat_model",
]
