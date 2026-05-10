# EM Brief on Market View — Implementation Plan (Patch M)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the rich `/em` Telegram output (DEALER EM BRIEF + ACTION GUIDE) on the Market View dashboard tab. Add a page-level "refresh all 35" button that triggers the existing silent-thesis batch. Add an anchored EM-brief panel at the top of the page with three entry paths (whole-card click, single-ticker input, URL deep-link).

**Architecture:** Two-phase delivery. Phase 1 (M.0–M.3) is a pure-extraction refactor of `_post_em_card`'s 474 lines into a `_compute_em_brief_data(ticker, session)` helper that returns a structured dict; both the existing Telegram path and the silent-thesis loop continue to consume it. The refactor is gated by a byte-identical snapshot test that's written FIRST, validated against pre-refactor code, then continues to pass at every commit. Phase 2 (M.4–M.8) adds the dashboard data module, three routes (page POST + status GET + brief GET), the anchored panel template + JS + CSS, hermetic tests, and CLAUDE.md update.

**Tech Stack:** Python 3.11, Flask, Jinja2, vanilla JS for the polling loop, plain CSS additions to `omega_dashboard/static/omega.css`. Reuses existing helpers (`_get_0dte_iv`, `_calc_intraday_em`, `_calc_bias`, `get_canonical_vol_regime`, etc.) without modification. No new dependencies. Patch letter: **M** (Market View / EM brief; does NOT claim **I** which is reserved for the barometer dashboard).

---

## Context the implementer needs

- Read `docs/superpowers/specs/2026-05-10-em-brief-on-market-view-design.md` end-to-end before starting. Spec is approved with 7 QC fixes already applied. The snapshot-test-gate sequencing is **load-bearing** — read the "Risks" section first.
- Read `CLAUDE.md` "Audit discipline" section. Non-negotiable: AST-check after every Python file write, separate commit per task, `# v11.7 (Patch M.N):` comment marker on every change, never inline a helper for a concept that has an existing implementation, run the canonical regression battery before declaring done.
- This patch ships AFTER Patch H.8 stabilizes (Wed 2026-05-13+). Do NOT parallel-ship with H.8 — both touch `omega.css` and `omega_dashboard/templates/dashboard/`.
- The Telegram path produces TWO messages per `/em`: the DEALER EM BRIEF (analytical card) and the ACTION GUIDE (plain-English playbook). Both must be reachable from the dashboard panel. The existing `_post_em_card` builds the brief text inline; the action guide is built later in the function (find by searching for `📡 WHAT TO DO`).

## Key codebase anchors

| Component | Location | Notes |
|---|---|---|
| `_post_em_card(ticker, session)` | `app.py:12474-12947` (~474 lines) | The function being extracted from. Builds + posts BOTH messages. Find the boundary between "compute" and "format text" by reading top-down — compute calls cluster in the first ~80 lines (`_get_0dte_iv`, `_calc_intraday_em`, `_calc_bias`, `get_canonical_vol_regime`, `compute_cagf`, etc.); the rest is `lines.append(...)` text formatting + `post_to_telegram(...)` calls. |
| `_generate_silent_thesis(ticker, refresh_only)` | `app.py:12948-13071` (~124 lines) | The other consumer of the same compute path. Stores ThesisContext to Redis. Refactor to call `_compute_em_brief_data` then `_write_thesis_context(data)`. |
| `_get_0dte_iv` | `app.py` (search for `def _get_0dte_iv`) | Returns 9-element tuple: `iv, spot, expiration, eng, walls, skew, pcr, vix, v4_result`. The single most important compute call inside `_post_em_card`. |
| `_calc_intraday_em(spot, iv, hours)` | `app.py` | Returns dict with `bull_1sd / bull_2sd / bear_1sd / bear_2sd / range_1sd / range_2sd`. |
| `_calc_bias(spot, em, walls, skew, eng, pcr, vix)` | `app.py` | Returns dict with `direction / score / max_score / strength / verdict / signals / up_count / down_count / neu_count / na_count / n_signals`. |
| `get_canonical_vol_regime(...)` | `app.py` (imported) | Returns vol regime dict. |
| `post_to_telegram(text)` | `app.py` (imported) | The side-effecting call. Mocked in the snapshot test. |
| `EM_TICKERS` | `app.py:10292` | `["SPY", "QQQ"]` — default `/em` set. NOT used by Patch M. |
| `FLOW_TICKERS` | `oi_flow.py:113-130` | The 35 (actually 33) tickers the refresh button operates on. |
| Telegram `/em` command handler | `telegram_commands.py:435-471` | Where `_post_em_card` is invoked from Telegram. Stays unchanged through Patch M. |
| Trading tab page route | `omega_dashboard/routes.py:297-310` | Pattern Patch M's new routes mirror. |
| Trading tab JSON endpoint | `omega_dashboard/routes.py:312-325` | Polling pattern. |
| Trading data layer | `omega_dashboard/data.py:1294` `trading_data()` | Existing card grid feed; reads `thesis_monitor:{ticker}` from Redis. |
| Trading template + card partial | `omega_dashboard/templates/dashboard/trading.html` + `_trading_card.html` | Whole-card click target gets added here. |
| Dashboard CSS file | `omega_dashboard/static/omega.css` | All styling additions. Use real tokens (`--bg-panel`, `--brass-bright`, `--positive-bright`, etc.) — see lines 17-63 for the full token list. |
| Patch H.6 hermetic test pattern | `test_alerts_data.py` | PASS/FAIL counter footer + tempfile setup. Match this convention. |

## File structure (created/modified by this plan)

**Created:**
- `tests/fixtures/em_brief_snapshots/` — directory for the 3 fixture files
- `tests/fixtures/em_brief_snapshots/spy.txt` — SPY pre-refactor brief (full text, both messages concatenated with a separator)
- `tests/fixtures/em_brief_snapshots/hood.txt` — HOOD pre-refactor brief
- `tests/fixtures/em_brief_snapshots/thin.txt` — thin-chain ticker pre-refactor brief
- `tests/fixtures/em_brief_snapshots/inputs.json` — the deterministic mock inputs for each scenario (so the test is reproducible without live Schwab)
- `test_em_brief_snapshot.py` — the snapshot regression test (M.0)
- `omega_dashboard/em_data.py` — read-side data layer (M.4)
- `omega_dashboard/templates/dashboard/_em_brief_panel.html` — structured panel partial (M.6)
- `test_em_data.py` — hermetic tests for em_data.py (M.4 + M.5 + M.8)

**Modified:**
- `app.py` — add `_compute_em_brief_data(ticker, session=None)` helper (M.1); refactor `_post_em_card` to consume it (M.2); refactor `_generate_silent_thesis` to consume it (M.3)
- `omega_dashboard/routes.py` — three new routes (M.5)
- `omega_dashboard/templates/dashboard/trading.html` — page-header strip with refresh button + ticker input, panel container, JS additions (M.7)
- `omega_dashboard/templates/dashboard/_trading_card.html` — wrap card body in clickable surface (M.7)
- `omega_dashboard/static/omega.css` — append panel + button + input + animation styles (M.7)
- `CLAUDE.md` — append Patch M entry to "What's done as of last session" (M.8)

**Untouched (validate no regressions):**
- All `canonical_*.py` modules and their tests
- `bot_state_producer.py` and its tests
- `alert_recorder.py`, `alerts_data.py`, recorder daemons — Patch G/H/H.8 intact
- `telegram_commands.py` — `/em` Telegram command unchanged
- All existing dashboard routes (`/dashboard`, `/trading`, `/portfolio`, `/research`, `/restore`, `/alerts`)
- All engine logic — Patch M is purely additive on the dashboard side; the refactor is byte-identical on the Telegram side

## Env vars

| Var | Default | Notes |
|---|---|---|
| `EM_BRIEF_DASHBOARD_ENABLED` | `true` (after ship) | Master kill switch. When `false`: routes return 410 Gone; trading.html omits the panel + refresh button + ticker input. One-flip rollback. |

No other new env vars. Refresh-all uses Redis keys under `em_refresh:{job_id}` for status; uses the existing `REDIS_URL` and `_get_redis()` helper.

---

## Constants and shared helpers

Define once in `omega_dashboard/em_data.py`. Listed here so the implementer doesn't duplicate.

```python
# omega_dashboard/em_data.py — top of file

"""EM brief read-side data layer for the Market View dashboard.

# v11.7 (Patch M): pure read access to the existing _compute_em_brief_data
# helper in app.py, plus the all-35 refresh job orchestration.
# Never imports private helpers from telegram_commands or the recorder.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CHICAGO_TZ = ZoneInfo("America/Chicago")

# Status-key prefix in Redis for in-flight all-35 refresh jobs.
REFRESH_KEY_PREFIX = "em_refresh:"
REFRESH_STATUS_TTL_SEC = 30 * 60  # 30 min — long enough that mid-refresh
                                  # page reloads still see the status,
                                  # short enough that stale jobs expire.

# Daemon throttle (per QC fix #1) — explicit serialization between
# tickers to protect the Schwab rate limiter during market hours.
INTER_TICKER_SLEEP_SEC = 2.0

# After this elapsed seconds, the UI prepends a "this can take several
# minutes during market hours" caption to the progress text.
SLOW_CAPTION_THRESHOLD_SEC = 60


def _kill_switch_off() -> bool:
    """Return True if EM_BRIEF_DASHBOARD_ENABLED is explicitly set to false.
    Defaults to enabled (env unset → not killed)."""
    return os.getenv("EM_BRIEF_DASHBOARD_ENABLED", "true").strip().lower() in ("0", "false", "no")
```

---

## Phase 1 — Pure-extraction refactor (snapshot-gated)

**This phase ships in 4 commits.** Each commit must keep the snapshot test green. Snapshot test is written FIRST (M.0) and is the ONLY guard against silent drift in `/em` output.

---

### Task M.0: Snapshot test gate

**Files:**
- Create: `tests/fixtures/em_brief_snapshots/inputs.json` — deterministic inputs for 3 scenarios (spy / hood / thin)
- Create: `tests/fixtures/em_brief_snapshots/{spy,hood,thin}.txt` — captured pre-refactor output text
- Create: `test_em_brief_snapshot.py` — the regression test

**Why this task ships first:** the test must pass against PRE-refactor code. Writing it after the refactor would lock in post-refactor output, defeating the purpose. See spec's "Risks" section for the full sequencing rationale.

- [ ] **Step 1: Create the fixture-inputs JSON**

Write `tests/fixtures/em_brief_snapshots/inputs.json` with 3 scenarios. Each scenario specifies the deterministic inputs the mocks return when `_post_em_card` runs. SPY = trade-on / volatile; HOOD = neutral / pin-zone; THIN = no-data edge case.

```json
{
  "spy": {
    "ticker": "SPY",
    "session": "manual",
    "now_iso_ct": "2026-05-12T10:30:00-05:00",
    "_get_0dte_iv_returns": {
      "iv": 0.18, "spot": 588.50, "expiration": "2026-05-13",
      "eng": {"gex": 12.4, "dex": -3.1, "vanna": 1.2, "charm": -0.8,
              "flip_price": 585.00,
              "regime": {"preferred": "long puts on rallies", "avoid": "naked calls"}},
      "walls": {"call_wall": 595, "call_wall_oi": 42000,
                "put_wall": 580, "put_wall_oi": 38000,
                "gamma_wall": 590,
                "call_top3": [595, 600, 605], "put_top3": [580, 575, 570]},
      "skew": {"skew_25d": -0.04, "rr_25d": -0.025, "interpretation": "put skew rich"},
      "pcr": {"pcr_oi": 1.42, "pcr_vol": 1.31},
      "vix": {"vix": 18.5, "vix9d": 17.2, "term": "normal", "source": "live"},
      "v4_result": {"confidence": {"label": "HIGH", "composite": 0.78},
                    "downgrades": [],
                    "vol_regime": {"realized_vol_20d": 0.16}}
    },
    "_estimate_liquidity_returns": {"adv": 80_000_000, "_": null},
    "get_daily_candles_returns": [580.1, 581.5, 583.2, 585.8, 587.1, 588.0, 588.50]
  },
  "hood": {
    "ticker": "HOOD",
    "session": "manual",
    "now_iso_ct": "2026-05-12T14:00:00-05:00",
    "_get_0dte_iv_returns": {
      "iv": 0.42, "spot": 77.03, "expiration": "2026-05-15",
      "eng": {"gex": 4.4, "dex": 0.2, "vanna": 0.1, "charm": 0.05,
              "flip_price": 75.79,
              "regime": {"preferred": "iron condor", "avoid": "directional"}},
      "walls": {"call_wall": 80, "call_wall_oi": 12000,
                "put_wall": 75, "put_wall_oi": 11000,
                "gamma_wall": 75},
      "skew": null,
      "pcr": {"pcr_oi": 0.95, "pcr_vol": 0.88},
      "vix": {"vix": 17.2, "vix9d": null, "term": "normal", "source": "live"},
      "v4_result": {}
    },
    "_estimate_liquidity_returns": {"adv": 5_000_000, "_": null},
    "get_daily_candles_returns": [75.5, 76.0, 76.8, 77.2, 76.9, 77.0, 77.03]
  },
  "thin": {
    "ticker": "ZZZZ",
    "session": "manual",
    "now_iso_ct": "2026-05-12T11:15:00-05:00",
    "_get_0dte_iv_returns": {
      "iv": null, "spot": null, "expiration": null,
      "eng": null, "walls": null, "skew": null,
      "pcr": null, "vix": null, "v4_result": {}
    },
    "_estimate_liquidity_returns": {"adv": 100_000, "_": null},
    "get_daily_candles_returns": []
  }
}
```

- [ ] **Step 2: Write the snapshot test (initially without fixtures)**

Create `test_em_brief_snapshot.py`:

```python
"""Snapshot regression test for the _post_em_card refactor.

# v11.7 (Patch M.0): MUST pass against pre-refactor code BEFORE the
# extraction begins. Each subsequent refactor commit (M.1 / M.2 / M.3)
# must keep this test green. Without this gate, the refactor could
# silently drift from the original Telegram output and break the
# /em command's behavior invisibly.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytz

FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "em_brief_snapshots"


def _load_inputs():
    with open(FIXTURE_DIR / "inputs.json") as f:
        return json.load(f)


def _run_post_em_card_capture(scenario_key: str, scenario: dict) -> str:
    """Mock all the data sources, monkey-patch post_to_telegram to capture
    text, run _post_em_card, return concatenated captured text."""
    captured: list = []

    def _fake_post(text, **kwargs):
        captured.append(text)
        return True

    # Pin "now" so any time-of-day branches are deterministic.
    fake_now_ct = datetime.fromisoformat(scenario["now_iso_ct"])

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fake_now_ct.astimezone(tz)
            return fake_now_ct

    iv_tuple = scenario["_get_0dte_iv_returns"]
    iv_return = (
        iv_tuple["iv"], iv_tuple["spot"], iv_tuple["expiration"],
        iv_tuple["eng"], iv_tuple["walls"], iv_tuple["skew"],
        iv_tuple["pcr"], iv_tuple["vix"], iv_tuple["v4_result"],
    )

    with patch("app._get_0dte_iv", return_value=iv_return), \
         patch("app.post_to_telegram", side_effect=_fake_post), \
         patch("app._estimate_liquidity",
               return_value=(scenario["_estimate_liquidity_returns"]["adv"], None)), \
         patch("app.get_daily_candles",
               return_value=scenario["get_daily_candles_returns"]), \
         patch("app.datetime", _FakeDateTime):
        from app import _post_em_card
        _post_em_card(scenario["ticker"], scenario["session"])

    return "\n=== MESSAGE BREAK ===\n".join(captured)


def _fixture_path(scenario_key: str) -> Path:
    return FIXTURE_DIR / f"{scenario_key}.txt"


def test_em_brief_snapshot_unchanged():
    """For each of spy/hood/thin: capture current _post_em_card output
    and assert byte-identical to the committed fixture."""
    inputs = _load_inputs()
    failures = []
    for key, scenario in inputs.items():
        actual = _run_post_em_card_capture(key, scenario)
        fixture_path = _fixture_path(key)
        if not fixture_path.exists():
            failures.append(f"{key}: fixture missing — capture-and-write step not done")
            continue
        with open(fixture_path) as f:
            expected = f.read()
        if actual != expected:
            # Write the diff to a debug file for easier inspection
            debug_path = fixture_path.with_suffix(".actual.txt")
            with open(debug_path, "w") as f:
                f.write(actual)
            failures.append(
                f"{key}: snapshot drift. Compare {fixture_path} vs {debug_path}"
            )
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        raise AssertionError(f"{len(failures)} snapshot(s) failed")


if __name__ == "__main__":
    try:
        test_em_brief_snapshot_unchanged()
        print("PASS: test_em_brief_snapshot_unchanged")
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
```

- [ ] **Step 3: Capture the fixtures by running once with no fixtures**

Run the test once. It will fail with "fixture missing" + write `*.actual.txt` files. **Inspect each `.actual.txt` carefully** — read the captured text top-to-bottom. Make sure the SPY scenario looks like a trade-on brief, HOOD looks like a pin-zone brief, THIN renders the no-data error path. If any look wrong, fix the input data in `inputs.json` and re-run.

```powershell
python test_em_brief_snapshot.py
# Inspect: tests/fixtures/em_brief_snapshots/spy.actual.txt
#          tests/fixtures/em_brief_snapshots/hood.actual.txt
#          tests/fixtures/em_brief_snapshots/thin.actual.txt
```

- [ ] **Step 4: Promote the .actual.txt files to fixtures**

```powershell
mv tests/fixtures/em_brief_snapshots/spy.actual.txt   tests/fixtures/em_brief_snapshots/spy.txt
mv tests/fixtures/em_brief_snapshots/hood.actual.txt  tests/fixtures/em_brief_snapshots/hood.txt
mv tests/fixtures/em_brief_snapshots/thin.actual.txt  tests/fixtures/em_brief_snapshots/thin.txt
```

- [ ] **Step 5: Re-run the test — should now PASS**

```powershell
python test_em_brief_snapshot.py
```

Expected: `PASS: test_em_brief_snapshot_unchanged`

- [ ] **Step 6: AST-check + commit**

```powershell
python -c "import ast; ast.parse(open('test_em_brief_snapshot.py').read()); print('AST OK')"
git add tests/fixtures/em_brief_snapshots/ test_em_brief_snapshot.py
git commit -m "$(cat <<'EOF'
Patch M.0: Snapshot test gate for _post_em_card refactor

Captures byte-identical pre-refactor output for 3 scenarios
(spy / hood / thin) using deterministic mocks of _get_0dte_iv,
post_to_telegram, _estimate_liquidity, get_daily_candles.

This test MUST pass before the M.1 extraction begins, and MUST
continue to pass at every commit through M.3. It is the only
guard against silent drift in the /em Telegram output during
the pure-extraction refactor.

Inputs in inputs.json; outputs as 3 .txt fixtures concatenating
both messages (DEALER EM BRIEF + ACTION GUIDE) with a separator.
EOF
)"
```

---

### Task M.1: Extract `_compute_em_brief_data`

**Files:**
- Modify: `app.py` — add `_compute_em_brief_data(ticker, session=None)` near `_post_em_card`. Do NOT modify `_post_em_card` yet — extraction is additive in M.1.

This task adds the new helper alongside the old function. The snapshot test still exercises the OLD `_post_em_card` (unchanged), so it continues to pass. M.2 is the swap that actually routes `_post_em_card` through the new helper.

- [ ] **Step 1: Read the boundary in `_post_em_card`**

Open `app.py:12474-12947` and identify the boundary: top section (~lines 12474–12555 covers IV/spot/walls/em/bias/vol/v4 compute) is the data layer; everything else is text formatting + post calls. Note specifically:

- Lines that START with `result_tuple = _get_0dte_iv(...)` and continue through the `bias = _calc_bias(...)` call — these are the **compute** that moves into `_compute_em_brief_data`.
- The `cagf = ...` block (~12604+) and `dte_rec = ...` block — these are also compute, conditional on `ticker in ("SPY", "QQQ", "SPX")`.
- Everything wrapped in `lines.append(...)` or `lines += [...]` is **format** that stays in `_post_em_card`.
- Any `post_to_telegram(...)` is the **emit** that stays.

- [ ] **Step 2: Add the new helper at the end of `_post_em_card`'s neighborhood (before `_generate_silent_thesis`)**

Insert at `app.py:12948` (just before `def _generate_silent_thesis`):

```python
# v11.7 (Patch M.1): pure-compute extraction. Single source of truth for
# the EM brief data layer. Consumed by:
#   1. _post_em_card  (Telegram emit path)        — refactored in M.2
#   2. _generate_silent_thesis  (silent-thesis store)  — refactored in M.3
#   3. omega_dashboard.em_data.get_em_brief  (dashboard path) — added in M.4
#
# Must produce identical output for the same inputs as the original
# _post_em_card body. Snapshot-gated by test_em_brief_snapshot_unchanged.
def _compute_em_brief_data(ticker: str, session: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Compute the structured data dict for an EM brief.

    session=None resolves from time-of-day using the existing _post_em_card
    auto-detect (after-hours flips to next-day preview, etc.).

    Returns None if the underlying chain/IV is unavailable for the ticker.
    Otherwise returns a dict containing every field _post_em_card's text
    formatter and the dashboard panel template need:
      - meta:        ticker, expiration, target_date_str, session_resolved,
                     hours_for_em, session_emoji, session_label, horizon_note
      - inputs:      iv, spot, eng, walls, skew, pcr, vix, v4_result, vol_regime
      - computed:    em (dict), bias (dict), cagf (dict|None), dte_rec (dict|None)
      - quality:     available_sections (List[str]) — Patch M dashboard uses this
                     to render partial briefs with n/a fields when underlying
                     data is missing.
    """
    try:
        import pytz
        ct = pytz.timezone("America/Chicago")
        now_ct = datetime.now(ct)
        today_dt = now_ct.date()

        # Auto-detect session from time-of-day if caller passes None.
        # Matches _post_em_card's existing logic at lines 12477-12490.
        market_open_ct = now_ct.replace(hour=8, minute=30, second=0, microsecond=0)
        market_close_ct = now_ct.replace(hour=15, minute=0, second=0, microsecond=0)
        is_market_closed = now_ct > market_close_ct or now_ct < market_open_ct

        if session is None:
            # Same default-resolution logic _post_em_card uses today.
            session_resolved = "afternoon" if is_market_closed else "manual"
        else:
            session_resolved = session

        is_afternoon = (session_resolved == "afternoon")
        if not is_afternoon and is_market_closed:
            is_afternoon = True

        if is_afternoon:
            target_date_str = _get_next_trading_day(today_dt)
            hours_for_em = 6.5
            session_emoji = "🌆"
            session_label = "Next Day Preview"
            horizon_note = f"Full session EM for {target_date_str}"
        else:
            target_date_str = today_dt.strftime("%Y-%m-%d")
            hours_for_em = max((market_close_ct - now_ct).total_seconds() / 3600, 0.25)
            if now_ct >= market_open_ct:
                session_emoji = "🔔"; session_label = "Today (Live)"
            else:
                session_emoji = "🌅"; session_label = "Today (Pre-Open)"
            horizon_note = f"{hours_for_em:.1f}h remaining today"

        result_tuple = _get_0dte_iv(ticker, target_date_str)
        iv, spot, expiration = result_tuple[0], result_tuple[1], result_tuple[2]
        eng, walls, skew, pcr, vix = (result_tuple[3], result_tuple[4],
                                       result_tuple[5], result_tuple[6],
                                       result_tuple[7])
        v4_result = result_tuple[8] if len(result_tuple) > 8 else {}

        if iv is None or spot is None:
            log.warning(f"_compute_em_brief_data: IV unavailable for {ticker}")
            return None

        em = _calc_intraday_em(spot, iv, hours_for_em)
        if not em:
            return None

        # VIX proxy fallback (matches _post_em_card lines 12522-12526).
        if not vix or not vix.get("vix"):
            if iv and iv > 0:
                proxy_vix = round(iv * 100, 1)
                vix = {"vix": proxy_vix, "vix9d": None,
                       "term": "unknown", "source": "iv_proxy"}

        vol_regime = get_canonical_vol_regime(
            ticker, get_daily_candles(ticker, days=30), vix_override=vix
        )
        bias = _calc_bias(spot, em, walls or {}, skew or {}, eng or {}, pcr or {}, vix or {})

        # CAGF + DTE recommendation — SPY/QQQ/SPX only (matches existing).
        cagf = None
        dte_rec = None
        if eng and ticker.upper() in ("SPY", "QQQ", "SPX"):
            _now_ct_local = now_ct  # already CT
            _mkt_open = _now_ct_local.replace(hour=8, minute=30, second=0, microsecond=0)
            _mkt_close = _now_ct_local.replace(hour=15, minute=0, second=0, microsecond=0)
            _session_secs = (_mkt_close - _mkt_open).total_seconds()
            _elapsed = max(0, (_now_ct_local - _mkt_open).total_seconds())
            _sess_progress = min(1.0, _elapsed / _session_secs) if _session_secs > 0 else 0.5
            _rv = 0
            if v4_result and v4_result.get("vol_regime"):
                _rv = v4_result["vol_regime"].get("realized_vol_20d", 0) or 0
            _vix_val = vix.get("vix", 20) if vix else 20
            _adv, _ = _estimate_liquidity(ticker, spot)
            _closes = get_daily_candles(ticker, days=60)
            cagf = compute_cagf(
                dealer_flows={
                    "gex": eng.get("gex", 0), "dex": eng.get("dex", 0),
                    "vanna": eng.get("vanna", 0), "charm": eng.get("charm", 0),
                    "gamma_flip": eng.get("flip_price"),
                },
                iv=iv, rv=_rv, spot=spot, vix=_vix_val,
                session_progress=_sess_progress, adv=_adv,
                candle_closes=_closes, ticker=ticker,
            )
            dte_rec = recommend_dte(
                cagf=cagf, iv=iv, vix=_vix_val,
                session_progress=_sess_progress,
            )

        # available_sections drives the dashboard's partial-brief rendering
        # (Patch M.4+). True/False per major section.
        available_sections = []
        if iv is not None and spot is not None:
            available_sections.append("header")
        if em:
            available_sections.append("em_range")
        if walls:
            available_sections.append("walls")
        if eng:
            available_sections.append("dealer_flow")
        if bias:
            available_sections.append("bias")
        if cagf:
            available_sections.append("cagf")
        if dte_rec:
            available_sections.append("dte_rec")
        if vol_regime:
            available_sections.append("vol_regime")

        return {
            # Meta
            "ticker": ticker,
            "session_resolved": session_resolved,
            "expiration": expiration,
            "target_date_str": target_date_str,
            "hours_for_em": hours_for_em,
            "session_emoji": session_emoji,
            "session_label": session_label,
            "horizon_note": horizon_note,
            # Inputs
            "iv": iv,
            "spot": spot,
            "eng": eng,
            "walls": walls,
            "skew": skew,
            "pcr": pcr,
            "vix": vix,
            "v4_result": v4_result,
            "vol_regime": vol_regime,
            # Computed
            "em": em,
            "bias": bias,
            "cagf": cagf,
            "dte_rec": dte_rec,
            # Quality
            "available_sections": available_sections,
        }
    except Exception as e:
        log.warning(f"_compute_em_brief_data({ticker}): {e}", exc_info=True)
        return None
```

- [ ] **Step 2.5: Confirm `Optional` is imported at the top of `app.py`**

```powershell
Select-String -Path app.py -Pattern "^from typing import" | Select-Object -First 3
```

If `Optional` isn't already in the typing import, add it:

```python
# At the top of app.py, near other typing imports:
from typing import Any, Dict, List, Optional
```

- [ ] **Step 3: AST-check**

```powershell
python -c "import ast; ast.parse(open('app.py').read()); print('AST OK')"
```

- [ ] **Step 4: Re-run snapshot test (must STILL pass — old code path unchanged)**

```powershell
python test_em_brief_snapshot.py
```

Expected: `PASS: test_em_brief_snapshot_unchanged`. M.1 only ADDS the helper — it does not yet route `_post_em_card` through it.

- [ ] **Step 5: Commit**

```powershell
git add app.py
git commit -m "$(cat <<'EOF'
Patch M.1: Extract _compute_em_brief_data helper (additive)

Adds _compute_em_brief_data(ticker, session=None) at app.py:12948
as a pure-compute extraction of the data layer from _post_em_card's
top section (~80 lines worth of compute). Returns a structured dict
with meta/inputs/computed/quality fields including the new
`available_sections` list that drives partial-brief rendering on
the dashboard.

session=None auto-resolves from time-of-day via the same logic
_post_em_card uses today (matches QC fix #3).

PURELY ADDITIVE — _post_em_card is unchanged in this commit; the
snapshot test still exercises the original path. M.2 routes
_post_em_card through the new helper; M.3 does the same for
_generate_silent_thesis.

Snapshot test: still passes (no behavior change yet).
EOF
)"
```

---

### Task M.2: Refactor `_post_em_card` to consume the helper

**Files:**
- Modify: `app.py:12474-12947` — replace the compute section in `_post_em_card` with a single call to `_compute_em_brief_data(ticker, session)`. Keep all the text-formatting and `post_to_telegram` calls intact, but read inputs from the returned dict instead of from local variables.

This is the load-bearing commit for the refactor. The snapshot test catches any drift.

- [ ] **Step 1: Replace the compute prefix of `_post_em_card`**

The current top of `_post_em_card` (lines 12474-12555ish) is the compute. Replace it with a call to the helper. The function shape becomes:

```python
def _post_em_card(ticker: str, session: str):
    try:
        # v11.7 (Patch M.2): consume _compute_em_brief_data for the
        # data layer. Text formatting + post_to_telegram calls below
        # are byte-identical to the pre-refactor code; snapshot test
        # (test_em_brief_snapshot.py) gates this contract.
        data = _compute_em_brief_data(ticker, session)
        if data is None:
            log.warning(f"EM card skipped for {ticker}: data unavailable")
            post_to_telegram(f"⚠️ {ticker} EM: could not fetch IV for {ticker}")
            return

        # Unpack into the same locals the original body used so the
        # rest of the function (text formatting) stays byte-identical.
        ticker = data["ticker"]
        session_emoji = data["session_emoji"]
        session_label = data["session_label"]
        horizon_note = data["horizon_note"]
        target_date_str = data["target_date_str"]
        iv = data["iv"]
        spot = data["spot"]
        expiration = data["expiration"]
        eng = data["eng"]
        walls = data["walls"]
        skew = data["skew"]
        pcr = data["pcr"]
        vix = data["vix"]
        v4_result = data["v4_result"]
        vol_regime = data["vol_regime"]
        em = data["em"]
        bias = data["bias"]
        cagf = data["cagf"]
        dte_rec = data["dte_rec"]

        # Original IV-emoji selection (was at line 12532 in pre-refactor):
        iv_pct = iv * 100
        if iv_pct < 10:    iv_emoji = "🟢"; iv_note = "Low IV — tight range."
        elif iv_pct < 20:  iv_emoji = "🟡"; iv_note = "Moderate IV — EM ranges reliable."
        elif iv_pct < 35:  iv_emoji = "🔴"; iv_note = "Elevated IV — respect stops."
        else:              iv_emoji = "🚨"; iv_note = "EXTREME IV — EM may understate. Minimum size."

        # ══════ HEADER ══════
        lines = [
            f"{session_emoji} {ticker} — Institutional EM Brief ({session_label})",
            f"Spot: ${spot:.2f}  |  IV: {iv_emoji} {iv_pct:.1f}%  |  Exp: {expiration}",
            f"Session: {horizon_note}",
        ]

        # ── v4.3: Confidence (single line, no duplicate) ──
        if v4_result:
            # ... rest of the function continues UNCHANGED from pre-refactor ...
```

The only changed code is the BLOCK ABOVE `iv_pct = iv * 100`. Everything from the IV-emoji block down to `post_to_telegram(...)` calls stays byte-for-byte identical.

- [ ] **Step 2: AST-check**

```powershell
python -c "import ast; ast.parse(open('app.py').read()); print('AST OK')"
```

- [ ] **Step 3: Run snapshot test — MUST PASS byte-identical**

```powershell
python test_em_brief_snapshot.py
```

Expected: `PASS: test_em_brief_snapshot_unchanged`. If it fails:
- Compare `tests/fixtures/em_brief_snapshots/{key}.actual.txt` vs `{key}.txt`
- Identify the drift line
- Trace back to which field in `_compute_em_brief_data` is producing different output
- Fix and re-run

Do NOT proceed to M.3 unless this passes.

- [ ] **Step 4: Commit**

```powershell
git add app.py
git commit -m "$(cat <<'EOF'
Patch M.2: Route _post_em_card through _compute_em_brief_data

Replaces _post_em_card's top ~80 lines of compute with a single
call to _compute_em_brief_data(ticker, session). Local-variable
unpacking preserves the rest of the function byte-for-byte
(text formatting + post_to_telegram calls unchanged).

Snapshot test continues to pass — refactor is provably
text-identical for the 3 mocked scenarios. /em Telegram output
unchanged.
EOF
)"
```

---

### Task M.3: Refactor `_generate_silent_thesis` to consume the helper

**Files:**
- Modify: `app.py:12948-13071` — replace the compute prefix of `_generate_silent_thesis` with a call to `_compute_em_brief_data`, keeping the ThesisContext-write side intact.

`_generate_silent_thesis` is the OTHER consumer of the same compute path. It currently duplicates much of `_post_em_card`'s top-of-function logic. Same refactor pattern.

- [ ] **Step 1: Replace the compute prefix**

Find the body of `_generate_silent_thesis(ticker, refresh_only=False)`. The first ~60 lines are compute (mirrors `_post_em_card`'s compute); the rest is the ThesisContext-write logic. Replace the compute prefix with:

```python
def _generate_silent_thesis(ticker: str, refresh_only: bool = False):
    """Generate and store thesis from EM card data WITHOUT posting to Telegram.
    ... (existing docstring) ...
    """
    try:
        # v11.7 (Patch M.3): consume _compute_em_brief_data for the
        # data layer. ThesisContext-write side below is unchanged.
        data = _compute_em_brief_data(ticker, session=None)
        if data is None:
            log.debug(f"Silent thesis skipped for {ticker}: data unavailable")
            return False

        # Unpack into the locals the rest of the function uses.
        # (Mirror the same names the pre-refactor body used.)
        iv = data["iv"]
        spot = data["spot"]
        expiration = data["expiration"]
        eng = data["eng"]
        walls = data["walls"]
        skew = data["skew"]
        pcr = data["pcr"]
        vix = data["vix"]
        v4_result = data["v4_result"]
        vol_regime = data["vol_regime"]
        em = data["em"]
        bias = data["bias"]
        # session-resolution locals (silent thesis uses today_str specifically):
        import pytz
        ct = pytz.timezone("America/Chicago")
        now_ct = datetime.now(ct)
        today_str = now_ct.strftime("%Y-%m-%d")

        # ── rest of the function (ThesisContext build + Redis write +
        # em_predictions Sheet write) is UNCHANGED from pre-refactor ──
```

- [ ] **Step 2: AST-check**

```powershell
python -c "import ast; ast.parse(open('app.py').read()); print('AST OK')"
```

- [ ] **Step 3: Run snapshot test (still passes — silent thesis isn't in the snapshot scope but proves no regression)**

```powershell
python test_em_brief_snapshot.py
```

- [ ] **Step 4: Spot-check `_generate_silent_thesis` import-time runs**

```powershell
python -c "from app import _generate_silent_thesis; print('import OK')"
```

Expected: `import OK`. (Validates the refactor doesn't break Python import — full execution requires Schwab credentials.)

- [ ] **Step 5: Commit**

```powershell
git add app.py
git commit -m "$(cat <<'EOF'
Patch M.3: Route _generate_silent_thesis through _compute_em_brief_data

Same pure-extraction pattern as M.2. _generate_silent_thesis's
top-of-function compute is replaced with a single
_compute_em_brief_data call; the ThesisContext-write side stays
byte-identical.

Both consumers (Telegram /em and silent-thesis store) now share
one compute path. Patch M.4 adds the third consumer (dashboard).

Snapshot test still passes; import smoke OK.
EOF
)"
```

---

## Phase 2 — Dashboard surface

**This phase ships in 5 commits.** Builds on the M.1-extracted helper. Adds dashboard-only code; no Telegram or recorder side effects.

---

### Task M.4: `omega_dashboard/em_data.py` data module

**Files:**
- Create: `omega_dashboard/em_data.py` — read-side data layer
- Modify: `app.py` — expose `_compute_em_brief_data` for cross-module import (already module-level, just confirm)

- [ ] **Step 1: Create `em_data.py` with constants + helpers + `get_em_brief`**

Write the constants block from earlier ("Constants and shared helpers" section), then append:

```python
def get_em_brief(ticker: str, session: Optional[str] = None) -> Dict[str, Any]:
    """Compute the EM brief for a single ticker, shaped for the dashboard.

    Wraps app._compute_em_brief_data. Adds dashboard-specific shape
    (CT-formatted timestamps, partial-brief warning flags). Returns
    a dict the _em_brief_panel.html template renders. NEVER raises —
    returns a dict with `available=False` + friendly error message
    on any failure (route renders the error state)."""
    if _kill_switch_off():
        return {
            "available": False,
            "error": "EM brief panel disabled (EM_BRIEF_DASHBOARD_ENABLED=false).",
            "ticker": ticker,
        }
    try:
        from app import _compute_em_brief_data
        data = _compute_em_brief_data(ticker, session)
    except Exception as e:
        log.warning(f"em_data.get_em_brief({ticker}): {e}", exc_info=True)
        return {
            "available": False,
            "error": f"Couldn't compute brief for {ticker}: {type(e).__name__}",
            "ticker": ticker,
        }
    if data is None:
        return {
            "available": False,
            "error": f"Couldn't compute brief for {ticker}: no option chain available.",
            "ticker": ticker,
        }
    # Partial-brief detection — drives the warning banner.
    REQUIRED_SECTIONS = {"header", "em_range", "walls", "bias"}
    missing = REQUIRED_SECTIONS - set(data["available_sections"])
    partial_warning = None
    if missing:
        partial_warning = (
            f"Partial brief — sections unavailable: {', '.join(sorted(missing))}. "
            f"Underlying data may be incomplete."
        )
    return {
        "available": True,
        "error": None,
        "partial_warning": partial_warning,
        "ticker": ticker,
        "data": data,
        "computed_at_ct": datetime.now(timezone.utc)
            .astimezone(CHICAGO_TZ).strftime("%H:%M:%S CT"),
    }


# ─────────────────────────────────────────────────────────────────────
# All-35 refresh job orchestration
# ─────────────────────────────────────────────────────────────────────

def _redis():
    """Get the app's Redis client. Returns None if Redis is unavailable."""
    try:
        from app import _get_redis
        return _get_redis()
    except Exception as e:
        log.warning(f"em_data._redis(): {e}")
        return None


def _refresh_key(job_id: str) -> str:
    return f"{REFRESH_KEY_PREFIX}{job_id}"


def _existing_inflight_job(rc) -> Optional[str]:
    """Return job_id of an in-flight refresh job, or None.

    A job is "in flight" if its Redis key exists AND has no `finished_at`
    field. This is the idempotency check for concurrent refresh-all
    requests."""
    if rc is None:
        return None
    try:
        for key in rc.scan_iter(match=f"{REFRESH_KEY_PREFIX}*", count=50):
            key_str = key.decode() if isinstance(key, bytes) else key
            data = rc.hgetall(key_str)
            if not data:
                continue
            # Decode hash values (redis-py returns bytes)
            decoded = {(k.decode() if isinstance(k, bytes) else k):
                       (v.decode() if isinstance(v, bytes) else v)
                       for k, v in data.items()}
            if "finished_at" not in decoded:
                return key_str.split(":", 1)[1]  # strip prefix
    except Exception as e:
        log.warning(f"em_data._existing_inflight_job: {e}")
    return None


def start_refresh_all() -> Dict[str, Any]:
    """Start an all-35 refresh job. Idempotent — if a job is in flight,
    returns its job_id rather than starting a new one.

    Returns: {job_id, started_now: bool, total: int}
    """
    if _kill_switch_off():
        return {"job_id": None, "started_now": False, "total": 0,
                "error": "EM brief dashboard disabled."}
    rc = _redis()
    if rc is None:
        return {"job_id": None, "started_now": False, "total": 0,
                "error": "Redis unavailable."}
    existing = _existing_inflight_job(rc)
    if existing:
        return {"job_id": existing, "started_now": False, "total": 0}

    from oi_flow import FLOW_TICKERS
    tickers = list(FLOW_TICKERS)
    job_id = str(uuid.uuid4())
    key = _refresh_key(job_id)
    started_at = int(time.time() * 1000)

    try:
        rc.hset(key, mapping={
            "started_at": started_at,
            "total": len(tickers),
            "completed": 0,
            "errors": 0,
        })
        rc.expire(key, REFRESH_STATUS_TTL_SEC)
    except Exception as e:
        log.warning(f"em_data.start_refresh_all: redis init failed: {e}")
        return {"job_id": None, "started_now": False, "total": 0,
                "error": str(e)}

    def _run():
        # v11.7 (Patch M.4): refresh daemon. Serialized per-ticker with
        # explicit time.sleep(2.0) between calls — protects the global
        # Schwab rate limiter from competing with the live trading
        # path during market hours (QC fix #1).
        from app import _generate_silent_thesis
        for i, ticker in enumerate(tickers):
            try:
                _generate_silent_thesis(ticker)
            except Exception as e:
                log.warning(f"em refresh: {ticker} failed: {e}")
                try:
                    rc.hincrby(key, "errors", 1)
                except Exception:
                    pass
            try:
                rc.hincrby(key, "completed", 1)
            except Exception:
                pass
            # Don't sleep after the last ticker.
            if i < len(tickers) - 1:
                time.sleep(INTER_TICKER_SLEEP_SEC)
        try:
            rc.hset(key, "finished_at", int(time.time() * 1000))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name=f"em_refresh_{job_id[:8]}").start()
    return {"job_id": job_id, "started_now": True, "total": len(tickers)}


def get_refresh_progress(job_id: str) -> Dict[str, Any]:
    """Return the current status of a refresh job.

    Shape: {found: bool, started_at, total, completed, errors,
            finished_at, elapsed_seconds, slow_caption: bool}
    """
    rc = _redis()
    if rc is None:
        return {"found": False, "error": "Redis unavailable."}
    try:
        raw = rc.hgetall(_refresh_key(job_id))
    except Exception as e:
        return {"found": False, "error": str(e)}
    if not raw:
        return {"found": False}
    decoded = {(k.decode() if isinstance(k, bytes) else k):
               (v.decode() if isinstance(v, bytes) else v)
               for k, v in raw.items()}
    started_at = int(decoded.get("started_at", 0))
    finished_at = decoded.get("finished_at")
    finished_at_int = int(finished_at) if finished_at else None
    now_ms = int(time.time() * 1000)
    elapsed_seconds = (now_ms - started_at) // 1000 if started_at else 0
    return {
        "found": True,
        "started_at": started_at,
        "total": int(decoded.get("total", 0)),
        "completed": int(decoded.get("completed", 0)),
        "errors": int(decoded.get("errors", 0)),
        "finished_at": finished_at_int,
        "elapsed_seconds": elapsed_seconds,
        "slow_caption": elapsed_seconds > SLOW_CAPTION_THRESHOLD_SEC and finished_at_int is None,
    }
```

- [ ] **Step 2: AST-check**

```powershell
python -c "import ast; ast.parse(open('omega_dashboard/em_data.py').read()); print('AST OK')"
```

- [ ] **Step 3: Smoke import**

```powershell
python -c "from omega_dashboard.em_data import get_em_brief, start_refresh_all, get_refresh_progress, REFRESH_KEY_PREFIX, INTER_TICKER_SLEEP_SEC; print('import OK')"
```

- [ ] **Step 4: Commit**

```powershell
git add omega_dashboard/em_data.py
git commit -m "$(cat <<'EOF'
Patch M.4: Read-side data layer for the EM brief panel

omega_dashboard/em_data.py — wraps app._compute_em_brief_data
(extracted in M.1) and adds dashboard-specific shape:
  - get_em_brief(ticker, session=None): returns {available, error,
    partial_warning, ticker, data, computed_at_ct} for the panel
    template. Detects partial briefs from data.available_sections
    and surfaces a warning banner (QC fix #2).
  - start_refresh_all(): idempotent. If a job is in flight, returns
    its job_id rather than starting a new one. Spawns daemon thread
    that calls _generate_silent_thesis serially for FLOW_TICKERS
    with time.sleep(2.0) between tickers (QC fix #1 — protects the
    Schwab rate limiter from competing with the live trading path
    during market hours).
  - get_refresh_progress(job_id): returns counters + slow_caption
    flag when elapsed > 60s.

Constants:
  REFRESH_KEY_PREFIX = "em_refresh:"
  REFRESH_STATUS_TTL_SEC = 30 * 60
  INTER_TICKER_SLEEP_SEC = 2.0
  SLOW_CAPTION_THRESHOLD_SEC = 60

Kill switch: EM_BRIEF_DASHBOARD_ENABLED=false → all entry points
return friendly disabled responses.
EOF
)"
```

---

### Task M.5: Three new routes in `omega_dashboard/routes.py`

**Files:**
- Modify: `omega_dashboard/routes.py` — add three login-required routes after the existing `/alerts/<alert_id>` route (~line 380 area, after Patch H's routes)

- [ ] **Step 1: Add the three routes**

Append after the existing `/alerts/<alert_id>` route:

```python
# v11.7 (Patch M.5): EM brief routes for Market View. Three routes:
#   GET  /em/brief/<ticker>          — synchronous, returns JSON for the panel
#   POST /em/refresh                 — starts the all-35 refresh job
#   GET  /em/refresh/status/<job_id> — progress poll (every 2s from JS)
#
# All login-required. All read-only against the existing trading path
# except /em/refresh which writes ThesisContext via _generate_silent_thesis
# (same write path the periodic loop uses). EM_BRIEF_DASHBOARD_ENABLED
# kill switch handled inside em_data — routes return 410 when disabled.

import re as _re

_TICKER_RE = _re.compile(r"^[A-Z]{1,8}$")


@dashboard_bp.route("/em/brief/<string:ticker>", methods=["GET"])
@login_required
def em_brief(ticker):
    from . import em_data
    ticker_upper = (ticker or "").upper().strip()
    if not _TICKER_RE.match(ticker_upper):
        return jsonify({"available": False,
                        "error": f"Invalid ticker: {ticker}",
                        "ticker": ticker}), 400
    payload = em_data.get_em_brief(ticker_upper)
    if payload.get("error") and "disabled" in (payload.get("error") or ""):
        return jsonify(payload), 410
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard_bp.route("/em/refresh", methods=["POST"])
@login_required
def em_refresh():
    from . import em_data
    payload = em_data.start_refresh_all()
    if payload.get("error") and "disabled" in (payload.get("error") or ""):
        return jsonify(payload), 410
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dashboard_bp.route("/em/refresh/status/<string:job_id>", methods=["GET"])
@login_required
def em_refresh_status(job_id):
    from . import em_data
    # Reject anything that isn't a UUID-shaped string before hitting Redis.
    if not _re.match(r"^[0-9a-f-]{8,40}$", job_id, _re.IGNORECASE):
        return jsonify({"found": False, "error": "Invalid job_id"}), 400
    payload = em_data.get_refresh_progress(job_id)
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp
```

- [ ] **Step 2: AST-check**

```powershell
python -c "import ast; ast.parse(open('omega_dashboard/routes.py').read()); print('AST OK')"
```

- [ ] **Step 3: Verify routes register**

```powershell
python -c "
from flask import Flask
from omega_dashboard.routes import dashboard_bp
app = Flask(__name__)
app.config['SECRET_KEY']='t'
app.register_blueprint(dashboard_bp)
rules = sorted(str(r) for r in app.url_map.iter_rules() if '/em/' in str(r))
for r in rules: print(r)
print(f'{len(rules)} EM routes registered')"
```

Expected: 3 routes — `/em/brief/<string:ticker>`, `/em/refresh`, `/em/refresh/status/<string:job_id>`.

- [ ] **Step 4: Commit**

```powershell
git add omega_dashboard/routes.py
git commit -m "$(cat <<'EOF'
Patch M.5: Three new EM brief routes for Market View

  GET  /em/brief/<ticker>          — synchronous, panel JSON
  POST /em/refresh                 — start all-35 refresh
  GET  /em/refresh/status/<id>     — progress poll

All login_required. All read-only HTTP except /em/refresh which
spawns a daemon thread inside em_data.start_refresh_all (idempotent
— concurrent POSTs return the existing job_id).

Ticker validation: ^[A-Z]{1,8}$ regex before any DB/Schwab work.
job_id validation: hex+dash shape before any Redis hit.
Kill switch (EM_BRIEF_DASHBOARD_ENABLED=false) returns 410 Gone
from all three routes.
EOF
)"
```

---

### Task M.6: Anchored panel template

**Files:**
- Create: `omega_dashboard/templates/dashboard/_em_brief_panel.html`

This partial is rendered server-side ONLY for the initial empty-placeholder state. The actual brief content is built client-side by JS (added in M.7) so polling can swap content without round-tripping HTML.

- [ ] **Step 1: Create the placeholder partial**

```jinja
{# v11.7 (Patch M.6): EM brief panel partial.
   Renders ONLY the empty-placeholder state for initial page render.
   When a ticker is selected (per-card click, ticker input, URL ?em=),
   the JS in trading.html replaces this content via fetch() against
   /em/brief/<ticker>.

   States the JS toggles:
     - empty placeholder (this template's content; default state)
     - loading (spinner overlay)
     - populated (full structured brief)
     - error (friendly message + dismiss)
     - partial (warning banner + populated brief)
#}
<div class="em-brief-panel em-brief-panel-empty" id="em-brief-panel">
  <div class="em-brief-empty-message">
    Click any card or type a ticker above to see the dealer EM brief.
  </div>
</div>
```

- [ ] **Step 2: Smoke-include the partial in trading.html (just to verify it loads)**

This is a temp step — M.7 wires the panel for real. For now, just append the include at the bottom of the existing trading.html `{% block content %}` so we can confirm the partial renders without a Jinja error.

```jinja
{% include 'dashboard/_em_brief_panel.html' %}
```

- [ ] **Step 3: Smoke-render**

```powershell
python -c "
from flask import Flask
from omega_dashboard.routes import dashboard_bp
app = Flask(__name__)
app.config['SECRET_KEY']='t'
app.register_blueprint(dashboard_bp)
client = app.test_client()
with client.session_transaction() as sess:
    sess['auth'] = True; sess['account'] = 'combined'
r = client.get('/trading')
print('status', r.status_code)
print('panel present:', b'em-brief-panel' in r.data)
print('placeholder present:', b'em-brief-empty-message' in r.data)
"
```

Expected: 200, both markers True.

- [ ] **Step 4: Revert the temp include (M.7 wires it properly)**

Remove the temp `{% include %}` from trading.html — M.7's diff will add it back in the right place.

- [ ] **Step 5: Commit**

```powershell
git add omega_dashboard/templates/dashboard/_em_brief_panel.html
git commit -m "$(cat <<'EOF'
Patch M.6: EM brief panel placeholder template

Server-side rendering of the empty-placeholder state ONLY.
JS in trading.html (added in M.7) handles the populated/loading/
error/partial states via fetch + innerHTML swap — keeps polling
client-side without round-tripping HTML.
EOF
)"
```

---

### Task M.7: Page-level wiring — refresh button, ticker input, panel container, whole-card click target, JS

**Files:**
- Modify: `omega_dashboard/templates/dashboard/trading.html` — add header strip (refresh + input + status), include the panel partial, wrap card grid in click-handler container, add inline JS for the three entry paths
- Modify: `omega_dashboard/templates/dashboard/_trading_card.html` — wrap the existing card body in a clickable surface (whole card is the click target — QC fix #6)
- Modify: `omega_dashboard/static/omega.css` — append all the panel + button + input + animation styles

This is the largest task in Phase 2 — three files change together for the same surface.

- [ ] **Step 1: Modify `_trading_card.html` — make whole card a click target**

Find the existing `<div class="tcard ...">` wrapper. Add `data-ticker="{{ card.ticker }}"` and the click handler attribute. The card itself becomes the affordance — no separate button.

```jinja
{# v11.7 (Patch M.7): whole card is the EM brief click target.
   No corner button — matches alerts-feed pattern from Patch H.
   The .tcard-clickable class is what the JS event delegate listens for. #}
<div class="tcard tcard-clickable stripe-{{ card.thesis.stripe_class }}"
     data-ticker="{{ card.ticker }}"
     role="button"
     tabindex="0"
     aria-label="Show EM brief for {{ card.ticker }}">
  {# ... rest of the existing card content unchanged ... #}
</div>
```

- [ ] **Step 2: Modify `trading.html` — add header strip + panel container**

Find the existing `<div class="trading-cards-section ...">` opening. Insert above it (between the tagline section and the cards section):

```jinja
{# v11.7 (Patch M.7): EM brief header strip + anchored panel.
   Three entry paths populate the panel: whole-card click,
   ticker input + Enter, URL ?em=TICKER on page load. #}
<div class="em-brief-header-strip">
  <button id="em-refresh-all-btn" class="em-refresh-all-btn" type="button">
    ↻ Refresh all 35
  </button>
  <input id="em-ticker-input"
         class="em-ticker-input"
         type="text"
         placeholder="Type ticker (e.g. HOOD) and press Enter…"
         autocomplete="off"
         spellcheck="false"
         maxlength="8"
         aria-label="Look up EM brief for any ticker" />
  <span id="em-refresh-status" class="em-refresh-status" role="status"></span>
</div>

{% include 'dashboard/_em_brief_panel.html' %}
```

- [ ] **Step 3: Add the JS block to `trading.html`**

Find the existing `<script>` block at the bottom of trading.html. Append a NEW script block AFTER the existing one (don't merge — keep separation between the polling-cards script and the EM brief script):

```jinja
<script>
(function () {
  'use strict';
  // v11.7 (Patch M.7): EM brief panel + refresh-all wiring.
  // Three entry paths populate the same panel; last-write-wins via
  // currentRequestId to ignore stale responses.

  const POLL_INTERVAL_MS = 2000;  // refresh-status poll cadence
  const PANEL_EL = document.getElementById('em-brief-panel');
  const REFRESH_BTN = document.getElementById('em-refresh-all-btn');
  const TICKER_INPUT = document.getElementById('em-ticker-input');
  const STATUS_EL = document.getElementById('em-refresh-status');
  const GRID = document.getElementById('trading-cards-grid');

  let currentRequestId = 0;     // last-write-wins guard
  let activeRefreshJob = null;  // job_id of in-flight refresh-all

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function setUrl(ticker) {
    const url = new URL(window.location.href);
    if (ticker) url.searchParams.set('em', ticker);
    else url.searchParams.delete('em');
    window.history.replaceState({}, '', url.toString());
  }

  // ── EM brief: render functions ─────────────────────────────
  function renderEmpty() {
    PANEL_EL.className = 'em-brief-panel em-brief-panel-empty';
    PANEL_EL.innerHTML = '<div class="em-brief-empty-message">Click any card or type a ticker above to see the dealer EM brief.</div>';
  }

  function renderLoading(ticker, refreshInFlight) {
    const caption = refreshInFlight
      ? 'Computing brief for ' + escapeHtml(ticker) + '… (refresh-all in progress, may be slower)'
      : 'Computing brief for ' + escapeHtml(ticker) + '…';
    // Don't wipe the existing content — overlay a spinner.
    let overlay = PANEL_EL.querySelector('.em-brief-loading');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'em-brief-loading';
      PANEL_EL.appendChild(overlay);
    }
    overlay.innerHTML = '<div class="em-brief-spinner"></div><div class="em-brief-loading-caption">' + caption + '</div>';
    PANEL_EL.classList.add('em-brief-panel-loading');
  }

  function renderError(ticker, message) {
    PANEL_EL.className = 'em-brief-panel em-brief-panel-error';
    PANEL_EL.innerHTML =
      '<div class="em-brief-error-icon">⚠</div>' +
      '<div class="em-brief-error-title">' + escapeHtml(ticker) + '</div>' +
      '<div class="em-brief-error-message">' + escapeHtml(message) + '</div>' +
      '<button class="em-brief-dismiss" type="button" aria-label="Dismiss">×</button>';
  }

  function renderBrief(payload) {
    // payload: { available, error, partial_warning, ticker, data,
    //            computed_at_ct }
    const d = payload.data;
    const t = payload.ticker;
    const warning = payload.partial_warning
      ? '<div class="em-brief-partial-warning">⚠ ' + escapeHtml(payload.partial_warning) + '</div>'
      : '';
    // Header
    const verdict = (d.bias && d.bias.verdict) ? d.bias.verdict : '';
    const verdictClass = verdictToClass(verdict);
    const headerHtml =
      '<div class="em-brief-section em-brief-header em-brief-engine-' + verdictClass + '">' +
        '<div class="em-brief-engine-label">' + escapeHtml(d.session_emoji || '') + ' ' + escapeHtml(t) + ' — DEALER EM BRIEF</div>' +
        '<div class="em-brief-meta">Spot $' + Number(d.spot).toFixed(2) +
          ' · IV ' + Number(d.iv * 100).toFixed(1) + '%' +
          ' · Exp ' + escapeHtml(d.expiration || '') +
          ' · ' + escapeHtml(d.session_label || '') + '</div>' +
      '</div>';
    // Bias
    const biasHtml = d.bias
      ? '<div class="em-brief-section">' +
          '<div class="em-brief-section-title">Bias</div>' +
          '<div class="em-brief-bias-pill em-brief-bias-' + biasToClass(d.bias.direction) + '">' +
            escapeHtml(d.bias.direction) +
            ' (score ' + d.bias.score + '/' + d.bias.max_score + ')' +
          '</div>' +
        '</div>' : '';
    // EM Range + Walls — 2-column grid
    const levelsHtml = renderLevelsGrid(d);
    // Dealer flow
    const dealerHtml = renderDealerFlow(d);
    // Vol / regime / posture
    const volHtml = renderVolBlock(d);
    // Action guide (if available)
    const actionHtml = renderActionGuide(d);
    // Footer
    const footer = '<div class="em-brief-footer">computed at ' + escapeHtml(payload.computed_at_ct) + ' — Not financial advice</div>';

    PANEL_EL.className = 'em-brief-panel em-brief-panel-populated';
    PANEL_EL.innerHTML =
      '<div class="em-brief-panel-header">' +
        '<div class="em-brief-panel-ticker">' + escapeHtml(t) + '</div>' +
        '<button class="em-brief-refresh" type="button" title="Refresh this brief" aria-label="Refresh">↻</button>' +
        '<button class="em-brief-dismiss" type="button" title="Close (Esc)" aria-label="Dismiss">×</button>' +
      '</div>' +
      warning +
      headerHtml + biasHtml + levelsHtml + dealerHtml + volHtml + actionHtml + footer;
  }

  function verdictToClass(v) {
    const u = (v || '').toUpperCase();
    if (u.indexOf('NO TRADE') >= 0) return 'no-trade';
    if (u.indexOf('STRONG') >= 0)   return 'strong';
    return 'neutral';
  }
  function biasToClass(dir) {
    const u = (dir || '').toUpperCase();
    if (u.indexOf('BULL') >= 0) return 'bull';
    if (u.indexOf('BEAR') >= 0) return 'bear';
    return 'neutral';
  }

  function renderLevelsGrid(d) {
    if (!d.em && !d.walls) return '';
    const rows = [];
    if (d.em) {
      rows.push(['1σ Range',
        '$' + Number(d.em.bear_1sd).toFixed(2) + ' – $' + Number(d.em.bull_1sd).toFixed(2)]);
    }
    if (d.eng && d.eng.flip_price !== null && d.eng.flip_price !== undefined) {
      rows.push(['Gamma Flip', '$' + Number(d.eng.flip_price).toFixed(2)]);
    }
    if (d.walls) {
      if (d.walls.call_wall !== undefined) rows.push(['Call Wall', '$' + Number(d.walls.call_wall).toFixed(0)]);
      if (d.walls.put_wall !== undefined)  rows.push(['Put Wall',  '$' + Number(d.walls.put_wall).toFixed(0)]);
      if (d.walls.gamma_wall !== undefined) rows.push(['Gamma Wall', '$' + Number(d.walls.gamma_wall).toFixed(0)]);
    }
    if (!rows.length) return '';
    const body = rows.map(r =>
      '<div class="em-brief-label">' + escapeHtml(r[0]) + '</div>' +
      '<div class="em-brief-value">' + escapeHtml(r[1]) + '</div>'
    ).join('');
    return '<div class="em-brief-section"><div class="em-brief-section-title">Levels</div>' +
           '<div class="em-brief-levels-grid">' + body + '</div></div>';
  }

  function renderDealerFlow(d) {
    if (!d.eng) {
      return '<div class="em-brief-section"><div class="em-brief-section-title">Dealer Flow</div>' +
             '<div class="em-brief-na">n/a</div></div>';
    }
    const gex = d.eng.gex || 0;
    const gexSign = gex >= 0 ? '+$' + gex.toFixed(1) + 'M' : '-$' + Math.abs(gex).toFixed(1) + 'M';
    const gexMode = gex >= 0 ? 'SUPPRESSING moves' : 'AMPLIFYING moves';
    return '<div class="em-brief-section">' +
             '<div class="em-brief-section-title">Dealer Flow</div>' +
             '<div class="em-brief-dealer-row">GEX: <span class="' + (gex >= 0 ? 'positive' : 'negative') + '">' + escapeHtml(gexSign) + '</span> — ' + escapeHtml(gexMode) + '</div>' +
             '<div class="em-brief-dealer-row">DEX: ' + (d.eng.dex || 0).toFixed(1) + 'M</div>' +
             '<div class="em-brief-dealer-row">Vanna: ' + (d.eng.vanna || 0).toFixed(1) + 'M · Charm: ' + (d.eng.charm || 0).toFixed(1) + 'M</div>' +
           '</div>';
  }

  function renderVolBlock(d) {
    const vix = d.vix || {};
    const reg = (d.vol_regime && d.vol_regime.regime) || 'unknown';
    return '<div class="em-brief-section">' +
             '<div class="em-brief-section-title">Volatility Regime</div>' +
             '<div class="em-brief-vol-row">Regime: ' + escapeHtml(reg) +
               ' · VIX ' + (vix.vix || 'n/a') +
               ' · Term: ' + escapeHtml(vix.term || 'n/a') + '</div>' +
           '</div>';
  }

  function renderActionGuide(d) {
    // Action guide is built later in _post_em_card; for V1 the dashboard
    // shows the bias verdict + signals breakdown rather than the full
    // plain-English action guide. V1.1 can add the full action guide.
    if (!d.bias || !d.bias.signals || !d.bias.signals.length) return '';
    const items = d.bias.signals.map(s => {
      const arrow = (s[0] || '').replace(/[<>&]/g, '');
      const text = escapeHtml(s[1] || '');
      return '<li>' + escapeHtml(arrow) + ' ' + text + '</li>';
    }).join('');
    return '<div class="em-brief-section">' +
             '<div class="em-brief-section-title">Signal Breakdown (' + d.bias.n_signals + '/14)</div>' +
             '<ul class="em-brief-signals">' + items + '</ul>' +
           '</div>';
  }

  // ── EM brief: fetch + load ─────────────────────────────────
  function loadEmBrief(ticker) {
    if (!ticker) return;
    const t = ticker.toUpperCase().trim();
    setUrl(t);
    const requestId = ++currentRequestId;
    renderLoading(t, activeRefreshJob !== null);
    fetch('/em/brief/' + encodeURIComponent(t), { credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : r.json().then(j => Promise.reject(j)))
      .then(payload => {
        if (requestId !== currentRequestId) return;  // stale
        if (!payload.available) {
          renderError(t, payload.error || 'Couldn\'t compute brief.');
          return;
        }
        renderBrief(payload);
      })
      .catch(err => {
        if (requestId !== currentRequestId) return;
        const msg = (err && err.error) ? err.error : 'Network error fetching brief.';
        renderError(t, msg);
      });
  }

  function dismiss() {
    setUrl(null);
    renderEmpty();
  }

  // ── Refresh-all: button click ──────────────────────────────
  function triggerRefreshAll() {
    if (REFRESH_BTN.disabled) return;
    REFRESH_BTN.disabled = true;
    REFRESH_BTN.textContent = 'Starting…';
    fetch('/em/refresh', { method: 'POST', credentials: 'same-origin' })
      .then(r => r.json())
      .then(payload => {
        if (!payload.job_id) {
          STATUS_EL.textContent = payload.error || 'Refresh failed to start.';
          REFRESH_BTN.textContent = '↻ Refresh all 35';
          REFRESH_BTN.disabled = false;
          return;
        }
        activeRefreshJob = payload.job_id;
        pollRefreshStatus();
      })
      .catch(() => {
        REFRESH_BTN.textContent = '↻ Refresh all 35';
        REFRESH_BTN.disabled = false;
      });
  }

  function pollRefreshStatus() {
    if (!activeRefreshJob) return;
    fetch('/em/refresh/status/' + encodeURIComponent(activeRefreshJob),
          { credentials: 'same-origin' })
      .then(r => r.json())
      .then(p => {
        if (!p.found) {
          activeRefreshJob = null;
          REFRESH_BTN.textContent = '↻ Refresh all 35';
          REFRESH_BTN.disabled = false;
          return;
        }
        if (p.finished_at) {
          const errs = p.errors > 0 ? ' (' + p.errors + ' errors)' : '';
          REFRESH_BTN.textContent = 'Refreshed' + errs;
          REFRESH_BTN.disabled = false;
          activeRefreshJob = null;
          // Revert button text after 30s
          setTimeout(() => {
            if (!activeRefreshJob) REFRESH_BTN.textContent = '↻ Refresh all 35';
          }, 30000);
          return;
        }
        const cap = p.slow_caption
          ? ' (this can take several minutes during market hours)' : '';
        REFRESH_BTN.textContent = 'Refreshing ' + p.completed + '/' + p.total + '…' + cap;
        setTimeout(pollRefreshStatus, POLL_INTERVAL_MS);
      })
      .catch(() => {
        // Network blip — retry next interval
        setTimeout(pollRefreshStatus, POLL_INTERVAL_MS);
      });
  }

  // ── Wire up entry paths ────────────────────────────────────
  // Path 1: whole-card click. Event delegation on the cards grid.
  if (GRID) {
    GRID.addEventListener('click', function (e) {
      const card = e.target.closest('.tcard-clickable');
      if (!card) return;
      // Don't hijack clicks inside interactive elements within the card
      // (none expected today, but defensive).
      if (e.target.closest('a, button, input, textarea, select')) return;
      const ticker = card.getAttribute('data-ticker');
      if (ticker) loadEmBrief(ticker);
    });
    // Keyboard accessibility — Enter/Space on focused card.
    GRID.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const card = e.target.closest('.tcard-clickable');
      if (!card) return;
      e.preventDefault();
      const ticker = card.getAttribute('data-ticker');
      if (ticker) loadEmBrief(ticker);
    });
  }

  // Path 2: ticker input + Enter
  if (TICKER_INPUT) {
    TICKER_INPUT.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      const ticker = TICKER_INPUT.value.toUpperCase().trim();
      if (ticker) loadEmBrief(ticker);
    });
  }

  // Path 3: URL deep-link on page load
  const initialTicker = (new URL(window.location.href)).searchParams.get('em');
  if (initialTicker) {
    loadEmBrief(initialTicker);
  }

  // Refresh-all button
  if (REFRESH_BTN) {
    REFRESH_BTN.addEventListener('click', triggerRefreshAll);
  }

  // Panel buttons (delegated — they get added after fetch)
  if (PANEL_EL) {
    PANEL_EL.addEventListener('click', function (e) {
      if (e.target.closest('.em-brief-dismiss')) {
        dismiss();
      } else if (e.target.closest('.em-brief-refresh')) {
        const ticker = (new URL(window.location.href)).searchParams.get('em');
        if (ticker) loadEmBrief(ticker);
      }
    });
  }

  // Esc key dismisses panel
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && PANEL_EL.classList.contains('em-brief-panel-populated')) {
      dismiss();
    }
  });

  // On page load: check if a refresh is already in flight (e.g. user
  // clicked refresh, navigated away, came back). Scan Redis side via
  // the route — if there's an in-flight job, resume polling.
  // V1: skip this; user re-clicks Refresh which is idempotent and
  // returns the existing job_id.
})();
</script>
```

- [ ] **Step 4: Append CSS to `omega.css`**

Append at the end of `omega.css`:

```css
/* ════════════════════════════════════════════════════════
   v11.7 (Patch M.7): EM brief panel + header strip styles.
   Sticky panel with opaque background + z-index ensures clean
   overlay during the cards grid 5s repaints (QC fix #4).
   ════════════════════════════════════════════════════════ */

.em-brief-header-strip {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--brass-bright);
  margin: 0 24px 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}
.em-refresh-all-btn {
  background: rgba(213, 176, 107, 0.10);
  color: var(--brass-bright);
  border: 1px solid var(--brass-deep);
  padding: 6px 14px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}
.em-refresh-all-btn:hover:not(:disabled) {
  background: rgba(213, 176, 107, 0.20);
}
.em-refresh-all-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.em-ticker-input {
  background: var(--bg-void);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 6px 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  width: 320px;
  min-width: 200px;
}
.em-ticker-input:focus {
  outline: none;
  border-color: var(--brass-bright);
}
.em-refresh-status {
  color: var(--text-dim);
  font-size: 11px;
  margin-left: auto;
}

/* Panel — sticky, overlays grid cleanly during repaints */
.em-brief-panel {
  position: sticky;
  top: 0;
  z-index: 5;
  background: var(--bg-panel);   /* opaque — prevents flicker */
  border: 1px solid var(--border);
  border-left: 4px solid var(--brass-bright);
  margin: 0 24px 12px;
  font-family: 'Outfit', sans-serif;
  transition: max-height 0.2s ease, opacity 0.15s ease;
  max-height: 80vh;
  overflow-y: auto;
}
.em-brief-panel-empty {
  padding: 14px 18px;
  border-left-color: var(--brass-deep);
  border-style: dashed;
}
.em-brief-empty-message {
  color: var(--brass-deep);
  font-size: 12px;
  font-family: 'JetBrains Mono', monospace;
  text-align: center;
}

.em-brief-panel-loading { position: relative; }
.em-brief-loading {
  position: absolute; inset: 0;
  background: rgba(13, 15, 16, 0.72);
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  z-index: 10;
}
.em-brief-spinner {
  width: 28px; height: 28px;
  border: 3px solid var(--border);
  border-top-color: var(--brass-bright);
  border-radius: 50%;
  animation: emBriefSpin 0.7s linear infinite;
  margin-bottom: 12px;
}
@keyframes emBriefSpin { to { transform: rotate(360deg); } }
.em-brief-loading-caption {
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}

.em-brief-panel-error {
  padding: 24px 18px;
  text-align: center;
  border-left-color: var(--negative);
  position: relative;
}
.em-brief-error-icon { font-size: 24px; color: var(--negative-soft); }
.em-brief-error-title {
  font-family: 'Cinzel', serif;
  font-size: 14px;
  letter-spacing: 3px;
  color: var(--text);
  margin: 8px 0 4px;
}
.em-brief-error-message {
  color: var(--text-dim);
  font-size: 12px;
}

.em-brief-panel-populated {
  padding: 16px 22px;
}
.em-brief-panel-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}
.em-brief-panel-ticker {
  font-family: 'JetBrains Mono', monospace;
  font-size: 18px;
  color: var(--text);
  font-weight: 500;
}
.em-brief-refresh, .em-brief-dismiss {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--brass-deep);
  padding: 4px 10px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  cursor: pointer;
}
.em-brief-refresh:hover, .em-brief-dismiss:hover {
  color: var(--brass-bright);
  border-color: var(--brass-deep);
}
.em-brief-dismiss { margin-left: auto; }
.em-brief-refresh { margin-left: auto; }
.em-brief-dismiss + .em-brief-refresh,
.em-brief-refresh + .em-brief-dismiss {
  margin-left: 0;
}

.em-brief-partial-warning {
  background: rgba(213, 176, 107, 0.08);
  color: var(--brass-bright);
  border: 1px solid var(--brass-deep);
  padding: 8px 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  margin-bottom: 10px;
}

.em-brief-section {
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}
.em-brief-section:last-child { border-bottom: none; }
.em-brief-section-title {
  font-family: 'Cinzel', serif;
  font-size: 10px;
  letter-spacing: 4px;
  color: var(--brass-deep);
  text-transform: uppercase;
  margin-bottom: 8px;
}

.em-brief-engine-no-trade { border-left-color: var(--negative); }
.em-brief-engine-strong   { border-left-color: var(--positive); }
.em-brief-engine-neutral  { border-left-color: var(--brass-bright); }

.em-brief-engine-label {
  font-family: 'Outfit', sans-serif;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 1px;
  color: var(--text);
}
.em-brief-meta {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 4px;
}

.em-brief-bias-pill {
  display: inline-block;
  padding: 4px 10px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  border: 1px solid var(--border);
}
.em-brief-bias-bull { color: var(--positive-bright); border-color: var(--positive); }
.em-brief-bias-bear { color: var(--negative-soft); border-color: var(--negative); }
.em-brief-bias-neutral { color: var(--brass-bright); border-color: var(--brass-deep); }

.em-brief-levels-grid {
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: 4px 16px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}
.em-brief-label { color: var(--text-dim); }
.em-brief-value { color: var(--text); }

.em-brief-dealer-row, .em-brief-vol-row {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--text-muted);
  padding: 2px 0;
}
.em-brief-dealer-row .positive { color: var(--positive-bright); }
.em-brief-dealer-row .negative { color: var(--negative-soft); }

.em-brief-signals {
  list-style: none;
  padding: 0;
  margin: 0;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--text-muted);
}
.em-brief-signals li { padding: 2px 0; }

.em-brief-na {
  color: var(--text-dim);
  font-style: italic;
  font-size: 11px;
}

.em-brief-footer {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-dim);
  text-align: center;
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
}

/* Make tcard click-target visually obvious */
.tcard-clickable {
  cursor: pointer;
}
.tcard-clickable:focus-visible {
  outline: 2px solid var(--brass-bright);
  outline-offset: -2px;
}
```

- [ ] **Step 5: Smoke-render**

```powershell
python -c "
from flask import Flask
from omega_dashboard.routes import dashboard_bp
app = Flask(__name__)
app.config['SECRET_KEY']='t'
app.register_blueprint(dashboard_bp)
client = app.test_client()
with client.session_transaction() as sess:
    sess['auth'] = True; sess['account'] = 'combined'
r = client.get('/trading')
print('status', r.status_code)
b = r.data.decode('utf-8', errors='replace')
checks = [
  ('em-brief-header-strip', 'header strip present'),
  ('em-refresh-all-btn',    'refresh button present'),
  ('em-ticker-input',       'ticker input present'),
  ('em-brief-panel',        'panel container present'),
  ('em-brief-empty-message','panel placeholder text present'),
  ('tcard-clickable',       'cards have click-target class'),
  ('loadEmBrief',           'JS loadEmBrief function present'),
  ('triggerRefreshAll',     'JS triggerRefreshAll function present'),
]
for needle, label in checks:
    print(f'  [{\"OK\" if needle in b else \"FAIL\"}] {label}')"
```

Expected: status 200, all 8 checks OK. Any FAIL means the template/CSS edit didn't land — fix and re-run.

- [ ] **Step 6: Commit**

```powershell
git add omega_dashboard/templates/dashboard/trading.html `
        omega_dashboard/templates/dashboard/_trading_card.html `
        omega_dashboard/static/omega.css
git commit -m "$(cat <<'EOF'
Patch M.7: Page-level EM brief wiring (refresh + input + panel + JS)

trading.html:
  - Header strip with refresh-all button + ticker input + status text
  - Anchored EM brief panel container (renders empty placeholder
    initially; JS replaces with populated/loading/error/partial states)
  - 200+ lines of inline JS: loadEmBrief, triggerRefreshAll,
    pollRefreshStatus, dismiss, render functions for each panel state
  - Three entry paths wired: whole-card click (event delegation on
    grid), ticker input + Enter, URL ?em= deep-link on page load
  - Esc key dismisses populated panel; URL state via
    history.replaceState (no full reload)
  - Last-write-wins via currentRequestId guard

_trading_card.html:
  - Whole card is now the click target (tcard-clickable + role=button
    + tabindex=0 + aria-label). No corner button — matches
    alerts-feed pattern from Patch H (QC fix #6).
  - Keyboard accessibility: Enter/Space on focused card opens panel.

omega.css:
  - .em-brief-panel: position:sticky + opaque background + z-index:5
    for clean overlay during card grid 5s repaints (QC fix #4)
  - States: empty / loading / error / populated / partial-warning
  - Sections: header, bias pill, levels grid, dealer flow, vol regime,
    signal breakdown, footer
  - Refresh-all button + ticker input + status text styles
  - Loading spinner @keyframes
  - tcard-clickable cursor + focus-visible outline

Smoke render: status 200, all 8 markers present in served HTML.
EOF
)"
```

---

### Task M.8: Hermetic tests + CLAUDE.md + final regression sweep + push

**Files:**
- Create: `test_em_data.py` — hermetic tests for `omega_dashboard/em_data.py`
- Modify: `CLAUDE.md` — append Patch M entry under "What's done as of last session"

- [ ] **Step 1: Write `test_em_data.py`**

```python
"""Tests for omega_dashboard.em_data.

# v11.7 (Patch M.8): hermetic — no network, no Schwab, no Telegram.
# Mocks app._compute_em_brief_data so each test owns its own scenario.
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock


def _kill_switch_env(value):
    """Helper to set/unset the EM_BRIEF_DASHBOARD_ENABLED env var."""
    if value is None:
        os.environ.pop("EM_BRIEF_DASHBOARD_ENABLED", None)
    else:
        os.environ["EM_BRIEF_DASHBOARD_ENABLED"] = value


def test_get_em_brief_returns_disabled_when_kill_switch_set():
    _kill_switch_env("false")
    try:
        from omega_dashboard import em_data
        result = em_data.get_em_brief("SPY")
        assert result["available"] is False
        assert "disabled" in (result["error"] or "").lower()
    finally:
        _kill_switch_env(None)


def test_get_em_brief_returns_data_when_compute_succeeds():
    _kill_switch_env(None)
    fake_data = {
        "ticker": "SPY", "session_resolved": "manual",
        "expiration": "2026-05-13", "target_date_str": "2026-05-13",
        "hours_for_em": 6.5, "session_emoji": "🌆",
        "session_label": "Test", "horizon_note": "test",
        "iv": 0.18, "spot": 588.50,
        "eng": {"gex": 12.4, "dex": -3.1, "vanna": 1.2, "charm": -0.8,
                "flip_price": 585.0, "regime": {}},
        "walls": {"call_wall": 595, "put_wall": 580, "gamma_wall": 590},
        "skew": None, "pcr": None, "vix": {"vix": 18.5},
        "v4_result": {}, "vol_regime": {"regime": "NORMAL"},
        "em": {"bull_1sd": 590.0, "bear_1sd": 585.0,
               "bull_2sd": 595.0, "bear_2sd": 580.0,
               "range_1sd": 5.0, "range_2sd": 15.0},
        "bias": {"direction": "SLIGHT BULLISH", "score": 1, "max_score": 14,
                 "verdict": "NEUTRAL — wait", "signals": [],
                 "up_count": 1, "down_count": 0, "neu_count": 0,
                 "na_count": 13, "n_signals": 1, "strength": ""},
        "cagf": None, "dte_rec": None,
        "available_sections": ["header", "em_range", "walls", "bias",
                               "dealer_flow", "vol_regime"],
    }
    with patch("app._compute_em_brief_data", return_value=fake_data):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("SPY")
    assert result["available"] is True
    assert result["error"] is None
    assert result["partial_warning"] is None  # all required sections present
    assert result["data"]["ticker"] == "SPY"
    assert result["computed_at_ct"]  # CT timestamp string


def test_get_em_brief_renders_partial_warning_when_sections_missing():
    """When available_sections doesn't include all REQUIRED_SECTIONS,
    partial_warning should be set (drives the warning banner — QC fix #2)."""
    fake_data = {
        "ticker": "FAKE", "session_resolved": "manual",
        "iv": 0.20, "spot": 100.0, "expiration": "2026-05-15",
        "target_date_str": "2026-05-15", "hours_for_em": 1.0,
        "session_emoji": "🔔", "session_label": "Test", "horizon_note": "x",
        "eng": None, "walls": None, "skew": None, "pcr": None,
        "vix": {"vix": 20}, "v4_result": {}, "vol_regime": {"regime": "NORMAL"},
        "em": {"bull_1sd": 102, "bear_1sd": 98, "bull_2sd": 104, "bear_2sd": 96,
               "range_1sd": 4, "range_2sd": 8},
        "bias": None, "cagf": None, "dte_rec": None,
        "available_sections": ["header", "em_range"],   # missing walls + bias
    }
    with patch("app._compute_em_brief_data", return_value=fake_data):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("FAKE")
    assert result["available"] is True
    assert result["partial_warning"] is not None
    assert "walls" in result["partial_warning"]
    assert "bias" in result["partial_warning"]


def test_get_em_brief_returns_unavailable_when_compute_returns_none():
    with patch("app._compute_em_brief_data", return_value=None):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("ZZZZ")
    assert result["available"] is False
    assert "no option chain" in (result["error"] or "").lower()


def test_get_em_brief_swallows_compute_exceptions():
    with patch("app._compute_em_brief_data",
               side_effect=RuntimeError("schwab boom")):
        from omega_dashboard import em_data
        result = em_data.get_em_brief("SPY")
    assert result["available"] is False
    assert "RuntimeError" in (result["error"] or "")


def test_start_refresh_all_returns_disabled_when_kill_switch_set():
    _kill_switch_env("false")
    try:
        from omega_dashboard import em_data
        result = em_data.start_refresh_all()
        assert result["job_id"] is None
        assert "disabled" in (result["error"] or "").lower()
    finally:
        _kill_switch_env(None)


def test_start_refresh_all_returns_no_redis_when_unavailable():
    _kill_switch_env(None)
    with patch("omega_dashboard.em_data._redis", return_value=None):
        from omega_dashboard import em_data
        result = em_data.start_refresh_all()
    assert result["job_id"] is None
    assert "Redis" in (result["error"] or "")


def test_get_refresh_progress_returns_not_found_for_unknown_job():
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {}
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("00000000-0000-4000-a000-000000000000")
    assert result["found"] is False


def test_get_refresh_progress_decodes_redis_hash_correctly():
    """Redis-py returns bytes; the helper must decode and coerce ints."""
    started_ms = int(time.time() * 1000) - 30_000  # 30s ago
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {
        b"started_at": str(started_ms).encode(),
        b"total": b"35",
        b"completed": b"12",
        b"errors": b"1",
    }
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("a-job-id")
    assert result["found"] is True
    assert result["total"] == 35
    assert result["completed"] == 12
    assert result["errors"] == 1
    assert result["finished_at"] is None
    assert result["elapsed_seconds"] >= 29  # ~30s ago, allow 1s slack
    assert result["slow_caption"] is False  # only 30s elapsed


def test_get_refresh_progress_sets_slow_caption_after_60s():
    started_ms = int(time.time() * 1000) - 90_000  # 90s ago
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {
        b"started_at": str(started_ms).encode(),
        b"total": b"35", b"completed": b"22", b"errors": b"0",
    }
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("a-job-id")
    assert result["slow_caption"] is True


def test_get_refresh_progress_shows_finished_when_complete():
    started_ms = int(time.time() * 1000) - 100_000
    finished_ms = started_ms + 95_000
    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {
        b"started_at": str(started_ms).encode(),
        b"total": b"35", b"completed": b"35", b"errors": b"0",
        b"finished_at": str(finished_ms).encode(),
    }
    with patch("omega_dashboard.em_data._redis", return_value=fake_redis):
        from omega_dashboard import em_data
        result = em_data.get_refresh_progress("a-job-id")
    assert result["finished_at"] == finished_ms
    assert result["slow_caption"] is False  # finished, no caption


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except Exception as e:
            failures += 1
            import traceback
            print(f"FAIL: {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
```

- [ ] **Step 2: Run the new test suite**

```powershell
python -c "import ast; ast.parse(open('test_em_data.py').read()); print('AST OK')"
python test_em_data.py
```

Expected: AST OK + 11/11 passed.

- [ ] **Step 3: Run full canonical + recorder regression battery**

```powershell
$env:PYTHONIOENCODING='utf-8'
$suites = @(
  'test_canonical_gamma_flip', 'test_canonical_iv_state',
  'test_canonical_exposures', 'test_canonical_expiration',
  'test_canonical_technicals', 'test_bot_state',
  'test_bot_state_producer', 'test_db_migrate',
  'test_alert_recorder', 'test_alert_tracker_daemon',
  'test_outcome_computer_daemon', 'test_engine_versions',
  'test_recorder_queries', 'test_research_data_consumer',
  'test_alerts_data', 'test_em_brief_snapshot', 'test_em_data'
)
foreach ($s in $suites) {
  $r = & python "$s.py" 2>&1
  $last = ($r | Select-Object -Last 1)
  Write-Host "[$($LASTEXITCODE -eq 0 ? 'OK' : 'FAIL')] $s : $last"
}
```

Expected: all OK. If `test_em_brief_snapshot` fails, the M.2/M.3 refactor regressed — go back and fix.

- [ ] **Step 4: Append Patch M entry to CLAUDE.md**

In `CLAUDE.md`, find the "What's done as of last session" section. After the most recent entry (Patch H.8 / Patch B.bsp-fix), append:

```markdown
- Patch M (EM brief on Market View) — Surfaces the rich `/em` Telegram
  output (DEALER EM BRIEF + ACTION GUIDE) on the Market View dashboard
  tab. Two-phase delivery:

  **Phase 1 (M.0–M.3) — pure-extraction refactor.** New
  `_compute_em_brief_data(ticker, session=None)` helper at
  `app.py:12948` lifts the data layer out of `_post_em_card`'s 474
  lines. Both the Telegram path and the silent-thesis store consume
  it (refactored in M.2 + M.3). `session=None` auto-resolves from
  time-of-day matching the existing `_post_em_card` logic. Refactor
  is gated by `test_em_brief_snapshot.py` (M.0) which captures
  byte-identical pre-refactor output for 3 scenarios (spy/hood/thin)
  with deterministic mocks of `_get_0dte_iv`, `post_to_telegram`,
  `_estimate_liquidity`, `get_daily_candles`. Test must pass at every
  refactor commit; fixtures live under `tests/fixtures/em_brief_snapshots/`.

  **Phase 2 (M.4–M.8) — dashboard surface.** New
  `omega_dashboard/em_data.py` module wraps `_compute_em_brief_data`
  with dashboard-specific shape (CT timestamps, partial-brief warning,
  `available_sections` detection). New `start_refresh_all()` is
  idempotent — concurrent POSTs return the existing job_id rather
  than starting a duplicate batch. Daemon thread serializes per-ticker
  with `time.sleep(2.0)` between calls (`INTER_TICKER_SLEEP_SEC`)
  to protect the global Schwab rate limiter. After 60s elapsed the
  status surfaces a "(this can take several minutes during market
  hours)" caption.

  Three new login_required routes in `omega_dashboard/routes.py`:
  `GET /em/brief/<ticker>` (ticker validated `^[A-Z]{1,8}$`),
  `POST /em/refresh`, `GET /em/refresh/status/<job_id>`. All return
  410 Gone when `EM_BRIEF_DASHBOARD_ENABLED=false`.

  Anchored panel below the header strip (refresh button + ticker
  input + status text), above the cards grid. Sticky positioning +
  opaque `var(--bg-panel)` background + `z-index: 5` ensures clean
  overlay during the cards grid 5s repaints. Three entry paths
  populate the panel: whole-card click (entire trading card is the
  click target — matches alerts-feed pattern from Patch H, no corner
  button), ticker input + Enter, URL `?em=TICKER` deep-link.
  Last-write-wins via `currentRequestId` guard. Esc key dismisses;
  × button clears; URL strips the param via `history.replaceState`.
  No auto-scroll on populate — sticky positioning makes it
  unnecessary, and auto-scroll would disrupt mid-grid browsing.

  Display fidelity: structured HTML cards (header + bias pill +
  levels grid + dealer flow + vol regime + signal breakdown +
  footer) using existing omega.css tokens. NOT a raw `<pre>` text
  dump. Partial-brief states render `n/a` for missing sections plus
  a subtle warning banner; full-error state only when nothing
  computes (e.g., truly unknown ticker).

  Hermetic tests in `test_em_data.py` (11 tests): kill-switch behavior,
  partial-brief detection, refresh-all idempotency + Redis state
  decoding + slow-caption threshold + finished-state. Plus
  `test_em_brief_snapshot.py` (1 mega-test, 3 scenarios) for the
  refactor gate.

  All env gates default ON for the dashboard surface. `/em` Telegram
  command behavior unchanged — refactor is provably text-identical.
  Ship sequencing: shipped Wed 2026-05-13+ after H.8 stabilized.
```

- [ ] **Step 5: Commit + push**

```powershell
git add test_em_data.py CLAUDE.md
git commit -m "$(cat <<'EOF'
Patch M.8: Hermetic tests + CLAUDE.md + final regression

test_em_data.py: 11 hermetic tests for omega_dashboard/em_data.py
  - Kill switch (EM_BRIEF_DASHBOARD_ENABLED=false) returns disabled
  - get_em_brief: success path, partial-brief detection (sections
    missing → warning banner), no-data path, exception swallowing
  - start_refresh_all: kill-switch guard, redis-unavailable guard
  - get_refresh_progress: not-found, decoded redis hash, slow-caption
    threshold, finished state

Mocks app._compute_em_brief_data + omega_dashboard.em_data._redis;
no Schwab, no Telegram, no live Redis.

CLAUDE.md: Patch M entry under "What's done as of last session".

Full canonical + recorder regression battery: 17/17 suites green
(15 from prior + test_em_brief_snapshot + test_em_data).
EOF
)"
git push origin main
```

---

## Risk & Rollback

**Risks (already addressed in spec; recap):**

1. **Refactor regression.** `test_em_brief_snapshot.py` (M.0) is the gate — must pass at every refactor commit. If it fails, the refactor diverged from current Telegram output; fix before proceeding.

2. **Schwab rate limit during refresh-all.** Mitigated by daemon serialization + `time.sleep(2.0)` between tickers + UI surfacing the slowdown after 60s.

3. **Three failure modes for single-ticker view.** Mitigated by `available_sections` field on the response; partial briefs render with `n/a` plus a warning banner; full-error state only for case 1.

4. **Concurrent click during refresh-all.** Documented as unguarded but bounded; loading caption surfaces the slowdown.

5. **Concurrent refresh-all requests.** `start_refresh_all` is idempotent — checks for in-flight job in Redis, returns existing job_id rather than starting a new batch.

**Rollback (kill-switch):**

```bash
# In Render env:
EM_BRIEF_DASHBOARD_ENABLED=false
# Redeploy. Within 60s:
#  - GET /em/brief/<ticker>           → 410
#  - POST /em/refresh                 → 410
#  - GET /em/refresh/status/<id>      → still works (read-only on Redis)
#  - Trading page renders unchanged (panel + button + input still in
#    DOM but route 410s; consider also templating-hiding them in V1.1).
```

**Full revert:**

```bash
# Revert in reverse order. M.4-M.8 are pure-additive (no behavior
# change to existing paths). M.1-M.3 are the refactor; reverting them
# restores the pre-extraction _post_em_card body. Snapshot test
# fixtures and test file remain (harmless).
git revert <M.8> <M.7> <M.6> <M.5> <M.4> <M.3> <M.2> <M.1>
git push origin main
```

---

## Out of scope (deferred)

- Compare mode (side-by-side two tickers). Panel is designed to grow this via a `+ compare` button.
- Saving / pinning briefs. V2.
- Editing FLOW_TICKERS list from the UI. V2.
- Per-engine refresh sub-buttons. V2.
- Mobile responsive polish. V1.1.
- Recording brief views in the alert recorder. Out of scope (recorder is for engine fires, not user lookups).
- WebSocket-driven progress for refresh-all. Polling every 2s is good enough.
- Brief delta / diff vs last refresh. Interesting but not V1.
- Full ACTION GUIDE rendering on the dashboard panel. V1 shows bias verdict + signal breakdown; full plain-English action guide ships in V1.1 once we know the panel layout works.
- Session selector dropdown (override the auto-detect). V1 always passes `session=None` to let `_compute_em_brief_data` resolve it.
