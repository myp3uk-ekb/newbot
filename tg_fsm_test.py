#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline FSM test runner for tg_autopilot logic (no Telegram).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional




# Fishing trigger phrases (kept in sync with game_parser.py)
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


def _norm_text(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    trans = str.maketrans({
        "a":"а","o":"о","e":"е","c":"с","p":"р","x":"х","t":"т","y":"у","k":"к","m":"м","h":"н","b":"в",
        "A":"а","O":"о","E":"е","C":"с","P":"р","X":"х","T":"т","Y":"у","K":"к","M":"м","H":"н","B":"в",
    })
    return t.translate(trans)


def is_bite_text(text: str) -> bool:
    t = _norm_text(text)
    if any(tr.replace("ё", "е") in t for tr in BITE_TRIGGERS):
        return True

    has_poplavok = "поплав" in t
    has_pull = ("тяни" in t) or ("тащи" in t)
    has_klyov = "клю" in t
    has_podsek = ("подсек" in t) or ("подсеч" in t)
    has_leska = "леск" in t
    has_natyan = ("натян" in t) or ("тянул" in t)
    has_ne_spi = "не спи" in t

    return (
        (has_poplavok and has_pull)
        or (has_poplavok and has_podsek)
        or (has_poplavok and has_ne_spi)
        or (has_klyov and (has_podsek or has_pull))
        or (has_leska and has_natyan)
    )


def is_result_text(text: str) -> bool:
    t = _norm_text(text)
    return any(tr.replace("ё", "е") in t for tr in RESULT_TRIGGERS)


class MainState(str, Enum):
    FOREST = "forest"
    HP_PAUSE = "hp_pause"
    PARTY_LOCK = "party_lock"
    BROKEN_GEAR = "broken_gear"
    IDLE = "idle"


class Activity(str, Enum):
    NONE = "none"
    FISHING = "fishing"
    PET = "pet"
    THIEF = "thief"
    HEAL = "heal"


@dataclass
class Config:
    fishing_enabled: bool = True
    pet_enabled: bool = True
    thief_enabled: bool = True
    heal_enabled: bool = True

    # Forest target level (LVL)
    lvl: int = 3

    # Blood mode
    blood_enabled: bool = True
    blood_hp_low: int = 60     # go to blood_level if HP < low
    blood_hp_high: int = 95    # return to LVL if HP >= high
    blood_level: int = 1

    # HP pause threshold
    hp_pause_threshold: int = 50  # if HP < 50 → request 'хп' and go pause

    # Anti-stuck
    stuck_silence_min: int = 3       # minutes without events
    stuck_kick_cooldown_min: int = 30


@dataclass
class Context:
    hp_percent: int = 100

    main_state: MainState = MainState.FOREST
    activity: Activity = Activity.NONE

    # Timers are "minutes remaining" in this offline runner
    hp_pause_minutes_remaining: int = 0

    # To avoid spamming HP command
    hp_request_sent: bool = False

    # Stuck detector
    minutes_since_last_event: int = 0
    stuck_kick_cooldown_remaining: int = 0

    # Current level (where we are now)
    current_level: int = 3

    # For golem logic: remember which level had triplet to re-check
    last_triplet_level: Optional[int] = None
    golem_pause_minutes_remaining: int = 0

    log: List[str] = field(default_factory=list)


def log(ctx: Context, msg: str) -> None:
    ctx.log.append(msg)
    print(msg)


def send_command(ctx: Context, text: str) -> None:
    log(ctx, f"➡️ SEND: {text}")


def equip_rod(ctx: Context) -> None:
    log(ctx, "🎣 Equip rod (оффлайн)")


def unequip_rod(ctx: Context) -> None:
    log(ctx, "🧰 Unequip rod (оффлайн)")


def record_event(ctx: Context, name: str) -> None:
    ctx.minutes_since_last_event = 0
    log(ctx, f"📩 EVENT: {name}")


def switch_activity(ctx: Context, new_activity: Activity) -> None:
    if ctx.activity == new_activity:
        return
    if new_activity == Activity.FISHING:
        equip_rod(ctx)
    elif new_activity == Activity.NONE and ctx.activity == Activity.FISHING:
        unequip_rod(ctx)
    ctx.activity = new_activity
    log(ctx, f"ACTIVITY -> {ctx.activity.value}")


def decide_level(ctx: Context, cfg: Config) -> None:
    if not cfg.blood_enabled:
        ctx.current_level = cfg.lvl
        return

    if ctx.hp_percent < cfg.blood_hp_low:
        if ctx.current_level != cfg.blood_level:
            log(ctx, f"🩸 Blood mode: HP<{cfg.blood_hp_low}% → go to blood_level={cfg.blood_level}")
        ctx.current_level = cfg.blood_level
        return

    if ctx.hp_percent >= cfg.blood_hp_high:
        if ctx.current_level != cfg.lvl:
            log(ctx, f"🩸 Blood mode: HP>={cfg.blood_hp_high}% → return to LVL={cfg.lvl}")
        ctx.current_level = cfg.lvl
        return


def on_hp_update(ctx: Context, cfg: Config, hp: int) -> None:
    ctx.hp_percent = max(0, min(100, hp))
    log(ctx, f"HP updated: {ctx.hp_percent}%")

    if ctx.main_state in (MainState.PARTY_LOCK, MainState.BROKEN_GEAR):
        return

    if ctx.hp_percent < cfg.hp_pause_threshold:
        if not ctx.hp_request_sent:
            log(ctx, "❤️ HP below 50%")
            log(ctx, "📨 Sending HP command: хп")
            send_command(ctx, "хп")
            ctx.hp_request_sent = True
            ctx.hp_pause_minutes_remaining = 10
            log(ctx, f"⏳ HP pause: {ctx.hp_pause_minutes_remaining} minutes")

        if cfg.fishing_enabled:
            switch_activity(ctx, Activity.FISHING)

        ctx.main_state = MainState.HP_PAUSE
        return

    if ctx.main_state == MainState.HP_PAUSE and ctx.hp_percent >= cfg.hp_pause_threshold:
        ctx.main_state = MainState.FOREST
        ctx.hp_pause_minutes_remaining = 0
        ctx.hp_request_sent = False
        switch_activity(ctx, Activity.NONE)
        log(ctx, "✅ HP recovered → exit HP_PAUSE → back to FOREST")


def tick_minutes(ctx: Context, cfg: Config, minutes: int) -> None:
    for _ in range(minutes):
        if ctx.hp_pause_minutes_remaining > 0:
            ctx.hp_pause_minutes_remaining -= 1
            if ctx.hp_pause_minutes_remaining == 0 and ctx.main_state == MainState.HP_PAUSE:
                ctx.main_state = MainState.FOREST
                ctx.hp_request_sent = False
                switch_activity(ctx, Activity.NONE)
                log(ctx, "⏱ HP pause finished → return to FOREST")

        if ctx.stuck_kick_cooldown_remaining > 0:
            ctx.stuck_kick_cooldown_remaining -= 1

        if ctx.golem_pause_minutes_remaining > 0:
            ctx.golem_pause_minutes_remaining -= 1
            if ctx.golem_pause_minutes_remaining == 0:
                log(ctx, "⏱ Golem level-1 pause finished → re-check target LVL")

        ctx.minutes_since_last_event += 1
        if ctx.minutes_since_last_event >= cfg.stuck_silence_min:
            log(ctx, f"🕳 No updates for {cfg.stuck_silence_min * 60}s")

            if ctx.main_state in (MainState.PARTY_LOCK, MainState.BROKEN_GEAR, MainState.HP_PAUSE) or ctx.golem_pause_minutes_remaining > 0:
                log(ctx, "⏸ Anti-stuck skipped (lock/pause active)")
                continue

            if ctx.stuck_kick_cooldown_remaining > 0:
                log(ctx, "⏳ Kick skipped (cooldown active)")
                continue

            cmd = resolve_kick_command(ctx)
            ctx.minutes_since_last_event = 0
            ctx.stuck_kick_cooldown_remaining = cfg.stuck_kick_cooldown_min
            log(ctx, f"🔄 Anti-stuck: sending {cmd}")
            log(ctx, f"⏳ Kick cooldown set: {cfg.stuck_kick_cooldown_min}m")
            send_command(ctx, cmd)




def resolve_kick_command(ctx: Context) -> str:
    if ctx.activity == Activity.FISHING:
        return "Рыбалка"
    if ctx.main_state == MainState.FOREST:
        return "/forest"
    return "/character"

def on_battle_targets(ctx: Context, cfg: Config, targets: List[str]) -> None:
    record_event(ctx, "battle_targets")
    decide_level(ctx, cfg)

    if len(targets) == 3 and all("Голем" in t for t in targets):
        log(ctx, "💀 Triplet golems detected → RETREAT")
        send_command(ctx, "Отступить")
        ctx.last_triplet_level = ctx.current_level

        if ctx.current_level > 1:
            ctx.current_level -= 1
            log(ctx, f"⬇️ Lower level -> {ctx.current_level}")
        else:
            ctx.golem_pause_minutes_remaining = 10
            log(ctx, "🪨 GOLEM TRIPLET on level 1")
            log(ctx, "⏳ 10 minute cooldown started")
        return

    log(ctx, f"✅ Targets OK ({', '.join(targets)}) → fight allowed (если fight=on)")


def on_thief_event(ctx: Context, cfg: Config, stage: str) -> None:
    """Handle thief mini-event flow in forest."""
    record_event(ctx, f"thief_{stage}")
    if not cfg.thief_enabled:
        log(ctx, "🦝 Thief event ignored (thief module OFF)")
        return
    if ctx.main_state in (MainState.PARTY_LOCK, MainState.BROKEN_GEAR):
        log(ctx, "🦝 Thief event skipped (lock active)")
        return
    if ctx.main_state == MainState.HP_PAUSE:
        log(ctx, "🦝 Thief event deferred (HP pause active)")
        return

    if stage == "dir":
        log(ctx, "🦝 Thief event: direction choice")
        send_command(ctx, "Налево")
    elif stage == "hide":
        log(ctx, "🦝 Thief event: hideout choice")
        send_command(ctx, "В кустах")
    else:
        log(ctx, "🦝 Thief event: done")


def on_heal_event(ctx: Context, cfg: Config, reason: str = "post_battle") -> None:
    """Handle healing branch to keep combat survivability logic explicit."""
    record_event(ctx, f"heal_{reason}")
    if not cfg.heal_enabled:
        log(ctx, "🩹 Heal skipped (heal module OFF)")
        return
    if ctx.main_state in (MainState.PARTY_LOCK, MainState.BROKEN_GEAR):
        log(ctx, "🩹 Heal skipped (lock active)")
        return

    # Keep it simple in offline model: prefer full-heal action during battle aftermath.
    log(ctx, f"🩹 Heal flow: reason={reason}")
    send_command(ctx, "Полное лечение")


def scenario_test(ctx: Context, cfg: Config) -> None:
    log(ctx, "=== START TEST (default) ===")
    on_hp_update(ctx, cfg, 45)
    log(ctx, "MODE: FOREST -> HP_PAUSE, ACTIVITY -> FISHING (if enabled)")
    tick_minutes(ctx, cfg, 3)
    on_hp_update(ctx, cfg, 55)
    tick_minutes(ctx, cfg, 1)
    on_hp_update(ctx, cfg, 96)
    log(ctx, "=== END TEST ===")


def scenario_hp(ctx: Context, cfg: Config, hp: int) -> None:
    log(ctx, f"=== START SCENARIO: hp {hp} ===")
    on_hp_update(ctx, cfg, hp)
    tick_minutes(ctx, cfg, 12)
    log(ctx, "=== END SCENARIO ===")


def scenario_blood(ctx: Context, cfg: Config, hp: int) -> None:
    log(ctx, f"=== START SCENARIO: blood {hp} ===")
    cfg.blood_enabled = True
    on_hp_update(ctx, cfg, hp)
    decide_level(ctx, cfg)
    log(ctx, f"LEVEL decided: current_level={ctx.current_level}, LVL={cfg.lvl}, blood_level={cfg.blood_level}")
    on_hp_update(ctx, cfg, cfg.blood_hp_high)
    decide_level(ctx, cfg)
    log(ctx, f"LEVEL after heal: current_level={ctx.current_level}")
    log(ctx, "=== END SCENARIO ===")


def scenario_golem_triplet(ctx: Context, cfg: Config) -> None:
    log(ctx, "=== START SCENARIO: golem_triplet ===")
    ctx.current_level = cfg.lvl
    on_battle_targets(ctx, cfg, ["💀 Голем ветвей", "💀 Голем ветвей", "💀 Голем ветвей"])
    log(ctx, f"After: current_level={ctx.current_level}")
    on_battle_targets(ctx, cfg, ["🗡️ Акари стилет [5]", "🛡️ Ноктилид бронекрыл [5]", "🦴 Некрофаг [5]"])
    log(ctx, "=== END SCENARIO ===")




def scenario_fishing_triggers(ctx: Context, cfg: Config) -> None:
    del cfg
    log(ctx, "=== START SCENARIO: fishing_triggers ===")
    bite_samples = [
        "Поплавок скрылся под водой. Тяни быстрее!",
        "Клюет! Нужно срочно подсекать",
    ]
    result_samples = [
        "Отличный улов!",
        "Пустой крючок...",
        "Инвентарь полон",
    ]
    for t in bite_samples:
        log(ctx, f"BITE? {is_bite_text(t)} :: {t}")
    for t in result_samples:
        log(ctx, f"RESULT? {is_result_text(t)} :: {t}")
    log(ctx, "=== END SCENARIO ===")


def scenario_anti_stuck(ctx: Context, cfg: Config) -> None:
    log(ctx, "=== START SCENARIO: anti_stuck ===")
    tick_minutes(ctx, cfg, cfg.stuck_silence_min + 1)
    tick_minutes(ctx, cfg, cfg.stuck_silence_min + 1)

    log(ctx, "=== END SCENARIO ===")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg_fsm_test.py")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("test", help="run default quick scenario")

    p_hp = sub.add_parser("hp", help="scenario: set hp and simulate time")
    p_hp.add_argument("value", type=int)

    p_blood = sub.add_parser("blood", help="scenario: blood hysteresis demo")
    p_blood.add_argument("value", type=int)

    sub.add_parser("golem_triplet", help="scenario: triplet golems -> retreat/lower level")
    sub.add_parser("anti_stuck", help="scenario: 3m silence + 30m cooldown")
    sub.add_parser("fishing_triggers", help="scenario: verify fishing bite/result trigger phrases")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = Config()
    ctx = Context(current_level=cfg.lvl)

    cmd = args.cmd or "test"
    if cmd == "test":
        scenario_test(ctx, cfg); return 0
    if cmd == "hp":
        scenario_hp(ctx, cfg, args.value); return 0
    if cmd == "blood":
        scenario_blood(ctx, cfg, args.value); return 0
    if cmd == "golem_triplet":
        scenario_golem_triplet(ctx, cfg); return 0
    if cmd == "anti_stuck":
        scenario_anti_stuck(ctx, cfg); return 0
    if cmd == "fishing_triggers":
        scenario_fishing_triggers(ctx, cfg); return 0


    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
