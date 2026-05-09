-- v11.7 (Patch G.1): Alert recorder V1 — initial schema.
-- See docs/superpowers/specs/2026-05-08-alert-recorder-v1.md sections 4 & 6.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id              TEXT PRIMARY KEY,
    fired_at              INTEGER NOT NULL,
    engine                TEXT NOT NULL,
    engine_version        TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    classification        TEXT,
    direction             TEXT,
    suggested_structure   TEXT,
    suggested_dte         INTEGER,
    spot_at_fire          REAL,
    canonical_snapshot    TEXT,
    raw_engine_payload    TEXT,
    parent_alert_id       TEXT,
    posted_to_telegram    INTEGER NOT NULL,
    telegram_chat         TEXT,
    suppression_reason    TEXT,
    FOREIGN KEY (parent_alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_fired_at ON alerts(fired_at);
CREATE INDEX IF NOT EXISTS idx_alerts_engine ON alerts(engine, engine_version);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker, fired_at);
CREATE INDEX IF NOT EXISTS idx_alerts_classification ON alerts(engine, classification, fired_at);
CREATE INDEX IF NOT EXISTS idx_alerts_parent ON alerts(parent_alert_id);

CREATE TABLE IF NOT EXISTS alert_features (
    alert_id      TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    feature_value REAL,
    feature_text  TEXT,
    PRIMARY KEY (alert_id, feature_name),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_features_name_value ON alert_features(feature_name, feature_value);
CREATE INDEX IF NOT EXISTS idx_features_name_text ON alert_features(feature_name, feature_text);

CREATE TABLE IF NOT EXISTS alert_price_track (
    alert_id              TEXT NOT NULL,
    elapsed_seconds       INTEGER NOT NULL,
    sampled_at            INTEGER NOT NULL,
    underlying_price      REAL,
    structure_mark        REAL,
    structure_pnl_pct     REAL,
    structure_pnl_abs     REAL,
    market_state          TEXT,
    PRIMARY KEY (alert_id, elapsed_seconds),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_track_alert ON alert_price_track(alert_id, elapsed_seconds);

CREATE TABLE IF NOT EXISTS alert_outcomes (
    alert_id           TEXT NOT NULL,
    horizon            TEXT NOT NULL,
    outcome_at         INTEGER,
    underlying_price   REAL,
    structure_mark     REAL,
    pnl_pct            REAL,
    pnl_abs            REAL,
    hit_pt1            INTEGER DEFAULT 0,
    hit_pt2            INTEGER DEFAULT 0,
    hit_pt3            INTEGER DEFAULT 0,
    max_favorable_pct  REAL,
    max_adverse_pct    REAL,
    PRIMARY KEY (alert_id, horizon),
    FOREIGN KEY (alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_horizon ON alert_outcomes(horizon, pnl_pct);

CREATE TABLE IF NOT EXISTS engine_versions (
    engine          TEXT PRIMARY KEY,
    engine_version  TEXT NOT NULL,
    recorded_at     INTEGER NOT NULL
);
