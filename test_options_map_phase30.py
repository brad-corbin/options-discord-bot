from options_map import build_contract_ladders, format_options_map_card


def test_options_map_contract_identity_and_targets():
    rows = [
        {"strike": 690, "side": "call", "openInterest": 21000, "volume": 28000},
        {"strike": 700, "side": "call", "openInterest": 25000, "volume": 21000},
        {"strike": 680, "side": "put", "openInterest": 17000, "volume": 53000},
        {"strike": 670, "side": "put", "openInterest": 23000, "volume": 23000},
    ]

    def lookup(ticker, strike, side, expiry):
        if ticker == "SPY" and strike == 680 and side == "put" and expiry == "2026-02-13":
            return {"tag": "buildup", "stalk_type": "watch_for_trigger"}
        return None

    ladders = build_contract_ladders(
        rows,
        ticker="SPY",
        spot=683.53,
        expiry="2026-02-13",
        dte=1,
        lookup_fn=lookup,
        top_n=2,
    )
    assert ladders["resistance"]
    assert ladders["support"]
    msg = format_options_map_card({
        "ticker": "SPY",
        "spot": 683.53,
        "expiry": "2026-02-13",
        "dte": 1,
        "em": {"bear_1sd": 670.0, "bull_1sd": 696.0, "em_1sd": 13.0},
        "structure": {"gamma_flip": 680.0, "put_wall": 670.0, "call_wall": 700.0, "max_pain": 685.0},
        "eng": {"gex": -50.0, "flip_price": 680.0},
        "bias": {"score": 4, "direction": "BULLISH"},
        "ladders": ladders,
        "generated_at": "2026-02-12 09:05 CT",
    })
    assert "SPY WATCH MAP" in msg
    assert "Exp 2026-02-13, 1DTE" in msg
    assert "aggregate" not in msg.lower()
    assert "buildup" in msg
    assert "Watch:" in msg
    assert "Flow/OI:" in msg
    assert "watch card, not a trade signal" in msg


def test_watch_map_compact_mode():
    rows = [
        {"strike": 690, "side": "call", "openInterest": 21000, "volume": 28000},
        {"strike": 680, "side": "put", "openInterest": 17000, "volume": 53000},
    ]
    ladders = build_contract_ladders(rows, ticker="SPY", spot=683.53, expiry="2026-02-13", dte=1, top_n=2)
    msg = format_options_map_card({
        "ticker": "SPY",
        "spot": 683.53,
        "expiry": "2026-02-13",
        "dte": 1,
        "em": {"bear_1sd": 670.0, "bull_1sd": 696.0, "em_1sd": 13.0},
        "structure": {"gamma_flip": 680.0, "put_wall": 670.0, "call_wall": 700.0},
        "eng": {"gex": -50.0, "flip_price": 680.0},
        "bias": {"score": 4, "direction": "BULLISH"},
        "ladders": ladders,
        "compact": True,
    })
    assert "SPY WATCH MAP" in msg
    assert "Targets:" not in msg
    assert "Context only" not in msg
    assert "Watch:" in msg


def test_ladder_requires_expiry():
    ladders = build_contract_ladders(
        [{"strike": 690, "side": "call", "openInterest": 100, "volume": 50}],
        ticker="SPY",
        spot=683.53,
        expiry="",
        dte=None,
    )
    assert ladders["resistance"] == []
    assert ladders["support"] == []


if __name__ == "__main__":
    test_options_map_contract_identity_and_targets()
    test_watch_map_compact_mode()
    test_ladder_requires_expiry()
    print("test_options_map_phase30 passed")
