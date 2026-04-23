from typing import Optional, Tuple, Union
from telethon.tl.custom.message import Message
from telethon import TelegramClient
from ratelimit import human_delay_click, note_click, safe_call

def _linear_index(msg: Message, pos: Tuple[int, int]) -> int:
    rows = msg.buttons or []
    r, c = pos
    if r < 0 or r >= len(rows):
        raise IndexError(f"row {r} out of range (rows={len(rows)})")
    if c < 0 or c >= len(rows[r]):
        raise IndexError(f"col {c} out of range (cols_in_row={len(rows[r])})")
    return sum(len(rows[k]) for k in range(r)) + c

async def click_button(
    client: TelegramClient,
    msg: Message,
    *,
    pos: Optional[Union[Tuple[int, int], int]] = None,
    text: Optional[str] = None,
    index: Optional[int] = None,
):
    await human_delay_click()
    if pos is not None:
        # Backward compatibility: some callers may still pass a flattened index
        # as `pos=<int>`. Treat this exactly like `index=<int>`.
        if isinstance(pos, int):
            note_click()
            return await safe_call(msg.click, i=pos)
        if not isinstance(pos, tuple) or len(pos) != 2:
            raise TypeError("pos must be a (row, col) tuple or int index")
        i = _linear_index(msg, pos)
        note_click()
        return await safe_call(msg.click, i=i)
    if index is not None:
        note_click()
        return await safe_call(msg.click, i=index)
    if text is not None:
        note_click()
        return await safe_call(msg.click, text=text)
    raise ValueError("No selector provided")


async def click_button_contains(
    client: TelegramClient,
    msg: Message,
    substrings: list[str],
):
    """Click the first button whose text contains any of `substrings` (case-insensitive).

    Falls back to exact-text click if match is found.
    Returns the result of msg.click, or None if no match.
    """
    if not msg.buttons:
        return None
    subs = [s.lower() for s in substrings if s]
    if not subs:
        return None
    # Walk buttons row-major to keep behavior deterministic.
    for r, row in enumerate(msg.buttons):
        for c, btn in enumerate(row):
            t = (getattr(btn, "text", "") or "").strip()
            tl = t.lower()
            if any(s in tl for s in subs):
                return await click_button(client, msg, pos=(r, c))
    return None
