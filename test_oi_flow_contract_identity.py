"""Regression tests for Flow/OI contract identity display.

Scope: display-only. Does not touch V1/V2 scorer, entries/exits, or trade cards.
"""

import datetime
import json

from oi_tracker import OITracker
from persistent_state import PersistentState


def _yesterday_str() -> str:
    yday = datetime.date.today() - datetime.timedelta(days=1)
    if yday.weekday() == 6:  # Sunday -> Friday
        yday -= datetime.timedelta(days=2)
    elif yday.weekday() == 5:  # Saturday -> Friday
        yday -= datetime.timedelta(days=1)
    return yday.isoformat()


def test_unusual_flow_requires_contract_expiry_and_can_join_confirmation_tags():
    store = {}

    def get(key):
        return store.get(key)

    def set_(key, value, ttl=None):
        store[key] = value

    expiry = "2026-05-15"
    yday = _yesterday_str()
    store[f"oi_daily:QQQ:{yday}"] = json.dumps({
        "date": yday,
        "total_oi": 10_000,
        "call_oi": 6_000,
        "put_oi": 4_000,
        "top_strikes": [{"strike_key": "650.00|call", "oi": 1_000}],
        "strikes_by_exp": {expiry: {"650.00|call": 1_000, "640.00|put": 4_000}},
        "per_exp": {expiry: {"call_oi": 6_000, "put_oi": 4_000, "total": 10_000, "dte": 13}},
        "spot": 645,
        "updated_at": 1,
    })

    tracker = OITracker(get, set_)
    tracker.set_confirmation_lookup(
        lambda ticker, strike, side, exp: {
            "tag": "buildup",
            "stalk_type": "watch_for_trigger",
            "divergence": True,
        } if ticker == "QQQ" and exp == expiry and side == "call" and abs(float(strike) - 650) < 0.01 else None
    )
    tracker.record_chain("QQQ", expiry, {
        "optionSymbol": ["c650", "p640", "c660"],
        "strike": [650, 640, 660],
        "side": ["call", "put", "call"],
        "openInterest": [2500, 4050, 4000],
    }, spot=645)

    msg = tracker.format_unusual_flow()
    assert "aggregate exp" not in msg
    assert "exp 2026-05-15" in msg
    assert "DTE" in msg
    assert "[buildup, watch for trigger, divergence]" in msg


def test_aggregate_only_flow_is_suppressed_by_default():
    store = {}

    def get(key):
        return store.get(key)

    def set_(key, value, ttl=None):
        store[key] = value

    expiry = "2026-05-15"
    yday = _yesterday_str()
    # Legacy snapshot with top_strikes only and no strikes_by_exp.
    store[f"oi_daily:SPY:{yday}"] = json.dumps({
        "date": yday,
        "total_oi": 10_000,
        "top_strikes": [{"strike_key": "500.00|call", "oi": 100}],
        "spot": 500,
    })

    tracker = OITracker(get, set_)
    tracker.record_chain("SPY", expiry, {
        "optionSymbol": ["c500"],
        "strike": [500],
        "side": ["call"],
        "openInterest": [2000],
    }, spot=500)

    msg = tracker.format_unusual_flow()
    assert "aggregate exp" not in msg
    assert "exp aggregate" not in msg


def test_persistent_confirmation_index_exact_contract_lookup():
    store = {}

    def get(key):
        return store.get(key)

    def set_(key, value, ttl=None):
        store[key] = value

    ps = PersistentState(get, set_)
    ps.save_oi_confirmation_index(
        "2026-05-02",
        confirmations=[{
            "ticker": "QQQ",
            "expiry": "2026-05-15",
            "strike": 650,
            "side": "call",
            "flow_type": "confirmed_buildup",
            "oi_change": 1500,
            "oi_change_pct": 150,
            "divergence": True,
        }],
        stalks=[{
            "ticker": "QQQ",
            "expiry": "2026-05-15",
            "strike": 650,
            "side": "call",
            "flow_type": "confirmed_buildup",
            "stalk_type": "watch_for_trigger",
            "expected_direction": "BULLISH",
        }],
    )

    ctx = ps.get_oi_confirmation_for("QQQ", 650, "call", "2026-05-15", "2026-05-02")
    assert ctx["tag"] == "buildup"
    assert ctx["stalk_type"] == "watch_for_trigger"
    assert ctx["divergence"] is True


if __name__ == "__main__":
    test_unusual_flow_requires_contract_expiry_and_can_join_confirmation_tags()
    test_aggregate_only_flow_is_suppressed_by_default()
    test_persistent_confirmation_index_exact_contract_lookup()
    print("ALL OI/FLOW CONTRACT IDENTITY TESTS PASSED")
