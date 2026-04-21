from sqlmodel import SQLModel, Field, create_engine, Session
from typing import Optional
from datetime import datetime
import time

# New DB file to avoid schema conflicts with old archives
engine = create_engine("sqlite:///data_v10.db")

class KV(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str

class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow)
    chat_id: int
    msg_id: int
    kind: str
    raw_text: str

class ActionLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.utcnow)
    kind: str
    detail: str
    result: str

def init_db():
    SQLModel.metadata.create_all(engine)

def get_session():
    return Session(engine)

def get_kv(key: str, default: str | None = None) -> str | None:
    with get_session() as s:
        rec = s.get(KV, key)
        return rec.value if rec else default

def set_kv(key: str, value: str):
    with get_session() as s:
        rec = s.get(KV, key)
        if not rec:
            s.add(KV(key=key, value=value))
        else:
            rec.value = value
        s.commit()

def is_paused() -> bool:
    """Returns True if manual pause is set OR a timed pause is still active.

    Timed pause uses KV key: paused_until_ts (unix timestamp, seconds).
    """
    if get_kv("paused", "0") == "1":
        return True
    try:
        until = float(get_kv("paused_until_ts", "0") or "0")
    except Exception:
        until = 0.0
    return until > 0 and time.time() < until

def set_paused(val: bool):
    # Manual pause flag. Clearing pause also clears any timed pause.
    set_kv("paused", "1" if val else "0")
    if not val:
        set_kv("paused_until_ts", "0")


def set_pause_for_seconds(seconds: float):
    """Enable a timed pause for the given number of seconds."""
    seconds = float(seconds)
    if seconds <= 0:
        return
    set_kv("paused_until_ts", str(time.time() + seconds))
