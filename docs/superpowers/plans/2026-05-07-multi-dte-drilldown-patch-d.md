# Patch D — Multi-DTE Drilldown UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the producer's t7 / t30 / t60 wall data on the Research page. Each ticker card's WALLS section becomes a click-to-expand disclosure: collapsed shows the front-DTE walls (today's behavior); expanded shows all four intents' walls side-by-side with DTE tags ("1DTE", "8D", "32D", "61D").

**Architecture:** The producer (Patch B) is already writing four envelopes per ticker (`bot_state:{ticker}:{front|t7|t30|t60}`). The consumer currently reads only one (the page's `intent` param, default `front`). Patch D extends the consumer to read all four per ticker and attach a new `walls_by_intent` list to each `TickerSnapshot`. The template uses the native HTML `<details>` element for the disclosure — zero JS, full keyboard accessibility, a chevron animation in CSS.

**Tech Stack:** Python 3, redis-py (existing), Jinja2 templates, vanilla CSS. No new dependencies.

---

## Decisions already locked in

- **All four intents read per ticker** — 35 × 4 = 140 Redis GETs per page render. Microseconds total; well within the consumer's existing performance budget.
- **DTE tag format:** `"{N}DTE"` for N ≤ 1 (matches "0DTE"/"1DTE" trader vocabulary), `"{N}D"` for N > 1 (matches "8D"/"32D"/"61D"). Computed at consume time from `envelope.expiration` minus today (UTC).
- **Missing intents → "—" row, not omitted.** Spec says "Mock missing t60 key → expanded view shows '—' for that row, doesn't break." `walls_by_intent` always has 4 entries (one per intent in `INTENTS_ORDER`); intents with no populated envelope have `call_wall=None`/`put_wall=None`/`expiration=None`/`dte_days=None`. Template renders "—" for missing values.
- **`<details>` element, not custom JS toggle.** Native disclosure has built-in keyboard support (Enter/Space), screen-reader semantics, and zero JS. The chevron + animation is pure CSS.
- **Backward compatible.** `snap.call_wall`/`snap.put_wall`/`snap.gamma_wall` continue to be the front-intent values (existing behavior). `walls_by_intent` is the new field; legacy template paths render the existing block unchanged when the new field is empty.
- **Consumer-path-only.** The legacy inline-build path (`RESEARCH_USE_REDIS=0`) doesn't populate `walls_by_intent` — would require running BotState.build four times per ticker, defeating the legacy path's purpose. When the env var is off, the disclosure is just absent.
- **Patch C.5/C.6/C.7 auto-refresh interaction:** unchanged. Warming-up cards still flag as warming-up. The disclosure UI only appears on populated cards.

---

## File structure

**Modified:**
- `omega_dashboard/research_data.py` — adds `INTENTS_ORDER` constant, `_compute_dte_days()` helper, `_format_dte_tag()` helper, `_load_walls_for_all_intents()` reader, `walls_by_intent: list = field(default_factory=list)` field on `TickerSnapshot`, integration into `_research_data_from_redis()`. ~80 lines added.
- `omega_dashboard/templates/dashboard/research.html` — wraps the existing WALLS section in a `<details>` element when `walls_by_intent` has entries; renders all 4 intents with DTE tags. ~30 lines net change.
- `omega_dashboard/static/omega.css` — chevron rotation + transition styling for the disclosure. ~25 lines added.
- `CLAUDE.md` — vocabulary entry for `walls_by_intent` and DTE tag format; decision entry documenting the multi-intent read approach. ~12 lines added.

**Modified (test):**
- `test_research_data_consumer.py` — adds 6 tests covering DTE math, multi-intent reader, integration with `_research_data_from_redis`. ~120 lines added.

**Total touched:** 0 new files, 5 modified files (~270 lines added).

---

# Task D.1 — DTE helpers + multi-intent walls reader

**Why first:** Pure functions, exhaustively unit-tested. No callers in the production flow yet; D.2 wires them.

**Files:**
- Modify: `omega_dashboard/research_data.py` (add helpers + new field on `TickerSnapshot`)
- Modify: `test_research_data_consumer.py` (append tests)

### Task 1.1 — Failing tests for DTE helpers + reader

- [ ] **Step 1: Append tests to `test_research_data_consumer.py`**

Add these tests AFTER the existing tests but BEFORE the `if __name__ == "__main__":` block:

```python
# ─────────────────────────────────────────────────────────────────────
# Patch D.1: DTE helpers + multi-intent walls reader
# ─────────────────────────────────────────────────────────────────────

def test_compute_dte_days_basic():
    from omega_dashboard.research_data import _compute_dte_days
    from datetime import date
    today = date(2026, 5, 7)
    assert_eq(_compute_dte_days("2026-05-07", today=today), 0, "today → 0")
    assert_eq(_compute_dte_days("2026-05-08", today=today), 1, "tomorrow → 1")
    assert_eq(_compute_dte_days("2026-05-15", today=today), 8, "8 days out")
    assert_eq(_compute_dte_days("2026-06-12", today=today), 36, "monthly")


def test_compute_dte_days_handles_invalid_input():
    from omega_dashboard.research_data import _compute_dte_days
    assert_is_none(_compute_dte_days(None), "None input → None")
    assert_is_none(_compute_dte_days(""), "empty string → None")
    assert_is_none(_compute_dte_days("not-a-date"), "garbage → None")


def test_format_dte_tag_format():
    """Spec: '1DTE'/'8D'/'32D'/'61D' — ≤1 days → 'NDTE', >1 → 'ND'."""
    from omega_dashboard.research_data import _format_dte_tag
    assert_eq(_format_dte_tag(0), "0DTE", "0 days → '0DTE'")
    assert_eq(_format_dte_tag(1), "1DTE", "1 day → '1DTE'")
    assert_eq(_format_dte_tag(8), "8D", "8 days → '8D'")
    assert_eq(_format_dte_tag(32), "32D", "32 days → '32D'")
    assert_eq(_format_dte_tag(None), "—", "None → '—' fallback")


def test_load_walls_for_all_intents_full():
    """Producer has all 4 intents populated; reader returns 4 entries."""
    from omega_dashboard.research_data import (
        _load_walls_for_all_intents, INTENTS_ORDER, KEY_PREFIX,
    )
    fake = _FakeRedis()
    for intent, exp, cw, pw in [
        ("front", "2026-05-08", 590.0, 580.0),
        ("t7",    "2026-05-15", 595.0, 575.0),
        ("t30",   "2026-06-08", 600.0, 570.0),
        ("t60",   "2026-07-08", 610.0, 560.0),
    ]:
        env = _make_envelope(
            state_overrides={"ticker": "SPY", "call_wall": cw, "put_wall": pw, "gamma_wall": cw},
            intent=intent,
            expiration=exp,
        )
        fake.set(f"{KEY_PREFIX}SPY:{intent}", json.dumps(env))

    out = _load_walls_for_all_intents("SPY", redis_client=fake)
    assert_eq(len(out), 4, "four entries, one per intent")
    assert_eq([e["intent"] for e in out], list(INTENTS_ORDER), "ordered by INTENTS_ORDER")
    assert_eq(out[0]["call_wall"], 590.0, "front call_wall")
    assert_eq(out[1]["call_wall"], 595.0, "t7 call_wall")
    assert_eq(out[2]["expiration"], "2026-06-08", "t30 expiration carried")
    assert_true(out[3]["dte_days"] is not None and out[3]["dte_days"] > 30,
                "t60 dte_days computed")
    assert_true(out[0]["dte_tag"] in ("0DTE", "1DTE"),
                "front dte_tag populated as 'NDTE'")
    assert_true(out[1]["dte_tag"].endswith("D") and "DTE" not in out[1]["dte_tag"],
                "t7 dte_tag populated as 'ND'")


def test_load_walls_for_all_intents_partial():
    """Spec: missing t60 key → entry exists with None values, doesn't break."""
    from omega_dashboard.research_data import (
        _load_walls_for_all_intents, INTENTS_ORDER, KEY_PREFIX,
    )
    fake = _FakeRedis()
    # Populate only front; t7/t30/t60 missing.
    fake.set(
        f"{KEY_PREFIX}SPY:front",
        json.dumps(_make_envelope(
            state_overrides={"ticker": "SPY", "call_wall": 590.0, "put_wall": 580.0},
            intent="front", expiration="2026-05-08",
        )),
    )

    out = _load_walls_for_all_intents("SPY", redis_client=fake)
    assert_eq(len(out), 4, "four entries even with partial population")
    by_intent = {e["intent"]: e for e in out}
    assert_eq(by_intent["front"]["call_wall"], 590.0, "front populated")
    assert_is_none(by_intent["t7"]["call_wall"], "t7 missing → None")
    assert_is_none(by_intent["t30"]["expiration"], "t30 missing → expiration None")
    assert_is_none(by_intent["t60"]["dte_days"], "t60 missing → dte_days None")


def test_load_walls_for_all_intents_redis_unavailable():
    from omega_dashboard.research_data import _load_walls_for_all_intents, INTENTS_ORDER
    out = _load_walls_for_all_intents("SPY", redis_client=None)
    assert_eq(len(out), 4, "always returns 4 entries (one per intent)")
    assert_eq([e["intent"] for e in out], list(INTENTS_ORDER),
              "intents in canonical order")
    for e in out:
        assert_is_none(e["call_wall"], f"{e['intent']} call_wall is None")
        assert_is_none(e["expiration"], f"{e['intent']} expiration is None")
        assert_eq(e["dte_tag"], "—", f"{e['intent']} dte_tag is '—' fallback")
```

Update the `__main__` block. Find the existing trailing tests and append the new ones after them:

```python
    # Patch D.1: DTE helpers + multi-intent walls reader
    test_compute_dte_days_basic()
    test_compute_dte_days_handles_invalid_input()
    test_format_dte_tag_format()
    test_load_walls_for_all_intents_full()
    test_load_walls_for_all_intents_partial()
    test_load_walls_for_all_intents_redis_unavailable()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
python3 test_research_data_consumer.py
```

Expected: existing tests still pass; six new tests fail with `ImportError` for `_compute_dte_days`, `_format_dte_tag`, `_load_walls_for_all_intents`, `INTENTS_ORDER`.

### Task 1.2 — Implement helpers + reader + new field

- [ ] **Step 1: Add `walls_by_intent` field to `TickerSnapshot`**

Find the `TickerSnapshot` dataclass at `omega_dashboard/research_data.py` (the field block ends with `warming_up: bool = False`). Replace `warming_up: bool = False` with:

```python
    warming_up: bool = False
    # Patch D.1: list of {intent, expiration, dte_days, call_wall, put_wall,
    # gamma_wall} dicts, one per intent in INTENTS_ORDER. Populated by
    # _research_data_from_redis when consumer reads all 4 intents per ticker.
    # Empty on the legacy inline-build path. Template renders the click-to-
    # expand WALLS disclosure when this list has entries.
    walls_by_intent: list = field(default_factory=list)
```

- [ ] **Step 2: Add `INTENTS_ORDER` constant and helpers**

Find the existing C.1 helpers section (the block starting with `# Patch C: Redis consumer helpers` and ending around `_snapshot_from_envelope`). Immediately AFTER `_snapshot_from_envelope` and BEFORE `_load_snapshot_from_redis`, add:

```python
# ─────────────────────────────────────────────────────────────────────
# Patch D.1: Multi-DTE drilldown helpers
#
# The producer writes four envelopes per ticker, one per intent:
#   bot_state:{ticker}:front
#   bot_state:{ticker}:t7
#   bot_state:{ticker}:t30
#   bot_state:{ticker}:t60
# The Research page's WALLS section is a click-to-expand disclosure that
# shows all four intents' walls with DTE tags. _load_walls_for_all_intents
# reads all four per ticker and returns a list-of-dicts in INTENTS_ORDER.
# ─────────────────────────────────────────────────────────────────────

# Order matters — the front intent goes in the disclosure's collapsed
# summary; t7/t30/t60 expand below. Must match canonical_expiration's
# intent vocabulary.
INTENTS_ORDER = ("front", "t7", "t30", "t60")


def _compute_dte_days(expiration_iso: Optional[str], today=None) -> Optional[int]:
    """Days-to-expiration as an integer, or None on missing/malformed input.
    `today` is overridable for tests; production calls use UTC today."""
    if not expiration_iso:
        return None
    try:
        from datetime import date, datetime
        if today is None:
            today = datetime.now(timezone.utc).date()
        exp = datetime.fromisoformat(expiration_iso).date() if "T" in expiration_iso \
              else date.fromisoformat(expiration_iso)
        return (exp - today).days
    except Exception:
        return None


def _format_dte_tag(dte_days: Optional[int]) -> str:
    """Format a DTE integer as a display tag.

    Convention: "0DTE"/"1DTE" for the immediate expirations (matches
    options-trader vocabulary), "ND" for everything further out.
    None → "—" so missing-data rows render with a dash.
    """
    if dte_days is None:
        return "—"
    if dte_days <= 1:
        return f"{dte_days}DTE"
    return f"{dte_days}D"


def _load_walls_for_all_intents(ticker: str, redis_client) -> list:
    """Read all four intents' envelopes for a ticker. Returns a list of
    dicts in INTENTS_ORDER. Each dict has keys:
      - intent: str (e.g. "front")
      - expiration: ISO date string or None
      - dte_days: int or None
      - dte_tag: pre-formatted display string ("1DTE"/"8D"/"—")
      - call_wall, put_wall, gamma_wall: float or None

    Missing/malformed envelopes contribute an entry with all None values
    so the consumer always returns 4 entries (template renders "—" for
    missing rows). No exception ever propagates to the caller.

    `dte_tag` is computed here (not in the template) so the Jinja
    template stays simple — `{{ w.dte_tag }}` instead of inline
    string concatenation.
    """
    out = []
    for intent in INTENTS_ORDER:
        entry = {
            "intent": intent,
            "expiration": None,
            "dte_days": None,
            "dte_tag": "—",
            "call_wall": None,
            "put_wall": None,
            "gamma_wall": None,
        }
        if redis_client is None:
            out.append(entry)
            continue

        key = f"{KEY_PREFIX}{ticker}:{intent}"
        try:
            raw = redis_client.get(key)
        except Exception as e:
            log.debug(f"walls reader: redis GET {key} failed: {e}")
            out.append(entry)
            continue

        if raw is None:
            out.append(entry)
            continue

        try:
            envelope = json.loads(raw)
        except Exception:
            out.append(entry)
            continue
        if not isinstance(envelope, dict):
            out.append(entry)
            continue
        if _validate_envelope_versions(envelope, ticker) is not None:
            out.append(entry)
            continue

        state = envelope.get("state") or {}
        entry["expiration"] = envelope.get("expiration")
        entry["dte_days"] = _compute_dte_days(entry["expiration"])
        entry["dte_tag"] = _format_dte_tag(entry["dte_days"])
        entry["call_wall"] = state.get("call_wall")
        entry["put_wall"] = state.get("put_wall")
        entry["gamma_wall"] = state.get("gamma_wall")
        out.append(entry)

    return out
```

- [ ] **Step 3: Run tests, verify they pass**

```bash
python3 test_research_data_consumer.py
```

Expected: total `PASSED` count increases by ~14 (six new tests, multiple assertions each). `FAILED: 0`.

- [ ] **Step 4: AST check**

```bash
python3 -c "import ast; ast.parse(open('omega_dashboard/research_data.py', encoding='utf-8').read())"
python3 -c "import ast; ast.parse(open('test_research_data_consumer.py', encoding='utf-8').read())"
```

Expected: silent.

- [ ] **Step 5: Regression sanity**

```bash
python3 test_bot_state_producer.py
python3 test_canonical_expiration.py
python3 test_prev_close_store.py
```

Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add omega_dashboard/research_data.py test_research_data_consumer.py
git commit -m "$(cat <<'EOF'
Patch D.1: DTE helpers + multi-intent walls reader

Adds the data-layer foundation for the Multi-DTE drilldown UI. Pure
functions, exhaustively unit-tested:

  - INTENTS_ORDER = ("front", "t7", "t30", "t60") — canonical order for
    the disclosure (front in summary, others in expanded view)
  - _compute_dte_days(expiration_iso, today=None) — days-to-expiration
    integer, None on malformed input
  - _format_dte_tag(dte_days) — "0DTE"/"1DTE" for ≤1, "ND" for >1, "—"
    for None (matches spec's "1DTE"/"8D"/"32D"/"61D" examples)
  - _load_walls_for_all_intents(ticker, redis_client) — reads all four
    envelopes per ticker, returns list of 4 dicts in INTENTS_ORDER. Missing
    envelopes contribute None-valued entries (no exception ever propagates)
  - TickerSnapshot.walls_by_intent: list — new field, default empty.
    Populated by _research_data_from_redis in Patch D.2.

No production callers yet; D.2 wires the reader into the consumer path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task D.2 — Wire walls reader into consumer flow

**Why second:** D.1 has the helper; D.2 calls it from `_research_data_from_redis` so every ticker's snapshot carries `walls_by_intent` in production. Still no template change — the new data is dormant until D.3.

**Files:**
- Modify: `omega_dashboard/research_data.py` (`_research_data_from_redis` integration)
- Modify: `test_research_data_consumer.py` (integration test)

### Task 2.1 — Failing test for the integration

- [ ] **Step 1: Append the integration test**

Add to `test_research_data_consumer.py` after the D.1 tests:

```python
def test_research_data_from_redis_populates_walls_by_intent():
    """Integration: _research_data_from_redis attaches walls_by_intent
    to each snapshot (read from all 4 intent keys)."""
    from omega_dashboard.research_data import (
        _research_data_from_redis, INTENTS_ORDER, KEY_PREFIX,
    )
    fake = _FakeRedis()
    # Populate front + t7 for SPY; t30/t60 missing.
    fake.set(
        f"{KEY_PREFIX}SPY:front",
        json.dumps(_make_envelope(
            state_overrides={
                "ticker": "SPY", "spot": 590.0, "call_wall": 595.0, "put_wall": 585.0,
            },
            intent="front", expiration="2026-05-08",
        )),
    )
    fake.set(
        f"{KEY_PREFIX}SPY:t7",
        json.dumps(_make_envelope(
            state_overrides={
                "ticker": "SPY", "spot": 590.0, "call_wall": 600.0, "put_wall": 580.0,
            },
            intent="t7", expiration="2026-05-15",
        )),
    )

    payload = _research_data_from_redis(
        tickers=["SPY"], intent="front", redis_client=fake,
    )
    assert_eq(len(payload.snapshots), 1, "one ticker")
    snap = payload.snapshots[0]
    assert_eq(len(snap.walls_by_intent), 4, "always 4 entries (one per intent)")
    assert_eq(snap.walls_by_intent[0]["intent"], "front", "front first")
    assert_eq(snap.walls_by_intent[0]["call_wall"], 595.0, "front call_wall")
    assert_eq(snap.walls_by_intent[1]["intent"], "t7", "t7 second")
    assert_eq(snap.walls_by_intent[1]["call_wall"], 600.0, "t7 call_wall")
    assert_is_none(snap.walls_by_intent[2]["call_wall"],
                   "t30 missing → None (no envelope written)")
    assert_is_none(snap.walls_by_intent[3]["call_wall"],
                   "t60 missing → None (no envelope written)")


def test_warming_up_snapshot_has_empty_walls_by_intent():
    """Tickers that fail the primary-intent read still get walls_by_intent
    populated as empty list (NOT 4-entry-with-Nones — the warming-up
    snapshot is purely synthetic and shouldn't pretend to have data)."""
    from omega_dashboard.research_data import _warming_up_snapshot
    snap = _warming_up_snapshot("AAPL", reason="missing key")
    assert_eq(snap.walls_by_intent, [],
              "warming-up snapshot has empty walls_by_intent")
```

Update `__main__` to register the new tests:

```python
    # Patch D.2: integration
    test_research_data_from_redis_populates_walls_by_intent()
    test_warming_up_snapshot_has_empty_walls_by_intent()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
python3 test_research_data_consumer.py
```

Expected: existing pass; new tests fail because `snap.walls_by_intent` is still empty (D.1 only added the field, didn't populate it).

### Task 2.2 — Wire the reader into `_research_data_from_redis`

- [ ] **Step 1: Update `_research_data_from_redis`**

Find `_research_data_from_redis` in `omega_dashboard/research_data.py`. Locate the snapshots-building list comprehension (currently `snapshots = [_load_snapshot_from_redis(t, intent, redis_client=redis_client) for t in tickers]` or similar). Replace that single line with:

```python
    snapshots = []
    for t in tickers:
        snap = _load_snapshot_from_redis(t, intent, redis_client=redis_client)
        # Patch D.2: attach all-intents walls. For warming-up snapshots
        # leave walls_by_intent empty — the synthetic placeholder
        # shouldn't pretend to have data the producer never wrote.
        if not snap.warming_up:
            snap.walls_by_intent = _load_walls_for_all_intents(
                t, redis_client=redis_client,
            )
        snapshots.append(snap)
```

(Note: TickerSnapshot is a regular dataclass not frozen, so `snap.walls_by_intent = ...` mutation is allowed.)

- [ ] **Step 2: Run tests, verify they pass**

```bash
python3 test_research_data_consumer.py
```

Expected: all tests pass, total `PASSED` increases by 8-10.

- [ ] **Step 3: Regression sanity**

```bash
python3 test_bot_state_producer.py
python3 test_canonical_expiration.py
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add omega_dashboard/research_data.py test_research_data_consumer.py
git commit -m "$(cat <<'EOF'
Patch D.2: wire walls reader into consumer flow

_research_data_from_redis now calls _load_walls_for_all_intents per
ticker and attaches the result to snap.walls_by_intent. Warming-up
snapshots keep walls_by_intent empty — synthetic placeholders
shouldn't pretend to have data the producer never wrote.

Adds 4 reads per populated ticker per page render (35×4=140 total
for the default universe). Microseconds against the existing
35-read primary-intent baseline.

No template change yet — the new data is dormant until D.3 adds
the click-to-expand disclosure UI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task D.3 — Template + CSS disclosure UI

**Why third:** D.2 makes the data flow into the snapshot. D.3 is the visible UI: each ticker card's WALLS section becomes a `<details>` element, rendering the front-DTE walls in the summary and t7/t30/t60 below when expanded.

**Files:**
- Modify: `omega_dashboard/templates/dashboard/research.html` (replace WALLS section with disclosure)
- Modify: `omega_dashboard/static/omega.css` (chevron rotation, expand animation)

### Task 3.1 — Template

- [ ] **Step 1: Replace the existing WALLS section**

Find the existing block in `omega_dashboard/templates/dashboard/research.html`:

```html
          {# ─── LIVE: walls (Patch 11.5 — wired from canonical_exposures) ─── #}
          {% if snap.call_wall is not none or snap.put_wall is not none or snap.gamma_wall is not none %}
          <div class="research-card-section">
            <div class="research-card-eyebrow">WALLS &middot; <span class="research-live">live</span></div>
            {% if snap.call_wall is not none %}
              <div class="research-card-row">
                <span class="research-card-label">Call Wall</span>
                <span class="research-card-value positive">{{ "%.2f"|format(snap.call_wall) }}</span>
              </div>
            {% endif %}
            {% if snap.put_wall is not none %}
              <div class="research-card-row">
                <span class="research-card-label">Put Wall</span>
                <span class="research-card-value negative">{{ "%.2f"|format(snap.put_wall) }}</span>
              </div>
            {% endif %}
            {% if snap.gamma_wall is not none %}
              <div class="research-card-row">
                <span class="research-card-label">Gamma Wall</span>
                <span class="research-card-value">{{ "%.2f"|format(snap.gamma_wall) }}</span>
              </div>
            {% endif %}
          </div>
          {% endif %}
```

Replace with:

```html
          {# ─── LIVE: walls (Patch D.3 — multi-DTE drilldown disclosure) ─── #}
          {% if snap.walls_by_intent %}
            {# Multi-intent path: <details> disclosure with all 4 intents.
               dte_tag is pre-formatted in research_data.py so the template
               stays simple. #}
            {% set front = snap.walls_by_intent[0] %}
            <details class="research-card-section research-card-walls-disclosure">
              <summary class="research-card-walls-summary">
                <div class="research-card-eyebrow">
                  WALLS &middot; <span class="research-live">live</span>
                  <span class="research-walls-chevron">▾</span>
                </div>
                {% if front.call_wall is not none %}
                  <div class="research-card-row">
                    <span class="research-card-label">Call Wall &middot; {{ front.dte_tag }}</span>
                    <span class="research-card-value positive">{{ "%.2f"|format(front.call_wall) }}</span>
                  </div>
                {% endif %}
                {% if front.put_wall is not none %}
                  <div class="research-card-row">
                    <span class="research-card-label">Put Wall &middot; {{ front.dte_tag }}</span>
                    <span class="research-card-value negative">{{ "%.2f"|format(front.put_wall) }}</span>
                  </div>
                {% endif %}
              </summary>
              {# Expanded: t7/t30/t60 rows #}
              {% for w in snap.walls_by_intent[1:] %}
                <div class="research-card-row research-walls-extra">
                  <span class="research-card-label">Call Wall &middot; {{ w.dte_tag }}</span>
                  {% if w.call_wall is not none %}
                    <span class="research-card-value positive">{{ "%.2f"|format(w.call_wall) }}</span>
                  {% else %}
                    <span class="research-card-value muted">—</span>
                  {% endif %}
                </div>
                <div class="research-card-row research-walls-extra">
                  <span class="research-card-label">Put Wall &middot; {{ w.dte_tag }}</span>
                  {% if w.put_wall is not none %}
                    <span class="research-card-value negative">{{ "%.2f"|format(w.put_wall) }}</span>
                  {% else %}
                    <span class="research-card-value muted">—</span>
                  {% endif %}
                </div>
              {% endfor %}
            </details>
          {% elif snap.call_wall is not none or snap.put_wall is not none or snap.gamma_wall is not none %}
            {# Legacy fallback: single-intent rendering (env var off, or
               warming-up edge cases). Identical to pre-Patch-D output. #}
            <div class="research-card-section">
              <div class="research-card-eyebrow">WALLS &middot; <span class="research-live">live</span></div>
              {% if snap.call_wall is not none %}
                <div class="research-card-row">
                  <span class="research-card-label">Call Wall</span>
                  <span class="research-card-value positive">{{ "%.2f"|format(snap.call_wall) }}</span>
                </div>
              {% endif %}
              {% if snap.put_wall is not none %}
                <div class="research-card-row">
                  <span class="research-card-label">Put Wall</span>
                  <span class="research-card-value negative">{{ "%.2f"|format(snap.put_wall) }}</span>
                </div>
              {% endif %}
              {% if snap.gamma_wall is not none %}
                <div class="research-card-row">
                  <span class="research-card-label">Gamma Wall</span>
                  <span class="research-card-value">{{ "%.2f"|format(snap.gamma_wall) }}</span>
                </div>
              {% endif %}
            </div>
          {% endif %}
```

- [ ] **Step 2: Verify Jinja parses**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('omega_dashboard/templates')); env.get_template('dashboard/research.html'); print('Jinja parse OK')"
```

Expected: `Jinja parse OK`.

### Task 3.2 — CSS

- [ ] **Step 1: Append disclosure styling to `omega_dashboard/static/omega.css`**

Add to the END of the file:

```css

/* ─── Patch D.3: walls disclosure ─── */
/* Ticker card's WALLS section is a <details> element. Collapsed shows
   the front-DTE walls. Click/keyboard-activate to reveal t7/t30/t60. */
.research-card-walls-disclosure {
  /* Inherits .research-card-section spacing; overrides default <details> */
}

.research-card-walls-summary {
  cursor: pointer;
  list-style: none;
  /* Suppress the default browser disclosure triangle (we draw our own
     in the eyebrow row via .research-walls-chevron). */
}

.research-card-walls-summary::-webkit-details-marker {
  display: none;
}

.research-walls-chevron {
  display: inline-block;
  margin-left: 0.4em;
  font-size: 0.8em;
  color: var(--research-warming-color, #888);
  transition: transform 0.2s ease;
}

details[open] .research-walls-chevron {
  transform: rotate(180deg);
}

.research-walls-extra {
  /* Slight indent for the t7/t30/t60 rows so the visual hierarchy
     reads "summary row → expansion rows below". */
  padding-left: 0.6em;
  border-left: 1px solid rgba(255, 255, 255, 0.08);
  margin-left: 0.2em;
}

.research-card-walls-disclosure[open] .research-walls-chevron {
  /* Already covered by the generic details[open] selector above, but
     scoping locally guards against future global overrides. */
  transform: rotate(180deg);
}
```

### Task 3.3 — Final regression sweep + commit

- [ ] **Step 1: Run all relevant test suites**

```bash
python3 test_research_data_consumer.py
python3 test_bot_state_producer.py
python3 test_canonical_expiration.py
python3 test_prev_close_store.py
python3 test_spot_prices_streaming.py
```

Expected: all clean.

- [ ] **Step 2: Verify Jinja still parses**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('omega_dashboard/templates')); env.get_template('dashboard/research.html'); print('Jinja parse OK')"
```

- [ ] **Step 3: Commit**

```bash
git add omega_dashboard/templates/dashboard/research.html omega_dashboard/static/omega.css
git commit -m "$(cat <<'EOF'
Patch D.3: WALLS section becomes click-to-expand disclosure

Replaces the static WALLS render with a native HTML <details> element.
Collapsed state shows the front-DTE Call/Put walls (matches today's
output). Click or keyboard-activate to reveal t7/t30/t60 rows below,
each with its own DTE tag ("1DTE"/"8D"/"32D"/"61D").

Missing intents render as "—" rows so the expanded view always shows
4 rows per side regardless of producer coverage. The legacy single-
intent block stays as a fallback when snap.walls_by_intent is empty
(env var off, or warming-up snapshots that bypassed D.2's reader).

CSS:
  - Suppresses the default browser disclosure triangle (Webkit + Firefox
    quirks differ).
  - Custom chevron in the eyebrow row, rotates 180° via CSS transition
    on details[open].
  - Slight left-border indent on the expanded rows for visual hierarchy.

Zero JS — full keyboard accessibility (Enter/Space) and screen-reader
semantics come free with <details>.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Task D.4 — CLAUDE.md update

**Why last:** Documents the new vocabulary and architectural decision for future sessions. ~12 lines.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add to "Project vocabulary"**

Find the existing `RESEARCH_USE_REDIS` vocabulary entry. Insert this NEW entry IMMEDIATELY AFTER the `MIN_COMPATIBLE_PRODUCER_VERSION / EXPECTED_CONVENTION_VERSION` entry:

```markdown
- **walls_by_intent / DTE tag** — `TickerSnapshot.walls_by_intent` is a
  4-entry list (one per intent in `INTENTS_ORDER = ("front", "t7", "t30",
  "t60")`) attached to every snapshot on the consumer path. Each entry
  has `intent`, `expiration`, `dte_days`, `call_wall`, `put_wall`,
  `gamma_wall`. Missing intents contribute None-valued entries so the
  template always renders 4 rows. The Research page's WALLS section is a
  click-to-expand `<details>` disclosure (Patch D): collapsed shows the
  front-DTE walls; expanded shows all 4 with DTE tags formatted as
  `"0DTE"/"1DTE"` for ≤1 day or `"ND"` for >1.
```

- [ ] **Step 2: Add to "Decisions already made"**

Append to the end of that section:

```markdown
- Multi-DTE walls drilldown (Patch D) reads all 4 intent envelopes per
  ticker on every Research page render — 35×4=140 Redis GETs per page
  load. The cost is microseconds; the benefit is a single page render
  showing all DTE windows without round-trips. The `<details>` element
  is native HTML (no custom JS toggle, free keyboard support). Missing
  intents render as "—" so the expanded view stays stable even when the
  producer hasn't caught up to t60 yet.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
CLAUDE.md: walls_by_intent + DTE tag vocab + Patch D decision

One vocab entry covering walls_by_intent shape (4 entries always,
INTENTS_ORDER, DTE tag format) and one decision entry documenting
the all-4-intents-per-page-render approach with the per-render cost
math.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Deployment & cutover

After all four patches (D.1–D.4) are committed and pushed:

1. **Push to main.** Render auto-rebuild picks up the code.

2. **Behavior on first visit to `/research` after rebuild:**
   - Producer is already writing all 4 intents (Tier C populated as of today's deploy).
   - Each ticker card's WALLS section now has a chevron next to "WALLS · live".
   - Click any card's WALLS section to expand. You see Call Wall · 1DTE / 8D / 32D / 61D and Put Wall · 1DTE / 8D / 32D / 61D.
   - Tickers where t60 is missing show "—" in those rows.

3. **No env flag needed for D itself** — the multi-intent disclosure is automatic when `RESEARCH_USE_REDIS=1`. If you turn `RESEARCH_USE_REDIS=0` later, the disclosure goes away (legacy template fallback) and the page reverts to the single-intent walls render.

4. **Verify in Render shell:**
   ```bash
   redis-cli -u $REDIS_URL KEYS 'bot_state:SPY:*'
   ```
   Expected: 4 keys (`front`, `t7`, `t30`, `t60`).

5. **Rollback:** unset `RESEARCH_USE_REDIS`, redeploy. Within 60s the entire consumer path is dormant; `walls_by_intent` stays empty on every snapshot; template renders the legacy single-intent block.

# Follow-ups (NOT in this plan)

- **Cadence re-tier patch** — driven by 24h timing analysis from `bot_state_producer:timings:{YYYYMMDD}` sorted set. Tomorrow afternoon you'll have a full trading day of build timings to slice.
- **canonical_technicals** — RSI / MACD / ADX / VWAP. Foundation for the next Research-page sections.
- **First production migration** — silent thesis migration to consume from Redis. Big strategic move; want producer at steady-state for several days first.
