#!/usr/bin/env python3
"""
v8.4 fresh-start wipe — clears tracker state, local CSV logs, and (optionally)
Google Sheet data rows. Run this Sunday before Monday's deploy when you want
to start fresh.

USAGE — run from the bot's working directory on Render shell:

  # Dry-run first to see what WOULD be deleted (no changes):
  python3 wipe_for_fresh_start.py --dry-run

  # Then actually do it:
  python3 wipe_for_fresh_start.py

  # Skip Sheets if you'd rather clear those by hand:
  python3 wipe_for_fresh_start.py --no-sheets

  # Skip CSVs (e.g. you want to keep the historical logs even with bad data):
  python3 wipe_for_fresh_start.py --no-csvs

The script will refuse to run during US equity-options session hours (8:30 AM
- 3:00 PM CT, Mon-Fri) unless you pass --force.

Backups: every file deleted gets copied to ./wipe_backup_<date>/ first.
Sheet data is NOT auto-backed-up; export the tabs manually if you want a copy.
"""
from __future__ import annotations

import argparse
import os
import sys
import shutil
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Match recommendation_tracker.py
KEY_PREFIX = "omega:rec_tracker:"
DASHBOARD_KEY_PREFIX = "dashboard:close_245:"

# Files in BOT_LOG_DIR to clear. Headers regenerate automatically.
# One CSV per Omega 3000 tab. Verified against app.py _append_csv_row sites.
CSV_FILES = [
    "conviction_plays.csv",
    "scorer_decisions.csv",
    "signal_decisions.csv",
    "v2_peer_signals.csv",
    "em_predictions.csv",
    "em_reconciliation.csv",
    "shadow_signals.csv",
    "crisis_put_signals.csv",
    "scorer_suppressed_reposts.csv",
    "position_tracking_swing.csv",
    "position_tracking_income.csv",
    "position_tracking_conviction.csv",
    "position_tracking_shadow.csv",
]

# Sheets — tab names to clear (rows 2+, leave header alone).
# Verified against actual sheet 2026-05-02 — all 3 + 12 tabs.
DASHBOARD_3000_TABS = ["Dashboard", "Signal Log", "Position PnL"]
OMEGA_3000_TABS = [
    "conviction_plays",
    "scorer_decisions",
    "signal_decisions",
    "v2_peer_signals",
    "em_predictions",
    "em_reconciliation",
    "shadow_signals",
    "crisis_put_signals",
    "position_tracking_swing",
    "position_tracking_income",
    "position_tracking_conviction",
    "position_tracking_shadow",
]


def is_market_hours() -> bool:
    ct = datetime.now(ZoneInfo("America/Chicago"))
    if ct.weekday() >= 5:
        return False
    minutes = ct.hour * 60 + ct.minute
    return 510 <= minutes <= 900


def wipe_redis(dry_run: bool) -> int:
    """Delete every key under omega:rec_tracker:* and dashboard:close_245:*"""
    try:
        import redis
    except ImportError:
        print("  ! redis library not available — skipping Redis wipe")
        return 0
    url = os.getenv("REDIS_URL")
    if not url:
        print("  ! REDIS_URL not set — skipping Redis wipe")
        return 0
    try:
        r = redis.from_url(url, socket_timeout=5)
        r.ping()
    except Exception as e:
        print(f"  ! Redis connection failed: {e}")
        return 0

    deleted = 0
    for prefix in (KEY_PREFIX, DASHBOARD_KEY_PREFIX):
        # SCAN to avoid blocking
        cursor = 0
        keys = []
        while True:
            cursor, batch = r.scan(cursor=cursor, match=f"{prefix}*", count=500)
            keys.extend(batch)
            if cursor == 0:
                break
        if dry_run:
            print(f"  [DRY] {len(keys)} keys under {prefix}* would be deleted")
        else:
            for chunk in [keys[i:i+500] for i in range(0, len(keys), 500)]:
                if chunk:
                    r.delete(*chunk)
            print(f"  deleted {len(keys)} keys under {prefix}*")
        deleted += len(keys)
    return deleted


def wipe_csvs(dry_run: bool, backup_dir: str) -> int:
    log_dir = os.getenv("BOT_LOG_DIR", "/opt/render/project/src/bot_logs")
    if not os.path.isdir(log_dir):
        print(f"  ! BOT_LOG_DIR ({log_dir}) doesn't exist — skipping CSV wipe")
        return 0
    deleted = 0
    for fn in CSV_FILES:
        p = os.path.join(log_dir, fn)
        if not os.path.isfile(p):
            continue
        size = os.path.getsize(p)
        if dry_run:
            print(f"  [DRY] would delete {p} ({size} bytes)")
        else:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copy2(p, os.path.join(backup_dir, fn))
            os.remove(p)
            print(f"  deleted {p} ({size} bytes, backed up to {backup_dir}/)")
        deleted += 1
    return deleted


def _load_sa_inline() -> dict | None:
    """Inline equivalent of app._load_google_service_account.
    Importing app.py would trigger the bot's background threads to start —
    very bad to do from a maintenance script. Mirror the loader here so the
    wipe script is self-contained.
    """
    import json
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"  ! GOOGLE_SERVICE_ACCOUNT_JSON parse failed: {e}")
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    for p in (file_path, "/mnt/data/corbin-bot-tracking-0249b119c63f.json"):
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"  ! Failed to read SA file {p}: {e}")
    return None


def wipe_sheets(dry_run: bool) -> int:
    """Clear data rows on the named tabs of both Sheets, keeping the header row."""
    enabled = os.getenv("GOOGLE_SHEETS_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        print("  ! GOOGLE_SHEETS_ENABLE not set — skipping Sheets wipe")
        return 0
    dash_id = os.getenv("DASHBOARD_SHEET_ID", "").strip()
    omega_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not dash_id and not omega_id:
        print("  ! No DASHBOARD_SHEET_ID or GOOGLE_SHEET_ID set — skipping Sheets wipe")
        return 0
    sa = _load_sa_inline()

    # Build the list of (sheet_id, tab, label) we'd touch — independent of auth
    # so dry-run can print the plan even without valid creds.
    pairs = []
    if dash_id:
        for tab in DASHBOARD_3000_TABS:
            pairs.append((dash_id, tab, "Dashboard 3000"))
    if omega_id:
        for tab in OMEGA_3000_TABS:
            pairs.append((omega_id, tab, "Omega 3000"))

    if dry_run:
        print(f"  [DRY] would clear rows 2+ on {len(pairs)} tabs:")
        for _, tab, label in pairs:
            print(f"    - {label} → '{tab}'")
        if not sa:
            print("    (no SA creds — would also fail in real run)")
        return len(pairs)

    if not sa:
        print("  ! Google service account creds not loaded — skipping Sheets wipe")
        return 0
    try:
        import jwt
        import requests
        now = int(time.time())
        claim = {
            "iss": sa["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }
        assertion = jwt.encode(claim, sa["private_key"], algorithm="RS256")
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["access_token"]
    except Exception as e:
        print(f"  ! Could not get Sheets token: {e}")
        return 0

    cleared = 0
    for sheet_id, tab, label in pairs:
        rng = requests.utils.quote(f"{tab}!A2:Z", safe="!:")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}:clear"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                print(f"  cleared {label} → '{tab}' rows 2+")
                cleared += 1
            else:
                print(f"  ! {label} → '{tab}': {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            print(f"  ! {label} → '{tab}' clear failed: {e}")
    return cleared


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what WOULD happen without making changes")
    ap.add_argument("--no-redis", action="store_true")
    ap.add_argument("--no-csvs", action="store_true")
    ap.add_argument("--no-sheets", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run even during US equity-options session hours")
    args = ap.parse_args()

    if is_market_hours() and not args.force:
        print("REFUSING: US equity-options session hours (8:30-15:00 CT). "
              "Pass --force to override or wait for the close.")
        sys.exit(1)

    print(f"v8.4 fresh-start wipe — {datetime.now().isoformat()} "
          f"({'DRY RUN' if args.dry_run else 'LIVE'})")
    print()

    backup_dir = os.path.join(
        os.getcwd(),
        f"wipe_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )

    if not args.no_redis:
        print("Redis (recommendation tracker storage):")
        wipe_redis(args.dry_run)
        print()

    if not args.no_csvs:
        print("Local CSVs in BOT_LOG_DIR:")
        wipe_csvs(args.dry_run, backup_dir)
        print()

    if not args.no_sheets:
        print("Google Sheets data rows (header rows preserved):")
        wipe_sheets(args.dry_run)
        print()

    if args.dry_run:
        print("Dry run done. Re-run without --dry-run to execute.")
    else:
        print("Wipe complete. On next bot tick:")
        print("  - Redis tracker rebuilds from the next recorded recommendation.")
        print("  - CSVs regenerate with header rows on first append.")
        print("  - Sheets headers stay intact; data rebuilds on first dashboard tick.")
        print(f"  - CSV backups (if any) are in {backup_dir}/")


if __name__ == "__main__":
    main()
