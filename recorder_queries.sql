-- v11.7 (Patch G.10): Verification queries for the V1 alert recorder.
-- These are the queries Patch H's barometer dashboard reads. They run
-- against /var/backtest/desk.db.
-- Run any of these directly: sqlite3 /var/backtest/desk.db < <query.sql>

-- Q1. Win rate by engine at the 1h horizon (last 24h).
-- "Of alerts that fired in the past 24h, what fraction had pnl_pct > 0
-- at the 1-hour horizon?"
SELECT
    a.engine,
    a.engine_version,
    COUNT(*)                            AS n_alerts,
    SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END) AS wins,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS win_rate_pct,
    ROUND(AVG(o.pnl_pct), 2)            AS avg_pnl_pct
FROM alerts a
LEFT JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = '1h'
WHERE a.fired_at > (strftime('%s', 'now') - 86400) * 1000000
GROUP BY a.engine, a.engine_version
ORDER BY win_rate_pct DESC;

-- Q2. Win rate by engine at every standard horizon (last 7 days).
SELECT
    a.engine,
    o.horizon,
    COUNT(*)                            AS n,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS win_rate_pct
FROM alerts a
JOIN alert_outcomes o ON o.alert_id = a.alert_id
WHERE a.fired_at > (strftime('%s', 'now') - 7 * 86400) * 1000000
GROUP BY a.engine, o.horizon
ORDER BY a.engine,
    CASE o.horizon
        WHEN '5min' THEN 1 WHEN '15min' THEN 2 WHEN '30min' THEN 3
        WHEN '1h' THEN 4 WHEN '4h' THEN 5 WHEN '1d' THEN 6
        WHEN '2d' THEN 7 WHEN '3d' THEN 8 WHEN '5d' THEN 9
        WHEN 'expiry' THEN 10 ELSE 99
    END;

-- Q3. LONG CALL BURST: grade-A vs grade-B win rate (joining via parent
-- V2 5D alert).
SELECT
    pf.feature_text                     AS parent_grade,
    COUNT(*)                            AS n_lcb_alerts,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS lcb_win_rate_at_1h
FROM alerts a
JOIN alerts parent ON parent.alert_id = a.parent_alert_id
JOIN alert_features pf
    ON pf.alert_id = parent.alert_id AND pf.feature_name = 'v2_5d_grade'
JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = '1h'
WHERE a.engine = 'long_call_burst'
GROUP BY pf.feature_text
ORDER BY lcb_win_rate_at_1h DESC;

-- Q4. v8.4 CREDIT win rate by regime (joining via alert_features).
SELECT
    f.feature_text                      AS regime,
    COUNT(*)                            AS n,
    ROUND(100.0 * SUM(CASE WHEN o.pnl_pct > 0 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0), 1)  AS win_rate_at_expiry
FROM alerts a
JOIN alert_features f
    ON f.alert_id = a.alert_id AND f.feature_name = 'regime'
JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = 'expiry'
WHERE a.engine = 'credit_v84'
GROUP BY f.feature_text
ORDER BY win_rate_at_expiry DESC;

-- Q5. MFE distribution by engine at 1d (does the engine produce winners
-- that just need exit discipline?).
SELECT
    a.engine,
    COUNT(*)                            AS n,
    ROUND(AVG(o.max_favorable_pct), 1)  AS avg_mfe_pct,
    ROUND(MAX(o.max_favorable_pct), 1)  AS max_mfe_pct,
    ROUND(MIN(o.max_favorable_pct), 1)  AS min_mfe_pct
FROM alerts a
JOIN alert_outcomes o
    ON o.alert_id = a.alert_id AND o.horizon = '1d'
GROUP BY a.engine
ORDER BY avg_mfe_pct DESC;

-- Q6. Conditional win rate: "if it hit PT1 within 1h, did it win at expiry?"
SELECT
    a.engine,
    SUM(CASE WHEN o1h.hit_pt1 = 1 THEN 1 ELSE 0 END) AS hit_pt1_in_1h,
    SUM(CASE WHEN o1h.hit_pt1 = 1 AND ox.pnl_pct > 0 THEN 1 ELSE 0 END) AS won_at_expiry,
    ROUND(100.0 *
        SUM(CASE WHEN o1h.hit_pt1 = 1 AND ox.pnl_pct > 0 THEN 1 ELSE 0 END)
      / NULLIF(SUM(CASE WHEN o1h.hit_pt1 = 1 THEN 1 ELSE 0 END), 0), 1) AS conditional_win_rate
FROM alerts a
JOIN alert_outcomes o1h
    ON o1h.alert_id = a.alert_id AND o1h.horizon = '1h'
JOIN alert_outcomes ox
    ON ox.alert_id = a.alert_id AND ox.horizon = 'expiry'
GROUP BY a.engine;

-- Q7. Daily alert volume by engine (operational health check).
SELECT
    DATE(a.fired_at / 1000000, 'unixepoch') AS day,
    a.engine,
    COUNT(*)                            AS n
FROM alerts a
WHERE a.fired_at > (strftime('%s', 'now') - 30 * 86400) * 1000000
GROUP BY day, a.engine
ORDER BY day DESC, a.engine;

-- Q8. Engines currently active (any alert in past 24h).
SELECT
    a.engine,
    a.engine_version,
    MAX(a.fired_at) AS last_fired_at
FROM alerts a
WHERE a.fired_at > (strftime('%s', 'now') - 86400) * 1000000
GROUP BY a.engine, a.engine_version
ORDER BY last_fired_at DESC;

-- Q9. Recorder telemetry: rows per table.
SELECT 'alerts'             AS tbl, COUNT(*) AS rows FROM alerts
UNION ALL
SELECT 'alert_features',          COUNT(*) FROM alert_features
UNION ALL
SELECT 'alert_price_track',       COUNT(*) FROM alert_price_track
UNION ALL
SELECT 'alert_outcomes',          COUNT(*) FROM alert_outcomes
UNION ALL
SELECT 'engine_versions',         COUNT(*) FROM engine_versions
UNION ALL
SELECT 'schema_migrations',       COUNT(*) FROM schema_migrations;

-- Q10. Latest 10 alerts (forensic / "what just fired?" check).
SELECT
    DATETIME(a.fired_at / 1000000, 'unixepoch') AS fired,
    a.engine, a.ticker, a.classification, a.direction, a.spot_at_fire
FROM alerts a
ORDER BY a.fired_at DESC
LIMIT 10;
