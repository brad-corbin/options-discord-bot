# market_clock.py
# ═══════════════════════════════════════════════════════════════════
# Unified Market / Session Clock
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# ONE SOURCE OF TRUTH for all session timing across the bot.
#
# Prior state (inconsistent):
#   app.py:           8:30 AM – 3:00 PM CT (equity session)
#   bar_state.py:     8:30 AM – 3:15 PM CT (0DTE option session)
#   thesis_monitor.py: 510–915 minutes (= 8:30–3:15 PM CT)
#   exit_policy.py:   8:30 AM CT open, urgency brackets to 2:15 PM
#   active_scanner.py: 8:30 AM – 3:00 PM CT
#
# Reality:
#   Equity market:  8:30 AM – 3:00 PM CT (regular session)
#   0DTE options:   8:30 AM – 3:15 PM CT (SPY/QQQ/SPX trade 15 min longer)
#   Pre-market:     7:00 AM – 8:30 AM CT (some data available)
#   Power hour:     2:30 PM – 3:00 PM CT (take profits, pin risk)
#   Close zone:     2:30 PM – 3:00 PM CT (same as power hour)
#
# Usage:
#   from market_clock import (
#       is_equity_session, is_option_session, minutes_since_open,
#       minutes_to_close, session_progress, current_phase,
#   )
# ═══════════════════════════════════════════════════════════════════

from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# ── Session boundaries (minutes since midnight CT) ──
EQUITY_OPEN_MINS   = 510    # 8:30 AM CT
EQUITY_CLOSE_MINS  = 900    # 3:00 PM CT
OPTION_CLOSE_MINS  = 915    # 3:15 PM CT (0DTE SPY/QQQ/SPX)
PRE_MARKET_MINS    = 420    # 7:00 AM CT
POWER_HOUR_MINS    = 870    # 2:30 PM CT (matches current_phase logic)
CLOSE_ZONE_MINS    = 870    # 2:30 PM CT
NO_NEW_ENTRY_MINS  = 885    # 2:45 PM CT — no new 0DTE entries after this

# 0DTE tickers that trade until 3:15 PM CT
EXTENDED_SESSION_TICKERS = {"SPY", "QQQ", "SPX", "IWM", "DIA"}


def _now_ct() -> datetime:
    """Current time in Central timezone."""
    return datetime.now(CT)


def _mins_of_day(dt: datetime = None) -> int:
    """Minutes since midnight CT."""
    dt = dt or _now_ct()
    return dt.hour * 60 + dt.minute


def is_weekday(dt: datetime = None) -> bool:
    dt = dt or _now_ct()
    return dt.weekday() < 5


def is_equity_session(dt: datetime = None) -> bool:
    """True during regular equity trading hours (8:30 AM - 3:00 PM CT, Mon-Fri)."""
    dt = dt or _now_ct()
    if not is_weekday(dt):
        return False
    m = _mins_of_day(dt)
    return EQUITY_OPEN_MINS <= m < EQUITY_CLOSE_MINS


def is_option_session(ticker: str = "SPY", dt: datetime = None) -> bool:
    """True during option trading hours. 0DTE index options trade until 3:15 PM CT."""
    dt = dt or _now_ct()
    if not is_weekday(dt):
        return False
    m = _mins_of_day(dt)
    close = OPTION_CLOSE_MINS if ticker.upper() in EXTENDED_SESSION_TICKERS else EQUITY_CLOSE_MINS
    return EQUITY_OPEN_MINS <= m < close


def minutes_since_open(dt: datetime = None) -> int:
    """Minutes elapsed since 8:30 AM CT. Negative if pre-market."""
    dt = dt or _now_ct()
    return _mins_of_day(dt) - EQUITY_OPEN_MINS


def minutes_to_equity_close(dt: datetime = None) -> int:
    """Minutes remaining until 3:00 PM CT equity close."""
    dt = dt or _now_ct()
    return EQUITY_CLOSE_MINS - _mins_of_day(dt)


def minutes_to_option_close(ticker: str = "SPY", dt: datetime = None) -> int:
    """Minutes remaining until option session close for this ticker."""
    dt = dt or _now_ct()
    close = OPTION_CLOSE_MINS if ticker.upper() in EXTENDED_SESSION_TICKERS else EQUITY_CLOSE_MINS
    return close - _mins_of_day(dt)


def session_progress(dt: datetime = None) -> float:
    """
    0.0 = market open, 1.0 = equity close.
    Can exceed 1.0 during extended 0DTE option session (3:00-3:15 PM).
    """
    dt = dt or _now_ct()
    elapsed = _mins_of_day(dt) - EQUITY_OPEN_MINS
    session_len = EQUITY_CLOSE_MINS - EQUITY_OPEN_MINS  # 390 minutes
    if session_len <= 0:
        return 0.5
    return max(0.0, elapsed / session_len)


def current_phase(dt: datetime = None) -> dict:
    """
    Returns the current market phase with trading guidance.

    Phases:
        PRE_MARKET   — before 8:30 AM CT
        OPEN         — 8:30–9:00 AM CT (first 30 min, volatile)
        MORNING      — 9:00–11:30 AM CT (trend development)
        MIDDAY       — 11:30 AM–1:30 PM CT (often choppy)
        AFTERNOON    — 1:30–2:30 PM CT (trend resumption)
        POWER_HOUR   — 2:30–3:00 PM CT (take profits, pin risk, no new 0DTE)
        OPTION_CLOSE — 3:00–3:15 PM CT (0DTE only, extreme urgency)
        CLOSED       — after close
    """
    dt = dt or _now_ct()
    m = _mins_of_day(dt)

    if not is_weekday(dt) or m >= OPTION_CLOSE_MINS:
        return {"phase": "CLOSED", "label": "Market closed",
                "favor": "wait", "note": "No trading."}

    if m < EQUITY_OPEN_MINS:
        return {"phase": "PRE_MARKET", "label": "Pre-market",
                "favor": "wait", "note": "Wait for open to confirm."}

    if m < EQUITY_OPEN_MINS + 30:  # 8:30-9:00
        return {"phase": "OPEN", "label": "Opening range",
                "favor": "breakout", "note": "High vol — wait for OR to form or trade confirmed breaks."}

    if m < 690:  # before 11:30
        return {"phase": "MORNING", "label": "Morning session",
                "favor": "trend", "note": "Primary trend window. Best setups."}

    if m < 810:  # before 1:30
        return {"phase": "MIDDAY", "label": "Midday",
                "favor": "caution", "note": "Often choppy. Reduce size or wait."}

    if m < CLOSE_ZONE_MINS:  # before 2:30
        return {"phase": "AFTERNOON", "label": "Afternoon",
                "favor": "trend_resume", "note": "Trend resumption zone. Watch for continuation."}

    if m < EQUITY_CLOSE_MINS:  # before 3:00
        return {"phase": "POWER_HOUR", "label": "Power hour",
                "favor": "take_profit", "note": "Take profits. No new 0DTE entries. Pin risk."}

    # 3:00-3:15 — extended 0DTE only
    return {"phase": "OPTION_CLOSE", "label": "0DTE close",
            "favor": "exit_all", "note": "0DTE options expiring. Exit everything."}


def should_enter_0dte(dt: datetime = None) -> bool:
    """True if it's safe to open new 0DTE positions."""
    dt = dt or _now_ct()
    m = _mins_of_day(dt)
    return (is_weekday(dt) and
            EQUITY_OPEN_MINS + 15 <= m < NO_NEW_ENTRY_MINS)  # 8:45 AM - 2:45 PM


def time_urgency_multiplier(is_0dte: bool = True, dt: datetime = None) -> float:
    """
    Time-of-day urgency multiplier for exit decisions.
    Higher = tighter stops, faster scales, less patience.

    Replaces the standalone version in exit_policy.py.
    """
    if not is_0dte:
        return 1.0

    mins = minutes_since_open(dt)

    if mins < 120:      # before 10:30
        return 1.0
    elif mins < 210:    # 10:30–12:00
        return 1.2
    elif mins < 300:    # 12:00–1:30
        return 1.5
    elif mins < 345:    # 1:30–2:15
        return 2.0
    elif mins < 375:    # 2:15–2:45
        return 3.0
    else:               # after 2:45
        return 5.0


# ─────────────────────────────────────────────────────────
# v8.4 Phase 2.6: trading-day arithmetic
# ─────────────────────────────────────────────────────────

def count_trading_days_between(start, end) -> int:
    """Count trading days strictly between `start` and `end`, inclusive of
    `end` but not `start`. Trading days are weekdays (Mon-Fri).

    NOTE: does NOT honor market holidays. For the bot's 0-7 DTE windows
    this is acceptable — holidays inside that window are rare and the
    failure mode is benign (count is off by 1, picks one expiry over
    another). A holiday-aware version can use pandas_market_calendars
    later if we ever need precision beyond a 1-day window.

    Args:
        start: datetime.date or datetime.datetime — the reference day
        end:   datetime.date or datetime.datetime — the target day

    Returns:
        Integer count of weekdays from start (exclusive) to end (inclusive).
        Negative if end < start. 0 if same day or only weekends between.

    Examples (assuming weekdays):
        Friday 2026-05-01 → Monday 2026-05-04   → 1 trading day
        Friday 2026-05-01 → Tuesday 2026-05-05  → 2 trading days
        Friday 2026-05-01 → Friday 2026-05-08   → 5 trading days
        (vs calendar-day count: 3, 4, 7 respectively)
    """
    from datetime import date, datetime as _dt, timedelta
    if isinstance(start, _dt):
        start = start.date()
    if isinstance(end, _dt):
        end = end.date()
    if not isinstance(start, date) or not isinstance(end, date):
        raise TypeError(f"start/end must be date or datetime, got {type(start).__name__}/{type(end).__name__}")
    if end < start:
        return -count_trading_days_between(end, start)

    # Walk one day at a time, counting weekdays. Faster algorithm exists
    # (full-weeks math) but our windows are tiny (≤ 14 days) so simplicity wins.
    days = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:  # 0=Mon, 4=Fri
            days += 1
        cur = cur + timedelta(days=1)
    return days
