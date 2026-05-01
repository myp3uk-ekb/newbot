from dataclasses import dataclass
from typing import List, Optional
import re
from telethon.tl.custom.message import Message

@dataclass
class Choice:
    name: str
    btn_text: Optional[str] = None
    pos: Optional[tuple[int,int]] = None
    tier: Optional[int] = None

@dataclass
class GameState:
    stage: str                 # fishing_hook | fishing_cast | fishing_wait | post_battle | battle | forest | other
    buttons: List[Choice]
    can_act: bool = True
    human_ctx: str = ""      # transient context (hp_query/pet/thief etc.)

SWORD = "⚔️"
SUPER_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
RE_MONSTER_TIER = re.compile(r"\[(\d+)\]\s*$")


def has_button(buttons: List[Choice], needle: str) -> bool:
    """Return True if any button label matches or contains *needle*.

    The game UI sometimes adds extra spaces/emoji, and the user may have
    latin lookalikes in text. We therefore compare both strict and
    substring matches on normalized labels.
    """
    n = _normalize(needle).strip()
    if not n:
        return False
    for ch in buttons or []:
        lbl = _normalize(ch.btn_text or ch.name or "").strip()
        if not lbl:
            continue
        if lbl == n or n in lbl:
            return True
    return False

# Fishing bite triggers.
#
# The game text changes often and may contain latin lookalikes (o/c/p/t/e etc.).
# Relying on exact full phrases breaks frequently, so we combine:
#   1) a small list of known phrases, and
#   2) robust keyword heuristics.
BITE_TRIGGERS = [
    # Exact-ish fishing bite phrases seen in production (stored normalized/lowercase).
    "поплавок повело в сторону",
    "клюет! нужно срочно подсекать",
    "клюёт! нужно срочно подсекать",
    "клюет! подсекай быстрее",
    "клюёт! подсекай быстрее",
    "клюет! подсекай",
    "клюёт! подсекай",
    "поплавок скрылся под водой. тяни быстрее",
    "поплавок скрылся под водой. тяни",
    "поплавок скрылся под водой. не спи",
    "поплавок задергался. тащи",
    "поплавок дергается! тащи",
    "поплавок дергается. тащи",
    "поплавок дергается",
    "рыба натянула леску. не спи!",
    "рыба натянула леску",

]
# Result triggers (to recast)
RESULT_TRIGGERS = [
    "нет рыб",
    "пустой крючок",
    "рыба наелась",
    "уплыла",
    "на нем нет рыб",
    "на нём нет рыб",
    "отличный улов",
    "инвентарь полон",
    "улов!",
    "сорвалась",
]

def _btn_text(button) -> str:
    return (getattr(button, "text", "") or "").strip()

def _parse_sup_tier(lbl: str) -> Optional[int]:
    if not lbl:
        return None
    s = lbl.replace(" ", "")
    try:
        idx = s.index(SWORD) + len(SWORD)
        rest = s[idx:].translate(SUPER_MAP)
        m = re.match(r"(\d+)", rest)
        if m:
            return int(m.group(1))
    except ValueError:
        pass
    return None

def _normalize(s: str) -> str:
    # lowercase + replace yo + map common latin lookalikes used in screenshots/messages
    if not s:
        return ""
    t = s.lower().replace("ё", "е")
    # latin->cyr lookalikes often appear (a,o,e,c,p,x,t,y,k,m,h,b)
    trans = str.maketrans({
        "a":"а","o":"о","e":"е","c":"с","p":"р","x":"х","t":"т","y":"у","k":"к","m":"м","h":"н","b":"в",
        "A":"а","O":"о","E":"е","C":"с","P":"р","X":"х","T":"т","Y":"у","K":"к","M":"м","H":"н","B":"в",
    })
    return t.translate(trans)

def is_bite_text(text: str) -> bool:
    """Detect the moment when we should press the "Подсечь" button.

    The bot's UI text is not stable: it changes copy, adds punctuation,
    and often mixes latin letters that look like Cyrillic. We therefore
    use both phrase matching and keyword heuristics.
    """
    t = _normalize(text)
    if not t:
        return False

    # 1) phrase matches
    for tr in BITE_TRIGGERS:
        if _normalize(tr) in t:
            return True

    # 2) keyword heuristics (more resilient to text changes)
    # Examples seen in logs:
    #   "Пoплавoк cкрылся пoд водoй. Tяни!"
    #   "Клюeт! Пoдсекай быстрее"
    #   "Поплавoк дергaется! Tащи!"
    #   "Рыбкa натянула леску. Не зевай!"
    has_poplavok = "поплав" in t
    has_pull = ("тяни" in t) or ("тащи" in t)
    has_klyov = "клю" in t
    has_podsek = ("подсек" in t) or ("подсеч" in t)
    has_leska = "леск" in t
    has_natyan = ("натян" in t) or ("тянул" in t)
    has_pora = "пора" in t
    has_ne_spi = "не спи" in t

    if has_poplavok and has_pull:
        return True
    if has_poplavok and has_podsek:
        return True
    if has_poplavok and has_ne_spi:
        return True
    if has_klyov and (has_podsek or has_pull):
        return True
    if has_leska and has_natyan:
        return True
    # New variant seen in logs:
    #   "Пора подсекать. Не cпи!"
    if has_pora and has_podsek:
        return True

    return False

def is_result_text(text: str) -> bool:
    t = _normalize(text)
    return any(_normalize(tr) in t for tr in RESULT_TRIGGERS)

def parse_message(msg: Message, fish_hook_sub: str, fish_cast_sub: str) -> GameState:
    text = msg.message or ""
    buttons_raw = msg.buttons or []

    flat: list[Choice] = []
    for r, row in enumerate(buttons_raw):
        for c, b in enumerate(row):
            lbl = _btn_text(b)
            if not lbl:
                continue
            flat.append(Choice(name=lbl, btn_text=lbl, pos=(r,c), tier=None))

    hook_sub = (fish_hook_sub or "подсеч").lower()
    cast_sub = (fish_cast_sub or "закинуть").lower()
    has_hook_btn = any(hook_sub in (ch.btn_text or "").lower() for ch in flat)
    has_cast_btn = any(cast_sub in (ch.btn_text or "").lower() for ch in flat)
    text_l = (text or "").lower()
    # Guard against false positives like "Начать разбор?" on dismantle screens.
    # We only allow fishing start in explicit fishing context.
    fishing_start_ctx = (
        ("рыбал" in text_l)
        or (("наживк" in text_l) and ("разбор" not in text_l))
    )
    has_start_btn = any("начать" in (ch.btn_text or "").lower() for ch in flat) and fishing_start_ctx

    # fishing: use TEXT triggers to avoid premature clicking
    if has_cast_btn and is_result_text(text):
        return GameState(stage="fishing_cast", buttons=flat, can_act=True)

    if has_hook_btn and is_bite_text(text):
        return GameState(stage="fishing_hook", buttons=flat, can_act=True)

    # Экран входа в рыбалку: после команды «Рыбалка» появляется кнопка «Начать».
    if has_start_btn:
        return GameState(stage="fishing_start", buttons=flat, can_act=True)

    low = _normalize(text)

    # Guard: inventory / character dumps sometimes include HP and can be misclassified as post_battle.
    # Examples: "/inventory" output with lines like "/i_b Тело:" etc.
    text_raw = text or ""
    low_raw = text_raw.lower()
    is_inventory_dump = (
        ("/i_" in low_raw)
        or ("/inventory" in low_raw)
        or ("голова:" in low_raw)
        or ("тело:" in low_raw)
        or ("правая лапа" in low_raw)
        or ("левая лапа" in low_raw)
        or ("аксессуар" in low_raw)
    )

    # Another guard: on some screens we only have utility buttons (Дом/Хранилище/Разобрать все/Команды)
    # which are NOT post-battle heal actions.
    has_home_util_row = (
        has_button(flat, "Дом")
        and has_button(flat, "Хранилище")
        and (has_button(flat, "Разобрать") or has_button(flat, "Разобрать все"))
    )

    
    # Golem encounter can appear right after victory and may include healing buttons.
    # Detect it BEFORE post-battle so we don't miss the "Отступить" decision.
    if (
        ("голем" in low)
        and ("рискне" in low or "отступ" in low)
        and has_button(flat, "Напасть")
        and has_button(flat, "Отступить")
    ):
        return GameState(stage="golem", buttons=flat, can_act=True)

# Post-battle has highest priority.
    # It may contain side notices like "удочка сломана", which must NOT
    # reclassify this screen as a fishing state.
    #
    # Variants seen in the wild:
    #  - "Славная победа!" + buttons: "Осмотреться", "Котик", "Полное лечение", ...
    #  - "Противник одержал верх" (loss)
    #  - "Использовано ... Зелье исцеления котика"
    if (not is_inventory_dump) and (not has_home_util_row) and (
        ("продолжить вылазку" in low)
        or ("славная побед" in low)
        or ("противник одержал верх" in low)
        or ("зелье исцеления котика" in low)
        or ("котика" in low and "исцел" in low)
        # Some UIs omit the victory/loss text but show heal actions.
        or any(k in (ch.btn_text or "").lower() for ch in flat for k in ("котик", "пияв", "единорог", "полное лечение"))
    ):
        return GameState(stage="post_battle", buttons=flat, can_act=True)
    # fishing: handle missing rod as a fishing state so it can work even during global pause
    # Example:
    #   "Нет удочки!\n\nДля начала рыбалки нужно экипировать удочку..."
    # Fishing rod is missing.
    # NOTE: "удочка сломана" can appear in combat rewards (post-battle), so we only
    # treat it as fishing when the message explicitly asks to equip a rod for fishing.
    if ("нет удочки" in low) or (("удочка сломана" in low) and ("для начала рыбалки" in low or "экипир" in low)):
        return GameState(stage="fishing_no_rod", buttons=flat, can_act=True)

    # Fishing bait is really missing (cannot continue fishing).
    # Important: do NOT trigger on event texts like "Хитрая рыба украла наживку!"
    # because that's a normal fishing flow and may be followed by recast.
    no_bait_event_text = ("украла нажив" in low) or ("украл нажив" in low)
    no_bait_hard = (
        ("нужна нажив" in low)
        or ("нужен черв" in low)
        or ("нет черв" in low)
        or (
            ("нет нажив" in low)
            and (
                ("для начала рыбалки" in low)
                or ("в рюкзаке" in low)
                or ("нужно иметь" in low)
            )
        )
    )
    if no_bait_hard and not no_bait_event_text:
        return GameState(stage="fishing_no_bait", buttons=flat, can_act=True)

    if has_hook_btn or has_cast_btn or ("удочку" in _normalize(text)) or ("поплавок" in _normalize(text)):
        return GameState(stage="fishing_wait", buttons=flat, can_act=False)

    # Forest special encounter: golem choice (attack or retreat)
    # Typical: "...вырaстaeт голем... Рискнешь напасть или отступишь?" with buttons "Напасть"/"Отступить".
    if (
        ("голем" in low or "гolem" in low)
        and ("рискне" in low or "отступ" in low)
        and has_button(flat, "Напасть")
        and has_button(flat, "Отступить")
    ):
        return GameState(stage="golem", buttons=flat, can_act=True)


    # Thief mini-event ("воришка") in forest:
    #  - prompt 1: "Куда бежать?"  (Налево/Прямо/Направо)
    #  - prompt 2: "Где искать воришку?" (В кустах/В ветвях/В траве)
    #  - result: "Ах, вот ты где, ... воришка ..."
    if ("куда бежать" in low) and has_button(flat, "Налево") and has_button(flat, "Прямо") and has_button(flat, "Направо"):
        return GameState(stage="thief_dir", buttons=flat, can_act=True)
    
    if ("где искать воришку" in low) and has_button(flat, "В кустах") and has_button(flat, "В ветвях") and has_button(flat, "В траве"):
        return GameState(stage="thief_hide", buttons=flat, can_act=True)
    
    if ("ах, вот ты где" in low) and ("воришк" in low):
        return GameState(stage="thief_done", buttons=flat, can_act=False)
    
    
    if ("выбирай, на кого хочешь напасть" in low) or ("замечены следующие противники" in low):
        out: list[Choice] = []
        for ch in flat:
            if "отмена" in _normalize(ch.name):
                continue
            m = RE_MONSTER_TIER.search(ch.name)
            ch.tier = int(m.group(1)) if m else None
            out.append(ch)
        return GameState(stage="battle", buttons=out, can_act=bool(out))

    # Forest menu tiers are presented as sword+number buttons (⚔️1, ⚔️2, ...).
    # Other menus also contain sword buttons (e.g. "⚔️Арена", "⚔️В бой!") and must NOT be
    # treated as the forest tier picker, otherwise we will click the wrong thing.
    if any(SWORD in (ch.name or "") for ch in flat):
        # Dungeon entry buttons in Tower ("Подземелья", "Соло-подземелье") are not
        # forest tier selectors even though they contain sword+tier markers.
        # If we classify them as forest, the combat loop may run gear swaps and clicks
        # in a completely wrong context.
        if any("подзем" in _normalize(ch.name or "") for ch in flat):
            return GameState(stage="other", buttons=flat, can_act=False)
        out: list[Choice] = []
        for ch in flat:
            if SWORD not in (ch.name or ""):
                continue
            tier = _parse_sup_tier(ch.name)
            if tier is None:
                # Not a tier button -> ignore (prevents accidental Arena clicks)
                continue
            ch.tier = tier
            ch.name = f"⚔️{tier}"
            out.append(ch)
        if out:
            return GameState(stage="forest", buttons=out, can_act=True)

    return GameState(stage="other", buttons=flat, can_act=False)
