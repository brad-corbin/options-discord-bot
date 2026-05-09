# Alert Recorder V1 — Design Spec

**Status:** Approved, ready for Patch G implementation
**Author:** Brad Corbin (vision) + Claude (drafting)
**Date:** 2026-05-08
**Patch:** G (recorder schema + write hooks)

---

## 1. The Question

Brad cannot currently answer: **"Of the alerts you're showing me on Telegram, what's the win rate?"**

Today the bot fires alerts to the main Telegram channel. Some Brad takes, some he doesn't. Outcomes happen, but they're not measured systematically. There's no answer to "would I have won if I'd held burst signals for 30 minutes vs. 1 day," or "is v8.4 credit's win rate trending up or down," or "are these alerts producing real edge or am I telling myself a story."

V1 of the recorder solves this. It records every alert that hits the main channel, tracks the price evolution of the suggested structure, and computes outcomes at standard horizons. A barometer dashboard surface (Patch H) reads the recorder and shows win rate by engine, classification, and holding horizon.

V1 is intentionally narrow. It does not:
- Track which alerts Brad actually traded
- Track campaigns, rolls, or position transformations
- Implement an exit gate
- Record suppressed/internal events that didn't reach Telegram
- Implement engine promotion logic (proven vs. unproven)

These are V2+ work.

## 2. Engines wired in V1

Four trade-decision-grade alert types that hit the main Telegram channel today:

| # | Telegram Card | Source Module | Triggered By |
|---|---|---|---|
| 1 | 🚀 LONG CALL BURST | `long_call_burst_builder` | V2 5D classifies `momentum_burst_label="YES"` |
| 2 | ⚡ V2 5D EDGE MODEL (and 🟠 V2 SETUP VALID / VEHICLE REJECTED) | `v2_5d_edge_model` direct card | V2 5D evaluation produces a Grade |
| 3 | 💎 v8.4 CREDIT | `credit_card_builder` | V2 5D routing produces a credit setup |
| 4 | 💎🚨 CONVICTION PLAY | `oi_flow` | Institutional flow conviction threshold met |

**Universe:** Whatever ticker set the bot is currently running on. No additional filtering. If a card hits the main channel, it gets recorded.

**Engine versioning:** Mandatory. Every alert records the engine's version string. When scoring weights or thresholds change, the version bumps. Queries always group by engine_version implicitly. Non-negotiable — without it, long-window analysis silently mixes apples and oranges.

## 3. Storage architecture

**Backend:** SQLite at `/var/backtest/desk.db` with WAL mode for concurrent reads.

**Why SQLite, not Redis or Postgres:**
- Redis (current setup): 25 MB cap on free tier, LRU eviction. Forever-storage is not its job. Live state stays in Redis.
- Postgres: real database, but 90-day retention on Render's free tier is incompatible with forever-storage. Worth migrating to later if scale demands it.
- SQLite on persistent disk: free, durable, queryable, backed up by Render's volume snapshots. WAL mode handles concurrent writes from producer + dashboard daemons. Schema is portable to Postgres later via SQLAlchemy.

**Disk:** `/var/backtest/` is currently 5 GB (resized from 1 GB earlier today). Recorder volume estimate: ~7 MB/day, ~2.5 GB/year at full universe. Years of headroom.

## 4. Schema

Four tables. Schema migrations live as plain SQL files in `migrations/` and apply at startup.

### Table 1 — `alerts`

One row per alert that fired.

```sql
CREATE TABLE alerts (
    alert_id              TEXT PRIMARY KEY,        -- UUID v4
    fired_at              INTEGER NOT NULL,        -- microsecond UTC
    engine                TEXT NOT NULL,           -- 'long_call_burst' / 'v2_5d' / 'credit_v84' / 'oi_flow_conviction'
    engine_version        TEXT NOT NULL,           -- e.g. 'v2_5d@v8.4.2'
    ticker                TEXT NOT NULL,
    classification        TEXT,                    -- engine-specific: 'GRADE_A' / 'BURST_YES' / 'CREDIT_BULL_PUT' / 'CONVICTION_LONG_CALL'
    direction             TEXT,                    -- 'bull' / 'bear' / 'neutral'
    suggested_structure   TEXT,                    -- JSON: type, strikes, expiry, entry mark, etc.
    suggested_dte         INTEGER,                 -- target days-to-expiry
    spot_at_fire          REAL,
    canonical_snapshot    TEXT,                    -- JSON of full BotState envelope at fire time
    raw_engine_payload    TEXT,                    -- JSON of original webhook_data dict
    parent_alert_id       TEXT,                    -- nullable; for downstream cards (LCB / v8.4) linking back to V2 5D
    posted_to_telegram    INTEGER NOT NULL,        -- 0 or 1
    telegram_chat         TEXT,                    -- which channel it posted to
    suppression_reason    TEXT,                    -- if posted=0, why
    FOREIGN KEY (parent_alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX idx_alerts_fired_at ON alerts(fired_at);
CREATE INDEX idx_alerts_engine ON alerts(engine, engine_version);
CREATE INDEX idx_alerts_ticker ON alerts(ticker, fired_at);
CREATE INDEX idx_alerts_classification ON alerts(engine, classification, fired_at);
CREATE INDEX idx_alerts_parent ON alerts(parent_alert_id);
```

**Field notes:**

- **`alert_id`** — UUID v4, generated at fire time, stable forever.
- **`engine`** — fixed string per engine type. The four V1 engines: `long_call_burst`, `v2_5d`, `credit_v84`, `oi_flow_conviction`.
- **`engine_version`** — bumps on logic change. Format: `engine_name@version_string`.
- **`classification`** — the engine's output label. Examples: V2 5D produces `GRADE_A` / `GRADE_B` / `BLOCK`; long_call_burst produces `BURST_YES`; oi_flow produces `CONVICTION_LONG_CALL` / `CONVICTION_LONG_PUT`.
- **`suggested_structure`** — JSON describing what the alert recommended. Long call: `{"type":"long_call","strike":412.50,"expiry":"2026-05-15","entry_mark":2.85}`. Credit spread: `{"type":"bull_put","short":300,"long":295,"expiry":"2026-05-08","credit":0.85,"width":5}`.
- **`canonical_snapshot`** — full BotState envelope at fire time, JSON-encoded. **Records canonicals beyond what the engine consumed.** Lets future analysis ask "did this fire when dealer regime was X" even if the engine wasn't reading that field.
- **`raw_engine_payload`** — the original webhook_data dict. Forensic replay material.
- **`parent_alert_id`** — nullable. When a LONG CALL BURST or v8.4 CREDIT card fires downstream of a V2 5D evaluation, this links to the V2 5D's alert_id. Lets queries ask "of LONG CALL BURSTs that fired, what was the parent V2 5D grade?" If linkage isn't applicable (oi_flow conviction, standalone V2 5D card), null.
- **`posted_to_telegram` and `telegram_chat`** — every recorded alert reached Telegram in V1 (that's the scope), so `posted_to_telegram=1` always for V1. Field exists for V2+ when suppressed events get recorded too.

### Table 2 — `alert_features`

EAV (entity-attribute-value) for queryable features. One row per feature per alert.

```sql
CREATE TABLE alert_features (
    alert_id      TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    feature_value REAL,                            -- numeric features
    feature_text  TEXT,                            -- string features
    PRIMARY KEY (alert_id, feature_name),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX idx_features_name_value ON alert_features(feature_name, feature_value);
CREATE INDEX idx_features_name_text ON alert_features(feature_name, feature_text);
```

**What gets stored:** Every input the engine consumed PLUS every canonical field at fire time. Numeric features in `feature_value`; string features in `feature_text`.

Example rows for one V2 5D alert:
- `(alert_X, 'rsi', 47.3, NULL)`
- `(alert_X, 'adx', 22.1, NULL)`
- `(alert_X, 'macd_hist', -0.04, NULL)`
- `(alert_X, 'volume_ratio', 1.6, NULL)`
- `(alert_X, 'momentum_burst_score', 6, NULL)`
- `(alert_X, 'regime', NULL, 'BULL_BASE')`
- `(alert_X, 'dealer_regime', NULL, 'short_gamma_at_585')`
- `(alert_X, 'iv_state', NULL, 'normal')`
- `(alert_X, 'pb_state', NULL, 'in_box')`
- `(alert_X, 'cb_side', NULL, 'above_cb')`
- `(alert_X, 'time_of_day_minutes', 580, NULL)`
- `(alert_X, 'day_of_week', 1, NULL)`
- `(alert_X, 'days_to_fomc', 12, NULL)`

Roughly 30-50 feature rows per alert.

**Why EAV:** Different engines consume different inputs. Wide table with 100 nullable columns is messy and locks in features at design time. EAV adapts as new features are added (new canonical comes online → new feature rows on new alerts; old alerts simply don't have those rows). Indexes on `(feature_name, feature_value)` and `(feature_name, feature_text)` make filtering fast at any scale.

**Trade-off:** Slightly slower to assemble "all features for one alert" — requires N row joins. But that's the rare query. The common queries ("alerts where ADX > 25", "alerts where regime = BULL_BASE") are fast indexed lookups.

### Table 3 — `alert_price_track`

Continuous price track of the suggested structure after the alert fires. Substrate for "would it have won at horizon X" queries.

```sql
CREATE TABLE alert_price_track (
    alert_id              TEXT NOT NULL,
    elapsed_seconds       INTEGER NOT NULL,        -- seconds since fired_at
    sampled_at            INTEGER NOT NULL,        -- absolute UTC microseconds
    underlying_price      REAL,
    structure_mark        REAL,                    -- option mark or spread mark of suggested_structure
    structure_pnl_pct     REAL,                    -- vs. entry mark
    structure_pnl_abs     REAL,                    -- absolute $ per contract
    market_state          TEXT,                    -- 'rth' / 'pre' / 'post' / 'closed'
    PRIMARY KEY (alert_id, elapsed_seconds),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX idx_track_alert ON alert_price_track(alert_id, elapsed_seconds);
```

**Sampling cadence (variable by elapsed time):**
- 0-1 hour: every 1 minute (60 samples)
- 1-4 hours: every 5 minutes (36 samples)
- 4-24 hours: every 15 minutes (~80 samples)
- 1-7 days: every 30 minutes during RTH, hourly outside (~250 samples)
- 7+ days through expiry: every 1 hour during RTH only (~80 samples)

Total samples per alert through 1-week expiry: ~500. ~50 bytes per row → ~25 KB per alert.

**Tracking horizons by engine type:**
- LONG CALL BURST: 3 days max (intraday/short signal)
- V2 5D EDGE MODEL: 7 days
- v8.4 CREDIT: through expiry (suggested_dte days)
- CONVICTION PLAY: 5 days

After the horizon expires, the alert tracker daemon marks tracking complete and stops sampling.

**Implementation note — piggyback on existing polling:** `recommendation_tracker` already polls option marks for active positions (`update_tracking` at recommendation_tracker.py:649-690). The alert tracker can hook into the same poll cycle and write a price_track row when the poll completes naturally. Zero new Schwab REST calls. This is critical at scale — without it, 200 active tracks × 1-min cadence would blow the 110/min Schwab budget.

### Table 4 — `alert_outcomes`

Pre-computed outcomes at standard horizons. Derived from `alert_price_track`. Powers fast "win rate at horizon X" queries without scanning the full price track every time.

```sql
CREATE TABLE alert_outcomes (
    alert_id           TEXT NOT NULL,
    horizon            TEXT NOT NULL,           -- '5min'/'15min'/'30min'/'1h'/'4h'/'1d'/'2d'/'3d'/'5d'/'expiry'
    outcome_at         INTEGER,                 -- absolute UTC microseconds; NULL if horizon not yet reached
    underlying_price   REAL,
    structure_mark     REAL,
    pnl_pct            REAL,
    pnl_abs            REAL,
    hit_pt1            INTEGER DEFAULT 0,       -- did the structure hit PT1 ANYWHERE during the window before this horizon?
    hit_pt2            INTEGER DEFAULT 0,
    hit_pt3            INTEGER DEFAULT 0,
    max_favorable_pct  REAL,                    -- MFE during window from fire to this horizon
    max_adverse_pct    REAL,                    -- MAE during window
    PRIMARY KEY (alert_id, horizon),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX idx_outcomes_horizon ON alert_outcomes(horizon, pnl_pct);
```

**Computed by:** `outcome_computer_daemon` runs every 60s, looks for alerts whose price track has crossed a horizon boundary, computes the outcome, writes the row. Idempotent — re-running same alert recomputes same values.

**Critical: the `hit_pt1/pt2/pt3` flags reflect "did the path touch PT1 anywhere within the window."** This is the secret-sauce field. It enables Brad's actual question — "the trade got to +50% mid-day, would I have won if I'd exited then?" The `hit_pt1` flag at horizon=1d means "yes, somewhere in the first day, this did hit PT1." Then queries like "of credit spreads that hit PT1 within 1 day, what fraction ended up profitable at expiry?" become trivially answerable.

Standard horizons: 5min, 15min, 30min, 1h, 4h, 1d, 2d, 3d, 5d, expiry.

## 5. Implementation tasks (TDD per task)

Standard audit discipline: AST check after every file write, separate commits per task, `Patch G.N:` comment marker on every change, on/off env var defaulting OFF for full recorder enablement.

**G.1 — Schema migration framework + initial schema.** Create `migrations/` directory with sequentially-numbered SQL files. Boot-time migration runner. Tests: schema applies cleanly, idempotent, version table tracks applied migrations.

**G.2 — Alert recorder module (`alert_recorder.py`).** Pure write-side module. `record_alert(engine, payload, telegram_chat=...)` writes alert + features in one transaction. Wrapped in try/except — recorder failure never affects engine. Tests: round-trip read of recorded alert, malformed input handled gracefully, EAV expansion correct, parent_alert_id linkage correct.

**G.3 — Wire LONG CALL BURST.** First engine. Hook in `long_call_burst_builder.build_long_call_burst` so that after a card is built and posted, `record_alert` is called with engine=`long_call_burst`, the V2 5D parent's alert_id passed via `parent_alert_id`, full webhook_data as raw_engine_payload, structure JSON capturing strike/expiry/entry_mark. Behind `RECORDER_LCB_ENABLED` env var. Tests: alert appears in DB after card built, parent linkage correct, full feature inventory captured.

**G.4 — Wire V2 5D EDGE MODEL.** When v2_5d_edge_model.classify_v2_setup completes and a card is posted (whether 5D EDGE, MOMENTUM BURST, or VEHICLE REJECTED), call `record_alert(engine='v2_5d', ...)`. This alert is the parent for downstream LCB / credit cards. Returns alert_id so downstream callers can pass it as parent_alert_id. Tests: parent record created before downstream cards.

**G.5 — Wire v8.4 CREDIT.** Hook in `credit_card_builder.build_credit_card`. Same pattern as LCB. Records V2 5D parent_alert_id when applicable.

**G.6 — Wire CONVICTION PLAY.** Hook in oi_flow.py wherever the conviction card text is built (around line 2922). engine=`oi_flow_conviction`. No parent_alert_id (oi_flow is independent of V2 5D).

**G.7 — Alert tracker daemon (`alert_tracker_daemon.py`).** Daemon thread runs every 30s. For each active alert (within tracking horizon), checks if it's time to sample per cadence rules. Calls existing data infrastructure (recommendation_tracker's price polling, OptionQuoteStore for streaming marks where available) to get underlying spot and structure mark. Writes `alert_price_track` row. Behind `RECORDER_TRACKER_ENABLED` env var. Tests: cadence respected, horizon expiry stops tracking, daemon failure doesn't crash bot, no new Schwab REST calls beyond what already happens.

**G.8 — Outcome computer daemon (`outcome_computer_daemon.py`).** Runs every 60s. Looks for alerts whose price_track has crossed a horizon boundary. Computes pnl_pct, pnl_abs, hit_pt1/pt2/pt3, MFE, MAE for that horizon. Writes alert_outcomes row. Idempotent. Tests: horizon detection correct, MFE/MAE math correct, hit_pt logic respects "anywhere in window" semantics.

**G.9 — Engine versions auto-population.** Small startup hook: each engine writes its current version string to a small `engine_versions` lookup table at boot. Tests: version recorded, idempotent.

**G.10 — Verification queries.** 5-10 hand-written SQL queries against a few days of recorded data. These become the seed for Patch H's barometer dashboard. Examples:
- Win rate by engine at 1h, 4h, 1d, expiry
- LONG CALL BURST grade-A vs grade-B win rate (joining via parent_alert_id)
- v8.4 CREDIT win rate by regime (joining alert_features for regime)
- Distribution of MFE for each engine

## 6. Volume / disk math (full universe, V1 scope)

Based on actual main-channel Telegram export (4 engines, 7 days, full universe production):

| Engine | Per-day average |
|---|---|
| LONG CALL BURST | ~1/day |
| V2 5D EDGE MODEL (all card types) | ~7/day |
| v8.4 CREDIT | ~2/day |
| CONVICTION PLAY | ~0.6/day |
| **Total** | **~11/day** |

Storage with full schema (alerts + features + price_track + outcomes):

| Item | Per alert | Per day (~11) | Per year |
|---|---|---|---|
| `alerts` row | ~5 KB | 0.06 MB | 22 MB |
| `alert_features` (~50 features) | ~3 KB | 0.04 MB | 14 MB |
| `alert_price_track` | ~25 KB | 0.3 MB | 100 MB |
| `alert_outcomes` (10 horizons) | ~1 KB | 0.01 MB | 4 MB |
| **Total** | **~34 KB** | **~0.4 MB/day** | **~140 MB/year** |

**5 GB disk = ~35 years of V1 data.** Disk is a non-issue for V1.

If V2 expands to recording suppressed/internal events, volume goes up significantly. That's a V2 conversation.

## 7. Acceptance criteria

After Patch G ships:

1. All 4 engines write to the recorder when they fire.
2. Each alert has its full feature snapshot, canonical state, and starts a price track.
3. Price track samples respect cadence rules and use existing polling (no new Schwab REST budget consumption).
4. Outcomes auto-compute at standard horizons.
5. Recorder failure (DB locked, disk full, malformed payload) does NOT crash any engine. Try/except everywhere.
6. Verification SQL queries return real numbers from at least 24 hours of production data.
7. 78/78 existing tests still pass (no regressions in canonical, producer, or other suites).
8. New tests added for recorder modules — at least 30 new tests across G.1-G.9.

## 8. Out of scope for V1 (V2+ work)

- Recording suppressed/internal events that didn't reach Telegram (the `posted_to_telegram=0` cases)
- Took-trade tracking (linking Brad's portfolio entries to alert IDs)
- Campaigns, rolls, transformations, multi-signal justification
- Engine promotion logic (proven vs. unproven)
- Setup-level dedup with fire sub-rows (current design records each fire as its own alert; dedup is a V2 query-layer concern, not a schema concern)
- Exit gate (shadow / notify / trusted modes)
- Backtesting query layer for "what if I tweaked engine threshold X"

These are real, valuable features. None of them are V1. The V1 schema does not preclude any of them — they are purely additive, new tables that reference `alerts.alert_id`. Old V1 data stays valid forever.

## 9. Patch sequence after Patch G

```
Patch H:    Barometer dashboard — query layer + standard views    [no blast radius]
            "Win rate by engine, classification, regime, holding 
             horizon" as the day-one views. Reads recorder data,
             renders cards on a Market View page.

Patch I+:   Took-trade tracking (manual button + portfolio inference)
Patch J+:   Campaigns and transformations
Patch K+:   Multi-signal justification, mid-campaign confirmations
Patch L+:   Exit gate (shadow → notify → trusted)
```

Patch H is the deliverable that makes Patch G's data visible. Without H, the recorder is a silent database. With H, it's the lever-finding workbench.

---

**End of design spec. Ship it.**
