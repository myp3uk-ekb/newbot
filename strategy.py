import random
from typing import Optional
from config import CFG
from storage import get_kv
from game_parser import GameState, Choice

class Profile:
    def __init__(self, mode: str, blacklist: list[str]):
        self.mode = mode
        self.blacklist = set(blacklist)

def _pick_by_tier(pool: list[Choice]) -> Optional[Choice]:
    # Desired tier is controlled via chat command: /lvl N
    # Fallback rule: pick the closest available tier; if none known, pick random.
    want = None
    # Runtime can temporarily override tier via blood-heal routing.
    # Keep user-configured /lvl as a base fallback.
    # During anti-golem "wave" mode we must force base forest_level,
    # otherwise stale forest_level_effective can keep us at the old tier.
    if (get_kv('golem_wave_active') or '0') == '1':
        v = get_kv('forest_level') or get_kv('forest_level_effective')
    else:
        v = get_kv('forest_level_effective') or get_kv('forest_level')
    if v and str(v).strip().isdigit():
        want = int(str(v).strip())
    if want is None:
        # default from env PREFERRED_TIERS (first value) if present
        want = (CFG.preferred_tiers or [1])[0]

    known = [e for e in pool if e.tier is not None]
    if known:
        tiers = sorted({e.tier for e in known if e.tier is not None})
        # Exact match first
        if want in tiers:
            cands = [e for e in known if e.tier == want]
            return random.choice(cands)
        # Closest lower tier (safer) else closest higher tier
        lower = [t for t in tiers if t < want]
        higher = [t for t in tiers if t > want]
        pick_t = max(lower) if lower else (min(higher) if higher else tiers[0])
        cands = [e for e in known if e.tier == pick_t]
        return random.choice(cands) if cands else random.choice(pool)
    return random.choice(pool) if pool else None

def _kv_bool(key: str, default: bool = False) -> bool:
    v = (get_kv(key) or "").strip().lower()
    if v in ("1","true","yes","on"):
        return True
    if v in ("0","false","no","off"):
        return False
    return default

def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")

def _is_golem_choice(c: Choice) -> bool:
    t = _norm((c.name or "") + " " + (c.btn_text or ""))
    return ("голем" in t) or ("golem" in t)
def choose_target(state: GameState, profile: Profile) -> Optional[Choice]:
    pool = [b for b in state.buttons if b.name not in profile.blacklist]
    # Avoid attacking golems when golem_fight is OFF.
    if state.stage == "battle" and (not _kv_bool("mod_golem_fight", False)):
        ng = [b for b in pool if not _is_golem_choice(b)]
        if ng:
            pool = ng
    if not pool:
        return None
    if state.stage in ("forest", "battle"):
        pool2 = []
        for b in pool:
            txt = (b.btn_text or b.name or "")
            if b.tier is None:
                continue
            if ("⚔️" not in txt) and ("🗡" not in txt) and ("✖️" not in txt):
                continue
            pool2.append(b)
        if pool2:
            return _pick_by_tier(pool2)
        return _pick_by_tier(pool)
    if state.stage == "post_battle":
        for b in pool:
            if "вылазка" in (b.btn_text or b.name).lower():
                return b
        return pool[0]
    return pool[0]
