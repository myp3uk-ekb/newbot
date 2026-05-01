from __future__ import annotations
import asyncio
import time

import logging
import random
import re
import json
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError
from telethon.sessions import StringSession

try:
    import qrcode
except Exception:
    qrcode = None

from config import CFG
from storage import init_db, get_session, Event, ActionLog, is_paused, set_paused, get_kv, set_kv

# ---- KV shim (global) ----
class _KVShim:
    def get(self, k: str, default: str = "") -> str:
        v = get_kv(k, None)
        return v if v is not None else (default if default is not None else "")

    def set(self, k: str, v: str) -> None:
        set_kv(k, str(v))

kv = _KVShim()
# --------------------------
from game_parser import parse_message, is_bite_text, is_result_text, BITE_TRIGGERS, RESULT_TRIGGERS
from strategy import Profile, choose_target
from actions import click_button, click_button_contains
from dungeon_lm import ask_lmstudio_choice, choose_dungeon_room_by_priority, looks_like_dungeon_prompt


_FAQ_TEXT = 'FAQ — как пользоваться автопилотом\n\n• Запуск:\n  1) Заполни API_ID / API_HASH и прочие параметры в .env / config.py\n  2) Установи зависимости: pip install -r requirements.txt\n  3) Запусти: python main.py\n  4) Авторизуйся (код из Telegram)\n\n• Где писать команды управления:\n  Команды /pause /resume /status /help /version вводи ТОЛЬКО в «Избранном» (Saved Messages).\n  В игровой чат бот ничего управляющего не пишет.\n\n• Основные команды:\n  /pause      — пауза (рыбалка может продолжать работать, если включена)\n  /resume     — продолжить\n  /status     — текущие режимы/паузы\n  /help       — этот FAQ\n  /version    — версия сборки\n  /fishtriggers — показать активные рыболовные триггеры\n  /party on|off — включить/выключить party-режим\n  /partyhp 60 — порог HP для лечения в party-режиме\n  /blood hyst 60 95 — пороги гистерезиса blood-режима (в %)\n\n• HP-пауза:\n  Отправь в игру «хп». Бот распарсит ответ (💚: X/Y и “До полного восстановления …”) и поставит паузу на восстановление.\n\n• “Человеческие задержки”:\n  Действия (клики/экип) выполняются с рандомными паузами, чтобы меньше походить на макрос и не ломаться от лагов UI.\n\nЕсли что-то зависло:\n  1) /pause\n  2) проверь, что в игре актуальные кнопки\n  3) /resume\n  4) при необходимости перезапусти скрипт и смотри логи\n'

# ----------------- SET SYSTEM -----------------
# We store sets in KV as JSON: key "set:<name>" -> {"slots": {"a1": "item name", ...}, "priority": {"a1": 100, ...}}
# Slot ids: h,b,r,l,a1,a2,a3 (as in /character output)

SLOT_LABELS = {
    "h": "Голова",
    "b": "Тело",
    "r": "Правая лапа",
    "l": "Левая лапа",
    "a1": "Аксессуар 1",
    "a2": "Аксессуар 2",
    "a3": "Аксессуар 3",
}

RE_SLOT_LINE = re.compile(r"^/i_(h|b|r|l|a1|a2|a3)\s+[^:]+:\s+(.+)$", re.MULTILINE)
RE_HP = re.compile(r"^💚:\s*(\d+)\s*/\s*(\d+)", re.MULTILINE)
def parse_character(text: str) -> dict:
    """Parse /inventory or character-like dump. Returns dict with hp_cur, hp_max, slots, backpack_lines."""
    if not text:
        return {}
    m = RE_HP.search(text)
    hp_cur = hp_max = None
    if m:
        try:
            hp_cur = int(m.group(1))
            hp_max = int(m.group(2))
        except Exception:
            pass
    slots = {}
    for line in text.splitlines():
        line=line.strip()
        if not line.startswith("/i_"):
            continue
        # keep full line for later use
        parts=line.split(maxsplit=1)
        if not parts:
            continue
        slot=parts[0]  # /i_37
        rest=parts[1] if len(parts)>1 else ""
        slots[slot]=rest
    # also parse equipped slots lines like "/i_a1 Аксессуар 1: 📿⁵ Дубовая удочка 59/60"
    equipped={}
    for key in ["/i_a1","/i_a2","/i_a3","/i_r","/i_l","/i_p","/i_h","/i_b"]:
        mm=re.search(re.escape(key)+r"\s+[^:]+:\s*(.+)", text)
        if mm:
            equipped[key]=mm.group(1).strip()
    return {"hp_cur": hp_cur, "hp_max": hp_max, "slots": slots, "equipped": equipped, "raw": text}
RE_BACKPACK_ITEM = re.compile(r"^(/i_\d+)\s+(.+)$", re.MULTILINE)
RE_DUR = re.compile(r"(\d+)\s*/\s*(\d+)")
RE_SLOT_INLINE = re.compile(r"/i_(h|b|r|l|a1|a2|a3)\s+[^:]+:\s*(.+?)(?=\s+/i_(?:h|b|r|l|a1|a2|a3|\d+)\b|$)", re.IGNORECASE | re.DOTALL)
RE_BACKPACK_INLINE = re.compile(r"(/i_\d+)\s+(.+?)(?=\s+/i_(?:h|b|r|l|a1|a2|a3|\d+)\b|$)", re.IGNORECASE | re.DOTALL)
RE_CHARACTER_RACE_LINE = re.compile(r"^\s*[^:\n]{1,64}\s*\[(?:\d+)\]", re.MULTILINE)


def parse_hp_from_text(text: str) -> tuple[int | None, int | None]:
    """Extract current and max HP from any message containing the 💚 line."""
    m = RE_HP.search(text or "")
    if not m:
        return (None, None)
    return (int(m.group(1)), int(m.group(2)))


def parse_hp_any(text: str) -> tuple[int | None, int | None]:
    """Backward-compatible HP parser used by runtime handlers.

    Historically runtime code called ``parse_hp_any`` while parser helpers used
    ``parse_hp_from_text``. Keep this alias so message handlers never crash on
    missing symbol and HP snapshots continue to update.
    """
    return parse_hp_from_text(text)


def _parse_race_from_character_text(text: str) -> str | None:
    t = _normalize_ru(text or "")
    # Prefer explicit race cues from character header/effects text.
    if any(k in t for k in ("рысь", "бастет", "для рыс")):
        return "lynx"
    if any(k in t for k in ("енот", "тануки", "для енот")):
        return "raccoon"
    if any(k in t for k in ("лис", "лиса", "инари", "для лис")):
        return "fox"
    return None


def _learn_dungeon_race_from_character(text: str) -> None:
    """Update persisted dungeon race from /character dump when possible."""
    if not text:
        return
    low = _normalize_ru(text)
    # Accept both classic /character dumps and compact variants where only
    # the header with level is visible.
    if ("боевой рейтинг" not in low) and ("временные эффекты" not in low) and ("[" not in text):
        return
    race = _parse_race_from_character_text(text)
    if race not in ("fox", "raccoon", "lynx"):
        return
    prev = (get_kv("dungeon_race") or "").strip().lower()
    if prev != race:
        _kv_set("dungeon_race", race)
        log.info("🧬 CHARACTER: определена раса '%s', сохраняю для алтарей (prev=%s)", race, prev or "<empty>")
    else:
        log.info("🧬 CHARACTER: раса подтверждена '%s' (без изменений)", race)

def _clean_item_name(s: str) -> str:
    # drop durability " 59/60" and bracket tiers "[5]" at end if present
    t = (s or "").strip()
    t = re.sub(r"\s+\d+\s*/\s*\d+\s*$", "", t)
    return t.strip()

def _parse_character(text: str):
    slots = {}
    for m in RE_SLOT_LINE.finditer(text):
        sid, item = m.group(1), m.group(2)
        slots[sid] = item.strip()
    # Fallback: some inventory dumps arrive as one long line, not multiline.
    if not slots:
        for m in RE_SLOT_INLINE.finditer(text or ""):
            sid, item = m.group(1), m.group(2)
            slots[sid.lower()] = item.strip()

    hp = None
    mhp = None
    m = RE_HP.search(text)
    if m:
        hp, mhp = int(m.group(1)), int(m.group(2))

    backpack = []
    # backpack lines are like "/i_41 📿⁵ Дубовая удочка 59/60" or with (count)
    for m in RE_BACKPACK_ITEM.finditer(text):
        cmd, rest = m.group(1), m.group(2).strip()
        backpack.append((cmd, rest))
    # Fallback for single-line /inventory dumps.
    if not backpack:
        for m in RE_BACKPACK_INLINE.finditer(text or ""):
            cmd, rest = m.group(1), m.group(2).strip()
            backpack.append((cmd, rest))

    return {"slots": slots, "hp": hp, "mhp": mhp, "backpack": backpack}

parse_character = _parse_character  # backward-compat alias

def _best_rod(backpack: list[tuple[str,str]]):

    # Prefer the most worn usable rod first to burn through old rods sooner.
    # Only rods with durability > 0 are considered. We compare by:
    #   1) lowest current durability
    #   2) then lowest max durability

    best = None
    for cmd, rest in backpack:
        if "удочк" not in rest.lower():
            continue
        dm = RE_DUR.search(rest)
        if not dm:
            continue
        cur, mx = int(dm.group(1)), int(dm.group(2))
        if cur <= 0:
            continue
        cand = (cur, mx, cmd, rest)
        if best is None or cand[:2] < best[:2]:
            best = cand
    if not best:
        return None
    cur, mx, cmd, rest = best
    return {"cmd": cmd, "label": rest, "cur": cur, "max": mx}

def _find_item_cmd(backpack: list[tuple[str,str]], want: str) -> str | None:
    if not want:
        return None
    w = _clean_item_name(want).lower()
    for cmd, rest in backpack:
        if _clean_item_name(rest).lower() == w:
            return cmd
    # fallback substring
    for cmd, rest in backpack:
        if w and w in _clean_item_name(rest).lower():
            return cmd
    return None
_FETCH_CHAR_LOCK = asyncio.Lock()
_LAST_FETCH_CHAR_TS = 0.0
_FETCH_CHAR_MIN_INTERVAL = 2.0  # sec

def _load_set(name: str) -> dict | None:
    raw = get_kv(f"set:{name}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def _save_set(name: str, payload: dict):
    _kv_set(f"set:{name}", json.dumps(payload, ensure_ascii=False))

# ----------------- /SET SYSTEM -----------------

async def _get_recent_bot_message_with_buttons(client, chat, *, limit: int = 8):
    """Fetch recent messages and return the newest one that has buttons."""
    msgs = await client.get_messages(chat, limit=limit)
    for m in msgs:
        try:
            if m.buttons:
                return m
        except Exception:
            continue
    return None


def _norm_btn_label(v: str) -> str:
    s = " ".join((v or "").strip().lower().split())
    # UI often prefixes actionable buttons with emoji/symbols like "🔼В слот 1".
    # For deterministic matching we normalize such prefixes away.
    s = re.sub(r"^[^a-zа-яё0-9]+", "", s, flags=re.IGNORECASE)
    return s

async def _equip_item_to_slot(client, chat, item_cmd: str, slot_key: str) -> bool:
    """Open an item card by its /i_x command and press a slot button.
    Supports accessory slots a1/a2/a3 reliably (rod / torch swaps).
    """
    slot_key = (slot_key or "").lower().strip()
    if slot_key in ("a1", "acc1", "accessory1", "slot1"):
        btn_text = "В слот 1"
    elif slot_key in ("a2", "acc2", "accessory2", "slot2"):
        btn_text = "В слот 2"
    elif slot_key in ("a3", "acc3", "accessory3", "slot3"):
        btn_text = "В слот 3"
    else:
        btn_text = "Надеть"

    def _norm_btn_text(v: str) -> str:
        return _norm_btn_label(v)


    # Human-like pacing: opening an item card and equipping is a multi-step UI action.
    # IMPORTANT: for rods we intentionally keep delays longer (users reported "слишком быстро")
    # to look less "ботово" and reduce accidental misclicks when the UI is still updating.
    await _human_sleep(kind="inventory", lo=1.8, hi=4.0, note=f"equip: open {item_cmd}")
    await client.send_message(chat, item_cmd)
    await _human_sleep(kind="inventory", lo=1.2, hi=3.0, note=f"equip: wait card {item_cmd}")

    m = await _get_recent_bot_message_with_buttons(client, chat)
    if not m:
        log.warning("🧰 equip: не нашёл сообщение с кнопками для %s", item_cmd)
        return False

    # For accessory slots use strict exact-text match to avoid accidental clicks on
    # 'Надеть' or a wrong slot caused by substring matching.


    target = _norm_btn_text(btn_text)
    pos = None
    actual = ""
    if getattr(m, "buttons", None):
        for r, row in enumerate(m.buttons):
            for c, btn in enumerate(row):
                t = (getattr(btn, "text", "") or "").strip()

                if _norm_btn_text(t) == target:

                    pos = (r, c)
                    actual = t
                    break
            if pos is not None:
                break

    if pos is None:
        log.warning("🧰 equip: точная кнопка '%s' не найдена для %s (fallback disabled)", btn_text, item_cmd)
        return False

    # Before clicking on the item card, add a small jitter.
    await _human_sleep(kind="inventory", lo=1.0, hi=2.6, note=f"equip: click '{btn_text}'")
    log.info("🧰 equip: жму точно '%s' (btn='%s', pos=%s)", btn_text, actual, pos)
    ok = await click_button(client, m, pos=pos)
    if ok:
        await _human_sleep(kind="inventory", lo=0.9, hi=2.2, note="equip: after")
    else:
        log.warning("🧰 equip: клик по '%s' не сработал для %s", btn_text, item_cmd)
    return bool(ok)


def _norm_item_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\[\]{}()<>]", " ", s)
    s = re.sub(r"[^0-9a-zа-яё\s]+", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def _send_set_command(client: TelegramClient, set_num: int) -> None:
    """Send /e_<N> set command with a small human-like delay."""
    try:
        n = int(set_num)
    except Exception:
        return
    if n < 1 or n > 4:
        return
    # Deduplicate rapid repeated set-switches (e.g. same logic triggered on
    # both NewMessage and MessageEdited for the same screen update).
    # This avoids spam like "Набор уже используется." while keeping behaviour.
    try:
        now = _now_ts()
        last_n = int((_kv_get("last_set_num", "0") or "0").strip() or 0)
        last_ts = float((_kv_get("last_set_ts", "0") or "0").strip() or 0.0)
        if last_n == n and (now - last_ts) < 2.2:
            _dbg_log(f"set e{n}: skip duplicate ({now - last_ts:.2f}s)")
            return
    except Exception:
        pass
    await _human_sleep(kind="inventory", lo=0.7, hi=1.8, note=f"set e{n}")
    await client.send_message(CFG.game_chat, f"/e_{n}")
    try:
        _kv_set("last_set_num", str(n))
        _kv_set("last_set_ts", f"{_now_ts():.3f}")
    except Exception:
        pass


def _find_backpack_item_cmd(backpack: list[tuple[str, str]], patterns: list[str]) -> str | None:
    pats = [_norm_item_name(p) for p in (patterns or []) if p]
    if not pats:
        return None
    for cmd, line in (backpack or []):
        n = _norm_item_name(line)
        if any(p in n for p in pats):
            return cmd
    return None


def _looks_like_effect_expired(text: str) -> bool:
    low = _normalize_ru(text or "")
    if not low:
        return False
    has_end = any(k in low for k in ("закончил", "истек", "истёк", "рассеял", "пропал", "закончился", "спал"))
    if not has_end:
        return False
    watched = ("карас", "форел", "лосос", "кожа", "волк", "медвед", "дракон", "титан")
    return any(w in low for w in watched)

def _detect_dungeon_key_target(text: str) -> tuple[str, str | None] | None:
    """Detect next-dungeon target and optional tier (I..V) from key text."""
    low = _normalize_ru(text or "")
    if ("ключ" not in low) and ("ключи" not in low):
        return None
    tier = None
    m = re.search(r"\b(i|ii|iii|iv|v)\b", low, flags=re.IGNORECASE)
    if m:
        tier = m.group(1).upper()
    if ("шип" in low) or ("сток" in low):
        return ("spike", tier)   # Катакомбы Шипов
    if "ноч" in low:
        return ("night", tier)   # Темнейшая Ночи
    return None


async def _use_preferred_dungeon_buffs(client: TelegramClient, *, reason: str, force: bool = False) -> bool:
    """Use preferred dungeon/party consumables from inventory.

    Priority requested by user:
      fish: huge crucian/huge trout/huge salmon
      utility: wealth fruit
      armor: titanium skin > iron skin
      power: dragon > bear > wolf
    """
    now = time.time()
    # Buff consumables are not usable in active combat screens.
    # Also avoid opening inventory while forest/battle loops are in control.
    ui_stage = (get_kv("last_stage", "") or "").strip().lower()
    if ui_stage in ("battle", "forest"):
        log.info("🧪 buff-use skipped (%s): ui_stage=%s", reason, ui_stage or "?")
        return False

    if not force:
        last = float(get_kv("dungeon_buffs_last_ts", "0") or 0.0)
        if (now - last) < 25.0:
            return False

    snap = await _fetch_character(client)
    if not snap:
        return False
    backpack = snap.get("backpack", []) or []
    if not backpack:
        return False

    effects_raw = await _fetch_character_effects_raw(client)
    effects_state = _parse_effects_state(effects_raw or "")

    # If negative effects are active, cleanse first to avoid wasting buff consumables.
    if effects_state["has_negative"]:
        cleaned = await _try_use_cleansing_potion(client, backpack=backpack)
        if cleaned:
            # Re-fetch snapshot so backpack commands stay fresh after using an item.
            snap = await _fetch_character(client)
            if not snap:
                return True
            backpack = snap.get("backpack", []) or []

    # Top-up buffs to target window, accounting for already active remaining time.
    spec = (
        # group, patterns, cooldown, target_minutes, per_item_minutes
        ("wealth", ["фрукт богатства"], 90 * 60, 120, 120),
        ("vitality", ["огромный карась"], 20 * 60, 180, 30),
        ("combat_xp", ["огромная форель"], 20 * 60, 180, 30),
        ("regen", ["огромный лосось"], 20 * 60, 180, 30),
        ("armor", ["титановой кожи", "железной кожи"], 60 * 60, 180, 90),
        ("power", ["силы дракона", "силы медведя", "силы волка"], 60 * 60, 180, 90),
    )

    plan: list[tuple[str, str, list[str], int, int]] = []
    for group, patterns, group_cd_sec, target_min, per_item_min in spec:
        rem_min = _effect_group_remaining_min(effects_state["active_norm"], group)
        need_min = max(0, int(target_min) - int(rem_min))
        qty_needed = (need_min + int(per_item_min) - 1) // int(per_item_min)
        if qty_needed <= 0:
            continue
        nxt = float(get_kv(f"dungeon_buff_next_ts:{group}", "0") or 0.0)
        if (not force) and now < nxt:
            continue
        cmd = _find_backpack_item_cmd(backpack, patterns)
        if cmd:
            plan.append((group, cmd, patterns, group_cd_sec, int(qty_needed)))

    if not plan:
        return False

    used = 0
    for group, item_cmd, patterns, group_cd_sec, qty_needed in plan:
        await _human_sleep(kind="inventory", lo=0.9, hi=1.9, note=f"buff {item_cmd}")
        # Fast path: use quantity command directly to avoid opening each card.
        # Example command accepted by the game: "Использовать 24 2".
        qty = max(1, int(qty_needed))
        m_id = re.search(r"/i_(\d+)\b", item_cmd or "")
        used_fast = False
        if m_id:
            fast_cmd = f"Использовать {m_id.group(1)} {qty}"
            try:
                await client.send_message(CFG.game_chat, fast_cmd)
                used_fast = True
            except Exception as e:
                log.warning("🧪 fast buff-use failed for %s via %r: %s", item_cmd, fast_cmd, e)
        if not used_fast:
            await client.send_message(CFG.game_chat, item_cmd)
            # Fallback for UIs that require opening card + clicking "Использовать".
            try:
                await asyncio.sleep(0.9)
                m = await _get_recent_bot_message_with_buttons(client, CFG.game_chat, limit=10)
                if m is not None:
                    await click_button_contains(client, m, ["использовать", "▶️ использовать", "▶ использовать"])
            except Exception as e:
                log.warning("🧪 buff-use click failed for %s: %s", item_cmd, e)
        used += 1
        _kv_set(f"dungeon_buff_next_ts:{group}", f"{(now + group_cd_sec):.3f}")

    _kv_set("dungeon_buffs_last_ts", f"{now:.3f}")
    log.info("🧪 DUNGEON/PARTY buffs applied (%s): %s item(s)", reason, used)
    return used > 0


def _can_apply_dungeon_buffs_now() -> bool:
    """Gate dungeon/party buff usage to active combat contexts only.

    Prevents wasting fish/consumables in hub/mail/inventory screens when effects
    expire outside a dungeon run.
    """
    now = time.time()
    run_until = float(get_kv("dungeon_run_until_ts", "0") or 0.0)
    if now < run_until:
        return True
    # IMPORTANT: party presence alone is not enough (can be idle in town).
    # For auto-reapply on "effect expired" we should be in dungeon runtime.
    return False


_FETCH_EFFECTS_LOCK = asyncio.Lock()
_LAST_FETCH_EFFECTS_TS = 0.0
_FETCH_EFFECTS_MIN_INTERVAL = 2.0


async def _fetch_character_effects_raw(client: TelegramClient, timeout: float = 15.0) -> str | None:
    """Request /character and return raw text when "Временные эффекты" block is visible."""
    timeout = float(timeout) if timeout is not None else 15.0
    global _LAST_FETCH_EFFECTS_TS
    async with _FETCH_EFFECTS_LOCK:
        now = time.time()
        delta = now - _LAST_FETCH_EFFECTS_TS
        if delta < _FETCH_EFFECTS_MIN_INTERVAL:
            await asyncio.sleep(_FETCH_EFFECTS_MIN_INTERVAL - delta)
        _LAST_FETCH_EFFECTS_TS = time.time()

        await asyncio.sleep(human_delay_cmd("inventory"))
        await client.send_message(CFG.game_chat, "/character")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        def _looks_like_character_dump(txt: str) -> bool:
            t = _normalize_ru(txt)
            return ("временные эффекты" in t) and ("/character" in t or "боевой рейтинг" in t)

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                fut = asyncio.get_running_loop().create_future()

                async def _tmp_handler(e):
                    if not fut.done():
                        fut.set_result(e)

                tmp_event = events.NewMessage(chats=CFG.game_chat)
                client.add_event_handler(_tmp_handler, tmp_event)
                try:
                    ev = await asyncio.wait_for(fut, timeout=remaining)
                finally:
                    client.remove_event_handler(_tmp_handler, tmp_event)
            except asyncio.TimeoutError:
                return None

            txt = (ev.raw_text or "").strip()
            if not txt:
                continue
            if not _looks_like_character_dump(txt):
                continue
            return txt


_NEGATIVE_EFFECT_MARKERS = (
    "-",
    "минус",
    "потеряшлив",
    "мягколап",
    "сляв",
    "спляв",
    "штраф",
)

_EFFECT_GROUP_MARKERS = {
    "wealth": ("бонус 🌙", "бонус луны", "лунные лепестки"),
    "vitality": ("живучесть", "🐋"),
    "combat_xp": ("боевой ⚜️", "боевой опыт", "🦈"),
    "regen": ("регенерация", "🐬"),
    "armor": ("броня", "кожа"),
    "power": ("атака",),
}


def _parse_effects_state(text: str) -> dict:
    """Parse /character temporary effects block."""
    low = _normalize_ru(text or "")
    active_norm: list[str] = []
    has_negative = False
    in_block = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        norm = _normalize_ru(line)
        if not line:
            if in_block:
                break
            continue
        if "временные эффекты" in norm:
            in_block = True
            continue
        if not in_block:
            continue
        active_norm.append(norm)
        if any(mark in norm for mark in _NEGATIVE_EFFECT_MARKERS):
            has_negative = True
    # Fallback for compact/single-line responses.
    if (not active_norm) and ("временные эффекты" in low):
        has_negative = any(mark in low for mark in _NEGATIVE_EFFECT_MARKERS)
    return {"active_norm": active_norm, "has_negative": has_negative}


def _effect_group_is_active(active_norm: list[str], group: str) -> bool:
    markers = _EFFECT_GROUP_MARKERS.get(group) or ()
    if not markers:
        return False
    return any(any(m in line for m in markers) for line in (active_norm or []))


def _parse_effect_line_remaining_min(norm_line: str) -> int:
    total = 0
    h = re.search(r"(\d+)\s*ч", norm_line or "")
    if h:
        total += int(h.group(1)) * 60
    m = re.search(r"(\d+)\s*мин", norm_line or "")
    if m:
        total += int(m.group(1))
    return total


def _effect_group_remaining_min(active_norm: list[str], group: str) -> int:
    markers = _EFFECT_GROUP_MARKERS.get(group) or ()
    if not markers:
        return 0
    best = 0
    for line in (active_norm or []):
        if any(mark in line for mark in markers):
            best = max(best, _parse_effect_line_remaining_min(line))
    return best


async def _try_use_cleansing_potion(client: TelegramClient, backpack: list[tuple[str, str]]) -> bool:
    cmd = _find_backpack_item_cmd(backpack or [], ["зелье очищения"])
    if not cmd:
        log.info("🧪 debuff found, but cleansing potion is missing in backpack")
        return False
    await _human_sleep(kind="inventory", lo=0.8, hi=1.7, note=f"cleanse {cmd}")
    await client.send_message(CFG.game_chat, cmd)
    try:
        await asyncio.sleep(0.9)
        m = await _get_recent_bot_message_with_buttons(client, CFG.game_chat, limit=10)
        if m is not None:
            await click_button_contains(client, m, ["использовать", "▶️ использовать", "▶ использовать"])
    except Exception as e:
        log.warning("🧪 cleanse click failed for %s: %s", cmd, e)
    log.info("🧪 debuffs detected: used cleansing potion")
    return True


async def _try_move_one_item_to_storage(client, chat, avoid_norms: set[str] | None = None) -> bool:
    """Try to free 1 inventory slot by moving a low-priority item to (market) storage.

    We keep it intentionally fuzzy: open some item card and press a button containing 'склад'/'рыноч' etc.
    Returns True if we believe a move happened.
    """
    avoid_norms = avoid_norms or set()

    snap = await _fetch_character(client)  # always uses CFG.game_chat
    if not snap:
        return False

    backpack = snap.get("backpack", []) or []
    # choose candidate: first item that isn't protected
    def _norm_line(line: str) -> str:
        return _norm_item_name(line)

    protected_kw = ("удочк", "наживк", "талисман", "амулет", "артефакт")
    cand_cmd = None
    cand_line = None
    for cmd, line in backpack:
        n = _norm_line(line)
        if not cmd:
            continue
        if any(k in n for k in protected_kw):
            continue
        if n in avoid_norms:
            continue
        cand_cmd, cand_line = cmd, line
        break

    if not cand_cmd:
        log.warning("🎒 инвентарь полон: не нашёл безопасный предмет для отправки на склад")
        return False

    log.info("🎒 инвентарь полон: пытаюсь освободить слот → %s", cand_line)
    await client.send_message(chat, cand_cmd)
    await asyncio.sleep(0.8)

    m = await _get_recent_bot_message_with_buttons(client, chat)
    if not m:
        return False

    # Prefer "склад" actions; avoid selling if possible.
    moved = False
    for key in ("склад", "рыноч", "хранилищ"):
        if await click_button_contains(client, m, key):
            moved = True
            break
    if not moved:
        # fallback: some UIs use "Отправить" / "Переместить"
        for key in ("отправ", "перемест"):
            if await click_button_contains(client, m, key):
                moved = True
                break

    if moved:
        await asyncio.sleep(0.8)
        # optimistic: the game often reports '... отправляется на рыночный склад'
        _kv_set("inventory_full", "0")
        log.info("🎒 освободил(возможно) 1 слот в рюкзаке (склад).")
        return True

    log.warning("🎒 инвентарь полон: не нашёл кнопки 'на склад' для %s", cand_cmd)
    return False


async def _ensure_inventory_space_for_swap(client, chat, avoid_norms: set[str] | None = None) -> bool:
    """If KV says inventory is full, attempt to free 1 slot so we can unequip/swap accessories."""
    if (get_kv("inventory_full") or "0") != "1":
        return True
    # Try a couple of attempts with different items.
    for _ in range(2):
        ok = await _try_move_one_item_to_storage(client, chat, avoid_norms=avoid_norms)
        if ok:
            return True
        await asyncio.sleep(0.6)
    return False


async def _apply_set(client, *args):
    """Apply a saved equipment set.

    Backward compatible call forms:
      - _apply_set(client, set_name)
      - _apply_set(client, chat, set_name)  # chat is ignored; we always use CFG.game_chat

    We only touch accessory slots a1/a2/a3.
    """
    if len(args) == 1:
        chat = CFG.game_chat
        set_name = args[0]
    elif len(args) >= 2:
        chat = CFG.game_chat  # ignore provided chat to avoid breaking timeouts
        set_name = args[1]
    else:
        raise TypeError('_apply_set requires set_name')

    payload = _load_set(set_name)
    if not payload:
        log.info("🎒 set '%s' не найден — пропускаю применение.", set_name)
        return

    try:
        current = await _fetch_character(client)
    except Exception as e:
        log.warning("🎒 set '%s': не удалось получить /inventory: %s", set_name, e)
        return
    if not current:
        log.warning("🎒 set '%s': /inventory не вернул снимок — пропускаю", set_name)
        return

    desired = (payload.get("slots") or {}) if isinstance(payload, dict) else {}
    backpack = current.get("backpack", []) or []

    # Build index: normalized name -> cmd
    idx = []
    for cmd, rest in backpack:
        if not cmd or not rest:
            continue
        idx.append((_norm_item_name(rest), cmd))

    # avoid moving these items away when freeing slots
    avoid_norms = set(_norm_item_name(v) for v in desired.values() if v)

    for slot_key in ("a1", "a2", "a3"):
        want_name = desired.get(slot_key)
        if not want_name:
            continue
        want_norm = _norm_item_name(want_name)

        cur_item = (current.get("slots", {}) or {}).get(slot_key) or ""
        cur_norm = _norm_item_name(cur_item)

        if cur_item and cur_norm == want_norm:
            continue

        chosen_cmd = None
        for nm_norm, cmd in idx:
            if want_norm and (want_norm in nm_norm or nm_norm in want_norm):
                chosen_cmd = cmd
                break

        if not chosen_cmd:
            log.info("🎒 set '%s': не нашёл в рюкзаке '%s' для %s", set_name, want_name, slot_key)
            continue

        # If inventory is full and we're replacing something already equipped (e.g. rod -> talisman),
        # the game may refuse the swap because it can't place the removed item into the backpack.
        if cur_item and (get_kv("inventory_full") or "0") == "1":
            log.info("🎒 set '%s': рюкзак полон — освобождаю место перед сменой %s", set_name, slot_key)
            ok_space = await _ensure_inventory_space_for_swap(client, chat, avoid_norms=avoid_norms)
            if not ok_space:
                log.warning("🎒 set '%s': не удалось освободить слот — пропускаю смену %s", set_name, slot_key)
                continue

        log.info("🎒 set '%s': экипирую '%s' -> %s", set_name, want_name, slot_key)
        await _equip_item_to_slot(client, chat, chosen_cmd, slot_key)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("autopilot")
logging.getLogger("telethon").setLevel(logging.INFO)


def set_pause_for_seconds(seconds: float):
    """Compatibility helper: avoid hard import dependency on storage.set_pause_for_seconds."""
    seconds = float(seconds)
    if seconds <= 0:
        return
    set_kv("paused_until_ts", str(time.time() + seconds))


class _MemStore:
    """Tiny in-memory store for transient flows (not persisted).
    Used for multi-step actions like equipping a fishing rod."""

    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v

    def delete(self, k):
        if k in self._d:
            del self._d[k]


STORAGE = _MemStore()


# ----------------- DEBUG (runtime toggles via /debug ...) -----------------
def _kv_get(key: str, default: str = "") -> str:
    try:
        return (get_kv(key, default) or default)
    except Exception:
        return default

def _dbg_flag(key: str, default: str = "0") -> bool:
    return (_kv_get(key, default).strip() == "1")

def dbg_enabled() -> bool:
    return _dbg_flag("debug_enabled", "0")

def _dbg_log(msg: str) -> None:
    # We intentionally log at INFO so it shows up without changing log level.
    if dbg_enabled():
        log.info(f"🐛 {msg}")

def _kv_set(key: str, val: str) -> None:
    """set_kv with optional debug logging."""
    try:
        if dbg_enabled() and _dbg_flag("debug_kv", "0"):
            old = _kv_get(key, "")
            if str(old) != str(val):
                log.info(f"🐛 kv: {key}={old!r} -> {val!r}")
    except Exception:
        pass
    try:
        set_kv(key, val)
    except Exception:
        pass




# ----------------- MODULE TOGGLES (persisted in KV) -----------------
# Stored as strings "1"/"0" in sqlite KV.
# Defaults are ON to keep existing behaviour unless user disables.

def _kv_bool(key: str, default: bool = True) -> bool:
    v = get_kv(key)
    if v is None:
        return default
    v = str(v).strip().lower()
    return v in ("1", "true", "yes", "on")


def _set_kv_bool(key: str, value: bool):
    _kv_set(key, "1" if value else "0")


def mod_forest_enabled() -> bool:
    return _kv_bool("mod_forest", True)


def mod_fishing_enabled() -> bool:
    return _kv_bool("mod_fishing", True)


def set_mod_fishing_enabled(v: bool):
    """Enable/disable fishing module."""
    _kv_set("mod_fishing", "1" if v else "0")


def _disable_fishing(reason: str = ""):
    """Disable fishing module and return control to forest safely.

    Used when we detect that fishing cannot continue (no rod/bait, etc.).
    """
    if reason:
        log.warning(f"🎣 Fishing disabled: {reason}")
    set_mod_fishing_enabled(False)
    # Stop any transient flows related to fishing
    try:
        STORAGE.delete("rod_flow")
    except Exception:
        pass
    _kv_set("active_mode", "forest")
    _kv_set("pending_mode", "")
    _kv_set("fish_stop_cast", "0")
    _kv_set("fish_stop_cast_since", "0")



def mod_golem_fight_enabled() -> bool:
    """If enabled: attack golems; if disabled: retreat from golems."""
    return _kv_bool("mod_golem_fight", False)


def set_mod_golem_fight_enabled(v: bool):
    _set_kv_bool("mod_golem_fight", v)


def mod_heal_enabled() -> bool:
    return _kv_bool("mod_heal", True)


def mod_work_enabled() -> bool:
    return _kv_bool("mod_work", True)


def mod_dungeon_enabled() -> bool:
    return _kv_bool("mod_dungeon", False)


def mod_dungeon_altar_touch_enabled() -> bool:
    # Safer default: OFF. Touching a чужой altar can waste its last charge.
    return _kv_bool("mod_dungeon_altar_touch", False)


def mod_dungeon_altar_1000_touch_enabled() -> bool:
    # Separate switch for "Алтарь Тысячелапого"
    return _kv_bool("mod_dungeon_altar_1000_touch", False)

def mod_dungeon_rubble_break_enabled() -> bool:
    # Break "Каменный завал" automatically ("Разобрать")
    return _kv_bool("mod_dungeon_rubble_break", True)

def mod_dungeon_grave_open_enabled() -> bool:
    # Open "Могила" automatically ("Вскрыть")
    return _kv_bool("mod_dungeon_grave_open", True)

def mod_dungeon_boarded_chop_enabled() -> bool:
    # Chop "Заколоченный проход" automatically ("Прорубить")
    return _kv_bool("mod_dungeon_boarded_chop", True)


def mod_hunter_enabled() -> bool:
    # Track "Странные следы" automatically ("Выследить").
    # When disabled, wait 10s and continue with "Вперёд".
    return _kv_bool("mod_hunter", False)


def _lmstudio_enabled() -> bool:
    return bool(getattr(CFG, "lmstudio_base_url", "").strip()) and bool(getattr(CFG, "lmstudio_model", "").strip())


def _is_dungeon_runtime_context(text: str, buttons: list[str]) -> bool:
    """Return True when current screen should be treated as active dungeon flow.

    This is intentionally stricter than a plain "contains подзем" check so
    informational text does not unlock combat automation.
    """
    low = (text or "").lower().replace("ё", "е")
    btns = [((b or "").lower().replace("ё", "е")) for b in (buttons or [])]

    explicit_dungeon_text = any(
        marker in low
        for marker in (
            "ты отправляешься в подземелье",
            "ты спускаешься в темноту",
            "ворота захлопываются за спиной",
            "под лапами хлюпает",
            "взломать замок",
        )
    )
    nav_or_action_btns = sum(
        1
        for b in btns
        if any(k in b for k in ("осмотреть", "вперед", "вперёд", "налево", "направо", "отступ", "атак", "взлом"))
    )

    return explicit_dungeon_text or looks_like_dungeon_prompt(text, buttons) or nav_or_action_btns >= 2


async def _handle_dungeon_with_lm(client: TelegramClient, msg: Message, state) -> bool:
    if (not mod_dungeon_enabled()) or (not _lmstudio_enabled()):
        return False

    labels = []
    for c in (state.buttons or []):
        t = (c.btn_text or c.name or "").strip()
        if t:
            labels.append(t)
    if len(labels) < 2:
        return False

    text = msg.message or ""
    if not looks_like_dungeon_prompt(text, labels):
        return False

    try:
        choice = await asyncio.to_thread(
            ask_lmstudio_choice,
            text=text,
            buttons=labels,
            base_url=getattr(CFG, "lmstudio_base_url", "http://127.0.0.1:1234/v1"),
            model=getattr(CFG, "lmstudio_model", "local-model"),
            timeout_sec=float(getattr(CFG, "lmstudio_timeout_sec", 20.0)),
            temperature=float(getattr(CFG, "lmstudio_temperature", 0.1)),
            max_tokens=int(getattr(CFG, "lmstudio_max_tokens", 80)),
        )
    except Exception as e:
        log.warning(f"🕸 LM Studio недоступен: {e}")
        return False

    if not choice:
        return False

    def _n(v: str) -> str:
        return _norm_btn_label(v)

    selected = None
    choice_n = _n(choice)
    for c in state.buttons:
        lbl = (c.btn_text or c.name or "").strip()
        if _n(lbl) == choice_n:
            selected = c
            break
    if selected is None:
        # fallback contains match when model trimmed emoji/prefix
        for c in state.buttons:
            lbl = (c.btn_text or c.name or "").strip()
            nl = _n(lbl)
            if choice_n and (choice_n in nl or nl in choice_n):
                selected = c
                break

    if selected is None:
        log.warning(f"🕸 LM Studio предложил неизвестную кнопку: {choice!r}")
        return False

    try:
        await asyncio.sleep(human_delay_combat("battle"))
        if selected.pos is not None:
            await click_button(client, msg, pos=selected.pos)
        elif selected.btn_text:
            await click_button(client, msg, text=selected.btn_text)
        else:
            await click_button(client, msg, text=selected.name)
        log.info(f"🕸 Dungeon LM: выбрал '{selected.btn_text or selected.name}'")
        return True
    except Exception as e:
        log.warning(f"🕸 Dungeon LM click failed: {e}")
        return False


def mod_pet_enabled() -> bool:
    """If enabled: periodically go to Home and pet all animals.

    Scheduling is interval-based: after a successful run we schedule the next one
    in [PET_INTERVAL_MIN_HOURS .. PET_INTERVAL_MAX_HOURS].
    """
    return _kv_bool("mod_pet", bool(getattr(CFG, "mod_pet_enabled", False)))


def mod_thief_enabled() -> bool:
    """If enabled: auto-handle the 'воришка' mini-event after battles in forest.

    The flow is a 2-step mini-quest:
      1) "Куда бежать?"    -> Налево / Прямо / Направо
      2) "Где искать ..."  -> В кустах / В ветвях / В траве

    We parse hints from the post-battle text (e.g. "устремился налево и скрылся в ветвях")
    and click the correct options automatically to receive the bonus.
    """
    return _kv_bool("mod_thief", True)


def mod_party_enabled() -> bool:
    # Party is special: it can temporarily override forest/fishing/pauses.
    return _kv_bool("mod_party", getattr(CFG, "mod_party_enabled", False))


def party_driver_mode() -> str:
    """Party role mode: on(driver) / off(passive) / auto."""
    raw = (get_kv("party_driver_mode") or "").strip().lower()
    if raw in ("on", "off", "auto"):
        return raw
    return "auto"


def _set_party_driver_mode(mode: str) -> None:
    m = (mode or "").strip().lower()
    if m not in ("on", "off", "auto"):
        return
    _kv_set("party_driver_mode", m)


def is_party_driver() -> bool:
    """Whether this account should actively drive party dungeon navigation."""
    mode = party_driver_mode()
    if mode == "on":
        return True
    if mode == "off":
        return False
    # auto mode
    return _kv_bool("party_is_leader", False)


def _extract_game_name_from_profileish_text(text: str) -> str | None:
    # Example: "ТриТопора [69] 💚: 2540/2540 ..."
    m = re.search(r"^\s*([^\n\[]+?)\s*\[\d+\]\s*💚\s*:", (text or ""), re.S)
    if not m:
        return None
    name = (m.group(1) or "").strip()
    return name or None


def _party_extract_leader_name(text: str) -> str | None:
    m = re.search(r"лидер:\s*([^\[\n]+?)\s*\[\d+\]", (text or ""), re.I)
    if not m:
        return None
    name = (m.group(1) or "").strip()
    return name or None


def _normalize_party_name(name: str) -> str:
    n = (name or "").strip()
    # Some renders can wrap names with markdown/backticks or emojis.
    n = n.replace("`", "").replace("*", "").replace("_", "")
    n = re.sub(r"^[^\wа-яё]+", "", n, flags=re.I)
    n = re.sub(r"[^\wа-яё]+$", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip()
    return _normalize_ru(n)


def _maybe_refresh_party_identity_from_text(text: str) -> None:
    txt = (text or "").strip()
    if not txt:
        return
    game_name = _extract_game_name_from_profileish_text(txt)
    if game_name:
        _kv_set("party_self_name", game_name)

    low = txt.lower()
    if not (low.startswith("группа (id") or ("лидер:" in low and "участники:" in low)):
        return
    leader_name = _party_extract_leader_name(txt)
    if leader_name:
        _kv_set("party_leader_name", leader_name)
    self_name = (get_kv("party_self_name") or "").strip()
    if self_name and leader_name:
        is_leader = (_normalize_party_name(self_name) == _normalize_party_name(leader_name))
        _kv_set("party_is_leader", "1" if is_leader else "0")


def is_party_active() -> bool:
    # Guard against stale sticky party state: if we haven't seen any party
    # lifecycle/screen signal for a long time, auto-clear the flag.
    active = _kv_bool("party_active", False)
    if not active:
        return False
    try:
        last_seen = float(get_kv("party_last_seen_ts", "0") or 0.0)
    except Exception:
        last_seen = 0.0
    ttl = float(getattr(CFG, "party_active_ttl_sec", 15 * 60))
    now = time.time()
    if last_seen > 0 and (now - last_seen) <= ttl:
        return True

    log.info("🤝 PARTY: stale active flag (last_seen=%.0fs ago) → auto-clear", (now - last_seen) if last_seen else -1)
    set_kv("party_active", "0")
    set_kv("party_snapshot_done", "0")
    return False


def is_pet_flow_active() -> bool:
    """Pet flow is allowed even when /forest off or when HP-cooldown pauses combat."""
    return get_kv("active_mode", "") == "pet" or get_kv("human_ctx", "") == "pet"


def set_party_active(v: bool):
    set_kv("party_active", "1" if v else "0")
    set_kv("party_last_seen_ts", str(time.time()))


def _party_snapshot_modes():
    # Remember what the user had enabled before we join a party,
    # so we can restore it when the party ends.
    set_kv("party_prev_forest", "1" if mod_forest_enabled() else "0")
    set_kv("party_prev_fishing", "1" if mod_fishing_enabled() else "0")
    set_kv("party_prev_paused", "1" if is_paused() else "0")
    # We also remember whether "health pause" was running (deadline is stored separately).
    set_kv("party_prev_health_cd_deadline", get_kv("health_pause_until_ts", "0") or "0")


def _party_enter_modes(reason: str = ""):
    """Apply temporary mode overrides while party is active.

    Current policy: pause fishing casts immediately while we're in a party.
    Previous toggle values are restored by _party_restore_modes().
    """
    if mod_fishing_enabled():
        set_mod_fishing_enabled(False)
        log.info("🤝 PARTY: %s → временно выключаю fishing", reason or "enter")


def _party_restore_modes(reason: str = ""):
    prev_forest = get_kv("party_prev_forest", "0") == "1"
    prev_fishing = get_kv("party_prev_fishing", "0") == "1"
    prev_paused = get_kv("party_prev_paused", "0") == "1"

    set_party_active(False)

    # Restore toggles (do not change health pause timer; that's managed by health module).
    set_kv("mod_forest", "1" if prev_forest else "0")
    set_kv("mod_fishing", "1" if prev_fishing else "0")
    set_paused(prev_paused)

    log.info("🤝 PARTY: завершено%s → восстановил режимы: forest=%s fishing=%s paused=%s",
             f" ({reason})" if reason else "",
             "on" if prev_forest else "off",
             "on" if prev_fishing else "off",
             "on" if prev_paused else "off")



def _pet_interval_range_sec() -> tuple[float, float]:
    mn_h = float(getattr(CFG, "pet_interval_min_hours", 1.0) or 1.0)
    mx_h = float(getattr(CFG, "pet_interval_max_hours", 2.0) or 2.0)
    if mx_h < mn_h:
        mn_h, mx_h = mx_h, mn_h
    mn = max(60.0, mn_h * 3600.0)  # at least 1 minute
    mx = max(mn, mx_h * 3600.0)
    return (mn, mx)


def _pet_schedule_next(base_ts: float | None = None) -> float:
    """Pick next due timestamp and persist it to KV."""
    now = float(base_ts) if base_ts is not None else _now_ts()
    mn, mx = _pet_interval_range_sec()
    delay = random.uniform(mn, mx)
    nxt = now + delay
    _kv_set("pet_next_due_ts", f"{nxt:.3f}")
    _kv_set("pet_next_delay_sec", str(int(delay)))
    return nxt


def _pet_schedule_next_range_hours(base_ts: float | None, mn_h: float, mx_h: float) -> float:
    """Schedule next pet run using an explicit range (hours).

    Used for special cases like: after manual '/pet on' we want a shorter human-like
    cool-down (e.g. 1-2h) regardless of global CFG interval.
    """
    now = float(base_ts) if base_ts is not None else _now_ts()
    a = float(mn_h)
    b = float(mx_h)
    if b < a:
        a, b = b, a
    a = max(1.0 / 60.0, a)  # at least 1 minute
    delay = random.uniform(a * 3600.0, b * 3600.0)
    nxt = now + delay
    _kv_set("pet_next_due_ts", f"{nxt:.3f}")
    _kv_set("pet_next_delay_sec", str(int(delay)))
    return nxt


def _pet_due_now() -> bool:
    # PET runs are allowed even when automation is paused (manual or timed).
    # This avoids missing scheduled pet runs just because the user paused forest/fishing.
    if not mod_pet_enabled():
        return False

    # If we previously detected that the hero is currently "в походе" and terrarium
    # interaction is unavailable, we back off for several hours to avoid flood waits.
    try:
        blocked_until = float(get_kv("pet_blocked_until_ts") or "0")
    except Exception:
        blocked_until = 0.0

    now = _now_ts()
    if blocked_until and now < blocked_until:
        return False

    try:
        nxt = float(get_kv("pet_next_due_ts") or "0")
    except Exception:
        nxt = 0.0

    # First enable / after DB wipe: initialize schedule, but do not run immediately.
    if nxt <= 0:
        _pet_schedule_next(now)
        return False

    return now >= nxt

def heal_target_pct() -> float:
    """Target HP fraction (0..1) we try to maintain when heal module is enabled."""
    raw = get_kv("heal_target_pct")
    if raw is None:
        return float(getattr(CFG, "heal_target_pct_default", 0.99))
    try:
        v = float(str(raw).replace(",", ".").strip())
        if v > 1.5:  # user might store percent like 99
            v = v / 100.0
        return max(0.1, min(1.0, v))
    except Exception:
        return float(getattr(CFG, "heal_target_pct_default", 0.99))


def blood_enabled() -> bool:
    return _kv_bool("mod_blood", False)


def blood_hp_low() -> int:
    v = get_kv("blood_hp_low")
    try:
        return int(v) if v is not None else int(getattr(CFG, "blood_hp_low", 60))
    except Exception:
        return 60


def blood_hp_high() -> int:
    v = get_kv("blood_hp_high")
    try:
        return int(v) if v is not None else int(getattr(CFG, "blood_hp_high", 95))
    except Exception:
        return 95


def blood_level() -> int:
    v = get_kv("blood_level")
    try:
        lvl = int(v) if v is not None else int(getattr(CFG, "blood_level", 1))
    except Exception:
        lvl = 1
    return max(1, min(10, lvl))


def _update_hp_snapshot_from_text(txt: str) -> None:
    cur, mx = parse_hp_any(txt or "")
    if cur is None or mx is None or mx <= 0:
        return
    pct = int(round((cur * 100.0) / float(mx)))
    _kv_set("hp_cur", str(cur))
    _kv_set("hp_max", str(mx))
    _kv_set("hp_pct", str(max(0, min(100, pct))))


def _apply_blood_level_routing() -> None:
    """Set effective forest level based on optional blood-heal hysteresis (60/95 by default)."""
    base_lvl = get_kv("forest_level") or "1"
    if not blood_enabled():
        _kv_set("forest_level_effective", base_lvl)
        _kv_set("blood_active", "0")
        return

    hp_raw = get_kv("hp_pct")
    hp: int | None = None
    if hp_raw is not None and str(hp_raw).strip() != "":
        try:
            hp = int(str(hp_raw).strip())
        except Exception:
            hp = None

    low = max(1, min(99, blood_hp_low()))
    high = max(low, min(100, blood_hp_high()))
    prev_active = (get_kv("blood_active") or "0") == "1"
    active = prev_active

    # Unknown HP must NOT force blood mode, otherwise effective level can get
    # stuck at blood_level right after start/DB reset before first HP snapshot.
    if hp is not None:
        # Blood-heal is an additional contour; hard HP pause (<50) still has higher priority.
        if hp < low:
            if not active:
                log.info(f"🩸 Blood mode: HP {hp}% < {low}% → bloodLevel {blood_level()}")
            active = True
        elif hp >= high:
            if active:
                log.info(f"🩸 Blood mode: HP {hp}% >= {high}% → return LVL {base_lvl}")
            active = False

    _kv_set("blood_active", "1" if active else "0")
    _kv_set("forest_level_effective", str(blood_level() if active else base_lvl))

    # When blood mode activates, force-open forest selector once so that the
    # newly effective tier is applied immediately instead of continuing fights
    # from the currently opened enemy list.
    if active and (not prev_active):
        _kv_set("blood_force_forest", "1")


def human_delay_combat(kind: str) -> float:
    import random
    r = random.random()

    # Base "как человек" (вариант C)
    if kind == "forest":  # выбор уровня/локации
        if r < 0.70:
            return random.uniform(0.9, 1.8)
        if r < 0.95:
            return random.uniform(1.8, 2.8)
        return random.uniform(2.8, 3.8)

    if kind == "battle":  # выбор врага/действия
        if r < 0.70:
            return random.uniform(1.2, 2.4)
        if r < 0.95:
            return random.uniform(2.4, 3.6)
        return random.uniform(3.6, 4.6)

    if kind == "vylazka":  # кнопка "Вылазка" — чуть медленнее
        base = human_delay_combat("battle")
        return base + random.uniform(1.5, 2.5)  # +пара секунд

    return random.uniform(1.0, 2.0)


def human_delay_cmd(kind: str = "cmd") -> float:
    """Более "человеческая" задержка для текстовых команд и навигации.

    Используем для переключений режимов и запросов (/inventory, /character),
    чтобы команды не улетали слишком быстро.
    """
    k = (kind or "cmd").lower()
    r = random.random()

    # Переключение режимов (лес/рыбалка)
    if k in ("mode", "switch", "mode_switch"):
        if r < 0.70:
            return random.uniform(1.3, 2.6)
        if r < 0.95:
            return random.uniform(2.6, 4.2)
        return random.uniform(4.2, 6.0)

    # Инвентарь/персонаж/экипировка
    if k in ("inventory", "inv", "character", "gear", "equip"):
        if r < 0.75:
            return random.uniform(0.9, 1.8)
        if r < 0.95:
            return random.uniform(1.8, 3.0)
        return random.uniform(3.0, 4.8)

    # Дефолт
    return random.uniform(0.8, 2.2)


def human_delay_cmd(kind: str = "cmd") -> float:
    """Более "человеческая" задержка для текстовых команд и навигации.

    Нужна, чтобы переключения (лес/рыбалка/инвентарь) не выглядели как спам-бот.
    """
    k = (kind or "cmd").lower()
    r = random.random()

    # Переключение режимов
    if k in ("mode", "switch", "mode_switch"):
        if r < 0.70:
            return random.uniform(1.3, 2.6)
        if r < 0.95:
            return random.uniform(2.6, 4.2)
        return random.uniform(4.2, 6.5)

    # /inventory, /character, /i_* и похожие "меню" команды
    if k in ("inv", "inventory", "character", "menu", "gear", "equip"):
        if r < 0.80:
            return random.uniform(0.9, 1.8)
        if r < 0.97:
            return random.uniform(1.8, 3.0)
        return random.uniform(3.0, 4.8)

    # По умолчанию
    if r < 0.85:
        return random.uniform(0.8, 1.8)
    if r < 0.98:
        return random.uniform(1.8, 3.2)
    return random.uniform(3.2, 5.0)


def human_delay_cmd(kind: str = "cmd") -> float:
    """Более "человеческая" задержка для текстовых команд (переключение режимов, /inventory и т.п.)."""
    import random
    k = (kind or "cmd").lower()
    r = random.random()

    # Переключение режимов / выход-вход в локации
    if k in ("mode", "switch", "mode_switch"):
        if r < 0.70:
            return random.uniform(1.3, 2.6)
        if r < 0.95:
            return random.uniform(2.6, 3.8)
        return random.uniform(3.8, 5.2)

    # Команды меню ("Рыбалка", "Чаща" и т.п.)
    if k in ("menu", "forest", "fishing"):
        if r < 0.70:
            return random.uniform(0.9, 2.0)
        if r < 0.95:
            return random.uniform(2.0, 3.2)
        return random.uniform(3.2, 4.6)

    # Утилиты вроде /inventory — обычно быстрее
    if k in ("inventory", "util"):
        if r < 0.80:
            return random.uniform(0.6, 1.4)
        return random.uniform(1.4, 2.6)

    return random.uniform(0.8, 2.2)


async def _human_sleep(kind: str = "cmd", lo: float | None = None, hi: float | None = None, note: str | None = None):
    """Единая точка для "человеческих" задержек.

    Используется хендлерами (party/thief/другие), чтобы не забывать про delays.
    - Если lo/hi заданы — берём uniform(lo, hi)
    - Иначе — используем human_delay_cmd(kind)
    """
    import random
    d = random.uniform(lo, hi) if (lo is not None and hi is not None) else human_delay_cmd(kind)
    if note:
        log.info(f"🧠 {note}: жду {d:.2f}s перед действием")
    await asyncio.sleep(d)

LOSS_TRIGGERS = [
    "поражение",
    "ты проиграл",
    "вы проиграли",
    "проигрыш",
    "пал в бою",
    "пали в бою",
]

def _normalize_ru(s: str) -> str:
    return (s or "").lower().replace("ё", "е")

# Backward-compatible alias used by older handlers
def _normalize(s: str) -> str:
    return _normalize_ru(s)

def _looks_like_loss(text: str) -> bool:
    t = _normalize_ru(text)
    return any(x in t for x in LOSS_TRIGGERS)

def _loss_cd_remaining_sec() -> int:
    val = get_kv("loss_cd_until")
    if not val:
        return 0
    try:
        until = datetime.fromisoformat(val)
        left = (until - datetime.now()).total_seconds()
        return int(left) if left > 0 else 0
    except Exception:
        return 0

def _start_loss_cooldown_random():
    minutes = random.randint(CFG.health_pause_min, CFG.health_pause_max)
    until = datetime.now() + timedelta(minutes=minutes)
    _kv_set("loss_cd_until", until.isoformat())
    log.warning(f"💀 Проигрыш: ставлю паузу на {minutes} мин (до {until.strftime('%H:%M:%S')}).")

profile = Profile(mode=CFG.mode, blacklist=CFG.blacklist)
HP_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
HURRY_RE = re.compile(r"подожди\s+(\d+)\s+сек", re.I)

def _night_sleep_now() -> bool:
    h = datetime.now().hour
    f, t = CFG.sleep_night_from, CFG.sleep_night_to
    return (f <= t and f <= h < t) or (f > t and (h >= f or h < t))

def _health_cd_remaining_sec() -> int:
    val = get_kv("health_cd_until")
    if not val:
        return 0
    try:
        until = datetime.fromisoformat(val)
        left = (until - datetime.now()).total_seconds()
        return int(left) if left > 0 else 0
    except Exception:
        return 0


def _golem_cd_remaining_sec() -> int:
    val = get_kv("golem_cd_until")
    if not val:
        return 0
    try:
        until = datetime.fromisoformat(val)
        left = (until - datetime.now()).total_seconds()
        return int(left) if left > 0 else 0
    except Exception:
        return 0


def _start_golem_pause_minutes(min_m: int = 15, max_m: int = 25) -> int:
    """Set a hard pause window after meeting triple golems."""
    mins = random.randint(min_m, max_m)
    until = datetime.now() + timedelta(minutes=mins)
    _kv_set("golem_cd_until", until.isoformat(timespec="seconds"))
    log.warning(f"🪨 Пауза после големов на {mins} мин (до {until.strftime('%H:%M:%S')}).")
    return mins


def _golem_wave_active() -> bool:
    return (get_kv("golem_wave_active") or "0") == "1"


def _activate_golem_wave(reason: str = "") -> None:
    """Enable anti-wave mode and force forest level 1.

    If the wave flag is already active (e.g. from previous cycle), still enforce
    forest_level=1 so stale state cannot keep bot on a higher tier.
    """
    cur_lvl = (get_kv("forest_level") or "").strip()
    if _golem_wave_active():
        if cur_lvl != "1":
            _kv_set("forest_level", "1")
            log.warning("🪵🪨 Волна уже активна, принудительно ставлю лес lvl=1 (было: %s)", cur_lvl or "auto")
        return

    prev_lvl = cur_lvl
    _kv_set("golem_wave_prev_forest_level", prev_lvl)
    _kv_set("golem_wave_active", "1")
    _kv_set("forest_level", "1")
    _kv_set("golem_wave_reason", (reason or "")[:64])
    log.warning("🪵🪨 Големы x3 → переключаюсь на уровень 1 (анти-волна).%s", f" reason={reason}" if reason else "")


def _deactivate_golem_wave(reason: str = "") -> None:
    """Restore forest level from before wave mode."""
    if not _golem_wave_active():
        return

    prev_lvl = (get_kv("golem_wave_prev_forest_level") or "").strip()
    _kv_set("forest_level", prev_lvl)
    _kv_set("golem_wave_active", "0")
    _kv_set("golem_wave_reason", "")
    log.info(
        "🪵🪨 Волна закончилась → возвращаюсь на дефолтный лес: %s%s",
        prev_lvl if prev_lvl else "auto",
        f" reason={reason}" if reason else "",
    )


def _golem_wave_maybe_kick() -> bool:
    """Throttle follow-up forest refresh after golem-wave level switches.

    Returns True when it's safe to send one extra forest command to apply the
    updated level immediately.
    """
    now = _now_ts()
    try:
        last = float(get_kv("golem_wave_last_kick_ts") or "0")
    except Exception:
        last = 0.0
    if (now - last) < 8.0:
        return False
    _kv_set("golem_wave_last_kick_ts", str(now))
    return True


def _start_health_cooldown_random():
    minutes = random.randint(CFG.health_pause_min, CFG.health_pause_max)
    until = datetime.now() + timedelta(minutes=minutes)
    _kv_set("health_cd_until", until.isoformat())
    log.warning(f"💚 Пауза по здоровью на {minutes} мин (до {until.strftime('%H:%M:%S')}).")


def _set_health_cooldown_minutes(minutes: int, reason: str = ""):
    """Set health cooldown to an exact duration (in minutes)."""
    minutes = max(0, int(minutes))
    until = datetime.now() + timedelta(minutes=minutes)
    _kv_set("health_cd_until", until.isoformat())
    if reason:
        log.warning(f"💚 Пауза по здоровью на {minutes} мин (до {until.strftime('%H:%M:%S')}) — {reason}.")
    else:
        log.warning(f"💚 Пауза по здоровью на {minutes} мин (до {until.strftime('%H:%M:%S')}).")


_RE_HP_MIN = re.compile(r"До полного восстановления примерно\s+(\d+)\s*мин", re.IGNORECASE)
_RE_HP_HOUR = re.compile(r"До полного восстановления примерно\s+(\d+)\s*ч", re.IGNORECASE)


def _looks_like_hp_reply(txt: str) -> bool:
    if not txt:
        return False
    t = txt.lower()
    return ("💚:" in txt) and ("до полного восстановления" in t or "можно в бой" in t)


def _parse_hp_pause_minutes(txt: str) -> int | None:
    """Parse minutes from the in-game 'хп' reply."""
    if not txt:
        return None
    t = txt.lower()
    if "можно в бой" in t:
        return 0
    m = _RE_HP_MIN.search(txt)
    if m:
        return int(m.group(1))
    h = _RE_HP_HOUR.search(txt)
    if h:
        return int(h.group(1)) * 60
    return None

def _looks_like_health_warning(text: str) -> bool:
    t = (text or "").lower().replace("ё", "е")
    # Игра может присылать несколько формулировок предупреждения:
    # - "здоровье меньше 50% ..."
    # - "здоровье ниже 50% ..."
    # - "опасно выходить в бой ..."
    # Держим проверку чуть шире, чтобы не пропускать автозапрос "хп".
    has_hp_50 = bool(re.search(r"здоровье\s+(меньше|ниже)\s*50", t))
    has_danger_hint = ("опасно" in t) or ("в бой" in t)
    return has_hp_50 and has_danger_hint

def _find_pos_by_substring(msg, substr: str):
    sub = (substr or "").lower()
    if not msg.buttons:
        return None
    for r, row in enumerate(msg.buttons):
        for c, b in enumerate(row):
            lbl = (getattr(b, "text", "") or "").strip()
            if sub in lbl.lower():
                return (r,c)
    return None


def _find_pos_by_exact_label(msg, labels: list[str]):
    """Find button by exact normalized label match (safe against accidental substring clicks)."""
    if not msg.buttons:
        return None
    targets = {_norm_btn_label(x) for x in (labels or []) if x}
    if not targets:
        return None
    for r, row in enumerate(msg.buttons):
        for c, b in enumerate(row):
            lbl = (getattr(b, "text", "") or "").strip()
            if _norm_btn_label(lbl) in targets:
                return (r, c)
    return None


async def _click_action_button_resilient(client, msg, *, labels: list[str], timeout_sec: float = 4.0) -> bool:
    """Click only by exact normalized labels; refetch fresh keyboards while UI updates."""
    pos = _find_pos_by_exact_label(msg, labels)
    if pos is not None:
        try:
            return bool(await click_button(client, msg, pos=pos))
        except Exception:
            pass

    deadline = time.time() + max(0.2, float(timeout_sec or 0.0))
    while time.time() < deadline:
        await asyncio.sleep(0.25)
        cur_msg = await _get_recent_bot_message_with_buttons(client, CFG.game_chat, limit=8)
        if not cur_msg:
            continue
        pos = _find_pos_by_exact_label(cur_msg, labels)
        if pos is None:
            continue
        try:
            return bool(await click_button(client, cur_msg, pos=pos))
        except Exception:
            continue
    return False

def _button_labels(msg):
    """Flatten Telegram inline keyboard texts (safe)."""
    try:
        rows = msg.buttons or []
    except Exception:
        return []
    out = []
    for row in rows:
        for b in row:
            try:
                t = (getattr(b, 'text', '') or '').strip()
            except Exception:
                t = ''
            if t:
                out.append(t)
    return out


def _find_btn(msg, pred):
    """Return first inline button matching predicate, or None.

    Telethon stores buttons as a 2D list in `message.buttons`.
    The returned object can be passed to `msg.click(btn)` or `click_button(...)`.
    """
    try:
        rows = msg.buttons or []
    except Exception:
        rows = []
    for row in rows:
        for b in row:
            try:
                if pred(b):
                    return b
            except Exception:
                continue
    return None

def _has_heal_buttons(msg) -> bool:
    labels = ' '.join(x.lower() for x in _button_labels(msg))
    return ('полное лечение' in labels) or ('пиявка' in labels) or ('котик' in labels) or ('единорог' in labels)

def _now_ts() -> float:
    return datetime.now().timestamp()

def _fish_next_allowed_ts() -> float:
    try:
        return float(get_kv("fish_next_allowed_ts", "0") or "0")
    except Exception:
        return 0.0

def _set_fish_next_allowed_after(seconds: float):
    _kv_set("fish_next_allowed_ts", str(_now_ts() + float(seconds)))


# Backward-compatible alias used by some handler paths.
# Older revisions used `_set_fish_next_allowed(now, kind=...)`. We now keep a
# single timer and compute the delay from config.
def _set_fish_next_allowed(_now_ts_unused=None, kind: str = "generic"):
    # `kind` reserved for future per-action pacing.
    _set_fish_next_allowed_after(CFG.fish_min_click_gap_sec)


def _fish_can_click(now_ts: float) -> bool:
    """Rate-limit for fishing button clicks.

    We keep two guards:
      1) fish_next_allowed_ts — explicit lock set by the fishing handler.
      2) fish_last_click_ts  — last time we *scheduled* a click, to avoid
         double-scheduling on rapid message edits.
    """
    try:
        next_allowed = float(get_kv("fish_next_allowed_ts", "0") or 0)
    except Exception:
        next_allowed = 0.0

    if now_ts < next_allowed:
        return False

    try:
        last_click = float(get_kv("fish_last_click_ts", "0") or 0)
    except Exception:
        last_click = 0.0

    return (now_ts - last_click) >= float(CFG.fish_min_click_gap_sec)


def _fish_mark_scheduled(now_ts: float, extra_lock_sec: float = 0.0):
    """Mark that we scheduled a click (not necessarily executed yet)."""
    _kv_set("fish_last_click_ts", str(now_ts))
    # Extend the lock a bit to reduce double-clicking on fast edits.
    if extra_lock_sec and extra_lock_sec > 0:
        cur = _fish_next_allowed_ts()
        target = max(cur, now_ts + float(extra_lock_sec))
        _kv_set("fish_next_allowed_ts", str(target))

async def setup_client() -> TelegramClient:
    auth_mode = (getattr(CFG, "auth_mode", "phone") or "phone").strip().lower()
    string_session = (getattr(CFG, "string_session", "") or "").strip()

    session_obj = StringSession(string_session) if string_session else CFG.session_name
    client = TelegramClient(session_obj, CFG.api_id, CFG.api_hash)

    if not client.is_connected():
        await client.connect()


    login_phone = (getattr(CFG, "phone", "") or "").strip()

    if not await client.is_user_authorized():
        if auth_mode == "qr":
            log.info("🔐 Авторизация Telegram через QR")
            qr = await client.qr_login()
            log.info("📲 Откройте QR-ссылку в Telegram на телефоне/desktop и подтвердите вход:")
            log.info(qr.url)
            if qrcode is not None:
                try:
                    terminal_qr = qrcode.QRCode(border=1)
                    terminal_qr.add_data(qr.url)
                    terminal_qr.make(fit=True)
                    terminal_qr.print_ascii(invert=True)
                except Exception as e:
                    log.warning(f"⚠️ Не удалось вывести QR в терминал: {e}")
            else:
                log.warning("⚠️ Пакет qrcode не установлен — показываю только ссылку для входа.")
            try:
                await qr.wait(timeout=120)
            except SessionPasswordNeededError:
                pwd = input("Введите пароль 2FA Telegram: ").strip()
                await client.sign_in(password=pwd)
            except asyncio.TimeoutError as e:
                raise RuntimeError("Не удалось авторизоваться по QR: истекло время ожидания подтверждения.") from e
        else:
            phone = login_phone
            if not phone:
                phone = input("Введите номер телефона Telegram (пример: +79991234567): ").strip()
                while not phone:
                    phone = input("Номер не может быть пустым. Введите номер телефона Telegram: ").strip()
            login_phone = phone

            log.info(f"🔐 Авторизация Telegram для {phone}")
            if getattr(CFG, "force_sms", False):
                log.warning("⚠️ FORCE_SMS=1 игнорируется: Telethon больше не поддерживает force_sms")

            sent = await client.send_code_request(phone=phone)

            for attempt in range(1, 4):
                code = input(f"Введите код Telegram (попытка {attempt}/3): ").strip()
                if not code:
                    continue
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                    break
                except PhoneCodeInvalidError:
                    log.error("❌ Неверный код. Проверьте код из Telegram/SMS и попробуйте снова.")
                except PhoneCodeExpiredError:
                    log.warning("⌛ Код истёк. Запрашиваю новый код...")
                    sent = await client.send_code_request(phone=phone)
                except SessionPasswordNeededError:
                    pwd = input("Введите пароль 2FA Telegram: ").strip()
                    await client.sign_in(password=pwd)
                    break
            else:
                raise RuntimeError("Не удалось авторизоваться: исчерпаны попытки ввода кода.")

    if not await client.is_user_authorized():
        # Safety net for edge-cases (e.g. interrupted 2FA flow).
        if not login_phone:
            login_phone = input("Введите номер телефона Telegram для завершения входа: ").strip()
        await client.start(phone=(login_phone or None))


    me = await client.get_me()
    log.info(f"✅ Signed in as {me.first_name} ({me.id}) — chat={CFG.game_chat}")
    log.info(f"🎚 PREFERRED_TIERS={CFG.preferred_tiers}")
    if not string_session:
        try:
            session_str = StringSession.save(client.session)
            if session_str:
                log.info("💾 StringSession создан. Для входа без кода добавьте в .env STRING_SESSION=<ваша_строка>")
        except Exception:
            pass
    return client


async def _schedule_fishing_action(delay: float, coro):
    # Run an action after delay without blocking handler
    try:
        await asyncio.sleep(max(0.0, delay))
        await coro()
    except Exception as e:
        log.error(f"🎣 Scheduled action failed: {e}")



# Backward-compat: older code called _fish_mark_clicked().
# It means: we've scheduled or performed a fishing click and should lock further clicks
# for a short period.
def _fish_mark_clicked(now_ts: float, extra_lock_sec: float = 0.0):
    _fish_mark_scheduled(now_ts, extra_lock_sec=extra_lock_sec)


async def _leave_fishing_to_forest(client: TelegramClient, state: dict, reason: str = ""):
    """Graceful transition from fishing to forest.

    MODE loop sets fish_stop_cast=1 and pending_mode=forest. While that is set,
    fishing handler will stop new casts, optionally wait a short grace window
    for a bite, and then call this function.
    """
    if reason:
        log.info(f"🎣➡️🌲 Выход из рыбалки в лес ({reason})")
    else:
        log.info("🎣➡️🌲 Выход из рыбалки в лес")

    # Clear switching flags first (avoid double triggers)
    _kv_set("fish_stop_cast", "0")
    _kv_set("fish_stop_cast_since", "0")
    _kv_set("fish_stop_cast_kind", "")
    _kv_set("pending_mode", "")

    # Mark new active mode
    _kv_set("active_mode", "forest")
    _kv_set("mode_last_switch_ts", str(time.time()))
    state.active_mode = "forest"

    # Apply combat set (if saved)
    await _apply_set(client, "combat")

    # Small human-like delay before leaving
    await asyncio.sleep(random.uniform(0.8, 1.8))

    # Prefer clicking a "forest" entrypoint from the latest button message to avoid noisy /character screens.
    # Fallback to /character if we can't find a suitable button.
    kick_msg = await _get_recent_bot_message_with_buttons(client, CFG.game_chat, limit=10)
    kicked = False
    if kick_msg:
        for needle in ("Вылазк", "Чащ", "Лес", "🏕", "Напасть"):
            try:
                if await click_button_contains(client, kick_msg, needle):
                    kicked = True
                    break
            except Exception:
                continue

    if not kicked:
        # Stable fallback: request character to get the forest menu.
        await client.send_message(CFG.game_chat, "/character")
    _kv_set("mode_last_kick_ts", str(_now_ts()))

def human_delay_weighted(kind: str) -> float:
    import random
    r = random.random()

    if kind == "strike":  # Подсечь/Тащи
        lo = float(getattr(CFG, "fish_strike_delay_min", 0.35))
        hi = float(getattr(CFG, "fish_strike_delay_max", 1.20))
        if hi < lo:
            lo, hi = hi, lo
        span = max(0.0, hi - lo)
        # чаще быстро, редко к максимуму
        if r < 0.70:
            return random.uniform(lo, lo + span * 0.55)
        if r < 0.95:
            return random.uniform(lo + span * 0.55, lo + span * 0.85)
        return random.uniform(lo + span * 0.85, hi)

    if kind == "cast":  # Закинуть удочку
        # Отдельно не выносили в конфиг — держим "по-человечески" ~1-3с
        if r < 0.60:
            return random.uniform(1.10, 1.80)
        if r < 0.90:
            return random.uniform(1.80, 2.30)
        return random.uniform(2.30, 2.80)

    return random.uniform(1.0, 2.0)

async def _handle_fishing(client: TelegramClient, msg, state):
    # Guard: act only when fishing is the active mode.
    if get_kv("active_mode", "forest") != "fishing":
        return
    # When MODE scheduled a return away from fishing, we still allow the fishing
    # handler to finish the current bite (strike) and then leave.
    pending_mode = get_kv("pending_mode", "")
    stop_kind = (get_kv("fish_stop_cast_kind", "") or "").strip().lower()
    stop_cast = (get_kv("fish_stop_cast", "0") == "1" and pending_mode in ("forest","pet"))

    # If fishing is disabled, we still may need to "finish and leave" when stop_cast is set.
    if not mod_fishing_enabled() and not stop_cast:
        return
    if _desired_mode() != "fishing" and not stop_cast:
        return
    # throttle to avoid "Торопливость..."
    allowed = _fish_next_allowed_ts()
    in_cd = _now_ts() < allowed
    left = max(0.0, allowed - _now_ts()) if in_cd else 0.0

    # While waiting (no bite yet) and MODE scheduled return to forest:
    # don't cast new attempts; wait a short grace window for a possible bite,
    # then leave.
    if state.stage == "fishing_wait" and stop_cast and not is_bite_text(msg.message or ""):
        try:
            since = float(get_kv("fish_stop_cast_since", "0") or 0.0)
        except Exception:
            since = 0.0
        if pending_mode == "pet" or stop_kind == "pet":
            max_wait = float(getattr(CFG, "pet_wait_fish_finish_max_sec", 300.0))
            if since and (_now_ts() - since) < max_wait:
                return
            await _leave_fishing_to_forest(client, state, reason="pet_timeout")
            return
        grace = 20.0
        if since and (_now_ts() - since) < grace:
            return
        await _leave_fishing_to_forest(client, state, reason="scheduled")
        return

    text = msg.message or ""


    if state.stage == "fishing_no_rod":
        # If we are switching fishing -> pet, there is no catch to finish anymore.
        # Leave fishing immediately and continue pet flow instead of waiting for rod retries.
        if stop_cast and (pending_mode == "pet" or stop_kind == "pet"):
            await _leave_fishing_to_forest(client, state, reason="pet_no_rod")
            return

        # If game says "Нет удочки", try to auto-equip one into slot a1 and continue.
        # Disable fishing only on a hard failure (no usable rods in inventory).
        ok = await _ensure_best_rod_equipped(client, fast_retry=True)
        if ok is True:
            log.info("🎣 Нашёл и экипировал удочку в a1 после ошибки 'нет удочки' — продолжаю рыбалку.")
            try:
                await asyncio.sleep(human_delay_cmd("mode_switch"))
                await client.send_message(CFG.game_chat, "Рыбалка")
            except Exception as e:
                log.warning("🎣 После авто-экипа не удалось повторно открыть рыбалку: %s", e)
            return

        if ok is None:
            # Temporary issue (UI timeout/cooldown). Keep module enabled and retry later.
            log.warning("🎣 Нет удочки на экране, но инвентарь сейчас недоступен — повторю авто-поиск позже.")
            return

        # Hard failure: truly no rods in inventory.
        _kv_set("fish_stop_cast", "1")
        _kv_set("fish_stop_cast_since", str(int(time.time())))
        try:
            STORAGE.delete("rod_flow")
        except Exception:
            pass
        log.warning("🎣 Нет рабочей удочки в рюкзаке — выключаю рыбалку (fishing-off).")
        _disable_fishing("no_rod")
        return

    # Missing bait: immediately stop fishing mode to avoid spam/loops.
    if state.stage == "fishing_no_bait":
        log.warning("🎣 Нет наживки — выключаю рыбалку (fishing-off).")
        _disable_fishing("no_bait")
        return

    # Entry screen after sending «Рыбалка» command: press «Начать» to start.
    now = _now_ts()
    if state.stage == "fishing_start":
        if stop_cast:
            if pending_mode == "pet" or stop_kind == "pet":
                await _leave_fishing_to_forest(client, state, reason="pet_skip_start")
            else:
                await _leave_fishing_to_forest(client, state, reason="scheduled")
            return

        # Respect global fishing click cooldown
        if not _fish_can_click(now):
            return

        delay = human_delay_weighted("cast")
        log.info(f"🎣 Старт рыбалки: жду {delay:.2f}s и жму 'Начать'.")
        await asyncio.sleep(delay)
        # Безопасный старт: кнопку 'Начать' жмём ТОЛЬКО в рыболовном контексте (чтобы не нажать 'Начать разбор' и т.п.)
        text_l = (text or '').lower()
        if ('рыбал' in text_l) or (('наживк' in text_l) and ('разбор' not in text_l)):
            res = await click_button_contains(client, msg, ['🐟', '🐠', '🎣'])
            if res is None:
                await click_button_contains(client, msg, ['начать'])
        else:
            log.warning("🎣 На экране start есть 'Начать', но текст не про рыбалку — пропускаю (анти-разбор).")
        _set_fish_next_allowed(now, kind="cast")
        return

    # While waiting (no bite yet) and MODE scheduled return to forest:
    # don't cast new attempts; wait a short grace window for a possible bite,
    # then leave.
    if state.stage == "fishing_wait" and stop_cast and not is_bite_text(text):
        since = float(get_kv("fish_stop_cast_since", "0") or 0.0)
        if pending_mode == "pet" or stop_kind == "pet":
            max_wait = float(getattr(CFG, "pet_wait_fish_finish_max_sec", 300.0))
            if since and (_now_ts() - since) < max_wait:
                return
            await _leave_fishing_to_forest(client, state, reason="pet_timeout")
            return
        grace = 20.0
        if since and (_now_ts() - since) < grace:
            return
        await _leave_fishing_to_forest(client, state, reason="scheduled")
        return

    # If message says "hurry" handled elsewhere, but keep safe
    # Hook: only if bite text
    if state.stage == "fishing_hook":
        if not is_bite_text(text):
            log.info("🎣 Рыбалка: кнопка есть, но поклёвки нет — жду.")
            return
        pos_hook = _find_pos_by_substring(msg, CFG.fish_hook_button)
        if pos_hook is None:
            return
        # If fishing is paused but we are waiting to switch to pets, prioritize striking immediately.
        if stop_cast and pending_mode == "pet":
            try:
                wait_max = int(getattr(CFG, "pet_wait_fish_finish_max_sec", 300))
            except Exception:
                wait_max = 300
            started = float(get_kv("fish_stop_cast_since", "0") or 0)
            now = time.time()
            if started <= 0:
                started = now
                set_kv("fish_stop_cast_since", str(started))
            if (now - started) <= wait_max:
                log.info("🐾 MODE: fishing -> pet: нажимаю 'Подсечь' чтобы быстро закончить рыбку перед пэтами")
                # tiny jitter to look human
                await asyncio.sleep(0.15 + random.random() * 0.15)
                await click_button(client, msg, pos=pos_hook)
                # After striking, the next message will become fishing_cast/fishing_wait; leave will happen there.
                return
            else:
                log.warning(f"🐾 MODE: fishing -> pet: превышен таймаут ожидания поклёвки ({wait_max}s) — отменяю рыбалку")
                pos_cancel = _find_pos_by_substring(msg, CFG.fish_cancel_button)
                if pos_cancel is not None:
                    await click_button(client, msg, pos=pos_cancel)
                return
        delay = human_delay_weighted("strike")
        total = delay + (left if in_cd else 0.0)
        if in_cd:
            log.info(f"🎣 Поклёвка пришла во время cooldown ({int(left)}s). Планирую 'Подсечь/Тащи' через {total:.2f}s")
        else:
            log.info(f"🎣 Поклёвка! Жду {delay:.2f}s и жму 'Подсечь/Тащи'.")

        _fish_mark_clicked(now, extra_lock_sec=total + CFG.fish_min_click_gap_sec)

        async def _do_strike():
            await click_button(client, msg, pos=pos_hook)
            _set_fish_next_allowed_after(CFG.fish_min_click_gap_sec)

        if total > 0:
            asyncio.create_task(_schedule_fishing_action(total, _do_strike))
        else:
            await _do_strike()
        return

    # Cast: only if result text (or explicit cast stage)
    if state.stage == "fishing_cast":
        if stop_cast:
            if pending_mode == "pet" or stop_kind == "pet":
                await _leave_fishing_to_forest(client, state, reason="pet_after_catch")
            else:
                await _leave_fishing_to_forest(client, state, reason="scheduled")
            return
        pos_cast = _find_pos_by_substring(msg, CFG.fish_cast_button)
        if pos_cast is None:
            return
        delay = human_delay_weighted("strike")
        total = delay + (left if in_cd else 0.0)
        if in_cd:
            log.info(f"🎣 Результат пришёл во время cooldown ({int(left)}s). Планирую 'Закинуть' через {total:.2f}s")
        else:
            log.info(f"🎣 Результат/срыв — жду {delay:.2f}s и жму 'Закинуть'")

        _fish_mark_clicked(now, extra_lock_sec=total + CFG.fish_min_click_gap_sec)

        async def _do_cast():
            # Re-check guards at execution time: state may have changed since scheduling.
            # stop_cast is only active when handoff to another mode is in progress.
            pending_now = (get_kv("pending_mode", "") or "").strip().lower()
            stop_cast_now = ((get_kv("fish_stop_cast", "0") == "1") and pending_now in ("forest", "pet"))
            fishing_disabled = not mod_fishing_enabled()
            rod_flow_active = STORAGE.get("rod_flow") is not None
            if fishing_disabled or stop_cast_now or rod_flow_active:
                reasons = []
                if fishing_disabled:
                    reasons.append("fishing disabled")
                if stop_cast_now:
                    reasons.append(f"stop_cast active (pending_mode={pending_now or '-'})")
                if rod_flow_active:
                    reasons.append("rod_flow active")
                log.info(f"🎣 Skip 'Закинуть': {', '.join(reasons)}.")
                return
            await click_button(client, msg, pos=pos_cast)
            _set_fish_next_allowed_after(CFG.fish_min_click_gap_sec)

        if total > 0:
            asyncio.create_task(_schedule_fishing_action(total, _do_cast))
        else:
            await _do_cast()
        return

async def _handle_rod_flow(client: TelegramClient, msg, rod_flow: dict):
    """Equip a fishing rod from inventory into accessory slot 1.


    Mode A (as agreed): choose the most worn working rod by:
      1) lowest current durability
      2) then lowest max durability

    Only rods with current durability > 0 are considered.

    Flow:
      - step=await_inventory: parse /inventory response, pick rod, open its card via /i_N
      - step=await_item_card: click the "В слот 1" button
      - then: send "Рыбалка" to resume the fishing loop
    """

    step = (rod_flow or {}).get("step", "")
    text = msg.message or ""
    low = text.lower()

    if step == "await_inventory":
        # Parse rods in backpack.
        # Example line:
        #   /i_41 📿⁵ Дубовая удочка 25/60
        pattern = re.compile(r"(?P<cmd>/i_\d+)\s+[^\n]*?удочк[^\n]*?(?P<cur>\d+)\s*/\s*(?P<max>\d+)", re.IGNORECASE)
        best = None  # (cur, max, cmd)
        for m in pattern.finditer(text):
            cmd = m.group("cmd")
            cur = int(m.group("cur"))
            mx = int(m.group("max"))
            if cur <= 0:
                continue
            key = (cur, mx)
            if best is None or key < (best[0], best[1]):
                best = (cur, mx, cmd)

        if not best:
            tries = int((rod_flow or {}).get("tries", 0)) + 1
            STORAGE.set("rod_flow", {**rod_flow, "step": "await_inventory", "tries": tries})
            # /inventory may be truncated or temporarily unavailable; retry a few times before giving up.
            if tries <= 3:
                log.warning(f"🎣 В /inventory пока не вижу рабочей удочки — повторю проверку (попытка {tries}/3).")
                await asyncio.sleep(random.uniform(1.8, 3.2))
                await client.send_message(CFG.game_chat, "/inventory")
                return
            log.error("🎣 Не нашёл ни одной удочки с остаточной прочностью после 3 попыток — выключаю рыбалку (fishing-off).")
            _disable_fishing("no_rod_in_inventory")
            try:
                STORAGE.delete("rod_flow")
            except Exception:
                pass
            return

        cur, mx, cmd = best
        log.info(f"🎣 Выбрана удочка: {cmd} (прочность {cur}/{mx}) → открываю карточку")
        STORAGE.set("rod_flow", {"step": "await_item_card", "cmd": cmd})
        await client.send_message(CFG.game_chat, cmd)
        return

    if step == "await_item_card":
        # We expect the item card with buttons like "⬆️ В слот 1".
        clicked = await click_button_contains(client, msg, ["слот 1", "в слот 1"])
        if not clicked:
            # Not the expected message yet; ignore.
            return
        log.info("🎣 Удочка экипируется в слот 1 → возвращаюсь к рыбалке")
        STORAGE.delete("rod_flow")
        # Allow casting again.
        _kv_set("fish_stop_cast", "0")
        _kv_set("fish_stop_cast_since", "0")

        async def _resume():
            await asyncio.sleep(random.uniform(0.9, 1.6))
        await asyncio.sleep(human_delay_cmd("mode_switch"))
        await client.send_message(CFG.game_chat, "Рыбалка")
        asyncio.create_task(_resume())
        return

    # Unknown step → reset
    try:
        STORAGE.delete("rod_flow")
    except Exception:
        pass


async def _read_hp_from_text(text: str):
    m = HP_RE.search(text or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


# ----------------- THIEF (воришка) mini-event -----------------

THIEF_DIR_DEFAULT = "Прямо"
THIEF_HIDE_DEFAULT = "В кустах"

def _parse_thief_hints(text: str) -> tuple[str | None, str | None]:
    """Extract direction + hiding place from the post-battle narrative.

    Examples seen:
      - "Воришка устремился налево и скрылся в ветвях."
      - "Воришка ... скрылась в кустах." (variants possible)
    """
    import re

    low = _normalize_ru(text)
    direction: str | None = None
    hiding: str | None = None

    # Robust regex-based extraction (texts can be long and vary slightly)
    # Examples:
    #  - "Воришка устремился налево и скрылся в ветвях"
    #  - "... устремилась вперед ... скрылась в траве"
    m_dir = re.search(r"устремил(?:ся|ась)?\s+(налево|направо|прямо|вперед|впер[её]д)", low)
    if m_dir:
        d = m_dir.group(1)
        if "налев" in d:
            direction = "Налево"
        elif "направ" in d:
            direction = "Направо"
        else:
            direction = "Прямо"
    else:
        # Fallback heuristics if wording changed (including arrow emojis)
        if "◀" in text or "налево" in low:
            direction = "Налево"
        elif "▶" in text or "направо" in low:
            direction = "Направо"
        elif "🔼" in text or "⬆" in text or "прямо" in low or "вперед" in low or "впер" in low:
            direction = "Прямо"

    m_hide = re.search(r"скрыл(?:ся|ась)?\s+в\s+(куст(?:ах)?|ветв(?:ях)?|трав(?:е|е))", low)
    if m_hide:
        h = m_hide.group(1)
        if "куст" in h:
            hiding = "В кустах"
        elif "ветв" in h:
            hiding = "В ветвях"
        else:
            hiding = "В траве"
    else:
        if "в куст" in low:
            hiding = "В кустах"
        elif "в ветв" in low:
            hiding = "В ветвях"
        elif "в трав" in low:
            hiding = "В траве"

    return direction, hiding


def _thief_flow_get() -> dict:
    """Get or create in-memory thief flow state (expires quickly)."""
    f = STORAGE.get("thief_flow")
    if not isinstance(f, dict):
        f = {}
    now = float(_now_ts())
    ts = float(f.get("ts", 0) or 0)
    # expire after 5 minutes (safety)
    if ts and (now - ts) > 300.0:
        f = {}
    f["ts"] = now
    STORAGE.set("thief_flow", f)
    return f


async def _handle_thief(client: TelegramClient, msg: Message, state, txt_full: str) -> bool:
    """Return True if we clicked something for thief flow (and should stop further handling)."""
    if not mod_thief_enabled():
        return False

    low = _normalize_ru(txt_full)
    is_thief_related = ("воришк" in low) or (state.stage in ("thief_dir", "thief_hide", "thief_done"))
    if not is_thief_related:
        return False

    flow = _thief_flow_get()

    # 0) Start is typically embedded into the post-battle message.
    # We parse hints and (optionally) click "Преследовать".
    if ("воришк" in low) and ("преслед" in low or "устрем" in low or "противник роняет мешок" in low):
        d, h = _parse_thief_hints(txt_full)
        if d:
            flow["dir"] = d
        if h:
            flow["hide"] = h

        # If there is a "Преследовать" button on this message — click it.
        if msg.buttons:
            # 'Преследовать' should look like the player is thinking (longer delay).
            _kv_set("human_ctx", "thief_pursue")
            res = await click_button_contains(client, msg, ["Преследовать"])
            _kv_set("human_ctx", "")
            if res is not None:
                log.info("🦝 THIEF: старт → нажал 'Преследовать'")
                flow["step"] = "dir"
                return True
        # otherwise wait for the next prompt
        return False

    # 1) Direction step
    if state.stage == "thief_dir":
        want = str(flow.get("dir") or THIEF_DIR_DEFAULT)
        log.info(f"🦝 THIEF: куда бежать → {want}")
        await click_button_contains(client, msg, [want])
        flow["step"] = "hide"
        return True

    # 2) Hiding place step
    if state.stage == "thief_hide":
        want = str(flow.get("hide") or THIEF_HIDE_DEFAULT)
        log.info(f"🦝 THIEF: где искать → {want}")
        await click_button_contains(client, msg, [want])
        flow["step"] = "done"
        return True

    # 3) Done (result text)
    if (state.stage == "thief_done") or (("ах, вот ты где" in low) and ("воришк" in low)):
        # After the thief is caught, the bot usually shows a small menu
        # with a convenient "⚔️Вылазка" button. We should click it right away
        # (when any farm-like mode is enabled) instead of waiting for a stale kick.
        if getattr(msg, "buttons", None) and (mod_forest_enabled() or mod_fishing_enabled()):
            _kv_set("human_ctx", "thief_done")
            clicked = await click_button_contains(client, msg, ["Вылазка"])
            _kv_set("human_ctx", "")
            if clicked:
                log.info("🦝 THIEF: завершено → нажал 'Вылазка'")
                try:
                    STORAGE.delete("thief_flow")
                except Exception:
                    pass
                return True
            else:
                log.warning("🦝 THIEF: завершено → кнопка 'Вылазка' не найдена, сбрасываю состояние")

        log.info("🦝 THIEF: завершено → сбрасываю состояние")
        try:
            STORAGE.delete("thief_flow")
        except Exception:
            pass
        return True

    return False


async def _handle_party_event(client: TelegramClient, msg: Message, state) -> bool:
    """Handle party/group lifecycle messages.
    Returns True if the message was handled (we clicked or changed state)."""
    if not mod_party_enabled():
        return False

    text = (msg.text or "").strip()

    # --- Detect lifecycle messages (invite/join/leave/disband) ---
    is_invite = ("приглашает" in text.lower() and "групп" in text.lower())
    is_join = ("вступает" in text.lower() and "групп" in text.lower())
    is_created = ("группа создана" in text.lower())
    is_disband = ("группа распущена" in text.lower())
    is_kick = ("исключается из группы" in text.lower() or "исключен из группы" in text.lower())
    is_leave = ("покидает группу" in text.lower())

    # Some UIs show only buttons, without clear text.
    # IMPORTANT: game_parser.parse_message returns a *flat* list of Choice objects
    # in state.buttons, while Telethon itself uses a nested rows->buttons structure
    # in msg.buttons. Party logic must support both shapes.
    btns: list[str] = []
    if state.buttons:
        first = state.buttons[0]
        # Nested rows (telethon-like)
        if isinstance(first, (list, tuple)):
            for row in state.buttons:  # type: ignore[assignment]
                for b in row:
                    t = (getattr(b, "text", None) or getattr(b, "btn_text", None) or "").strip()
                    if t:
                        btns.append(t)
        else:
            # Flat list of Choice
            for b in state.buttons:
                t = (getattr(b, "btn_text", None) or getattr(b, "text", None) or "").strip()
                if t:
                    btns.append(t)
    has_accept = any("принять" in (t or "").lower() for t in btns)
    has_decline = any("отказ" in (t or "").lower() for t in btns)

    if is_invite or (has_accept and has_decline):
        # Snapshot modes only once per invite chain.
        if get_kv("party_snapshot_done", "0") != "1":
            _party_snapshot_modes()
            set_kv("party_snapshot_done", "1")

        log.info("🤝 PARTY: приглашение → принимаю")
        # Stop new casts (but allow existing hook to resolve).
        set_kv("fish_stop_cast", "1")
        set_kv("fish_stop_cast_since", str(time.time()))
        set_kv("pending_mode", "party")

        # Click accept if button exists.
        if has_accept:
            await _human_sleep(kind="click")
            await click_button_contains(client, msg, ["Принять", "✅Принять"])
            _party_enter_modes("invite_accept")
            set_party_active(True)
            set_kv("party_last_event", "invite_accept")
            try:
                await _use_preferred_dungeon_buffs(client, reason="party_invite_accept", force=True)
            except Exception as e:
                log.warning("🤝 PARTY: не удалось применить стартовые бафы: %s", e)
            return True

        # If we cannot click (no buttons), just mark active and wait for join msg.
        _party_enter_modes("invite_seen")
        set_party_active(True)
        set_kv("party_last_event", "invite_seen")
        return True

    if is_join or is_created:
        # We are joined or a new group was created.
        if get_kv("party_snapshot_done", "0") != "1":
            _party_snapshot_modes()
            set_kv("party_snapshot_done", "1")

        _party_enter_modes("joined")
        set_party_active(True)
        set_kv("party_last_event", "joined" if is_join else "created")
        if get_kv("party_buffs_applied", "0") != "1":
            try:
                await _use_preferred_dungeon_buffs(
                    client,
                    reason="party_joined" if is_join else "party_created",
                    force=True,
                )
                set_kv("party_buffs_applied", "1")
            except Exception as e:
                log.warning("🤝 PARTY: не удалось применить стартовые бафы: %s", e)
        log.info("🤝 PARTY: %s", "вступили в группу" if is_join else "группа создана")
        return False  # no click needed

    if is_disband or is_kick or is_leave:
        if is_party_active() or get_kv("party_snapshot_done", "0") == "1":
            _party_restore_modes("disband" if is_disband else "kick/leave")
            set_kv("party_snapshot_done", "0")
            set_kv("pending_mode", "")
            set_kv("fish_stop_cast", "0")
            set_kv("fish_stop_cast_since", "0")
            set_kv("party_last_event", "ended")
            set_kv("party_buffs_applied", "0")
            return True
        return False

    # Party screen (/party): we can use it as a weak signal we are in a party.
    if text.lower().startswith("группа (id") or ("лидер:" in text.lower() and "участники:" in text.lower()):
        _maybe_refresh_party_identity_from_text(text)
        _party_enter_modes("party_screen")
        set_party_active(True)
        set_kv("party_last_event", "party_screen")
        return False

    return False


async def _handle_post_battle_heal(client: TelegramClient, msg):
    """Auto-heal on post-battle screen.

    IMPORTANT: We may receive new messages right after pressing a potion (leader moves on etc).
    The potion buttons usually stay attached to the original 'victory' message, so we keep
    clicking that same message by id, refreshing it each iteration.
    """
    base_msg_id = getattr(msg, "id", None)
    text = msg.message or ""

    def _party_heal_target_pct() -> float:
        """Party-specific heal target configured via /partyhp (10..100%)."""
        raw = get_kv("party_heal_threshold_pct")
        try:
            v = float(str(raw).replace(",", ".").strip()) if raw is not None else float(getattr(CFG, "party_heal_threshold_pct", 0.6))
        except Exception:
            v = float(getattr(CFG, "party_heal_threshold_pct", 0.6))
        # Accept both 0..1 and 10..100 style values.
        if v > 1.5:
            v = v / 100.0
        return max(0.1, min(1.0, v))

    def _heal_wait(min_s: float, max_s: float) -> float:
        """Healing mode should click faster (~2x shorter waits)."""
        speed_mult = 0.5
        return random.uniform(min_s, max_s) * speed_mult


    # Special case: when the party revives you ("оказывает тебе первую помощь" / "без сознания ждет помощи"),
    # the safest and fastest action is to press "Полное лечение" once (if present).
    # IMPORTANT: keep this detector strict — inventory/utility screens must NOT trigger it.
    low = _normalize_ru(text)
    is_revive_help_msg = (
        ("оказывает тебе первую помощь" in low)
        or ("теперь можно восстановить силы" in low)
        or ("без сознания" in low and ("ждет помощи" in low or "ждёт помощи" in low))
        or ("ждет помощи" in low)
        or ("ждёт помощи" in low)
    )

    if is_revive_help_msg:
        # ВАЖНО: иногда кнопка "🧪Полное лечение" не inline, а reply-keyboard.
        # Тогда msg.buttons пустой, но внизу у пользователя видна reply-кнопка.
        # В таких случаях самый надёжный способ — отправить текст кнопки.
        pos_full = _find_pos_by_substring(msg, "полное лечение")
        delay = _heal_wait(CFG.heal_click_delay_min, CFG.heal_click_delay_max)
        if pos_full is not None:
            log.info("🩹 ВОСКРЕШЕНИЕ/ПОМОЩЬ → жду %.2fs и жму 'Полное лечение' (inline)", delay)
            await asyncio.sleep(delay)
            await click_button(client, msg, pos=pos_full)
            return
        # Fallback: reply button.
        # We only do it for STRICT revive/help messages to avoid accidental sends on other screens.
        log.info("🩹 ВОСКРЕШЕНИЕ/ПОМОЩЬ → жду %.2fs и отправляю '🧪Полное лечение' (reply)", delay)
        await asyncio.sleep(delay)
        try:
            await client.send_message(CFG.game_chat, "🧪Полное лечение")
        except Exception:
            # на некоторых раскладках эмодзи могут отличаться — пробуем без эмодзи
            await client.send_message(CFG.game_chat, "Полное лечение")
        return
    # After a loss we always try to press "Полное лечение" (even if /heal off)
    # so we don't stall on low HP.
    force_heal = "противник одержал верх" in text.lower()

    async def _continue_after_battle() -> None:
        """After heal flow, continue by pressing context action button.

        Priority:
        1) Осмотреться
        2) Вылазка
        3) Вперёд / Вперед
        """
        try:
            # 1) Prefer original post-battle message (where inline buttons usually live)
            cur_msg = msg
            if base_msg_id is not None:
                try:
                    cur_msg = await client.get_messages(chat, ids=base_msg_id)
                except Exception:
                    cur_msg = msg

            pos_look = _find_pos_by_substring(cur_msg, "осмотреться")
            if pos_look is not None:
                await click_button(client, cur_msg, pos=pos_look)
                return

            pos_v = _find_pos_by_substring(cur_msg, "вылазка")
            if pos_v is not None:
                await click_button(client, cur_msg, pos=pos_v)
                return
            pos_fwd = _find_pos_by_substring(cur_msg, "впер")
            if pos_fwd is not None:
                await click_button(client, cur_msg, pos=pos_fwd)
                return

            # 2) Fallback: sometimes UI updates and action buttons appear on a newer message
            recent = await _await_recent_message(
                client,
                chat,
                predicate=lambda m: bool(getattr(m, "buttons", None)) and (
                    ("осмотреться" in _normalize(m.message or ""))
                    or (_find_pos_by_substring(m, "осмотреться") is not None)
                    or ("вылазка" in _normalize(m.message or ""))
                    or (_find_pos_by_substring(m, "вылазка") is not None)
                    or ("впер" in _normalize(m.message or ""))
                    or (_find_pos_by_substring(m, "впер") is not None)
                ),
                timeout=4.0,
                poll=0.8,
                after_id=base_msg_id,
            )
            if recent is not None:
                pos_look = _find_pos_by_substring(recent, "осмотреться")
                if pos_look is not None:
                    await click_button(client, recent, pos=pos_look)
                    return
                pos_v = _find_pos_by_substring(recent, "вылазка")
                if pos_v is not None:
                    await click_button(client, recent, pos=pos_v)
                    return
                pos_fwd = _find_pos_by_substring(recent, "впер")
                if pos_fwd is not None:
                    await click_button(client, recent, pos=pos_fwd)
                    return

            # 3) Reply-keyboard fallback
            try:
                await client.send_message(chat, "👀Осмотреться")
            except Exception:
                try:
                    await client.send_message(chat, "Осмотреться")
                except Exception:
                    try:
                        await client.send_message(chat, "🔼Вперед!")
                    except Exception:
                        await client.send_message(chat, "Вперед!")
        except Exception:
            pass

    async def _party_revive_and_check() -> bool:
        """Post-battle party flow:
        1) Full heal self.
        2) Wait 15s, revive unconscious allies (green-heart buttons).
        3) Wait 15s, press/look around and send /party to verify statuses.
        Returns True when flow was executed.
        """
        low_text = _normalize_ru(text)
        if ("бой затих" not in low_text) or ("без сознания" not in low_text):
            return False

        full_pos = _find_pos_by_substring(msg, "полное лечение")
        if full_pos is not None:
            log.info("🩹 PARTY post_battle: жму 'Полное лечение'")
            await click_button(client, msg, pos=full_pos)
        else:
            try:
                await client.send_message(chat, "🧪Полное лечение")
            except Exception:
                await client.send_message(chat, "Полное лечение")

        await asyncio.sleep(15.0)

        # Refresh message and press all visible revive buttons (💚...).
        try:
            cur_msg = await client.get_messages(chat, ids=getattr(msg, "id", None))
        except Exception:
            cur_msg = msg
        btns = list(_iter_buttons(cur_msg))
        for i, b in enumerate(btns):
            label = _normalize_ru((b.btn_text or b.name or ""))
            if label.startswith("💚") or ("💚" in (b.btn_text or "")):
                try:
                    log.info("💚 PARTY post_battle: поднимаю союзника '%s'", (b.btn_text or b.name or "<ally>"))
                    await click_button(client, cur_msg, index=i)
                    await asyncio.sleep(0.8)
                except Exception:
                    pass

        await asyncio.sleep(15.0)
        await _continue_after_battle()
        try:
            await client.send_message(chat, "/party")
        except Exception:
            pass
        return True

    cur, mx = await _read_hp_from_text(text)
    if await _party_revive_and_check():
        return
    if cur is None or mx is None:
        await _continue_after_battle()
        return

    # Target HP. In party we typically want a higher target (near full),
    # otherwise use user-configurable threshold.
    try:
        target_pct = _party_heal_target_pct() if is_party_active() else heal_target_pct()
    except Exception:
        target_pct = 0.99

    # No-overheal rule: do not waste potions for tiny gaps.
    OVERHEAL_TOL = 20  # user said: "20 хп не жалко"
    target_hp = int(mx * target_pct + 0.9999)  # ceil

    # In party mode, healing is always enabled and controlled only by /partyhp threshold.
    # Outside party, global /heal on|off still controls post-battle healing.
    if (not is_party_active()) and (not mod_heal_enabled()) and (not force_heal):
        await _continue_after_battle()
        return

    # Forced heal (after loss): use "Полное лечение" if available.
    if force_heal:
        pos_full = _find_pos_by_substring(msg, "полное лечение")
        delay = _heal_wait(0.6, 1.6)
        if pos_full is not None:
            log.info("🩹 ПРОИГРЫШ → жду %.2fs и жму 'Полное лечение' (inline)", delay)
            await asyncio.sleep(delay)
            await click_button(client, msg, pos=pos_full)
        else:
            log.info("🩹 ПРОИГРЫШ → жду %.2fs и отправляю '🧪Полное лечение' (reply)", delay)
            await asyncio.sleep(delay)
            try:
                await client.send_message(CFG.game_chat, "🧪Полное лечение")
            except Exception:
                await client.send_message(CFG.game_chat, "Полное лечение")
        return

    # If there are no potion buttons in THIS message, we still should try to continue via 'Вылазка'.
    # (Sometimes we are on an 'Использовано ...' message and action buttons are on another message.)
    if not getattr(msg, "buttons", None):
        await _continue_after_battle()
        return

    # Choose potion to minimize waste (<= OVERHEAL_TOL), otherwise underheal.
    # User provided: Пиявка 150, Котик 450, Единорог 1500.
    potions = [
        ("единорог", 1500),
        ("котик", 450),
        ("пиявка", 150),
    ]

    # Safety: avoid infinite loops
    max_presses = 6
    presses = 0

    while presses < max_presses:
        need = target_hp - cur
        log.info("❤️ post_battle HP %s/%s, target=%s%% (%s), need=%s", cur, mx, int(target_pct * 100), target_hp, need)

        # If already close enough, stop.
        if need <= OVERHEAL_TOL:
            break

        # Refresh the original message (buttons remain there)
        cur_msg = msg
        if base_msg_id is not None:
            try:
                cur_msg = await client.get_messages(chat, ids=base_msg_id)
            except Exception:
                cur_msg = msg

        available = []
        for key, amt in potions:
            pos = _find_pos_by_substring(cur_msg, key)
            if pos is not None:
                available.append((key, amt, pos))

        # If no potions available on the screen, stop.
        if not available:
            break

        # Best: smallest waste within tolerance.
        best = None  # (waste, key, amt, pos)
        for key, amt, pos in available:
            waste = amt - need
            if 0 <= waste <= OVERHEAL_TOL:
                cand = (waste, key, amt, pos)
                if best is None or cand[0] < best[0]:
                    best = cand

        if best is None:
            # Otherwise: biggest underheal potion (amt <= need).
            under = [(amt, key, pos) for key, amt, pos in available if amt <= need]
            if under:
                amt, key, pos = max(under, key=lambda x: x[0])
                best = (-(need - amt), key, amt, pos)  # negative = underheal

        if best is None:
            # Can't heal without big waste → stop spending potions.
            break

        waste, key, amt, pos = best
        delay = _heal_wait(CFG.heal_click_delay_min, CFG.heal_click_delay_max)
        if waste >= 0:
            log.info("🩹 HEAL: need=%s → жду %.2fs и жму %s (%s), waste=%s", need, delay, key, amt, waste)
        else:
            log.info("🩹 HEAL: need=%s → жду %.2fs и жму %s (%s), underheal=%s", need, delay, key, amt, -waste)
        await asyncio.sleep(delay)

        await click_button(client, cur_msg, pos=pos)
        await asyncio.sleep(float(getattr(CFG, 'heal_after_click_wait_sec', 0.35)) * 0.5)
        presses += 1

        # Wait for an HP update ("Использовано ..." or any message containing X/Y💚) to update `cur`.
        # If we can't read it, approximate by adding potion amount but cap at max.
        updated = False
        try:
            upd = await _await_recent_message(
                client,
                chat,
                timeout=float(getattr(CFG, 'heal_hp_update_timeout_sec', 4.0)),
                predicate=lambda m: (("использовано" in (m.message or "").lower()) and ("💚" in (m.message or ""))) or ("/" in (m.message or "") and "💚" in (m.message or "")),
            )
            if upd is not None:
                ucur, umx = await _read_hp_from_text(upd.message or "")
                if ucur is not None and umx is not None:
                    cur, mx = ucur, umx
                    target_hp = int(mx * target_pct + 0.9999)
                    updated = True
        except Exception:
            pass

        if not updated:
            cur = min(mx, cur + amt)

    await _continue_after_battle()

async def _handle_golem_encounter(client: TelegramClient, msg, state: GameState):
    """Golem event in forest after victory.

    Respect golem mode flag:
    - golem ON  -> click "Напасть"
    - golem OFF -> click "Отступить"
    """
    low = _normalize(msg.text or "")
    if "голем" not in low and "golem" not in low:
        # Safety: only act on the expected event.
        return

    want_fight = mod_golem_fight_enabled()
    target = "напасть" if want_fight else "отступ"
    pos = _find_pos_by_substring(msg, target)
    if pos is None:
        # try exact Russian button
        pos = _find_pos_by_substring(msg, "Напасть") if want_fight else _find_pos_by_substring(msg, "Отступ")
    if pos is None:
        log.warning("🪵🗡️ Не нашёл кнопку '%s' на событии голема — пропускаю.", "Напасть" if want_fight else "Отступить")
        return

    d = human_delay_combat("golem")
    log.info(f"🪵 Голем: flag={'on' if want_fight else 'off'} → жду {d:.2f}s и жму '{'Напасть' if want_fight else 'Отступить'}'")
    await asyncio.sleep(d)
    await click_button(client, msg, pos=pos)
    return

async def handle_game_event(client: TelegramClient, event, kind: str):
    msg = event.message
    txt_full = msg.message or ""
    txt = txt_full.replace("\n", " ")
    log.info(f"🧩 {kind} msg {msg.id}: {txt[:180]!r}")
    low_full = _normalize_ru(txt_full)
    _maybe_refresh_party_identity_from_text(txt_full)
    _learn_dungeon_race_from_character(txt_full)

    # Party chat nudge: "Go" means "try to inspect/unstick and keep moving".
    # We store a short-lived flag and use it in dungeon handlers below.
    if "💬" in txt_full:
        if re.search(r"(^|\s)(go|го)($|\s|[!.?,:;])", low_full):
            _kv_set("party_go_until_ts", f"{(time.time() + 22.0):.3f}")
            # Also request fresh party screen once (helps unstick stale dungeon UI).
            try:
                last_party_cmd_ts = float(get_kv("party_go_last_party_cmd_ts", "0") or 0.0)
            except Exception:
                last_party_cmd_ts = 0.0
            now_go = time.time()
            if (now_go - last_party_cmd_ts) > 8.0:
                go_delay = random.uniform(2.0, 4.0)
                log.info("🤝🧭 PARTY: chat-триггер 'Go/Го' → жду %.2fs перед /party", go_delay)
                await asyncio.sleep(go_delay)
                await client.send_message(CFG.game_chat, "/party")
                _kv_set("party_go_last_party_cmd_ts", f"{time.time():.3f}")
            log.info("🤝🧭 PARTY: поймал chat-триггер 'Go/Го' → отправил /party и временно разрешаю 'Осмотреться'")

    # Anti-spam hurry message
    m = HURRY_RE.search(txt_full)
    if m:
        sec = int(m.group(1))
        log.warning(f"⏳ Антиспам: игра просит подождать {sec}s → ставлю fishing cooldown.")
        _set_fish_next_allowed_after(sec + 0.5)
        return

    # Parse FIRST (so fishing works on pause)
    state = parse_message(msg, CFG.fish_hook_button, CFG.fish_cast_button)

    # Inventory fullness heuristic (used for safe unequip/swap logic)
    # - "Инвентарь полон" or auto-send-to-market → full
    # - successful loot to backpack → not full
    try:
        t = txt_full
        if ("Инвентарь полон" in t) or ("рыночный склад" in t):
            _kv_set("inventory_full", "1")
        elif "в слот Рюкзак" in t:
            _kv_set("inventory_full", "0")
    except Exception:
        pass

    # Save last stage so background loops can avoid unsafe transitions
    # (e.g. don't auto-switch modes while the post-battle heal screen is open).
    try:
        _kv_set("last_stage", state.stage)
        _kv_set("last_update_ts", str(_now_ts()))
    except Exception:
        pass

    log.info(f"🔍 Экран: {state.stage}, кнопок={len(state.buttons)}")

    # Debug: dump visible buttons (labels) to help build parsers/flows.
    if dbg_enabled() and _dbg_flag("debug_buttons", "0"):
        try:
            labels = []
            for c in (state.buttons or []):
                t = (c.btn_text or c.name or "").strip()
                if t:
                    labels.append(t)
            if labels:
                show = labels[:25]
                tail = "" if len(labels) <= 25 else f" (+{len(labels)-25} more)"
                log.info("🐛 buttons: " + " | ".join(show) + tail)
        except Exception:
            pass


    rod_flow = STORAGE.get("rod_flow")
    is_fishing = state.stage.startswith("fishing")
    is_fishing_or_rodflow = is_fishing or (rod_flow is not None)

    # Thief mini-event ("воришка") is time-sensitive.
    # Handle it ASAP (even if forest module is OFF / cooldowns are active),
    # but never while we are in fishing / rod-equip flow.
    if not is_fishing_or_rodflow:

        try:
            acted = await _handle_thief(client, msg, state, txt_full)
            if acted:
                return
        except Exception as e:
            log.error(f"THIEF handler error: {e}")

    # Party / group events should be handled even if forest/fishing is disabled or health pause is active.
    try:
        acted = await _handle_party_event(client, msg, state)
        if acted:
            return
    except Exception as e:
        log.error(f"PARTY handler error: {e}")

    # Module toggles: fishing can work even when /pause is on, but can be disabled explicitly.
    # Exception: while mode-manager is switching fishing -> pet/forest, we still need to
    # process the current fishing screen (hook/cast/cancel) to finish the active catch.
    pending_mode = (get_kv("pending_mode", "") or "").strip().lower()
    stop_cast_active = ((get_kv("fish_stop_cast", "0") == "1") and pending_mode in ("forest", "pet"))
    if is_fishing_or_rodflow and not mod_fishing_enabled() and not stop_cast_active:
        log.info("🎛 Рыбалка выключена (/fish off) — игнорирую рыболовные действия.")
        return

    labels = []
    for c in (state.buttons or []):
        t = (c.btn_text or c.name or "").strip()
        if t:
            labels.append(t)
    low_full = _normalize_ru(txt_full)
    now_ts = time.time()
    run_until = float(get_kv("dungeon_run_until_ts", "0") or 0.0)
    enter_markers = (
        "ты отправляешься в подземелье",
        "ты спускаешься в темноту",
        "ворота захлопываются за спиной",
    )
    leave_markers = (
        "ты выбрался из подземелья",
        "подземелье пройдено",
        "покидаешь подземелье",
        "городок изумрудный холм",
    )
    if any(m in low_full for m in enter_markers):
        run_until = now_ts + (30 * 60)
        _kv_set("dungeon_run_until_ts", f"{run_until:.3f}")
    elif any(m in low_full for m in leave_markers):
        run_until = 0.0
        _kv_set("dungeon_run_until_ts", "0")

    dungeon_context_now = _is_dungeon_runtime_context(txt_full, labels)
    if dungeon_context_now:
        run_until = max(run_until, now_ts + (30 * 60))
        _kv_set("dungeon_run_until_ts", f"{run_until:.3f}")

    dungeon_runtime = mod_dungeon_enabled() and (dungeon_context_now or now_ts < run_until)
    go_until_ts = float(get_kv("party_go_until_ts", "0") or 0.0)
    go_hint_active = (now_ts < go_until_ts)
    party_passive_in_dungeon = is_party_active() and dungeon_runtime and (not is_party_driver())
    can_drive_dungeon = (not party_passive_in_dungeon)

    # NOTE: auto-relaunch chain must start ONLY from explicit post-dungeon completion
    # flow (after pressing "Завершить" -> inventory check). Do not arm from generic
    # "key acquired" messages, otherwise manual key crafting/buying can trigger
    # unintended /party navigation.

    # Post-dungeon key check flow:
    # 1) after pressing "Завершить", request /inventory
    # 2) if inventory dump shows known dungeon keys -> open /party
    if (get_kv("dungeon_postcheck_pending", "0") == "1"):
        low_inv = _normalize_ru(txt_full or "")
        is_inventory_dump = (" /i_h " in txt_full or "/i_h " in txt_full) and ("💚" in (txt_full or ""))
        if is_inventory_dump:
            detected = _detect_dungeon_key_target(txt_full)
            if detected:
                target, tier = detected
                log.info("🗝️ Данж: найден ключ (%s) → открываю /party для следующего запуска", target)
                _kv_set("dungeon_next_key_target", target)
                _kv_set("dungeon_next_key_tier", tier or "")
                _kv_set("dungeon_next_key_stage", "open_party")
                await _human_sleep(kind="mode_switch", lo=0.8, hi=1.8, note="post-dungeon key -> /party")
                await client.send_message(CFG.game_chat, "/party")
            else:
                log.info("🗝️ Данж: ключей после завершения не найдено.")
            _kv_set("dungeon_postcheck_pending", "0")

    # allow_noncombat: разрешаем некоторые "служебные" флоу даже во время пауз
    # (поймать воришку, пет-флоу, хил после боя, ответ на запрос ХП и т.п.)
    allow_noncombat = (
        is_fishing_or_rodflow
        or is_pet_flow_active()
        or mod_heal_enabled()
        or is_party_active()
        or dungeon_runtime
        or state.stage == 'post_battle'
        or state.human_ctx == 'hp_query'
    )

    # -------------------- HP reply parsing --------------------
    # Ответ игры на команду "хп" выглядит так:
    #   "💚: 924/2569\nДо полного восстановления примерно 82 мин." (или "... 1 ч")
    # Иногда команду "хп" отправляет пользователь вручную, поэтому парсим ответ
    # всегда (а не только когда human_ctx==hp_query).
    if txt_full and _looks_like_hp_reply(txt_full):
        _update_hp_snapshot_from_text(txt_full)
        _apply_blood_level_routing()
        minutes = _parse_hp_pause_minutes(txt_full)
        if minutes is not None:
            _set_health_cooldown_minutes(minutes, reason="по ответу 'хп'")
        else:
            # если не смогли распарсить — подстрахуемся стандартной паузой
            _start_health_cooldown_random()

        # Сбрасываем контекст запроса, если он был выставлен автоматикой.
        if state.human_ctx == "hp_query":
            _kv_set("human_ctx", "")

        # На сообщении-ответе ХП больше ничего не делаем.
        return

    # /pause blocks combat/forest automation, but allows fishing/pets/heal.
    if is_paused() and not allow_noncombat:
        log.info("⏸️ /pause активна — боёвка/лес выключены.")
        return

    if _night_sleep_now() and not allow_noncombat:
        log.info("🌙 Ночной режим — боёвка/лес пропуск.")
        return

    left_cd = _health_cd_remaining_sec()
    if left_cd > 0 and not allow_noncombat:
        log.info(f"💤 Пауза по здоровью ещё {left_cd//60}m {left_cd%60:02d}s — боёвка/лес запрещены.")
        return

    loss_left = _loss_cd_remaining_sec()
    if loss_left > 0 and not allow_noncombat:
        log.info(f"💀 Пауза после проигрыша ещё {loss_left//60}m {loss_left%60:02d}s — боёвка/лес запрещены.")
        return

    if _looks_like_health_warning(txt) and not allow_noncombat:
        # Просим у игры точное время КД по команде "хп", чтобы не гадать.
        # Ставим короткую страховочную паузу, пока ждём ответ.
        log.warning("⚠️ Здоровье <50% — запрашиваю точный таймер через 'хп'.")
        _kv_set("human_ctx", "hp_query")
        _set_health_cooldown_minutes(2, reason="ожидаю ответ 'хп'")
        try:
            # Человеческая задержка перед служебной командой, чтобы не палиться.
            await _human_sleep(kind="hp", lo=3.0, hi=10.0, note="hp query")
            log.info("📨 Sending HP command: хп")
            await client.send_message(CFG.game_chat, "хп")
        except Exception as e:
            log.warning(f"💚 Не смог отправить 'хп': {e} — ставлю обычную паузу.")
            _kv_set("human_ctx", "")
            _start_health_cooldown_random()
        return

    # Keep HP snapshot fresh from any regular game text containing 💚 line.
    if txt_full and ("💚:" in txt_full):
        _update_hp_snapshot_from_text(txt_full)
        _apply_blood_level_routing()

    # /forest off should stop ONLY лес/боёвка (вылазки, нападения, големы),
    # but must NOT block вспомогательные режимы (петы, лечение, рыбалка).
    if not mod_forest_enabled() and not (is_fishing_or_rodflow or is_pet_flow_active() or mod_heal_enabled() or is_party_active() or dungeon_runtime):
        log.info("🎛 Лес/боёвка выключены (/forest off) — ничего не нажимаю.")
        return

    # Save event (non-fatal)
    try:
        with get_session() as s:
            s.add(Event(chat_id=msg.chat_id, msg_id=msg.id, kind=kind, raw_text=txt_full))
            s.commit()
    except Exception as e:
        log.error(f"DB error(Event): {e}")

    # If we are in the middle of equipping a fishing rod, continue the flow first.
    if rod_flow is not None:
        await _handle_rod_flow(client, msg, rod_flow)
        return

    if is_fishing:
        await _handle_fishing(client, msg, state)
        return

    # Hard cooldown after golems: ignore combat/forest actions for a while.
    # (Fishing is handled above and can still run.)
    golem_left = _golem_cd_remaining_sec()
    if golem_left > 0:
        log.info(f"🪨 Пауза после големов ещё {golem_left}s — пропускаю действия по бою/лесу.")
        return

    if state.stage == "post_battle":
        if _looks_like_loss(txt_full):
            _start_loss_cooldown_random()
            return
        # In dungeon completion screens parser can classify the message as post_battle,
        # but the only actionable button is "Завершить". Handle it before heal flow.
        if can_drive_dungeon:
            pos_finish = _find_pos_by_substring(msg, "заверш")
            if pos_finish is not None:
                d = human_delay_combat("battle")
                log.info("✅ Данж: нажимаю 'Завершить' через %.2fs (post_battle)", d)
                await asyncio.sleep(d)
                if await click_button(client, msg, pos=pos_finish):
                    _kv_set("dungeon_run_until_ts", "0")
                    _kv_set("dungeon_postcheck_pending", "1")
                    await _human_sleep(kind="mode_switch", lo=1.0, hi=2.4, note="dungeon finish -> /inventory")
                    await client.send_message(CFG.game_chat, "/inventory")
                return
        await _handle_post_battle_heal(client, msg)
        return

    # Forest special encounter: golem choice (attack/retreat)
    if state.stage == "golem":
        await _handle_golem_encounter(client, msg, state)
        return

    # In dungeon, prefer scouting with torch set when the room asks what to do.
    pos_inspect = _find_pos_by_substring(msg, "осмотр")
    is_party_screen = (
        ("лидер:" in low_full and "участники:" in low_full)
        or ("группа (id" in low_full)
    )
    if dungeon_runtime and pos_inspect is not None and (("что же делать" in low_full) or ("славная побед" in low_full) or go_hint_active) and (not is_party_screen):
        try:
            await _send_set_command(client, 2)  # E2: torch/navigation set
        except Exception:
            pass
        d = human_delay_combat("battle")
        log.info(f"🔦 Данж: жму 'Осмотреться' через {d:.2f}s")
        await asyncio.sleep(d)
        await click_button(client, msg, pos=pos_inspect)
        return

    # Party "Go/Го" nudge: when /party screen is open and "Осмотреться" is visible,
    # press it with a human-like delay so the run continues.
    if go_hint_active and is_party_screen and pos_inspect is not None:
        d = random.uniform(2.0, 4.0)
        log.info("🤝🧭 PARTY: на экране группы жму 'Осмотреться' через %.2fs", d)
        await asyncio.sleep(d)
        await click_button(client, msg, pos=pos_inspect)
        return

    # Deterministic combat rule (no AI): if screen offers attack, always press it.
    pos_attack = _find_pos_by_substring(msg, "напасть")
    if pos_attack is None:
        pos_attack = _find_pos_by_substring(msg, "в бой")
    if pos_attack is not None and can_drive_dungeon:
        try:
            await _send_set_command(client, 1)  # E1: combat set
        except Exception:
            pass
        d = human_delay_combat("battle")
        log.info(f"⚔️ Найдена кнопка 'Напасть' → жду {d:.2f}s и атакую")
        await asyncio.sleep(d)
        if not await _click_action_button_resilient(client, msg, labels=["Напасть", "В бой"], timeout_sec=4.0):
            log.warning("⚔️ Не удалось нажать 'Напасть' после переключения E1")
        return

    # Lockpick flow in dungeon: switch to E3 and click lock.
    # Do NOT switch back to E2 immediately: chest result messages can arrive
    # while set-switch animation is still in progress, and the `/e_2` response
    # may hide the actionable "Вперёд!" button message. We switch back in
    # follow-up handlers right before navigation clicks.
    if can_drive_dungeon and (("взломать замок" in low_full) or (_find_pos_by_substring(msg, "взлом") is not None)):
        pos_lock = _find_pos_by_substring(msg, "взлом")
        if pos_lock is not None:
            try:
                await _send_set_command(client, 3)  # E3: utility/lockpick set
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info(f"🗝️ Данж: переключаюсь на E3 и жму 'Взломать' через {d:.2f}s")
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_lock)
            return

    # Dungeon room chooser (no AI):
    # 1) room with monster tier marker [1]..[10] (highest tier first),
    # 2) strange plants,
    # 3) alchemy table,
    # 4) campfire,
    # 5) chest,
    # 6) fallback to first room.
    if can_drive_dungeon and looks_like_dungeon_prompt(txt_full, labels):
        best_room = choose_dungeon_room_by_priority(txt_full)
        if best_room is not None:
            pos_room = _find_pos_by_substring(msg, str(best_room))
            if pos_room is not None:
                d = human_delay_combat("battle")
                log.info(f"🕸️ Данж: выбираю проход {best_room} по приоритетам через {d:.2f}s")
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_room)
                return
            # Some dungeon screens list only one room ("1. ..."), but buttons are
            # generic navigation controls (e.g. "Вперед"), without numeric labels.
            # In that case move forward explicitly.
            if best_room == 1:
                pos_forward_single = _find_pos_by_substring(msg, "впер")
                if pos_forward_single is not None:
                    try:
                        await _send_set_command(client, 2)  # E2: navigation/util
                    except Exception:
                        pass
                    d = human_delay_combat("battle")
                    log.info(f"🕸️ Данж: доступен только проход 1 → жму 'Вперёд' через {d:.2f}s")
                    await asyncio.sleep(d)
                    await click_button(client, msg, pos=pos_forward_single)
                    return

    # Dungeon well event:
    # - first click "drink" when available;
    # - then, on the follow-up message ("health restored"), there is often only one
    #   navigation button left, and we should press it even if it's not literally "вперёд".
    if can_drive_dungeon and dungeon_runtime and ("колодец" in low_full):
        pos_drink = _find_pos_by_substring(msg, "вып")
        if pos_drink is None:
            pos_drink = _find_pos_by_substring(msg, "пить")
        if pos_drink is not None:
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info(f"💧 Данж: найден колодец, жму 'Выпить' через {d:.2f}s")
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_drink)
            return

    if can_drive_dungeon and dungeon_runtime and len(state.buttons) == 1:
        low_txt = _normalize_ru(txt_full)
        if ("здоровье восполнено" in low_txt) or ("пьет из колодца" in low_txt):
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            only_btn = (state.buttons[0].btn_text or state.buttons[0].name or "").strip() or "<единственная>"
            log.info("➡️💧 Данж: после колодца жму единственную кнопку '%s' через %.2fs", only_btn, d)
            await asyncio.sleep(d)
            await click_button(client, msg, index=0)
            return

    # Chest follow-up often leaves a single navigation button ("Вперёд").
    # Keep moving automatically with E2 to avoid getting stuck in utility set.
    if can_drive_dungeon and dungeon_runtime and len(state.buttons) == 1:
        low_txt = _normalize_ru(txt_full)
        only_btn = _normalize_ru((state.buttons[0].btn_text or state.buttons[0].name or ""))
        chest_done = (
            ("сундук оказался" in low_txt)
            or ("получает" in low_txt and "слот рюкзак" in low_txt)
            or ("в сундуке" in low_txt)
            or ("пытается взломать сундук" in low_txt)
            or ("замок заклинило" in low_txt)
            or ("точно не открыть" in low_txt)
        )
        if chest_done and ("впер" in only_btn):
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("➡️🗝️ Данж: после сундука жму единственную кнопку '%s' через %.2fs",
                     (state.buttons[0].btn_text or state.buttons[0].name or "<единственная>"), d)
            await asyncio.sleep(d)
            await click_button(client, msg, index=0)
            return

    # Campfire follow-up after "Осмотреться":
    # if "Разжечь" is available, press it first.
    if can_drive_dungeon and dungeon_runtime:
        low_txt = _normalize_ru(txt_full)
        pos_fire = _find_pos_by_substring(msg, "разж")
        # Campfire descriptions vary (e.g. "Перед вами Костер!").
        # Trigger on broader campfire cues to avoid missing the ignite action.
        campfire_prompt = (
            ("костер" in low_txt)
            or ("костёр" in low_txt)
            or ("разжечь огонь" in low_txt)
            or ("меч" in low_txt and "безопас" in low_txt)
        )
        if campfire_prompt and (pos_fire is not None):
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("🔥 Данж: у костра жму 'Разжечь' через %.2fs", d)
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_fire)
            return

    # Campfire follow-up after "Разжечь":
    # once the fire is lit, the game usually leaves a single "Вперёд" button.
    # Continue automatically and make sure E2 is active for navigation.
    if can_drive_dungeon and dungeon_runtime and len(state.buttons) == 1:
        low_txt = _normalize_ru(txt_full)
        only_btn = _normalize_ru((state.buttons[0].btn_text or state.buttons[0].name or ""))
        campfire_done = (
            ("разжигает костер" in low_txt)
            or ("разжигает костёр" in low_txt)
            or ("можно попробовать убежать сюда" in low_txt)
        )
        if campfire_done and ("впер" in only_btn):
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("➡️🔥 Данж: после костра жму единственную кнопку '%s' через %.2fs",
                     (state.buttons[0].btn_text or state.buttons[0].name or "<единственная>"), d)
            await asyncio.sleep(d)
            await click_button(client, msg, index=0)
            return

    # Alchemy table follow-up:
    # equip utility set (E3) and press "Попробовать".
    if can_drive_dungeon and dungeon_runtime:
        low_txt = _normalize_ru(txt_full)
        pos_try = _find_pos_by_substring(msg, "попроб")
        if (
            ("заброшенный алхимический стол" in low_txt)
            or ("крысиную алхимическую лабораторию" in low_txt)
            or ("крысиная алхимическая лаборатория" in low_txt)
        ) and (pos_try is not None):
            try:
                await _send_set_command(client, 3)  # E3: utility/alchemy set
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("🧪 Данж: у алхимического стола жму 'Попробовать' через %.2fs", d)
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_try)
            return

    # Party helper: when party is active and only one navigation button is left ("Вперёд"),
    # press it immediately to keep the run moving.
    if is_party_active() and len(state.buttons) == 1:
        only_btn = _normalize_ru((state.buttons[0].btn_text or state.buttons[0].name or ""))
        if "впер" in only_btn:
            try:
                # For pure navigation in party, force E2 first.
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("🤝➡️ PARTY: жму единственную кнопку '%s' через %.2fs",
                     (state.buttons[0].btn_text or state.buttons[0].name or "<единственная>"), d)
            await asyncio.sleep(d)
            await click_button(client, msg, index=0)
            return

    # Post-dungeon key chain: /party -> Подземелья -> конкретный данж по ключу.
    nxt_stage = (get_kv("dungeon_next_key_stage", "") or "").strip()
    nxt_target = (get_kv("dungeon_next_key_target", "") or "").strip()
    nxt_tier = (get_kv("dungeon_next_key_tier", "") or "").strip().upper()
    # Safety gate: key-chain navigation must run only on neutral/party-like screens.
    # Some forest menus (e.g. Tower) can also contain a "Подземелья" button, and
    # without this guard we may misclick into dungeons unintentionally.
    if nxt_stage and nxt_target and state.buttons and state.stage == "other":
        btn_labels = [((b.btn_text or b.name or "").strip()) for b in state.buttons]
        low_buttons = [_normalize_ru(t) for t in btn_labels]
        low_txt_chain = _normalize_ru(txt_full)
        if nxt_stage == "open_party":
            # Tower screen can also have a "Подземелья" button.
            # Only allow this step on party-like menu where companion buttons
            # such as "Группа"/"Герои" are present.
            has_party_nav = any(("группа" in b) or ("герои" in b) for b in low_buttons)
            if not has_party_nav:
                return
            # IMPORTANT: click only the dedicated "/party -> Подземелья" button.
            # Using a broad substring ("подзем") misfires on other menus like
            # Pierre's key crafting list ("Темнейшее подземелье", etc.).
            pos = _find_pos_by_exact_label(msg, ["Подземелья"])
            if pos is not None:
                d = human_delay_combat("battle")
                log.info("🗝️ Данж-цепочка: в /party жму 'Подземелья' через %.2fs", d)
                await asyncio.sleep(d)
                if await click_button(client, msg, pos=pos):
                    _kv_set("dungeon_next_key_stage", "choose_dungeon")
                return
        elif nxt_stage == "choose_dungeon":
            # Run dungeon choice only on the actual party-dungeon chooser screen.
            # Pierre's key crafting menu has similar dungeon names, but is not a run launch UI.
            if not (("приключени" in low_txt_chain) and ("для групп" in low_txt_chain)):
                _kv_set("dungeon_next_key_stage", "open_party")
                return
            want = "темнейш" if nxt_target == "night" else "катакомб"
            pos = None
            # Prefer exact tier match when key has known level (I..V).
            if nxt_tier:
                for r, row in enumerate(getattr(msg, "buttons", []) or []):
                    for c, btn in enumerate(row):
                        t = _normalize_ru((getattr(btn, "text", "") or ""))
                        if want in t and nxt_tier.lower() in t:
                            pos = (r, c)
                            break
                    if pos is not None:
                        break
            if pos is None:
                pos = _find_pos_by_substring(msg, want)
            if pos is not None:
                d = human_delay_combat("battle")
                log.info("🗝️ Данж-цепочка: выбираю данж '%s' tier=%s через %.2fs", want, nxt_tier or "any", d)
                await asyncio.sleep(d)
                if await click_button(client, msg, pos=pos):
                    _kv_set("dungeon_next_key_stage", "")
                    _kv_set("dungeon_next_key_target", "")
                    _kv_set("dungeon_next_key_tier", "")
                return

    # Alchemy result follow-up:
    # after trying the table, game often leaves a single "Вперёд" button.
    if can_drive_dungeon and dungeon_runtime and len(state.buttons) == 1:
        low_txt = _normalize_ru(txt_full)
        only_btn = _normalize_ru((state.buttons[0].btn_text or state.buttons[0].name or ""))
        alchemy_done = (
            ("принюхивается к колбам" in low_txt)
            or ("полезных ингредиентов" in low_txt)
            or ("быстро смешав" in low_txt)
            or ("алхимическ" in low_txt and "убирает" in low_txt and "рюкзак" in low_txt)
        )
        if alchemy_done and ("впер" in only_btn):
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("➡️🧪 Данж: после алхимического стола жму единственную кнопку '%s' через %.2fs",
                     (state.buttons[0].btn_text or state.buttons[0].name or "<единственная>"), d)
            await asyncio.sleep(d)
            await click_button(client, msg, index=0)
            return

    # Strange plants event:
    # equip utility set (E3) and press "Собрать".
    if can_drive_dungeon and dungeon_runtime:
        low_txt = _normalize_ru(txt_full)
        pos_collect = _find_pos_by_substring(msg, "собрат")
        if ("странные растения" in low_txt) and (pos_collect is not None):
            try:
                await _send_set_command(client, 4)  # E4: utility/alchemy set
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("🌿 Данж: у странных растений жму 'Собрать' через %.2fs", d)
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_collect)
            return

    # Hunter event: "Странные следы".
    # If hunter mode is ON -> click "Выследить".
    # If OFF -> wait 10s and then click "Вперёд".
    if can_drive_dungeon and dungeon_runtime:
        low_txt = _normalize_ru(txt_full)
        pos_track = _find_pos_by_substring(msg, "выслед")
        pos_forward_local = _find_pos_by_substring(msg, "впер")
        hunter_room = ("странные следы" in low_txt) and (("свежие следы" in low_txt) or ("опытный охотник" in low_txt))
        hunter_wait_key = "dungeon_hunter_wait_until_ts"
        now_ts = _now_ts()
        wait_until = float(get_kv(hunter_wait_key, "0") or 0.0)

        if hunter_room and (pos_track is not None):
            if mod_hunter_enabled():
                d = human_delay_combat("battle")
                log.info("🏹 Данж: hunter=on, жму 'Выследить' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_track)
                _kv_set(hunter_wait_key, "0")
                return

            if wait_until <= now_ts:
                wait_until = now_ts + 10.0
                _kv_set(hunter_wait_key, f"{wait_until:.3f}")
                log.info("⏱️ Данж: hunter=off, старт паузы 10s перед 'Вперёд'")

            left = max(0.0, wait_until - now_ts)
            if left > 0 and pos_forward_local is not None:
                log.info("⏱️ Данж: hunter-пауза, осталось %.1fs", left)
                return

            if pos_forward_local is not None:
                try:
                    await _send_set_command(client, 2)
                except Exception:
                    pass
                d = human_delay_combat("battle")
                log.info("➡️ Данж: hunter=off, пауза завершена, жму 'Вперёд' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_forward_local)
                _kv_set(hunter_wait_key, "0")
                return

    # Race altars event:
    # touch only "your" altar (by configured race), otherwise wait 10s and continue.
    if can_drive_dungeon and dungeon_runtime:
        low_txt = _normalize_ru(txt_full)
        pos_touch = _find_pos_by_substring(msg, "прикосн")
        pos_forward_local = _find_pos_by_substring(msg, "впер")
        is_altar_room = (("алтар" in low_txt) or ("бастет" in low_txt) or ("инари" in low_txt) or ("тануки" in low_txt))
        is_altar_1000 = ("тысячелап" in low_txt)
        altar_race = None
        if ("инари" in low_txt) or ("наве" in low_txt) or ("для лис" in low_txt):
            altar_race = "fox"
        elif ("тануки" in low_txt) or ("для енот" in low_txt):
            altar_race = "raccoon"
        elif ("бастет" in low_txt) or ("для рыс" in low_txt):
            altar_race = "lynx"
        race_from_kv_raw = (get_kv("dungeon_race") or "").strip().lower()
        race_from_cfg_raw = (getattr(CFG, "dungeon_race", "") or "").strip().lower()
        my_race = race_from_kv_raw or race_from_cfg_raw
        # If altar wait was started on a previous message, it may expire on a
        # later message that no longer contains altar text. Complete the pause
        # as soon as any forward button appears.
        wait_key = "dungeon_altar_wait_until_ts"
        now_ts = _now_ts()
        wait_until_global = float(get_kv(wait_key, "0") or 0.0)
        if (wait_until_global > 0.0) and (now_ts >= wait_until_global) and (pos_forward_local is not None):
            try:
                await _send_set_command(client, 2)  # E2: navigation/util
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info("➡️ Данж: пауза у алтаря истекла на следующем экране, жму 'Вперёд' через %.2fs", d)
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_forward_local)
            _kv_set(wait_key, "0")
            return
        if is_altar_room and (pos_touch is not None):
            race_known = my_race in ("fox", "raccoon", "lynx")
            race_match = bool(race_known and altar_race and (my_race == altar_race))
            legacy_switch_touch = (mod_dungeon_altar_1000_touch_enabled() if is_altar_1000 else mod_dungeon_altar_touch_enabled())
            # Backward-compatible fallback:
            # if race is unknown or altar race can't be inferred, use legacy switches.
            allow_touch = race_match or (not race_known) or (not altar_race and legacy_switch_touch)
            if race_match:
                decision_reason = "race_match"
            elif not race_known:
                decision_reason = "race_unknown_fallback"
            elif not altar_race and legacy_switch_touch:
                decision_reason = "altar_unknown_legacy_switch"
            else:
                decision_reason = "race_mismatch_wait"
            log.info(
                "🐾 Данж: статус алтаря перед нажатием: сохраненная_раса=%s, раса_из_конфига=%s, используемая_раса=%s, раса_алтаря=%s, race_known=%s, race_match=%s, allow_touch=%s, причина=%s",
                race_from_kv_raw or "<empty>",
                race_from_cfg_raw or "<empty>",
                my_race or "<empty>",
                altar_race or "unknown",
                "yes" if race_known else "no",
                "yes" if race_match else "no",
                "yes" if allow_touch else "no",
                decision_reason,
            )
            wait_seconds = 10.0
            if not allow_touch:
                wait_until = float(get_kv(wait_key, "0") or 0.0)
                if wait_until <= now_ts:
                    wait_until = now_ts + wait_seconds
                    _kv_set(wait_key, f"{wait_until:.3f}")
                    altar_name = "Тысячелапого" if is_altar_1000 else (altar_race or "чужой")
                    log.info("🐾 Данж: алтарь %s не по расе (%s) → жду %.0fs перед 'Вперёд'", altar_name, my_race or "unknown", wait_seconds)

                left = max(0.0, wait_until - now_ts)
                if left > 0 and pos_forward_local is not None:
                    log.info("🐾 Данж: ожидание у алтаря, осталось %.1fs", left)
                    await asyncio.sleep(left)

                if pos_forward_local is not None:
                    try:
                        await _send_set_command(client, 2)  # E2: navigation/util
                    except Exception:
                        pass
                    d = human_delay_combat("battle")
                    log.info("➡️ Данж: ожидание у алтаря завершено, жму 'Вперёд' через %.2fs", d)
                    await asyncio.sleep(d)
                    await click_button(client, msg, pos=pos_forward_local)
                    _kv_set(wait_key, "0")
                    return

                if is_altar_1000:
                    log.info("🐾 Данж: алтарь Тысячелапого не по расе/логике касания")
                else:
                    log.info("🐾 Данж: алтарь найден, касание пропущено")
                return
            d = human_delay_combat("battle")
            log.info("🐾 Данж: у алтаря (%s) жму 'Прикоснуться' через %.2fs", altar_race or "unknown", d)
            await asyncio.sleep(d)
            await click_button(client, msg, pos=pos_touch)
            return

    # Blocked/boarded passage event:
    # wait up to 15s in "boarded passage" rooms, then continue forward (E2 + forward).
    if can_drive_dungeon and dungeon_runtime:
        low_txt = _normalize_ru(txt_full)
        pos_forward_local = _find_pos_by_substring(msg, "впер")
        pos_break_local = _find_pos_by_substring(msg, "разоб")
        pos_open_grave_local = _find_pos_by_substring(msg, "вскры")
        pos_chop_local = _find_pos_by_substring(msg, "проруб")
        boarded_room = ("заколочен" in low_txt) or ("заколоченный проход" in low_txt)
        rubble_room = ("каменный завал" in low_txt)
        grave_room = ("могила" in low_txt)
        wait_key = "dungeon_boarded_wait_until_ts"
        rubble_wait_key = "dungeon_rubble_wait_until_ts"
        grave_wait_key = "dungeon_grave_wait_until_ts"
        now_ts = _now_ts()

        if grave_room:
            if mod_dungeon_grave_open_enabled() and pos_open_grave_local is not None:
                d = human_delay_combat("battle")
                log.info("⚰️ Данж: могила, жму 'Вскрыть' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_open_grave_local)
                return

            wait_until = float(get_kv(grave_wait_key, "0") or 0.0)
            if wait_until <= now_ts:
                wait_until = now_ts + 10.0
                _kv_set(grave_wait_key, f"{wait_until:.3f}")
                log.info("⏱️ Данж: могила (mod off), старт паузы 10s")

            left = max(0.0, wait_until - now_ts)
            if left > 0:
                if pos_forward_local is not None:
                    log.info("⏱️ Данж: жду у могилы, осталось %.1fs", left)
                return

            if pos_forward_local is not None:
                try:
                    await _send_set_command(client, 2)
                except Exception:
                    pass
                d = human_delay_combat("battle")
                log.info("➡️ Данж: пауза у могилы завершена, жму 'Вперёд' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_forward_local)
                _kv_set(grave_wait_key, "0")
                return

        if rubble_room:
            if mod_dungeon_rubble_break_enabled() and pos_break_local is not None:
                d = human_delay_combat("battle")
                log.info("⛏️ Данж: каменный завал, жму 'Разобрать' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_break_local)
                return

            wait_until = float(get_kv(rubble_wait_key, "0") or 0.0)
            if wait_until <= now_ts:
                wait_until = now_ts + 10.0
                _kv_set(rubble_wait_key, f"{wait_until:.3f}")
                log.info("⏱️ Данж: каменный завал (mod off), старт паузы 10s")

            left = max(0.0, wait_until - now_ts)
            if left > 0:
                if pos_forward_local is not None:
                    log.info("⏱️ Данж: жду у завала, осталось %.1fs", left)
                return

            if pos_forward_local is not None:
                try:
                    await _send_set_command(client, 2)
                except Exception:
                    pass
                d = human_delay_combat("battle")
                log.info("➡️ Данж: пауза у завала завершена, жму 'Вперёд' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_forward_local)
                _kv_set(rubble_wait_key, "0")
                return

        if boarded_room:
            if mod_dungeon_boarded_chop_enabled() and pos_chop_local is not None:
                d = human_delay_combat("battle")
                log.info("🪓 Данж: заколоченный проход, жму 'Прорубить' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_chop_local)
                return

            wait_until = float(get_kv(wait_key, "0") or 0.0)
            if wait_until <= now_ts:
                wait_until = now_ts + 10.0
                _kv_set(wait_key, f"{wait_until:.3f}")
                log.info("⏱️ Данж: заколоченный проход (mod off), старт паузы 10s")

            left = max(0.0, wait_until - now_ts)
            if left > 0:
                if pos_forward_local is not None:
                    log.info("⏱️ Данж: жду у заколоченного прохода, осталось %.1fs", left)
                    return

            if pos_forward_local is not None:
                try:
                    await _send_set_command(client, 2)  # E2: navigation/util
                except Exception:
                    pass
                d = human_delay_combat("battle")
                log.info("➡️ Данж: пауза у прохода завершена, жму 'Вперёд' через %.2fs", d)
                await asyncio.sleep(d)
                await click_button(client, msg, pos=pos_forward_local)
                _kv_set(wait_key, "0")
                return

        # If we observe chopping/mining actions or "barrier was cleared" text,
        # drop waiting state and let generic forward handlers continue.
        if (
            ("разрубает баррикаду" in low_txt)
            or ("ничего другого за ней не оказалось" in low_txt)
            or ("проход прорублен" in low_txt)
        ):
            if float(get_kv(wait_key, "0") or 0.0) > 0.0:
                _kv_set(wait_key, "0")
                log.info("🪓 Данж: проход разобран, снимаю ожидание")

    # After "Осмотреться" the game can say "ничего интересного" and offer "Вперёд!".
    # Continue automatically to the next fork.
    pos_forward = _find_pos_by_substring(msg, "впер")
    if can_drive_dungeon and pos_forward is not None:
        low_txt = _normalize_ru(txt_full)
        if ("ничего интересного" in low_txt) or ("следующей развилке" in low_txt) or ("куда пойдем" in low_txt):
            try:
                await _send_set_command(client, 2)  # E2: torch/navigation
            except Exception:
                pass
            d = human_delay_combat("battle")
            log.info(f"➡️ Данж: жму 'Вперёд' через {d:.2f}s")
            await asyncio.sleep(d)
            if not await _click_action_button_resilient(client, msg, labels=["Вперёд!", "Вперед!"], timeout_sec=4.0):
                log.warning("➡️ Не удалось нажать 'Вперёд' после переключения E2")
            return

    # Dungeon completion: press green "Завершить", then run a key check flow.
    if can_drive_dungeon:
        pos_finish = _find_pos_by_substring(msg, "заверш")
        if pos_finish is not None:
            d = human_delay_combat("battle")
            log.info("✅ Данж: нажимаю 'Завершить' через %.2fs", d)
            await asyncio.sleep(d)
            if await click_button(client, msg, pos=pos_finish):
                _kv_set("dungeon_run_until_ts", "0")
                _kv_set("dungeon_postcheck_pending", "1")
                await _human_sleep(kind="mode_switch", lo=1.0, hi=2.4, note="dungeon finish -> /inventory")
                await client.send_message(CFG.game_chat, "/inventory")
            return

    if not state.can_act or not state.buttons:
        return

    # If golem fight is OFF:
    # - Prefer non-golem targets (handled in strategy.choose_target)
    # - If we see a "golem wave" (usually 3 golems offered) → switch to tier 1,
    #   farm there until we meet a golem encounter, retreat, then try default again.
    if state.stage == "battle" and (not mod_golem_fight_enabled()):
        golem_count = 0
        non_golem_count = 0
        for b in state.buttons:
            label = (b.btn_text or b.name or "")
            low = _normalize(label)
            if "голем" in low or "golem" in low:
                golem_count += 1
            else:
                non_golem_count += 1

        # Typical wave case: all offered enemies are golems (3 buttons).
        # We handle >=3 to be robust to future UI variants.
        if golem_count >= 3 and non_golem_count == 0:
            _activate_golem_wave("battle_all_golems")

            # Анти-зацикливание:
            # если на выбранном уровне леса выпали только големы (а fight=off),
            # то откатываемся на более низкий уровень и делаем паузу,
            # иначе бот начинает "ддосить" вылазки и палится.
            try:
                now_ts = time.time()
                cur_lvl = int((_kv_get("forest_level") or "1").strip() or 1)
                streak = int((_kv_get("golem_only_streak") or "0").strip() or 0) + 1
                _kv_set("golem_only_streak", str(streak))

                # Пауза после големов
                _kv_set("golem_cooldown_until", str(now_ts + 20.0))

                # Каждые 2 подряд голем-ситуации снижаем уровень, минимум 1
                if streak >= 2 and cur_lvl > 1:
                    _kv_set("forest_level", str(max(1, cur_lvl - 1)))
                    _kv_set("golem_only_streak", "0")
            except Exception:
                pass

            log.warning("💀 В выборе целей только големы (fight=off) → откат/пауза и возврат в лес.")
            try:
                await asyncio.sleep(human_delay_cmd("mode_switch"))
                await client.send_message(CFG.game_chat, CFG.forest_fallback_cmd)
            except Exception:
                pass
            return

    # Blood-mode routing just switched to a safer tier.
    # Open forest menu once to refresh enemy list according to effective level.
    if (get_kv("blood_force_forest") or "0") == "1" and state.stage in ("battle", "post_battle"):
        _kv_set("blood_force_forest", "0")
        try:
            await asyncio.sleep(human_delay_cmd("mode_switch"))
            await client.send_message(CFG.game_chat, CFG.forest_fallback_cmd)
            log.info("🩸 Blood mode active → запросил лес для смены уровня.")
        except Exception as e:
            log.warning(f"🩸 Blood mode: не удалось открыть лес: {e}")
        return


    if dbg_enabled() and _dbg_flag("debug_choose", "0"):
        try:
            cand = []
            for c in (state.buttons or [])[:20]:
                t = (c.btn_text or c.name or "").strip()
                if t:
                    cand.append(t)
            if cand:
                log.info("🐛 choose: candidates=" + " | ".join(cand))
        except Exception:
            pass

    # Если недавно были големы-only при fight=off — даём лесу "остыть".
    if state.stage == "forest":
        try:
            cd = float(_kv_get("golem_cooldown_until") or "0")
            if time.time() < cd:
                if dbg_enabled():
                    left = cd - time.time()
                    log.info(f"⏳ cooldown after golems: {left:.1f}s")
                return
        except Exception:
            pass

        # Proactive safety: on entering forest, try once in a while to remove/replace
        # an equipped rod so it does not stay in combat gear.
        try:
            now_ts = time.time()
            last_ts = float(get_kv("forest_rod_strip_last_ts") or "0")
            if (now_ts - last_ts) >= 90.0:
                _kv_set("forest_rod_strip_last_ts", f"{now_ts:.3f}")
                await _ensure_no_rod_before_forest(client, CFG.game_chat)
        except Exception as e:
            log.debug(f"forest rod-strip check skipped: {e}")

    target = choose_target(state, profile)

    if dbg_enabled() and _dbg_flag("debug_choose", "0"):
        try:
            if target:
                sel = (target.btn_text or target.name or "").strip()
                log.info("🐛 choose: selected=" + (sel or "<empty>"))
            else:
                log.info("🐛 choose: selected=<none>")
        except Exception:
            pass

    if not target:
        return

    try:
        # человеческая задержка перед боевым кликом
        kind = "battle" if state.stage == "battle" else ("forest" if state.stage == "forest" else "battle")
        btn_label = (target.btn_text or target.name or "").lower()
        if "вылазка" in btn_label:
            kind = "vylazka"
        d = human_delay_combat(kind)
        exp_stage = state.stage
        exp_update_ts = None
        try:
            exp_update_ts = _kv_get("last_update_ts")
        except Exception:
            exp_update_ts = None

        log.info(f"🧠 {state.stage}: жду {d:.2f}s перед кликом")
        await asyncio.sleep(d)

        # Защита от "просроченных" кликов:
        # пока мы ждали задержку, экран мог смениться (например, вылезло "подожди X секунд")
        # и клик по старой кнопке приводит к странным зацикливаниям.
        try:
            cur_stage = _kv_get("last_stage")
            cur_update_ts = _kv_get("last_update_ts")
            if (cur_stage and cur_stage != exp_stage) or (exp_update_ts and cur_update_ts and cur_update_ts != exp_update_ts):
                log.info("🛑 отменяю клик: экран сменился во время задержки")
                return
        except Exception:
            pass

        if target.pos is not None:
            await click_button(client, msg, pos=target.pos)
        elif target.btn_text:
            await click_button(client, msg, text=target.btn_text)
        else:
            await click_button(client, msg, text=target.name)
        log.info(f"✅ Клик: {target.name} pos={target.pos}")
        try:
            with get_session() as s:
                s.add(ActionLog(kind=f"click:{state.stage}", detail=target.name, result="ok"))
                s.commit()
        except Exception as e:
            log.error(f"DB error(ActionLog): {e}")
    except Exception as e:
        log.error(f"❌ Ошибка клика: {e}")

def _desired_mode() -> str:
    """Compute desired high-level mode.

    Rule: Forest is the main activity when it's allowed.
    During pause/health/loss/night (or when forest module off), we switch to fishing (if enabled).
    """
    # Pets override night/pause logic: when it's time, we temporarily switch into
    # a dedicated 'pet' mode to perform home-care flow.
    if _pet_due_now():
        return "pet"

    forest_ok = (
        mod_forest_enabled()
        and (not is_paused())
        and (not _night_sleep_now())
        and _health_cd_remaining_sec() <= 0
        and _loss_cd_remaining_sec() <= 0
    )

    if forest_ok:
        return "forest"
    if mod_fishing_enabled():
        return "fishing"
    return "idle"


async def _fetch_character(client: TelegramClient, timeout: float = 20.0) -> dict | None:
    timeout = float(timeout) if timeout is not None else 12.0
    timeout *= max(1.0, float(getattr(CFG, "response_timeout_factor", 1.0) or 1.0))
    global _LAST_FETCH_CHAR_TS
    async with _FETCH_CHAR_LOCK:
        now = time.time()
        delta = now - _LAST_FETCH_CHAR_TS
        if delta < _FETCH_CHAR_MIN_INTERVAL:
            await asyncio.sleep(_FETCH_CHAR_MIN_INTERVAL - delta)
        _LAST_FETCH_CHAR_TS = time.time()
        """Запрашивает инвентарь/экипировку и возвращает распарсенный снимок.

        Важно: в чат часто прилетают посторонние сообщения (подсказки/реклама/предупреждения),
        поэтому ждём именно сообщение формата /inventory (с блоком 'Рюкзак (...)' и слотами /i_*).
        """
        await asyncio.sleep(human_delay_cmd("inventory"))
        await client.send_message(CFG.game_chat, "/inventory")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        def _looks_like_inventory(txt: str) -> bool:
            t = txt.lower()
            return ("рюкзак" in t and "/i_h" in t and "нажми на активную команду" in t)

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("⌛ /inventory: timeout waiting for inventory snapshot")
                return None
            try:
                # Telethon has no client.wait_for; use a temporary handler + Future
                fut = asyncio.get_running_loop().create_future()

                async def _tmp_handler(e):
                    if not fut.done():
                        fut.set_result(e)

                tmp_event = events.NewMessage(chats=CFG.game_chat)
                client.add_event_handler(_tmp_handler, tmp_event)
                try:
                    ev = await asyncio.wait_for(fut, timeout=remaining)
                finally:
                    client.remove_event_handler(_tmp_handler, tmp_event)
            except asyncio.TimeoutError:
                log.warning("⌛ /inventory: timeout waiting for inventory snapshot")
                return None

            txt = (ev.raw_text or "").strip()
            if not txt:
                continue
            if not _looks_like_inventory(txt):
                continue

            snap = parse_character(txt)
            if not snap:
                continue
            return snap


async def _ensure_best_rod_equipped(client: TelegramClient, *, fast_retry: bool = False) -> bool | None:
    """Ensure we have a non-broken rod equipped in Accessory 1.

    Notes:
    - This function MUST be safe to call from the background mode loop.
    - We store throttling state in KV (not a local variable), so we don't crash
      with NameError and we don't spam /inventory when the game can't respond.
    """

    now_ts = time.time()
    try:
        retry_after = float(get_kv("rod_retry_after") or 0.0)
    except Exception:
        retry_after = 0.0
    if now_ts < retry_after:
        # Not a hard failure: we recently couldn't obtain a reliable /inventory snapshot
        # or couldn't equip. Back off to avoid spamming the game.
        log.info(f"🎣 rod retry cooldown: жду {int(retry_after - now_ts)}s")
        return None
    ch = await _fetch_character(client)
    if not ch:
        # If the game didn't respond, back off to avoid /inventory spam.
        backoff_sec = 30 if fast_retry else 120
        _kv_set("rod_retry_after", str(time.time() + backoff_sec))
        return None
    # Accept a working rod in any accessory slot (a1/a2/a3).
    # Some game flows may leave it outside a1 for a while; that should not
    # be treated as a hard "no rod" failure that disables fishing.
    for sid in ("a1", "a2", "a3"):
        cur = str(ch["slots"].get(sid, "") or "")
        if "удочк" not in cur.lower():
            continue
        dm = RE_DUR.search(cur)
        if dm and int(dm.group(1)) > 0:
            return True

    rod = _best_rod(ch.get("backpack", []))
    if not rod:
        # Hard failure: there is no usable rod in backpack (or all broken).
        # Let the caller decide whether to disable the fishing module.
        log.warning("🎣 Нет подходящей удочки в рюкзаке (прочность=0 или не найдено).")
        _kv_set("rod_retry_after", str(time.time() + 600))
        return False
    log.info(f"🎣 Экипирую лучшую удочку: {rod['label']} ({rod['cur']}/{rod['max']})")

    def _has_working_rod(snapshot: dict | None) -> bool:
        if not snapshot:
            return False
        for sid in ("a1", "a2", "a3"):
            cur = str(snapshot.get("slots", {}).get(sid, "") or "")
            if "удочк" not in cur.lower():
                continue
            dm = RE_DUR.search(cur)
            if dm and int(dm.group(1)) > 0:
                return True
        return False

    # Game-specific rule: equip/swap fishing rod through accessory slot 1 only.
    # Trying a2/a3 causes useless loops in some UIs ("В рюкзаке нет места") and
    # prevents a deterministic swap flow expected by users.
    slot = "a1"
    ok = await _equip_item_to_slot(client, CFG.game_chat, rod["cmd"], slot)
    if ok:
        # Important: button click != successful equip. Confirm through fresh /inventory.
        ch_after = await _fetch_character(client)
        if _has_working_rod(ch_after):
            return True
        log.warning("🎣 После попытки экипа в a1 удочка в слотах не появилась — отложу повтор.")

    # Temporary failure (UI/state). Don't disable fishing; retry later.
    log.warning("🎣 Не удалось экипировать удочку сейчас — попробую позже.")
    backoff_sec = 30 if fast_retry else 120
    _kv_set("rod_retry_after", str(time.time() + backoff_sec))
    return None


async def _open_card_with_buttons(client: TelegramClient, cmd: str, timeout: float = 12.0):
    """Отправляет команду и ждёт ответ бота с инлайн-кнопками."""

    fut: asyncio.Future = asyncio.get_event_loop().create_future()

    async def _tmp_handler(event):
        try:
            if event.chat_id != CFG.chat_id:
                return
            msg = event.message
            if getattr(msg, "buttons", None):
                if not fut.done():
                    fut.set_result(msg)
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)

    client.add_event_handler(_tmp_handler, events.NewMessage(chats=CFG.chat))
    try:
        await _human_sleep(kind="cmd")
        await client.send_message(CFG.chat, cmd)
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        client.remove_event_handler(_tmp_handler)


async def _unequip_from_accessory_slot(client: TelegramClient, slot_key: str) -> bool:
    """Снимает предмет из слота аксессуара (a1/a2/a3), если в рюкзаке есть место."""

    slot_cmd = f"/i_{slot_key}"
    try:
        msg = await _open_card_with_buttons(client, slot_cmd, timeout=12.0)
    except Exception as e:
        log.warning(f"🎒 не удалось открыть {slot_cmd} для снятия: {e}")
        return False

    # Кнопка может называться "Снять" или "Снять: ..."
    if await click_button_contains(client, msg, "Снять"):
        await _human_sleep(kind="click")
        return True
    return False


def _pick_non_rod_replacements(backpack_items: list[dict | tuple[str, str]]) -> list[dict]:
    """Возвращает кандидаты для замены удочки (в порядке приоритета).

    Приоритет: амулеты/кольца/аксессуары -> любые не-удочки.
    """

    def _to_item(it) -> dict:
        if isinstance(it, tuple) and len(it) >= 2:
            return {"cmd": str(it[0]), "label": str(it[1]), "name": str(it[1])}
        if isinstance(it, dict):
            return it
        return {}

    def _lbl(it: dict) -> str:
        return (it.get("label") or it.get("raw") or it.get("name") or "").strip()

    items = []
    for raw in (backpack_items or []):
        it = _to_item(raw)
        lbl = _lbl(it)
        if not it.get("cmd"):
            continue
        if "удоч" in lbl.lower():
            continue
        it["_lbl"] = lbl
        items.append(it)

    preferred: list[dict] = []
    fallback: list[dict] = []
    for it in items:
        lbl = it.get("_lbl", "")
        low = lbl.lower()
        if any(k in low for k in ("талисман", "амулет", "кольц", "аксесс")) or any(e in lbl for e in ("📿", "💍", "🎗")):
            preferred.append(it)
        else:
            fallback.append(it)
    return preferred + fallback


async def _ensure_no_rod_before_forest(client: TelegramClient, chat_id: str) -> None:
    """Перед Чащей пытается убрать удочку из аксессуаров, чтобы она не ломалась в бою.

    Логика:
    1) Если рюкзак НЕ полон — пробуем снять удочку кнопкой "Снять".
    2) Если рюкзак полон ИЛИ снять не удалось — делаем свап: надеваем любой талисман/аксессуар
       (кроме удочки) из рюкзака в тот же слот. Во многих экранах это работает как обмен местами,
       даже когда рюкзак заполнен.
    """

    snap = await _fetch_character(client)
    slots = (snap or {}).get("slots") or {}
    backpack = (snap or {}).get("backpack") or []

    rod_slot: str | None = None
    for k in ("a1", "a2", "a3"):
        v = (slots.get(k) or "")
        if isinstance(v, dict):
            name = (v.get("name") or "").lower()
        else:
            name = str(v).lower()
        if "удоч" in name:
            rod_slot = k
            break
    if not rod_slot:
        return

    inv_full = (get_kv("inventory_full", "0") or "0") == "1"

    # 1) Сначала всегда пробуем просто снять (иногда игра позволяет даже при "полном" рюкзаке,
    # или флаг inventory_full может быть устаревшим).
    ok = await _unequip_from_accessory_slot(client, rod_slot)
    if ok:
        if inv_full:
            log.info(f"🎒 Перед Чащей: снял удочку из {rod_slot} (хотя inventory_full=1)")
        else:
            log.info(f"🎒 Перед Чащей: снял удочку из {rod_slot}")
        return

    # 2) Свап — пробуем несколько кандидатов, кроме удочки.
    reps = _pick_non_rod_replacements(backpack)
    if not reps:
        log.warning("🎒 Перед Чащей: не нашёл замену (амулет/кольцо/любой предмет) для удочки")
        return

    replaced = False
    for rep in reps[:8]:
        label = (rep.get("name") or rep.get("label") or rep.get("raw") or rep.get("cmd") or "?")
        log.info(f"🎒 Перед Чащей: пытаюсь заменить удочку в {rod_slot} на '{label}'")
        try:
            ok_swap = await _equip_item_to_slot(client, chat_id, rep["cmd"], rod_slot)
            if ok_swap:
                replaced = True
                log.info(f"🎒 Перед Чащей: заменил удочку в {rod_slot} на '{label}'")
                break
        except Exception as e:
            log.warning(f"🎒 Перед Чащей: не удалось заменить удочку через '{label}': {e}")

    if not replaced:
        log.warning("🎒 Перед Чащей: не получилось заменить удочку ни одним предметом")


# ----------------- PET FLOW (Home -> Terrarium -> Pet all) -----------------

FURN_TERRARIUM_RE = re.compile(r"(?m)^(?P<cmd>/f_\d+)\s+.*террариум.*$")
# Pet commands often appear mid-line, e.g. "...: /t2_1 ... /t2_2 ...".
PET_CMD_RE = re.compile(r"(?P<cmd>/t\d+_\d+)\b")
TERRARIUM_CMD_RE = re.compile(r"/f_(\d+)\b")
TERRARIUM_TITLE_RE = re.compile(r"террариум\s+(\d+)", re.IGNORECASE)
PET_SELECTOR_RE = re.compile(r"/t(\d+)_(\d+)\b")
INVENTORY_CMD_RE = re.compile(r"(?m)^(?P<cmd>/i_[A-Za-z0-9]+)\s*(?P<rest>.*)$")

def _pet_extract_terrarium_cmds(home_text: str) -> list[str]:
    """From the Home message text, extract the /f_N command(s) for terrariums."""
    if not home_text:
        return []
    cmds: list[str] = []
    for line in home_text.splitlines():
        if "террариум" in line.lower():
            m = re.search(r"(/f_\d+)", line)
            if m:
                cmds.append(m.group(1))
    if not cmds:
        cmds = [m.group(1) for m in re.finditer(r"(/f_\d+)", home_text)]
    out: list[str] = []
    for c in cmds:
        if c not in out:
            out.append(c)
    return out

def _pet_extract_pet_buttons(msg) -> list[tuple[int,int,str]]:
    """Return [(row,col,text)] for buttons that look like individual pets.

    IMPORTANT: In the current Forest Spirits UI, the terrarium screen buttons
    are service actions like '🐾Прогулка', '🪹Кормушка', etc. They are NOT pet
    navigation buttons. Pet navigation is done via /t*_*
    commands in the message text (e.g. /t3_1).

    So we intentionally DO NOT treat generic buttons as pet selectors here.
    """
    return []


def _pet_extract_pet_cmds(text: str) -> list[str]:
    """Extract pet navigation commands from message text.

    We only keep commands that look like pet selectors: /t<terrarium>_<idx>
    Examples: /t3_1, /t12_8
    """
    if not text:
        return []
    cmds = [m.group(0) for m in re.finditer(r"/t\d+_\d+", text)]
    out: list[str] = []
    for c in cmds:
        if c not in out:
            out.append(c)
    return out


def _pet_parse_terrarium_no_from_cmd(cmd: str) -> int | None:
    m = TERRARIUM_CMD_RE.search(cmd or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _pet_parse_terrarium_no_from_screen(text: str) -> int | None:
    m = TERRARIUM_TITLE_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _pet_parse_selector_cmd(cmd: str) -> tuple[int, int] | None:
    m = PET_SELECTOR_RE.search(cmd or "")
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None


def _pet_extract_inventory_pet_cmds(text: str) -> list[str]:
    """Extract inventory commands that likely open helper/pet cards.

    We include:
      - explicit pet equipment slot command (/i_p),
      - backpack/equipment lines mentioning "питом" / "помощ".
    """
    if not text:
        return []
    # Some game responses collapse /inventory into a single line.
    # Split those dumps into pseudo-lines before applying line-based parsing.
    text = re.sub(r"\s+(?=/i_[A-Za-z0-9]+\b)", "\n", text)
    out: list[str] = []
    for m in INVENTORY_CMD_RE.finditer(text):
        cmd = (m.group("cmd") or "").strip()
        rest = (m.group("rest") or "").strip().lower()
        is_empty_slot = ("пуст" in rest) or ("нет" in rest and "питом" in rest)
        if is_empty_slot:
            continue


        if cmd == "/i_p" or ("питом" in rest) or ("помощ" in rest):
            if cmd and cmd not in out:
                out.append(cmd)
    return out




def _message_signature(msg) -> tuple[str, tuple[str, ...]]:
    txt = (getattr(msg, "message", "") or "").strip()
    return (txt, tuple(_button_labels(msg)))


async def _await_recent_message(
    client,
    chat,
    predicate,
    timeout: float = 12.0,
    poll: float = 0.35,
    after_id: int | None = None,
    baseline_msg=None,
    allow_same_id_updates: bool = False,
):
    """Poll recent messages and return the first that matches predicate.

    If ``after_id`` is provided, we normally accept only messages with ``id > after_id``.
    When ``allow_same_id_updates`` is True, we also accept an edited version of the same
    message id if its text/buttons changed relative to ``baseline_msg``. This is important
    for slow game bots that often update an existing message instead of sending a new one.
    """
    factor = max(1.0, float(getattr(CFG, "response_timeout_factor", 1.0) or 1.0))
    timeout = max(1.0, float(timeout) * factor)
    poll = max(float(poll), float(getattr(CFG, "response_poll_min_sec", 0.6) or 0.6))
    baseline_sig = _message_signature(baseline_msg) if baseline_msg is not None else None

    # NOTE: We poll chat history, which can hit Telegram flood-waits if done too aggressively.
    # In PET flow especially, Telegram may return FloodWaitError on GetHistoryRequest.
    # We handle it by sleeping and extending the deadline so the caller doesn't immediately time out.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msgs = await client.get_messages(chat, limit=12)
        except Exception as e:
            # Telethon raises FloodWaitError with attribute 'seconds'
            sec = getattr(e, "seconds", None)
            if sec is not None:
                log.info(f"Sleeping for {sec}s (0:00:{sec:02d}) on GetHistoryRequest flood wait")
                # Extend deadline: flood wait should not consume the caller's timeout budget.
                deadline += float(sec) + 1.0
                await asyncio.sleep(float(sec) + 1.0)
                continue
            raise
        for m in msgs:
            mid = getattr(m, "id", 0)
            if after_id is not None and mid <= after_id:
                if not (allow_same_id_updates and mid == after_id and baseline_sig is not None and _message_signature(m) != baseline_sig):
                    continue
            try:
                if predicate(m):
                    return m
            except Exception:
                # predicate errors should not kill the poll loop
                continue
        await asyncio.sleep(poll)
    return None


async def _pet_flow_run(client: TelegramClient) -> bool:
    """Run the full petting flow once. Returns True if we consider it successful."""
    if STORAGE.get("pet_flow_running"):
        return False
    STORAGE.set("pet_flow_running", True)
    try:
        # Tell the click humanizer we're in the pet flow.
        _kv_set("human_ctx", "pet")

        # If party is active, stop PET and defer until user enables it again.
        if is_party_active():
            log.warning("🐾 PET: обнаружена активная пати — выключаю /pet и жду ручного включения")
            _kv_set("pet_deferred", "1")
            _kv_set("mod_pet", "0")
            return False

        # Party has priority: if we are in a party right now,
        # don't do pet clicks (can mess up group UI). Defer until manual /pet on.
        if is_party_active():
            log.warning("🐾 PET: обнаружена активная пати → отключаю pet и откладываю")
            _kv_set("pet_deferred", "1")
            _kv_set("mod_pet", "0")
            return False
        chat = CFG.game_chat
        log.info("🐾 PET: стартую процедуру 'погладить всех'")

        async def _pet_human_delay(note: str, lo: float = 1.4, hi: float = 3.2) -> None:
            # PET flow should be visibly slower and consistent to avoid bursty /t_*
            # command chains that look bot-like.
            await _human_sleep(kind="cmd", lo=lo, hi=hi, note=f"PET {note}")

        # 0) Bring up a stable main menu
        await asyncio.sleep(human_delay_cmd("mode_switch"))
        await client.send_message(chat, "/character")

        # 1) Click "Дом" in the newest message with buttons
        # Use a slower poll to avoid FloodWait on GetHistoryRequest.
        m = await _await_recent_message(client, chat, lambda x: bool(getattr(x, "buttons", None)), timeout=20, poll=1.0)
        if not m:
            log.warning("🐾 PET: не нашёл сообщение с кнопками для входа в Дом")
            return False

        home_hint = str(getattr(CFG, "pet_home_button", "Дом") or "Дом")

        # IMPORTANT: in this game UI "Дом" is often a *reply-keyboard* button.
        # Telethon can't "click" reply buttons the same way it clicks inline buttons.
        # So: try click first; if we don't see a Home screen shortly, fall back to sending text.
        # User-facing requirement: prefer explicit "Дом" text as the first fallback.
        async def _home_opened(after_id: int | None) -> bool:
            probe = await _await_recent_message(
                client,
                chat,
                lambda x: ("🛖" in (x.message or "")) or ("/f_" in (x.message or "")) or ("уютный дом" in (x.message or "").lower()),
                timeout=8,
                poll=1.0,
                after_id=after_id,
                baseline_msg=m,
                allow_same_id_updates=True,
            )
            return probe is not None

        res = await click_button_contains(client, m, ["дом", home_hint, "🏠"])
        opened = False
        if res is not None:
            opened = await _home_opened(getattr(m, "id", None))

        if not opened:
            log.warning("🐾 PET: экран дома не открыт через кнопку — отправляю текст 'Дом'")
            await asyncio.sleep(human_delay_cmd("cmd"))
            await client.send_message(chat, "Дом")
            opened = await _home_opened(getattr(m, "id", None))

        if (not opened) and home_hint.strip() and home_hint.strip().lower() != "дом":
            log.warning("🐾 PET: 'Дом' не сработал → отправляю альтернативный текст кнопки: %s", home_hint)
            await asyncio.sleep(human_delay_cmd("cmd"))
            await client.send_message(chat, home_hint)
# 2) Wait for furniture list, parse terrariums
        # After we click "Дом", wait for furniture list (with /f_*) without hammering history.
        after = getattr(m, "id", None)
        furn = await _await_recent_message(
            client,
            chat,
            lambda x: ("/f_" in (x.message or "")) and ("мебел" in (x.message or "").lower() or "террариум" in (x.message or "").lower()),
            timeout=45,
            poll=1.2,
            after_id=after,
            baseline_msg=m,
            allow_same_id_updates=True,
        )
        if not furn:
            # Some UIs don't include 'мебель' word; still try any message with /f_ and 'терра'
            furn = await _await_recent_message(
                client,
                chat,
                lambda x: ("/f_" in (x.message or "")) and ("террариум" in (x.message or "").lower()),
                timeout=25,
                poll=1.2,
                after_id=after,
                baseline_msg=m,
                allow_same_id_updates=True,
            )
        if not furn:
            log.warning("🐾 PET: не дождался списка мебели с террариумами → пауза 4 часа")
            _kv_set("pet_blocked_until_ts", str(time.time() + 4 * 60 * 60))
            return False

        # If the game says the hero is currently "в походе", terrarium interaction is blocked.
        # In this case, we back off for 4 hours to avoid repeated retries and flood waits.
        try:
            recent = await client.get_messages(chat, limit=6)
        except Exception:
            recent = []
        hike = None
        for rm in recent:
            txt = (rm.message or "").lower()
            if "сейчас в походе" in txt or ("в походе" in txt and "сейчас" in txt):
                hike = rm
                break
        if hike and hike.id >= furn.id:
            until = time.time() + 4 * 60 * 60
            set_kv("pet_blocked_until_ts", str(until))
            set_kv("pet_blocked_reason", "hero_in_hike")
            set_kv("pet_next_due_ts", str(until))
            log.warning("🐾 PET: герой 'в походе' — откладываю поглаживание на 4 часа.")
            return False

        terr_cmds = [m.group("cmd") for m in FURN_TERRARIUM_RE.finditer(furn.message or "")]
        terr_cmds = list(dict.fromkeys(terr_cmds))  # keep order, unique
        if not terr_cmds:
            log.warning("🐾 PET: в доме не нашёл ни одного 'террариум' (/f_*) → пауза 4 часа")
            _kv_set("pet_blocked_until_ts", str(time.time() + 4 * 60 * 60))
            return False
        log.info(f"🐾 PET: террариумов найдено: {len(terr_cmds)} → {terr_cmds}")

        total_pets = 0
        petted = 0

        async def _pet_open_and_stroke_by_cmd(open_cmd: str, origin: str) -> bool:
            """Open an entity card by command and click pet action if available."""
            before_id = None
            before_msg = None
            try:
                last = await client.get_messages(chat, limit=1)
                if last:
                    before_msg = last[0]
                    before_id = last[0].id
            except Exception:
                before_id = None
                before_msg = None

            await _pet_human_delay(f"before command {open_cmd}", 1.2, 2.8)
            await client.send_message(chat, open_cmd)
            await _pet_human_delay("open pet card", 1.3, 3.0)


            # Some commands (especially /i_p) may point to an empty slot.
            # Detect this early and treat as a normal "nothing to pet" case.
            empty_reply = await _await_recent_message(
                client,
                chat,
                lambda x: "слот пуст" in ((x.message or "").lower()),
                timeout=4,
                poll=1.0,
                after_id=before_id,
                baseline_msg=before_msg,
                allow_same_id_updates=True,
            )
            if empty_reply is not None:
                log.info(f"🐾 PET: {origin}: {open_cmd} пустой слот — пропускаю")
                return False


            try:
                pet_view = await _await_recent_message(
                    client,
                    chat,
                    lambda x: bool(getattr(x, "buttons", None))
                    and any("поглад" in (bb.text or "").lower() for row in x.buttons for bb in row),
                    timeout=10,
                    poll=1.0,
                    after_id=before_id,
                    baseline_msg=before_msg,
                    allow_same_id_updates=True,
                )
            except Exception:
                pet_view = None

            if not pet_view:
                log.warning(f"🐾 PET: {origin}: не получил карточку по команде {open_cmd}")
                return False

            res = await click_button_contains(client, pet_view, ["Поглад", "Почес", "Приласк"])
            if res is not None:
                log.info(f"🐾 PET: {origin}: глажу ({open_cmd})")
                await _pet_human_delay("after pet action", 1.1, 2.4)
                return True

            log.warning(f"🐾 PET: {origin}: кнопка 'Погладить' не найдена ({open_cmd})")
            return False

        for fcmd in terr_cmds:
            # Если в этот момент обнаружилась пати — прекращаю петов и жду ручного включения.
            if kv.get("party_active", "0") == "1":
                log.warning("🐾 PET: обнаружена пати → выключаю pet и выхожу (жду /pet on).")
                kv.set("mod_pet", "0")
                kv.set("human_ctx", "")
                return

            # Открываем конкретный террариум по команде /f_N
            log.info(f"🐾 PET: открываю террариум {fcmd}")
            expected_terr_no = _pet_parse_terrarium_no_from_cmd(fcmd)
            # NOTE: In this codebase there is no _wait_recent() (it existed in older builds).
            # Using it would silently fail (caught by broad except) and we'd never see the terrarium screen.
            # So we always use _await_recent_message() which polls chat history carefully.
            try:
                before_id = None
                try:
                    last = await client.get_messages(chat, limit=1)
                    if last:
                        before_id = last[0].id
                except Exception:
                    before_id = None

                await client.send_message(chat, fcmd)
                await _pet_human_delay(f"open terrarium {fcmd}", 1.5, 3.6)

                def _is_terrarium_screen(x) -> bool:
                    txt = (x.message or "")
                    low = txt.lower()
                    if "террариум" not in low:
                        return False
                    if expected_terr_no is not None:
                        seen_terr_no = _pet_parse_terrarium_no_from_screen(txt)
                        if seen_terr_no != expected_terr_no:
                            return False
                    # Typical terrarium screen contains pets list and/or /t.. commands.
                    if "питомц" in low:
                        return True
                    if re.search(r"/t\d+_\d+", txt):
                        return True
                    # Fallback: some versions show only terrarium header + buttons.
                    return bool(getattr(x, "buttons", None))

                terr = await _await_recent_message(
                    client,
                    chat,
                    _is_terrarium_screen,
                    timeout=18,
                    poll=1.0,
                    after_id=before_id,
                    baseline_msg=last[0] if last else None,
                    allow_same_id_updates=True,
                )
            except Exception:
                terr = None

            if not terr:
                log.warning(f"🐾 PET: не получил экран террариума после {fcmd}")
                continue

            pet_btns = _pet_extract_pet_buttons(terr)
            pet_cmds = _pet_extract_pet_cmds(terr.message)

            # Prefer /t*_* commands over buttons (terrarium buttons are service actions)
            if pet_cmds:
                pet_btns = []

            total_pets += max(len(pet_btns), len(pet_cmds))

            if not pet_btns and not pet_cmds:
                log.warning(f"🐾 PET: не нашёл питомцев в террариуме {fcmd}")
                continue

            log.info(f"🐾 PET: {fcmd}: питомцев найдено: buttons={len(pet_btns)}, cmds={len(pet_cmds)}")

            async def _pet_back_from_pet_view() -> bool:
                # Возвращаемся в список питомцев/террариум
                # NOTE: _wait_recent() doesn't exist in this build; use chat polling helper.
                last = await _await_recent_message(
                    client,
                    chat,
                    lambda x: True,
                    timeout=4,
                    poll=1.0,
                )
                last = last or terr
                # Try to go back from pet card
                res = await click_button_contains(client, last, ["Назад", "Террариум"])
                if res is not None:
                    return True
                return False

            # 1) Проходим по кнопкам-питомцам (если они есть)
            for (r, c, t) in pet_btns:
                if kv.get("party_active", "0") == "1":
                    log.warning("🐾 PET: пати появилась во время процедуры → выключаю pet и выхожу.")
                    kv.set("mod_pet", "0")
                    kv.set("human_ctx", "")
                    return

                log.info(f"🐾 PET: {fcmd}: открываю питомца '{t}'")
                try:
                    last = await _await_recent_message(
                        client,
                        chat,
                        lambda x: bool(getattr(x, "buttons", None)),
                        timeout=8,
                        poll=1.0,
                        after_id=getattr(terr, "id", None),
                        baseline_msg=terr,
                        allow_same_id_updates=True,
                    )
                    last = last or terr
                    # Use centralized click helper so human_delay_click() is applied.
                    await click_button(client, last, pos=(r, c))
                except Exception:
                    log.warning(f"🐾 PET: не смог открыть питомца '{t}' (кнопка)")
                    continue

                await _pet_human_delay("open pet card", 1.3, 3.0)

                try:
                    pet_view = await _await_recent_message(
                        client,
                        chat,
                        lambda x: bool(getattr(x, "buttons", None))
                        and any("поглад" in (bb.text or "").lower() for row in x.buttons for bb in row),
                        timeout=10,
                        poll=1.0,
                        after_id=getattr(last, "id", None),
                        baseline_msg=last,
                        allow_same_id_updates=True,
                    )
                except Exception:
                    pet_view = None

                if pet_view:
                    # Pet card view: click 'Погладить' (inline button)
                    res = await click_button_contains(client, pet_view, ["Поглад", "Почес", "Приласк"])
                    if res is not None:
                        log.info("🐾 PET: глажу")
                        petted += 1
                        await _pet_human_delay("after pet action", 1.1, 2.4)
                    else:
                        log.warning("🐾 PET: кнопка 'Погладить' не найдена в карточке питомца")
                else:
                    log.warning("🐾 PET: не получил карточку питомца (таймаут)")

                await _pet_back_from_pet_view()
                await _pet_human_delay("return to terrarium", 1.0, 2.2)

            # 2) Фолбэк: если кнопок не было — открываем питомцев командами
            if not pet_btns and pet_cmds:
                for pcmd in pet_cmds:
                    if kv.get("party_active", "0") == "1":
                        log.warning("🐾 PET: пати появилась во время процедуры → выключаю pet и выхожу.")
                        kv.set("mod_pet", "0")
                        kv.set("human_ctx", "")
                        return

                    log.info(f"🐾 PET: {fcmd}: открываю питомца через команду {pcmd}")
                    if await _pet_open_and_stroke_by_cmd(pcmd, fcmd):
                        petted += 1
                    await _pet_back_from_pet_view()
                    await _pet_human_delay("return to terrarium", 1.0, 2.2)

        # 3) Also pet helper-pets available in gear/inventory (e.g. /i_p slot or pet helper items).
        await _pet_human_delay("open inventory for helper pet", 1.6, 3.4)
        await client.send_message(chat, "/inventory")
        inv_msg = await _await_recent_message(
            client,
            chat,
            lambda x: "/i_" in (x.message or ""),
            timeout=16,
            poll=1.1,
        )
        if inv_msg:
            inv_cmds = _pet_extract_inventory_pet_cmds(inv_msg.message or "")
            if inv_cmds:
                log.info(f"🐾 PET: инвентарь/экип: найдено pet-команд: {inv_cmds}")
            for icmd in inv_cmds:
                total_pets += 1
                if await _pet_open_and_stroke_by_cmd(icmd, "inventory"):
                    petted += 1
                    await _pet_human_delay("return from inventory pet", 1.0, 2.0)
        else:
            log.warning("🐾 PET: не дождался /inventory для поиска pet-помощника")

        log.info(f"🐾 PET: завершил. петов={total_pets}, поглажено={petted}")
        # Even if some failed, consider run ok if at least one action happened.
        return petted > 0
    finally:
        STORAGE.delete("pet_flow_running")
        # Clear explicit human context.
        if (get_kv("human_ctx") or "").strip().lower() == "pet":
            _kv_set("human_ctx", "")


async def _pet_flow_driver(client: TelegramClient):
    """Wrapper that schedules next run and restores normal mode after pet flow."""
    try:
        ok = await _pet_flow_run(client)
        if ok:
            now = _now_ts()
            _kv_set("pet_last_done_ts", f"{now:.3f}")
            # Special case: right after manual '/pet on' we want:
            #   1) pet immediately
            #   2) then pause automation for 1-2 hours
            #   3) and schedule next pet run also in 1-2 hours
            if get_kv("pet_on_immediate", "0") == "1":
                _kv_set("pet_on_immediate", "0")
                mn_h, mx_h = 1.0, 2.0
                nxt = _pet_schedule_next_range_hours(now, mn_h, mx_h)
                # Timed global pause (doesn't require manual /pause).
                pause_sec = max(60.0, nxt - now)
                set_pause_for_seconds(pause_sec)
                eta_min = int(max(0, (nxt - now) // 60))
                log.info(f"🐾 PET: погладил по /pet on. Пауза ~{eta_min} мин.")
            else:
                nxt = _pet_schedule_next(now)
                eta_min = int(max(0, (nxt - now) // 60))
                log.info(f"🐾 PET: выполнено. Следующий запуск через ~{eta_min} мин.")
        else:
            # Back off a bit to avoid tight retry loops (especially after UI changes / lag / FloodWait).
            now = _now_ts()
            backoff = random.uniform(10 * 60, 20 * 60)  # 10-20 minutes
            nxt = now + backoff
            _kv_set("pet_next_due_ts", f"{nxt:.3f}")
            eta_min = int(max(0, (nxt - now) // 60))
            log.warning(f"🐾 PET: завершился без действий → повторю не раньше чем через ~{eta_min} мин")
    except Exception as e:
        log.exception(f"🐾 PET: авария в pet-flow: {e!r}")
        # Backoff a bit to avoid a tight crash-loop
        now = _now_ts()
        backoff = random.uniform(10 * 60, 20 * 60)
        _kv_set("pet_next_due_ts", f"{(now + backoff):.3f}")

    finally:
        # Return control back to normal scheduler
        if (get_kv("active_mode") or "").strip().lower() == "pet":
            _kv_set("active_mode", "forest")

        # If Pet was initiated from fishing, resume fishing afterwards (unpause).
        if (get_kv("fish_resume_after_pet", "0") or "0") == "1":
            _kv_set("fish_resume_after_pet", "0")
            prev = (get_kv("fish_prev_mod_fishing", "1") or "1") == "1"
            _kv_set("fish_prev_mod_fishing", "")

            # Important: if pet flow was deferred/aborted because party is active,
            # do NOT auto-return to fishing. This keeps the bot in forest/idle and
            # avoids immediately restarting fishing after '/pet on' was disabled.
            pet_deferred = (get_kv("pet_deferred", "0") or "0") == "1"
            pet_enabled = (get_kv("mod_pet", "0") or "0") == "1"
            if pet_deferred or not pet_enabled:
                why = "deferred" if pet_deferred else "disabled"
                log.info(f"🎣🐾 PET: автовозврат к рыбалке пропущен (pet {why})")
                return

            if prev:
                # restore fishing toggle and re-enter fishing
                set_mod_fishing_enabled(True)
                try:
                    await asyncio.sleep(human_delay_cmd("mode_switch"))
                    await client.send_message(CFG.game_chat, "Рыбалка")
                    _kv_set("active_mode", "fishing")
                    _kv_set("mode_last_switch_ts", str(_now_ts()))
                    log.info("🎣🐾 PET: завершил — возвращаюсь к рыбалке")
                except Exception as e:
                    log.warning("🎣🐾 PET: не смог вернуться к рыбалке: %s", e)

async def _switch_mode(client: TelegramClient, mode: str):
    if mode == "forest":
        # Перед Чащей снимаем/заменяем удочку (чтобы не ломалась в бою)
        await _ensure_no_rod_before_forest(client, CFG.game_chat)
        # Apply combat set (priority-based) before going to forest.
        await _apply_set(client, "combat")
        # Open forest screen
        try:
            await asyncio.sleep(human_delay_cmd("mode_switch"))
            await client.send_message(CFG.game_chat, CFG.forest_fallback_cmd)
        except Exception as e:
            log.error(f"🔀 MODE: не удалось отправить {CFG.forest_fallback_cmd!r}: {e}")
            return
        _kv_set("active_mode", "forest")
        log.info("🔀 MODE -> forest")
        return

    if mode == "fishing":
        # If we are already on a fishing screen (e.g. ...)
        try:
            last_stage = (get_kv("last_stage") or "").strip().lower()
            last_up = float(get_kv("last_update_ts") or "0")
        except Exception:
            last_stage, last_up = "", 0.0

        if last_stage.startswith("fishing_") and (_now_ts() - last_up) <= 180:
            # Already fishing: do not spam /inventory or send another "Рыбалка".
            _kv_set("active_mode", "fishing")
            log.info("🎣 Уже в рыбалке — продолжаю без повторного входа.")
            return

        ok = await _ensure_best_rod_equipped(client)
        if ok is None:
            # Temporary: game didn't give /inventory or UI busy. Do not disable fishing.
            log.warning("🎣 Не удалось проверить/экипировать удочку сейчас (timeout/кд). Попробую позже.")
            return
        if ok is False:
            # Hard failure: no rod.
            log.warning("🎣 Нет рабочей удочки. Выключаю рыбалку (fishing-off).")
            _disable_fishing("no_working_rod")
            return

        # Start fishing loop (the rest is driven by incoming messages/buttons)
        try:
            await asyncio.sleep(human_delay_cmd("mode_switch"))
            await client.send_message(CFG.game_chat, "Рыбалка")
        except Exception as e:
            log.warning("🎣 Не удалось отправить команду рыбалки: %s", e)
            return

        _kv_set("active_mode", "fishing")
        log.info("🔀 MODE -> fishing")
        return


async def mode_manager_loop(client: TelegramClient):
    """Background loop: auto-switch between Forest and Fishing."""
    # Small startup delay to let the client fully connect
    await asyncio.sleep(2.0)

    while True:
        try:
            # Hard cooldown after meeting multiple golems: do nothing for a while.
            golem_left = _golem_cd_remaining_sec()
            if golem_left > 0:
                log.info(f"🪨 Пауза после големов ещё {golem_left}s — действий не выполняю.")
                await asyncio.sleep(min(30.0, float(golem_left)))
                continue

            desired = _desired_mode()
            current = (get_kv("active_mode") or "").strip().lower() or "unknown"

            # do not spam switches
            try:
                last_sw = float(get_kv("mode_last_switch_ts") or "0")
            except Exception:
                last_sw = 0.0
            now = _now_ts()

            # --- PET MODE (night home-care) ---
            if desired == "pet":
                # We need to operate from forest UI (not fishing).
                if current == "fishing":
                    # For Pet we want: finish the current catch (if any), then pause fishing, pet, and resume.
                    # We stop *new casts*, but we allow the handler to process a bite and reach the result screen.
                    if (get_kv("fish_stop_cast") or "0") != "1":
                        _kv_set("fish_stop_cast", "1")
                        _kv_set("fish_stop_cast_since", str(now))
                    _kv_set("fish_stop_cast_kind", "pet")
                    _kv_set("pending_mode", "pet")
                    _kv_set("fish_resume_after_pet", "1")
                    _kv_set("fish_prev_mod_fishing", "1" if mod_fishing_enabled() else "0")

                    # Fail-safe: if fishing screen stays silent for too long, stop waiting for fish
                    # and force-leave to forest to start pet-flow.
                    force_wait = float(getattr(CFG, "pet_force_switch_on_silence_sec", 60.0))
                    try:
                        since = float(get_kv("fish_stop_cast_since", "0") or 0.0)
                    except Exception:
                        since = 0.0
                    try:
                        last_up = float(get_kv("last_update_ts", "0") or 0.0)
                    except Exception:
                        last_up = 0.0
                    waited = (now - since) if since > 0 else 0.0
                    silent = (now - last_up) if last_up > 0 else 10**9
                    if waited >= force_wait and silent >= force_wait:
                        log.warning(
                            "🐾 MODE: fishing -> pet: нет обновлений %.0fs (ожидание %.0fs) → форсирую выход в лес",
                            silent,
                            waited,
                        )
                        class _DummyState:
                            pass
                        dummy = _DummyState()
                        dummy.active_mode = "fishing"
                        await _leave_fishing_to_forest(client, dummy, reason="pet_silent_timeout")
                        await asyncio.sleep(2.0)
                        continue

                    log.info("🐾 MODE: fishing -> pet (жду поимку рыбки, затем петы, потом вернусь к рыбалке)")
                    await asyncio.sleep(5.0)
                    continue
                # If we are not on forest UI, move there first.
                if current not in ("forest", "pet") and (now - last_sw) >= 10:
                    log.info(f"🐾 MODE: {current} -> forest (под Pet)")
                    await _switch_mode(client, "forest")
                    _kv_set("mode_last_switch_ts", str(_now_ts()))
                    await asyncio.sleep(5.0)
                    continue

                # Mark as pet mode (blocks other clickers in future extensions).
                if current != "pet":
                    _kv_set("active_mode", "pet")

                # Launch flow once.
                if not STORAGE.get("pet_flow_running"):
                    asyncio.create_task(_pet_flow_driver(client))

                await asyncio.sleep(5.0)
                continue


            # If desired == current, we normally do nothing. But after restarts the UI can be stale
            # and we may sit forever without receiving new updates. In that case, send a light
            # 'kick' command to refresh the menu. Throttled to avoid spam.
            try:
                last_up = float(get_kv("last_update_ts") or "0")
            except Exception:
                last_up = 0.0
            last_stage = (get_kv("last_stage") or "").strip().lower()
            try:
                last_kick = float(get_kv("mode_last_kick_ts") or "0")
            except Exception:
                last_kick = 0.0
            # If party is active and chat is silent for too long, refresh party UI
            # and try to continue with 'Осмотреться'.
            if is_party_active():
                stale_party = (now - last_up) > 180.0
                try:
                    last_party_kick = float(get_kv("party_idle_kick_ts") or "0")
                except Exception:
                    last_party_kick = 0.0
                if stale_party and (now - last_party_kick) > 180.0:
                    log.info("🤝⏱️ PARTY: тишина >3м → отправляю /party и жму кнопку 'Осмотреться' (без текста)")
                    try:
                        await asyncio.sleep(human_delay_cmd("mode_switch"))
                        await client.send_message(CFG.game_chat, "/party")
                        await asyncio.sleep(human_delay_cmd("battle"))

                        recent = await _await_recent_message(
                            client,
                            CFG.game_chat,
                            predicate=lambda m: _find_pos_by_substring(m, "осмотреться") is not None,
                            timeout=4.0,
                            poll=0.7,
                        )
                        if recent is not None:
                            pos_look = _find_pos_by_substring(recent, "осмотреться")
                            if pos_look is not None:
                                await click_button(client, recent, pos=pos_look)
                    except Exception:
                        pass
                    _kv_set("party_idle_kick_ts", str(now))
                    await asyncio.sleep(5.0)
                    continue

            if desired == current and desired in ("forest", "fishing"):
                stale = (now - last_up) > 20.0
                if stale and (now - last_kick) > 30.0:
                    if desired == "forest" and last_stage not in ("battle", "post_battle"):
                        log.info("MODE: forest stale - kick forest menu")
                        await asyncio.sleep(human_delay_cmd("mode_switch"))
                        await client.send_message(CFG.game_chat, CFG.forest_fallback_cmd)
                        _kv_set("mode_last_kick_ts", str(now))
                        await asyncio.sleep(5.0)
                        continue
                    # While fishing, long quiet windows are normal (we can sit on
                    # fishing_wait without any incoming updates for a while).
                    # Do not re-send "Рыбалка" if we are already on any fishing
                    # screen, otherwise MODE loop can spam chat every stale tick.
                    if desired == "fishing" and (not last_stage.startswith("fishing_")):
                        log.info("MODE: fishing stale - kick fishing menu")
                        await asyncio.sleep(human_delay_cmd("mode_switch"))
                        await client.send_message(CFG.game_chat, "Рыбалка")
                        _kv_set("mode_last_kick_ts", str(now))
                        await asyncio.sleep(5.0)
                        continue
            if desired != "idle" and desired != current and (now - last_sw) >= 10:
                # Safety gate: don't auto-switch away from Forest while we are still
                # on a forest/battle/post-battle screen. Otherwise we can interrupt
                # heal/continue prompts and lose actions.
                last_stage = (get_kv("last_stage") or "").strip()
                if current == "forest" and desired == "fishing" and last_stage == "battle":
                    log.info(f"🔀 MODE: жду завершения боя (stage={last_stage})")
                    await asyncio.sleep(5.0)
                    continue

                # Special case: fishing -> forest.
                # We must stop sending "Закинуть" first. We'll still allow finishing
                # an already-triggered bite, then the fishing handler will exit to forest.
                if current == "fishing" and desired == "forest":
                    # Do not keep resetting stop_cast_since; it breaks the grace window in fishing handler.
                    if (get_kv("fish_stop_cast") or "0") != "1":
                        _kv_set("fish_stop_cast", "1")
                        _kv_set("fish_stop_cast_since", str(now))
                    _kv_set("pending_mode", "forest")
                    log.info("🔀 MODE: fishing -> forest (запланировано: перестаю закидывать, ловлю поклёвку если будет)")
                    await asyncio.sleep(5.0)
                    continue

                log.info(f"🔀 MODE: {current} -> {desired}")
                await _switch_mode(client, desired)
                _kv_set("mode_last_switch_ts", str(_now_ts()))
        except Exception as e:
            log.error(f"🔀 MODE loop error: {e}")

        await asyncio.sleep(8.0)

async def run():
    init_db()
    client = await setup_client()

    @client.on(events.NewMessage(chats=[CFG.game_chat]))
    async def on_new(event):
        await handle_game_event(client, event, "new")

    @client.on(events.MessageEdited(chats=[CFG.game_chat]))
    async def on_edit(event):
        await handle_game_event(client, event, "edit")



    # ===== CONTROL HANDLER (Saved Messages + Control Chat) =====
    @client.on(events.NewMessage())
    async def on_control(evt):
        # ВАЖНО: управляющие команды принимаем ТОЛЬКО в "Saved Messages"
        # (чат с самим собой). Это исключает утечки статусов/ответов в игровой чат.
        if not (evt.is_private and evt.chat_id == CFG.owner_id):
            return

        text = (evt.raw_text or "").strip().lower()

        # Чтобы не палиться в игровом чате — команды управления обрабатываем
        # только в «Избранном» и только по префиксу '/'.
        if not (
            text.startswith("/")
        ):
            return
        #   /heal on|off
        #   /pet on|off
        #   /work on|off
        #   /dungeon on|off
        #   /pause [мин]
        #   /resume
        #   /debug on|off
        #   /human on|off
        if text.startswith("!"):
            return

        log.info(f"[CONTROL] {text}")

        if text == "/pause":
            set_paused(True)
            await evt.reply("⏸️ Пауза включена. Рыбалка работает.")
            return

        if text == "/resume":
            set_paused(False)
            await evt.reply("▶️ Продолжаю.")
            return
        if text in ("/help", "/faq"):
            await evt.reply(_FAQ_TEXT)
            return

        if text in ("/version", "/ver"):
            build = _compute_build_id()
            await evt.reply(f"📦 version={getattr(CFG, 'app_version', 'unknown')}\n🔖 build={build}")
            return



        # Party control (только в «Избранном»)
        #   /party on|off|status
        #   /partyhp <percent>|status
        #   /driver on|off|auto|status
        if text.startswith("/driver"):
            parts = text.split()
            if len(parts) == 1 or (len(parts) == 2 and parts[1] in ("status", "s")):
                mode = party_driver_mode()
                is_lead = "yes" if _kv_bool("party_is_leader", False) else "no"
                self_name = (get_kv("party_self_name") or "-").strip()
                leader_name = (get_kv("party_leader_name") or "-").strip()
                effective = "driver" if is_party_driver() else "passive"
                await evt.reply(
                    f"🚗 driver_mode={mode}, effective={effective}, is_leader={is_lead}\n"
                    f"self={self_name}, leader={leader_name}\n"
                    "Команды: /driver on|off|auto | /driver status"
                )
                return
            if len(parts) == 2 and parts[1] in ("on", "off", "auto"):
                _set_party_driver_mode(parts[1])
                await evt.reply(f"🚗 driver_mode={party_driver_mode()} (effective={'driver' if is_party_driver() else 'passive'})")
                return
            await evt.reply("Формат: /driver on|off|auto | /driver status")
            return

        if text.startswith("/partyhp"):
            parts = text.split()
            if len(parts) == 1 or (len(parts) == 2 and parts[1] in ("status","s")):
                cur = float(get_kv("party_heal_threshold_pct") or getattr(CFG, "party_heal_threshold_pct", 0.6))
                await evt.reply(f"🤝 party_hp_threshold={int(round(cur*100))}%\nКоманда: /partyhp 60")
                return
            if len(parts) == 2:
                try:
                    pct = int(parts[1])
                except Exception:
                    pct = -1
                if pct < 10 or pct > 100:
                    await evt.reply("Формат: /partyhp 10..100 (например /partyhp 60)")
                    return
                set_kv("party_heal_threshold_pct", str(pct/100.0))
                await evt.reply(f"🤝 party_hp_threshold={pct}%")
                return
            await evt.reply("Формат: /partyhp 10..100 | /partyhp status")
            return

        if text.startswith("/party"):
            parts = text.split()
            if len(parts) == 1 or (len(parts) == 2 and parts[1] in ("status","s")):
                enabled = "on" if mod_party_enabled() else "off"
                active = "yes" if is_party_active() else "no"
                cur = float(get_kv("party_heal_threshold_pct") or getattr(CFG, "party_heal_threshold_pct", 0.6))
                await evt.reply(
                    f"🤝 party={enabled}, active={active}, hp_threshold={int(round(cur*100))}%\n"
                    "Команды: /party on|off | /partyhp 60"
                )
                return
            if len(parts) == 2 and parts[1] in ("on","off"):
                on = (parts[1] == "on")
                set_kv("mod_party", "1" if on else "0")
                await evt.reply("🤝 party=" + ("on" if on else "off"))
                return
            await evt.reply("Формат: /party on|off | /party status | /partyhp 60")
            return


        # Debug control
        #   /debug on|off
        #   /debug buttons on|off
        #   /debug kv on|off
        #   /debug choose on|off
        #   /debug status
        if text.startswith("/debug"):
            parts = text.split()
            if len(parts) == 1 or (len(parts) == 2 and parts[1] in ("status", "s")):
                await evt.reply(
                    "🐛 debug="
                    + ("on" if dbg_enabled() else "off")
                    + f" buttons={'on' if _dbg_flag('debug_buttons','0') else 'off'}"
                    + f" kv={'on' if _dbg_flag('debug_kv','0') else 'off'}"
                    + f" choose={'on' if _dbg_flag('debug_choose','0') else 'off'}"
                    + "\nКоманды: /debug on|off, /debug buttons on|off, /debug kv on|off, /debug choose on|off"
                )
                return

            if len(parts) == 2 and parts[1] in ("on", "off"):
                on = (parts[1] == "on")
                _kv_set("debug_enabled", "1" if on else "0")
                # When enabling, default to useful sub-flags ON unless they were already set.
                if on:
                    if _kv_get("debug_buttons","") == "":
                        _kv_set("debug_buttons", "1")
                    if _kv_get("debug_kv","") == "":
                        _kv_set("debug_kv", "1")
                    if _kv_get("debug_choose","") == "":
                        _kv_set("debug_choose", "1")
                await evt.reply("🐛 debug=" + ("on" if on else "off"))
                return

            if len(parts) == 3 and parts[1] in ("buttons", "kv", "choose") and parts[2] in ("on", "off"):
                key = "debug_" + parts[1]
                _kv_set(key, "1" if parts[2] == "on" else "0")
                await evt.reply(f"🐛 {parts[1]}=" + ("on" if parts[2] == "on" else "off"))
                return

            await evt.reply("Формат: /debug on|off | /debug buttons on|off | /debug kv on|off | /debug choose on|off | /debug status")
            return

        # Human-like clicking control
        #   /human on|off
        #   /human status
        if text.startswith("/human"):
            parts = text.split()
            if len(parts) == 1 or (len(parts) == 2 and parts[1] in ("status", "s")):
                try:
                    from ratelimit import humanize_enabled

                    cur = "on" if humanize_enabled() else "off"
                except Exception:
                    v = (get_kv("humanize") or "").strip()
                    cur = "on" if v in ("1", "on", "true") else "off"
                await evt.reply(
                    f"🧍 human={cur}\nКоманды: /human on|off | /human status\n"
                    "ENV: HUMANIZE=1, HUMAN_CLICK_MIN/MAX, HUMAN_THIEF_MIN/MAX, HUMAN_FISH_HOOK_MIN/MAX, HUMAN_THINK_P"
                )
                return

            if len(parts) == 2 and parts[1] in ("on", "off"):
                on = (parts[1] == "on")
                _kv_set("humanize", "1" if on else "0")
                await evt.reply("🧍 human=" + ("on" if on else "off"))
                return

            await evt.reply("Формат: /human on|off | /human status")
            return

        def _fmt_sec(sec: int) -> str:
            if sec < 0:
                return "-"
            if sec < 60:
                return f"{sec}s"
            m, s = divmod(sec, 60)
            if m < 60:
                return f"{m}m{s:02d}s"
            h, m = divmod(m, 60)
            return f"{h}h{m:02d}m"

        def _kv_iso_to_left(key: str) -> int:
            """Return remaining seconds for ISO8601 stored in KV, else 0."""
            val = (get_kv(key) or "").strip()
            if not val:
                return 0
            try:
                until = datetime.fromisoformat(val)
                return int(max(0, (until - datetime.now()).total_seconds()))
            except Exception:
                return 0

        if text in ("/fishtriggers", "/fish_triggers", "/triggers fish"):
            bite = "\n".join([f"- {x}" for x in BITE_TRIGGERS])
            result = "\n".join([f"- {x}" for x in RESULT_TRIGGERS])
            await evt.reply(
                "🎣 Активные триггеры рыбалки (из game_parser.py)\n\n"
                f"BITE_TRIGGERS ({len(BITE_TRIGGERS)}):\n{bite}\n\n"
                f"RESULT_TRIGGERS ({len(RESULT_TRIGGERS)}):\n{result}\n\n"
                "Эвристики bite: poplav+pull | klyov+podsek/pull | leska+natyan."
            )
            return

        if text in ("/status", "/statusv", "/status v", "/status verbose"):
            left = _health_cd_remaining_sec()
            nxt = int(max(0, _fish_next_allowed_ts() - _now_ts()))
            now = _now_ts()
            # Timed global pause (1-2h after /pet on etc.)
            try:
                pu = float(get_kv("paused_until_ts") or "0")
            except Exception:
                pu = 0.0
            pause_left = int(max(0, pu - now)) if pu > 0 else 0
            pause_left_str = _fmt_sec(pause_left) if pause_left > 0 else "-"
            try:
                pet_next = float(get_kv("pet_next_due_ts") or "0")
            except Exception:
                pet_next = 0.0
            pet_left = int(max(0, pet_next - now)) if pet_next > 0 else -1
            pet_left_str = _fmt_sec(pet_left)
            pet_last = (get_kv("pet_last_done_ts") or get_kv("pet_last_done_date") or "-").strip()
            interval = f"{getattr(CFG,'pet_interval_min_hours',1)}-{getattr(CFG,'pet_interval_max_hours',2)}h"

            # Common (short) status
            lines = [
                f"paused={is_paused()} pause_left={pause_left_str} health_cd={left//60}m{left%60:02d}s fish_cd={nxt}s",
                f"forest={'on' if mod_forest_enabled() else 'off'} lvl={get_kv('forest_level','?')}",
                f"blood={'on' if blood_enabled() else 'off'} hp={get_kv('hp_pct','?')}% low/high={blood_hp_low()}/{blood_hp_high()} bloodLevel={blood_level()} effective_lvl={get_kv('forest_level_effective','?')}",
                f"fishing={'on' if mod_fishing_enabled() else 'off'}",
                f"heal={'on' if mod_heal_enabled() else 'off'}",
                f"work={'on' if mod_work_enabled() else 'off'}",
                f"dungeon={'on' if mod_dungeon_enabled() else 'off'}",
                f"party={'on' if mod_party_enabled() else 'off'} active={'yes' if is_party_active() else 'no'} hp_threshold={int(round(float(get_kv('party_heal_threshold_pct') or getattr(CFG, 'party_heal_threshold_pct', 0.6))*100))}%",
                f"driver_mode={party_driver_mode()} effective={'driver' if is_party_driver() else 'passive'}",
                f"pet={'on' if mod_pet_enabled() else 'off'} last={pet_last} next_in={pet_left_str} interval={interval} due={'yes' if _pet_due_now() else 'no'}",
            ]

            # Verbose extras
            if text != "/status":
                active_mode = (get_kv("active_mode") or "-").strip()
                pending_mode = (get_kv("pending_mode") or "-").strip()
                last_stage = (get_kv("last_stage") or "-").strip()
                try:
                    last_update_ts = float(get_kv("last_update_ts") or "0")
                except Exception:
                    last_update_ts = 0.0
                age = int(max(0, _now_ts() - last_update_ts)) if last_update_ts > 0 else -1

                inv_full = "yes" if (get_kv("inventory_full", "0") or "0") == "1" else "no"
                golem_left = _golem_cd_remaining_sec()
                loss_left = _kv_iso_to_left("loss_cd_until")

                stop_cast = "1" if (get_kv("fish_stop_cast") or "0") == "1" else "0"
                try:
                    stop_since = float(get_kv("fish_stop_cast_since") or "0")
                except Exception:
                    stop_since = 0.0
                stop_age = int(max(0, _now_ts() - stop_since)) if stop_since > 0 else -1


                # PET debug
                try:
                    pb = float(get_kv("pet_blocked_until_ts") or "0")
                except Exception:
                    pb = 0.0
                pet_block_left = int(max(0, pb - _now_ts())) if pb > 0 else 0
                pet_block_reason = (get_kv("pet_blocked_reason") or "-").strip()
                pet_deferred = (get_kv("pet_deferred") or "0").strip()
                heal_target = (get_kv("heal_target_pct") or "-").strip()

                golem_fight = "on" if mod_golem_fight_enabled() else "off"
                rod_retry_left = -1
                try:
                    rr = float(get_kv("rod_retry_after") or "0")
                    if rr > 0:
                        rod_retry_left = int(max(0, rr - time.time()))
                except Exception:
                    rod_retry_left = -1

                lines += [
                    "--- verbose ---",
                    f"mode: active={active_mode} pending={pending_mode}",
                    f"last: stage={last_stage} update_ago={_fmt_sec(age)}",
                    f"inventory_full={inv_full}",
                    f"golem_fight={golem_fight} golem_cd={_fmt_sec(golem_left)}",
                    f"loss_cd={_fmt_sec(loss_left)}",
                    f"fish_stop_cast={stop_cast} stop_ago={_fmt_sec(stop_age)}",
                    f"rod_retry_in={_fmt_sec(rod_retry_left)}",
                    f"heal_target={heal_target}",
                    f"pet_block_in={_fmt_sec(pet_block_left)} reason={pet_block_reason} deferred={pet_deferred}",
                ]

            await evt.reply(
                "\n".join(lines)
            )
            return

        m = re.match(r"^/(?:lvl|level)\s+(\d+)\s*$", text)
        if m:
            n = int(m.group(1))
            if n < 1:
                n = 1
            # User needs level 10.
            if n > 10:
                n = 10
            _kv_set("forest_level", str(n))
            # Keep effective level in sync when blood-routing is OFF, and
            # recalculate immediately when blood-routing is ON.
            _apply_blood_level_routing()
            await evt.reply(f"🌲 Уровень вылазки установлен: {n}")
            return

        # Heal control
        #   /heal on|off
        #   /heal pct 99        (target 99% of max HP)
        #   /heal target 0.99   (same)
        if text in ("/heal on", "/heal off"):
            _set_kv_bool("mod_heal", text.endswith("on"))
            await evt.reply(f"🩹 heal={'on' if mod_heal_enabled() else 'off'}")
            return

        m = re.match(r"^/heal\s+(?:pct|target)\s+([0-9.]+)$", text)
        if m:
            val = m.group(1)
            try:
                if "." in val:
                    pct = float(val)
                    if pct > 1.0:
                        pct = pct / 100.0
                else:
                    pct = int(val) / 100.0
            except Exception:
                await evt.reply("Формат: /heal pct 99  или  /heal target 0.99")
                return
            pct = max(0.10, min(1.0, pct))
            _kv_set("heal_target_pct", f"{pct:.4f}")
            await evt.reply(f"🩹 heal target: {pct*100:.0f}%")
            return

        # Blood-heal control (additional contour over /lvl)
        #   /blood on|off|status
        #   /blood low <n[%]>
        #   /blood high <n[%]>
        #   /blood level <n>
        #   /blood hyst <low[%]> <high[%]>
        if text in ("/blood on", "/blood off"):
            _set_kv_bool("mod_blood", text.endswith("on"))
            _apply_blood_level_routing()
            await evt.reply(f"🩸 blood={'on' if blood_enabled() else 'off'} low/high={blood_hp_low()}/{blood_hp_high()} bloodLevel={blood_level()}")
            return

        if text in ("/blood", "/blood status"):
            await evt.reply(
                f"🩸 blood={'on' if blood_enabled() else 'off'}\n"
                f"hp={get_kv('hp_pct','?')}% low/high={blood_hp_low()}/{blood_hp_high()}\n"
                f"bloodLevel={blood_level()} base_lvl={get_kv('forest_level','?')} effective_lvl={get_kv('forest_level_effective','?')}\n"
                f"Команды: /blood low 60 | /blood high 95 | /blood hyst 60 95 | /blood level 1"
            )
            return

        m = re.match(r"^/blood\s+hyst\s+(\d+)\s*%?\s+(\d+)\s*%?\s*$", text)
        if m:
            low = max(1, min(99, int(m.group(1))))
            high = max(low + 1, min(100, int(m.group(2))))
            _kv_set("blood_hp_low", str(low))
            _kv_set("blood_hp_high", str(high))
            _apply_blood_level_routing()
            await evt.reply(f"🩸 blood hysteresis updated: low/high={blood_hp_low()}/{blood_hp_high()}%")
            return

        m = re.match(r"^/blood\s+(low|high|level)\s+(\d+)\s*%?\s*$", text)
        if m:
            key = m.group(1)
            n = int(m.group(2))
            if key == "low":
                n = max(1, min(99, n))
                # Keep hysteresis valid: low < high
                cur_high = blood_hp_high()
                if n >= cur_high:
                    cur_high = min(100, n + 1)
                    _kv_set("blood_hp_high", str(cur_high))
                _kv_set("blood_hp_low", str(n))
            elif key == "high":
                n = max(2, min(100, n))
                # Keep hysteresis valid: low < high
                cur_low = blood_hp_low()
                if n <= cur_low:
                    cur_low = max(1, n - 1)
                    _kv_set("blood_hp_low", str(cur_low))
                _kv_set("blood_hp_high", str(n))
            else:
                n = max(1, min(10, n))
                _kv_set("blood_level", str(n))
            _apply_blood_level_routing()
            await evt.reply(f"🩸 blood updated: low/high={blood_hp_low()}/{blood_hp_high()} bloodLevel={blood_level()}")
            return


        # Pet control
        #   /pet on|off
        #   /pet now      (force run ASAP)
        if text in ("/pet on", "/pet off"):
            _set_kv_bool("mod_pet", text.endswith("on"))
            if text.endswith("on"):
                # NEW: When user enables pet module, run pet flow immediately,
                # then auto-pause for 1-2 hours (handled in _pet_flow_driver).
                _kv_set("pet_on_immediate", "1")
                # Clear previous backoff blocks (e.g. after a failed run / floodwait).
                _kv_set("pet_blocked_until_ts", "0")
                _kv_set("pet_deferred", "0")

                # Force due right now
                _kv_set("pet_next_due_ts", f"{_now_ts() - 1:.3f}")
                await evt.reply("🐾 pet=on → глажу сейчас, потом пауза 1–2ч (после пета)")
            else:
                await evt.reply("🐾 pet=off")
            return

        if text == "/pet now":
            _set_kv_bool("mod_pet", True)
            # Make it due immediately (mode loop will pick it up)
            _kv_set("pet_next_due_ts", f"{_now_ts() - 1:.3f}")
            await evt.reply("🐾 pet=on (запуск скоро)")
            return

# ---- SETS (multi-set, with slot priorities) ----
        # Usage:
        #   /set save <name>            - save current equipped items as set
        #   /set apply <name>           - apply set (equip items by priorities)
        #   /set list                   - list saved sets
        #   /set prio <name> <slot> <n> - set slot priority (slot: a1,a2,a3,r,l,h,b)
        m = re.match(r"^/set\s+(\w+)(?:\s+(.+))?$", text)
        if m:
            sub = m.group(1)
            rest = (m.group(2) or "").strip()
            if sub == "list":
                keys = ["combat","fishing","work","dungeon"]
                lines = ["Сеты:"]
                for k in keys:
                    lines.append(f"- {k}: {'ok' if _load_set(k) else '—'}")
                await evt.reply("\n".join(lines))
                return

            if sub in ("save","apply","prio"):
                if not rest:
                    await evt.reply("Формат: /set save <name> | /set apply <name> | /set prio <name> <slot> <n>")
                    return

            if sub == "save":
                name = rest.split()[0]
                ch = await _fetch_character(client)
                if not ch:
                    await evt.reply("Не смог получить инвентарь (/inventory).")
                    return
                slots = ch.get("slots", {})
                payload = {
                    "slots": {sid: _clean_item_name(val) for sid, val in slots.items() if sid in SLOT_LABELS},
                    "priority": {"a1": 300, "a2": 200, "a3": 100, "r": 50, "l": 50, "h": 40, "b": 40},
                }
                _save_set(name, payload)
                await evt.reply(f"🧰 Сет '{name}' сохранён. (приоритеты по умолчанию выставлены)")
                return

            if sub == "apply":
                name = rest.split()[0]
                ok = await _apply_set(client, name)
                await evt.reply(f"🧰 apply '{name}': {'ok' if ok else 'no changes / missing items'}")
                return

            if sub == "prio":
                parts = rest.split()
                if len(parts) != 3:
                    await evt.reply("Формат: /set prio <name> <slot> <n>")
                    return
                name, slot, n = parts[0], parts[1], parts[2]
                if slot not in SLOT_LABELS:
                    await evt.reply(f"Слот должен быть один из: {', '.join(SLOT_LABELS.keys())}")
                    return
                try:
                    pn = int(n)
                except Exception:
                    await evt.reply("n должно быть числом.")
                    return
                s = _load_set(name) or {"slots": {}, "priority": {}}
                s.setdefault("priority", {})[slot] = pn
                _save_set(name, s)
                await evt.reply(f"🧰 set '{name}': priority[{slot}]={pn}")
                return

            await evt.reply("Неизвестная подкоманда /set. Используй: save/apply/list/prio")
            return

        def _toggle(cmd, key, label):
            if text == f"/{cmd} on":
                _set_kv_bool(key, True)
                return f"{label}=on"
            if text == f"/{cmd} off":
                _set_kv_bool(key, False)
                return f"{label}=off"
            return None

        if text in ("/fish on", "/fishing on"):
            set_mod_fishing_enabled(True)
            # When fishing is manually re-enabled, clear transient blockers that may
            # remain from an interrupted stop-cast/rod recovery flow.
            _kv_set("fish_stop_cast", "0")
            _kv_set("fish_stop_cast_since", "0")
            _kv_set("fish_stop_cast_kind", "")
            try:
                STORAGE.delete("rod_flow")
            except Exception:
                pass
            await evt.reply("🎣 fishing=on")
            return

        if text in ("/fish off", "/fishing off"):
            set_mod_fishing_enabled(False)
            # Also clear stop-cast flags to avoid stale state after a manual off/on.
            _kv_set("fish_stop_cast", "0")
            _kv_set("fish_stop_cast_since", "0")
            _kv_set("fish_stop_cast_kind", "")
            try:
                STORAGE.delete("rod_flow")
            except Exception:
                pass
            await evt.reply("🎣 fishing=off")
            return

        # Common typo alias
        if text in ("/dangeon on", "/dangeon off"):
            _set_kv_bool("mod_dungeon", text.endswith("on"))
            await evt.reply("🕸 dungeon=" + ("on" if mod_dungeon_enabled() else "off"))
            return

        for cmd, key, label in [
            ("forest","mod_forest","🌲 forest"),
            ("blood","mod_blood","🩸 blood"),
            ("golem","mod_golem_fight","🪵 golem_fight"),
            ("heal","mod_heal","🩹 heal"),
            ("work","mod_work","⛏️ work"),
            ("dungeon","mod_dungeon","🕸 dungeon"),
            ("altar","mod_dungeon_altar_touch","🐾 altar_touch"),
            ("altar1000","mod_dungeon_altar_1000_touch","🕷 altar1000_touch"),
            ("boarded","mod_dungeon_boarded_chop","🪓 boarded_chop"),
            ("rubble","mod_dungeon_rubble_break","⛏️ rubble_break"),
            ("grave","mod_dungeon_grave_open","⚰️ grave_open"),
            ("hunter","mod_hunter","🏹 hunter"),
            ("pet","mod_pet","🐾 pet"),
            ("thief","mod_thief","🦝 thief"),
        ]:
            res = _toggle(cmd,key,label)
            if res:
                await evt.reply(res)
                return

        if text == "/mods":
            await evt.reply(
                "\n".join([
                    "🎛 Модули:",
                    f"/forest on|off   (сейчас: {'on' if mod_forest_enabled() else 'off'})",
                    f"/fish on|off     (сейчас: {'on' if mod_fishing_enabled() else 'off'})",
                    f"/blood on|off    (сейчас: {'on' if blood_enabled() else 'off'}) low/high={blood_hp_low()}/{blood_hp_high()}% level={blood_level()}",
                    "/blood hyst 60 95 (сменить пороги гистерезиса, %)",
                    f"/golem on|off    (сейчас: {'on' if mod_golem_fight_enabled() else 'off'})",
                    f"/heal on|off     (сейчас: {'on' if mod_heal_enabled() else 'off'})",
                    f"/work on|off     (сейчас: {'on' if mod_work_enabled() else 'off'})",
                    f"/dungeon on|off  (сейчас: {'on' if mod_dungeon_enabled() else 'off'})",
                    f"/altar on|off    (сейчас: {'on' if mod_dungeon_altar_touch_enabled() else 'off'})",
                    f"/altar1000 on|off (сейчас: {'on' if mod_dungeon_altar_1000_touch_enabled() else 'off'})",
                    f"/boarded on|off  (сейчас: {'on' if mod_dungeon_boarded_chop_enabled() else 'off'})",
                    f"/rubble on|off   (сейчас: {'on' if mod_dungeon_rubble_break_enabled() else 'off'})",
                    f"/grave on|off    (сейчас: {'on' if mod_dungeon_grave_open_enabled() else 'off'})",
                    f"/hunter on|off   (сейчас: {'on' if mod_hunter_enabled() else 'off'})",
                    f"/driver on|off|auto (сейчас: {party_driver_mode()}, effective={'driver' if is_party_driver() else 'passive'})",
                    f"/pet on|off      (сейчас: {'on' if mod_pet_enabled() else 'off'})  interval={getattr(CFG,'pet_interval_min_hours',1)}-{getattr(CFG,'pet_interval_max_hours',2)}h",
                    f"/thief on|off    (сейчас: {'on' if mod_thief_enabled() else 'off'})",
                ])
            )
            return

    # Auto mode switcher (forest <-> fishing)
    asyncio.create_task(mode_manager_loop(client))

    log.info("✅ Хендлеры установлены (game + control).")
    await client.run_until_disconnected()
    # Re-apply key dungeon/party buffs when game reports effect expiration,
    # but only in active dungeon/party context.
    try:
        if _looks_like_effect_expired(txt_full):
            if dungeon_context_now and _can_apply_dungeon_buffs_now():
                await _use_preferred_dungeon_buffs(client, reason="effect_expired", force=False)
            else:
                log.info("🧪 buff-reapply skipped (inactive context): %s", "effect_expired")
    except Exception as e:
        log.warning("🧪 buff-reapply error: %s", e)
