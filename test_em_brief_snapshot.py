"""Snapshot regression test for app._post_em_card.

# v11.7 (Patch M.0): Snapshot test gate for _post_em_card refactor.

Captures the byte-exact text payloads that _post_em_card pushes to
post_to_telegram for three deterministic input scenarios (spy, hood, thin),
and pins them against fixture files. Patches M.1-M.3 will refactor
_post_em_card to delegate compute to a new pure helper
_compute_em_brief_data; this test must continue to pass byte-for-byte
across that refactor.

How it works:
- Each scenario in tests/fixtures/em_brief_snapshots/inputs.json describes
  a frozen "now" timestamp (CT) plus return values for the external IO
  helpers that _post_em_card calls (_get_0dte_iv, _estimate_liquidity,
  get_daily_candles).
- The test patches:
    * app.datetime  -> _FakeDateTime returning the scenario's frozen instant
    * app.post_to_telegram -> capture-to-list shim
    * app._get_0dte_iv / app._estimate_liquidity / app.get_daily_candles
      -> deterministic scenario data
    * external-IO-only helpers (_post_trade_card, _log_em_prediction,
      build_thesis_from_em_card, get_thesis_engine, _get_0dte_chain,
      _extract_atm_option_data, _derive_structure_levels_from_chain,
      _compute_price_structure_levels, _merge_price_structure_with_walls)
      -> no-op stubs so the test never hits Schwab / Redis / network.
- get_canonical_vol_regime is patched to return a deterministic dict
  derived from the scenario (its real implementation hits CBOE / Yahoo
  for VIX / VIX9D / VIX MA200 / VVIX, which would make the snapshot
  non-deterministic).
- All other pure formatter / compute helpers (_calc_intraday_em,
  _calc_bias, _format_em_block, _format_skew_line, format_cagf_block,
  format_dte_block, format_vol_regime_line, format_trade_sign_line,
  _format_canonical_vol_line, compute_cagf, recommend_dte,
  resolve_unified_regime) run for real -- they are the output we are
  pinning.
- Captured texts are joined with the literal separator "=== MESSAGE BREAK ==="
  and compared against tests/fixtures/em_brief_snapshots/<scenario>.txt.
- On first run (or any time the fixture is missing) the test writes
  <scenario>.actual.txt next to the fixture and fails with instructions.
  The maintainer reviews .actual.txt and promotes it to the .txt fixture
  iff the output matches expectations.

Run:
    python test_em_brief_snapshot.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime as _real_datetime
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import patch, MagicMock

# Repo root on sys.path so `import app` works when this file is run directly.
REPO_ROOT = Path(__file__).parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "em_brief_snapshots"
INPUTS_PATH = FIXTURES_DIR / "inputs.json"
MESSAGE_SEPARATOR = "\n=== MESSAGE BREAK ===\n"


def _load_inputs() -> dict:
    with open(INPUTS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_iso_ct(iso_str: str) -> _real_datetime:
    """Parse the scenario's now_iso_ct ('2026-05-12T10:30:00-05:00')
    into a real timezone-aware datetime. We do NOT carry the iso offset
    forward; instead we treat it as the wall-clock UTC instant and let
    callers re-tz it via .astimezone(tz)."""
    return _real_datetime.fromisoformat(iso_str)


def _make_fake_datetime(frozen_instant: _real_datetime):
    """Build a datetime subclass whose .now() / .utcnow() return
    `frozen_instant`. All other behavior (constructor, arithmetic,
    .replace, .strftime, .astimezone) is inherited unchanged so calls
    like `now_ct.replace(hour=8, ...)` work normally."""

    class _FakeDateTime(_real_datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None):
            if tz is None:
                # Caller wants naive local time. Strip tz from the frozen
                # instant after converting it to UTC, then return naive.
                return frozen_instant.astimezone(timezone.utc).replace(tzinfo=None)
            return frozen_instant.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return frozen_instant.astimezone(timezone.utc).replace(tzinfo=None)

    return _FakeDateTime


def _deterministic_vol_regime(vix_payload: dict | None) -> dict:
    """Build a deterministic vol_regime stand-in derived only from the
    scenario's vix dict. Real get_canonical_vol_regime would hit CBOE /
    Yahoo for live VIX9D and MA200; we hard-code values that exercise
    the same code paths in _post_em_card without network IO."""
    vix_payload = vix_payload or {}
    vix_val = vix_payload.get("vix")
    return {
        "label": "NORMAL",
        "base": "NORMAL",
        "regime": "NORMAL",
        "emoji": "\U0001F321️",
        "vix": vix_val,
        "vix9d": vix_payload.get("vix9d"),
        "vvix": None,
        "term_structure": vix_payload.get("term", "normal"),
        "vix_ma200": 18.0,
        "above_ma200": (vix_val is not None and vix_val > 18.0),
        "caution_score": 1,
        "rv5": None,
        "rv20": None,
        "transition_warning": False,
    }


class _NoopThesisEngine:
    """Stand-in for app.get_thesis_engine() return value. The card
    calls .store_thesis(ticker, thesis), .get_thesis(ticker),
    .evaluate(ticker, spot), and .build_guidance(ticker, spot)."""

    def store_thesis(self, ticker, thesis):
        return None

    def get_thesis(self, ticker):
        return None  # signals "no thesis stored" so guidance branch is skipped

    def evaluate(self, ticker, spot):
        return None

    def build_guidance(self, ticker, spot):
        return []


class EmBriefSnapshotTest(unittest.TestCase):
    """One mega-test, parametric over the 3 scenarios. We do not use
    unittest's subTest helper because each scenario should be visibly
    independent in failure output."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.inputs = _load_inputs()
        cls.failures: list[str] = []

    def _capture_brief(self, scenario_key: str) -> str:
        scenario = self.inputs[scenario_key]
        ticker = scenario["ticker"]
        session = scenario["session"]
        frozen_instant = _parse_iso_ct(scenario["now_iso_ct"])
        fake_dt_cls = _make_fake_datetime(frozen_instant)

        # _get_0dte_iv returns a 9-element tuple.
        iv_payload = scenario["_get_0dte_iv_returns"]
        iv_tuple = (
            iv_payload["iv"],
            iv_payload["spot"],
            iv_payload["expiration"],
            iv_payload["eng"],
            iv_payload["walls"],
            iv_payload["skew"],
            iv_payload["pcr"],
            iv_payload["vix"],
            iv_payload["v4_result"],
        )

        # _estimate_liquidity returns a 2-tuple (adv, _).
        liq_payload = scenario["_estimate_liquidity_returns"]
        liq_tuple = (liq_payload["adv"], liq_payload.get("_"))

        candles = scenario["get_daily_candles_returns"]

        captured: list[str] = []

        def _capture_post(text, *args, **kwargs):
            captured.append(text)
            return True

        # Import app lazily so the patches above are not applied to a
        # cached module from a prior run.
        import app

        # thesis_monitor uses its own `datetime` import for _get_time_phase_ct;
        # without this patch the action block leaks real wall-clock-derived
        # phase labels ("Pre-Market", "Power Hour", etc) into the snapshot.
        import thesis_monitor

        patches = [
            patch.object(app, "datetime", fake_dt_cls),
            patch.object(thesis_monitor, "datetime", fake_dt_cls),
            patch.object(app, "post_to_telegram", side_effect=_capture_post),
            patch.object(app, "_get_0dte_iv", return_value=iv_tuple),
            patch.object(app, "_estimate_liquidity", return_value=liq_tuple),
            patch.object(app, "get_daily_candles", return_value=candles),
            # Vol regime: real impl hits live CBOE/Yahoo, would be flaky.
            patch.object(app, "get_canonical_vol_regime",
                         return_value=_deterministic_vol_regime(iv_payload.get("vix"))),
            # External IO / side-effect helpers — silenced.
            # NOTE: _post_trade_card is intentionally NOT mocked. It produces
            # Telegram output that is part of what users see when /em fires,
            # and the M-series refactor must preserve that output verbatim.
            patch.object(app, "_log_em_prediction", return_value=None),
            patch.object(app, "build_thesis_from_em_card", return_value=MagicMock()),
            patch.object(app, "get_thesis_engine", return_value=_NoopThesisEngine()),
            patch.object(app, "_get_0dte_chain", return_value=({}, iv_payload["spot"], iv_payload["expiration"])),
            patch.object(app, "_extract_atm_option_data", return_value={
                "call_delta": 0.0, "call_premium": 0.0,
                "put_delta": 0.0, "put_premium": 0.0,
            }),
            patch.object(app, "_derive_structure_levels_from_chain",
                         side_effect=lambda data, spot, base_walls=None, eng=None: (base_walls or {})),
            patch.object(app, "_compute_price_structure_levels", return_value={}),
            patch.object(app, "_merge_price_structure_with_walls",
                         side_effect=lambda price_struct, chain_struct, spot, em=None: (chain_struct or {})),
        ]

        for p in patches:
            p.start()
        try:
            app._post_em_card(ticker, session)
        finally:
            for p in patches:
                p.stop()

        return MESSAGE_SEPARATOR.join(captured)

    def _run_scenario(self, scenario_key: str):
        actual = self._capture_brief(scenario_key)
        fixture_path = FIXTURES_DIR / f"{scenario_key}.txt"
        actual_path = FIXTURES_DIR / f"{scenario_key}.actual.txt"

        if not fixture_path.exists():
            actual_path.write_text(actual, encoding="utf-8", newline="\n")
            self.fail(
                f"Fixture missing: {fixture_path.name}. Wrote captured output to "
                f"{actual_path.name}. Review it; if correct, rename "
                f"{actual_path.name} -> {fixture_path.name} and re-run."
            )

        expected = fixture_path.read_text(encoding="utf-8")
        if actual != expected:
            actual_path.write_text(actual, encoding="utf-8", newline="\n")
            self.fail(
                f"Snapshot drift for scenario {scenario_key!r}. "
                f"Captured output written to {actual_path.name}. "
                f"Diff against {fixture_path.name} to investigate. "
                f"If the new output is intentional, replace the fixture."
            )

        # On a clean pass, make sure no stale .actual.txt lingers.
        if actual_path.exists():
            try:
                actual_path.unlink()
            except OSError:
                pass

    # ── Scenario tests ────────────────────────────────────────────────
    def test_spy_full_brief_snapshot(self):
        self._run_scenario("spy")

    def test_hood_pin_zone_brief_snapshot(self):
        self._run_scenario("hood")

    def test_thin_data_brief_snapshot(self):
        self._run_scenario("thin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
