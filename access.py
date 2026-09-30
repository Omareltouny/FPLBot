"""Phase 5 (no-payments release): who may use the bot right now.

Everyone gets the FULL bot during a free trial that covers a fixed number of gameweeks. When the trial's
last gameweek is over the bot stops working for everyone except admins (and anyone an admin has
manually granted access) until a payment method exists.

The trial is global, not per user: it starts at the gameweek the bot first runs and lasts
TRIAL_GAMEWEEKS gameweeks (default 3). Set TRIAL_END_GW in .env to pin the exact last gameweek instead.
"The gameweek" is FPL's current gameweek, which ticks over at each gameweek deadline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import config
import db
from fpl_client import FPLClient, FPLError

log = logging.getLogger(__name__)

FOOTER = ""  # appended to paid-command replies for non-admins during the final trial gameweek


@dataclass
class TrialState:
    started: bool
    start_gw: int | None
    end_gw: int | None  # last gameweek the trial works
    ended: bool
    current_gw: int | None

    @property
    def open(self) -> bool:
        return self.started and not self.ended


def load_state(path: str | None = None) -> TrialState:
    start = db.get_setting("trial_start_gw", path)
    stored_end = db.get_setting("trial_end_gw", path)
    cur = db.get_setting("last_gw", path)
    if start is None:
        return TrialState(False, None, None, False, int(cur) if cur else None)
    end = config.TRIAL_END_GW or (int(stored_end) if stored_end else int(start) + config.TRIAL_GAMEWEEKS - 1)
    return TrialState(True, int(start), end, db.get_setting("trial_ended", path) == "1",
                      int(cur) if cur else None)


async def refresh(fpl: FPLClient, path: str | None = None) -> TrialState:
    """Read FPL's current gameweek (cached), start the trial on first ever run, and update the ended flag.
    If FPL can't be reached, the last known state is used, so an API outage never re-opens a finished trial."""
    global FOOTER
    try:
        gw = await fpl.current_gameweek()
    except FPLError:
        gw = None
    if gw is not None:
        db.set_setting("last_gw", str(gw), path)
        if db.get_setting("trial_start_gw", path) is None:
            db.set_setting("trial_start_gw", str(gw), path)
            db.set_setting("trial_end_gw", str(gw + config.TRIAL_GAMEWEEKS - 1), path)
            log.info("Free trial started at GW%d (ends after GW%d)", gw, gw + config.TRIAL_GAMEWEEKS - 1)
    state = load_state(path)
    if gw is not None and state.started:
        ended = gw > state.end_gw  # recomputed every time, so raising TRIAL_END_GW re-opens the app
        db.set_setting("trial_ended", "1" if ended else "0", path)
        state = load_state(path)
    FOOTER = footer_text(state)
    return state


def is_admin(telegram_id: int) -> bool:
    return telegram_id in config.ADMIN_TELEGRAM_IDS


def has_access(telegram_id: int, path: str | None = None, state: TrialState | None = None) -> bool:
    """Admins, manually granted/subscribed users, and everyone while the trial is open."""
    if db.is_subscribed(telegram_id, path):  # includes admins
        return True
    state = state or load_state(path)
    return state.open


def is_locked(telegram_id: int, path: str | None = None, state: TrialState | None = None) -> bool:
    """True only when the trial has definitely ended and this user has no other access."""
    state = state or load_state(path)
    return state.started and state.ended and not db.is_subscribed(telegram_id, path)


def _span(state: TrialState) -> str:
    return f"GW{state.start_gw}" if state.start_gw == state.end_gw else f"GW{state.start_gw}–GW{state.end_gw}"


def trial_line(state: TrialState) -> str:
    if not state.started:
        return "Free trial: starting soon."
    if state.ended:
        return f"Free trial: ended after GW{state.end_gw}."
    left = (state.end_gw - state.current_gw) if state.current_gw else None
    tail = ""
    if left is not None:
        tail = " Last free gameweek." if left <= 0 else f" {left} more gameweek{'s' if left != 1 else ''} after this one."
    return f"Free trial: everything is free for {_span(state)}.{tail}"


def footer_text(state: TrialState) -> str:
    if state.open and state.current_gw is not None and state.end_gw - state.current_gw <= 0:
        return f"\n\n<i>Free trial ends after GW{state.end_gw}. Paid plans aren't available yet.</i>"
    return ""


def footer_for(telegram_id: int) -> str:
    return "" if is_admin(telegram_id) else FOOTER


def denied_message(state: TrialState | None = None) -> str:
    state = state or load_state()
    if state.started and state.ended:
        return (f"⏹ The free trial has ended (it covered {_span(state)}) and the bot is paused. "
                "Paid plans aren't available yet — I'll announce it when they are. Thanks for testing!")
    return "🔒 This is a paid feature, and I couldn't confirm the free trial right now. Try again in a moment."
