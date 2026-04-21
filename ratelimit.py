import asyncio
import os
import random
import time
from telethon.errors.rpcerrorlist import FloodWaitError


# ------------------------
# Human-like delays
# ------------------------

_CLICK_TS: list[float] = []  # sliding window of recent click timestamps


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(name: str, default: float) -> float:
    v = (os.getenv(name, "") or "").strip()
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _kv_get_safe(key: str) -> str:
    """Read KV without hard dependency to avoid import cycles."""
    try:
        from storage import get_kv  # local import

        return (get_kv(key) or "").strip()
    except Exception:
        return ""


def humanize_enabled() -> bool:
    # Env default is ON.
    env_on = _env_bool("HUMANIZE", True)
    v = _kv_get_safe("humanize")
    if v in ("0", "off", "false"):
        return False
    if v in ("1", "on", "true"):
        return True
    return env_on


def _human_context() -> str:
    # Explicit context (set by flows) wins.
    ctx = _kv_get_safe("human_ctx")
    if ctx:
        return ctx.lower()
    stage = _kv_get_safe("last_stage")
    return stage.lower() if stage else ""


def _delay_range_for_context(ctx: str) -> tuple[float, float]:
    """Return (min,max) click delay for the current context."""
    # Thief mini-game: noticeably human
    if ctx == "thief_pursue":
        return (
            _env_float("HUMAN_THIEF_PURSUE_MIN", 3.0),
            _env_float("HUMAN_THIEF_PURSUE_MAX", 6.0),
        )
    if ctx in ("thief", "thief_dir", "thief_hide"):
        return (
            _env_float("HUMAN_THIEF_MIN", 1.2),
            _env_float("HUMAN_THIEF_MAX", 2.6),
        )

    # Thief: decision to chase should look like a real person thinking.
    if ctx in ("thief_pursue", "thief_chase"):
        return (
            _env_float("HUMAN_THIEF_PURSUE_MIN", 3.0),
            _env_float("HUMAN_THIEF_PURSUE_MAX", 6.0),
        )

    # Fishing strike must stay fast
    if ctx in ("fishing_hook", "fish_hook"):
        return (
            _env_float("HUMAN_FISH_HOOK_MIN", 0.4),
            _env_float("HUMAN_FISH_HOOK_MAX", 0.9),
        )

    # Pet flow
    if ctx in ("pet", "petting"):
        return (
            _env_float("HUMAN_PET_MIN", 0.8),
            _env_float("HUMAN_PET_MAX", 1.6),
        )

    # General default
    return (
        _env_float("HUMAN_CLICK_MIN", 0.9),
        _env_float("HUMAN_CLICK_MAX", 1.8),
    )


async def human_delay(min_s: float = 0.2, max_s: float = 0.9):
    """Backward-compatible simple delay."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def human_delay_click():
    """Human-like delay before clicking a button.

    - Adds random base delay based on current context (last_stage / human_ctx)
    - Sometimes adds a 'thinking' delay
    - Adds a cooldown if too many clicks happen within a short window
    """
    if not humanize_enabled():
        return

    ctx = _human_context()
    mn, mx = _delay_range_for_context(ctx)
    base = random.uniform(mn, mx)

    # Occasional "thinking" pause
    think_p = _env_float("HUMAN_THINK_P", 0.10)
    if random.random() < max(0.0, min(1.0, think_p)):
        base += random.uniform(
            _env_float("HUMAN_THINK_MIN", 1.5),
            _env_float("HUMAN_THINK_MAX", 3.5),
        )

    # Anti-spam: if we already clicked a lot recently, add a bigger pause
    now = time.time()
    window = _env_float("HUMAN_CHAIN_WINDOW", 10.0)
    burst_n = int(_env_float("HUMAN_CHAIN_N", 3))
    extra_min = _env_float("HUMAN_CHAIN_EXTRA_MIN", 2.0)
    extra_max = _env_float("HUMAN_CHAIN_EXTRA_MAX", 5.0)

    global _CLICK_TS
    _CLICK_TS = [t for t in _CLICK_TS if (now - t) <= window]
    if len(_CLICK_TS) >= burst_n:
        base += random.uniform(extra_min, extra_max)

    # Small jitter
    base += random.uniform(0.0, _env_float("HUMAN_JITTER", 0.4))

    await asyncio.sleep(max(0.0, base))


def note_click():
    """Record a click timestamp for anti-spam."""
    try:
        _CLICK_TS.append(time.time())
    except Exception:
        pass


async def safe_call(coro_fn, *args, **kwargs):
    try:
        return await coro_fn(*args, **kwargs)
    except FloodWaitError as e:
        # Add a little jitter to look less bot-like
        await asyncio.sleep(e.seconds + random.uniform(1.0, 2.0))
        return await coro_fn(*args, **kwargs)
