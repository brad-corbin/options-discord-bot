# scheduled_reports.py
# ═══════════════════════════════════════════════════════════════════
# Scheduled Telegram Report Posting
# NOTE: Educational/demo code. Not financial advice. Use at your own risk.
#
# Wraps the recommendation_tracker report functions into time-scheduled
# Telegram posts. Wire into your existing scheduler (threading.Timer,
# apscheduler, schedule module — whatever you already have).
#
# Default schedule:
#   08:00 AM CT weekdays  →  Morning Briefing
#   03:15 PM CT weekdays  →  End-of-Day Recresults
#   03:30 PM CT Fridays   →  Weekly Digest (recweek + shadowedge)
#   09:00 AM CT 1st of month →  Monthly Attribution
#
# All posts use reply_long() so they auto-split at Telegram's 4096 limit.
# ═══════════════════════════════════════════════════════════════════

import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# MORNING BRIEFING (08:00 CT)
# ─────────────────────────────────────────────────────────

def post_morning_briefing(
    post_fn: Callable,
    rec_tracker_store,
    greeks_summary_fn: Optional[Callable] = None,
    regime_fn: Optional[Callable] = None,
    earnings_check_fn: Optional[Callable] = None,
) -> None:
    """Post the morning briefing to Telegram.

    Args:
        post_fn: Telegram post callable (will be passed one string per chunk).
        rec_tracker_store: the recommendation tracker's store.
        greeks_summary_fn: optional () -> str that returns the book Greeks
                            report (from omega_small_fixes.format_book_greeks_report).
        regime_fn: optional () -> dict with {label, vix} for the current regime.
        earnings_check_fn: optional () -> list[str] of tickers with earnings today
                            that you have open positions on.
    """
    from recommendation_tracker import (
        generate_open_positions_report, reply_long,
    )

    lines = [f"☕ BOT IDEA BRIEFING — {datetime.now().strftime('%Y-%m-%d %A')}"]
    lines.append("━" * 44)
    lines.append("Diagnostic only unless RECOMMENDATION_REPORTS_MAIN_ENABLED=1.")
    lines.append("")

    # Regime context
    if regime_fn:
        try:
            r = regime_fn()
            lines.append(f"Regime:    {r.get('label', '?')}")
            lines.append(f"VIX:       {r.get('vix', '?')}")
            lines.append("")
        except Exception as e:
            log.warning(f"Briefing regime_fn failed: {e}")

    # Earnings warnings
    if earnings_check_fn:
        try:
            tickers_with_earnings = earnings_check_fn()
            if tickers_with_earnings:
                lines.append("⚠️  EARNINGS TODAY IN OPEN POSITIONS:")
                for t in tickers_with_earnings:
                    lines.append(f"   • {t}")
                lines.append("")
        except Exception as e:
            log.warning(f"Briefing earnings_check_fn failed: {e}")

    # Book Greeks
    if greeks_summary_fn:
        try:
            g = greeks_summary_fn()
            if g:
                lines.append(g)
                lines.append("")
        except Exception as e:
            log.warning(f"Briefing greeks_summary_fn failed: {e}")

    # Open positions from tracker
    try:
        lines.append(generate_open_positions_report(rec_tracker_store))
    except Exception as e:
        log.warning(f"Briefing open positions failed: {e}")
        lines.append("(open positions unavailable)")

    reply_long(post_fn, "\n".join(lines))


# ─────────────────────────────────────────────────────────
# END OF DAY (15:15 CT)
# ─────────────────────────────────────────────────────────

def post_eod_report(
    post_fn: Callable,
    rec_tracker_store,
    date_str: Optional[str] = None,
) -> None:
    """Post end-of-day recresults for the given date (default today)."""
    from recommendation_tracker import generate_daily_report, reply_long
    try:
        report = generate_daily_report(rec_tracker_store, date_str=date_str)
        reply_long(post_fn, report)
    except Exception as e:
        log.error(f"EOD report failed: {e}")
        post_fn(f"⚠️ EOD report error: {e}")


# ─────────────────────────────────────────────────────────
# WEEKLY DIGEST (Friday 15:30 CT)
# ─────────────────────────────────────────────────────────

def post_weekly_digest(
    post_fn: Callable,
    rec_tracker_store,
) -> None:
    """Post weekly bot idea summary + diagnostic shadow edge analysis."""
    from recommendation_tracker import (
        generate_weekly_summary,
        analyze_shadow_edge_from_campaigns,
        format_shadow_edge_report,
        reply_long,
    )
    try:
        summary = generate_weekly_summary(rec_tracker_store, days=7)
        reply_long(post_fn, summary)
    except Exception as e:
        log.error(f"Weekly summary failed: {e}")
        post_fn(f"⚠️ Weekly summary error: {e}")

    try:
        analysis = analyze_shadow_edge_from_campaigns(
            rec_tracker_store, lookback_days=30,
        )
        edge_report = format_shadow_edge_report(analysis)
        reply_long(post_fn, edge_report)
    except Exception as e:
        log.error(f"Shadow edge report failed: {e}")
        # Don't surface this error — shadow analysis isn't critical


# ─────────────────────────────────────────────────────────
# MONTHLY ATTRIBUTION (1st of month, 09:00 CT)
# ─────────────────────────────────────────────────────────

def post_monthly_attribution(
    post_fn: Callable,
    rec_tracker_store,
) -> None:
    """Post 30-day summary + full shadow edge + monthly review prompt."""
    from recommendation_tracker import (
        generate_weekly_summary,   # works for any N days
        analyze_shadow_edge_from_campaigns,
        format_shadow_edge_report,
        reply_long,
    )

    lines = ["📅 MONTHLY BOT IDEA ATTRIBUTION"]
    lines.append(f"Period ending {datetime.now().strftime('%Y-%m-%d')}")
    lines.append("━" * 44)
    lines.append("")

    try:
        monthly = generate_weekly_summary(rec_tracker_store, days=30)
        lines.append(monthly)
    except Exception as e:
        log.error(f"Monthly summary failed: {e}")
        lines.append(f"⚠️ Monthly summary error: {e}")

    reply_long(post_fn, "\n".join(lines))

    try:
        analysis = analyze_shadow_edge_from_campaigns(
            rec_tracker_store, lookback_days=30,
        )
        edge_report = format_shadow_edge_report(analysis)
        reply_long(post_fn, edge_report)
    except Exception as e:
        log.error(f"Monthly shadow edge failed: {e}")

    # Monthly review checklist
    review_prompt = (
        "📋 MONTHLY REVIEW CHECKLIST\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Take 30 minutes this Saturday to:\n"
        "  1. Evaluate each trade type — is it profitable net of slippage?\n"
        "  2. Any trade type with PF < 0.9 for 2+ months — suspend it?\n"
        "  3. Any shadow signal flagged GO LIVE for 2+ months — flip it?\n"
        "  4. Any confidence bucket bleeding? → raise the floor\n"
        "  5. Any source module underperforming? → tighten its gates\n"
        "Make ONE change per month. Measure the impact before the next change."
    )
    post_fn(review_prompt)


# ─────────────────────────────────────────────────────────
# SCHEDULER WIRING (reference implementation)
# ─────────────────────────────────────────────────────────
#
# Below is a reference implementation using plain threading.Timer.
# If you use apscheduler / schedule / celery, adapt this pattern.
#
# def _is_weekday(dt):
#     return dt.weekday() < 5
#
# def _seconds_until(hour_ct, minute_ct, now=None):
#     """Seconds from now until next occurrence of HH:MM CT."""
#     now = now or datetime.now(timezone.utc)
#     # Approximate CT: UTC-5 (standard, adjust for DST if needed)
#     ct = now - timedelta(hours=5)
#     target = ct.replace(hour=hour_ct, minute=minute_ct, second=0, microsecond=0)
#     if target <= ct:
#         target += timedelta(days=1)
#     delta = target - ct
#     return delta.total_seconds()
#
# def _schedule_daily(fn, hour_ct, minute_ct, weekday_only=True):
#     """Recursively schedule fn to run at HH:MM CT every day."""
#     import threading
#     def _run():
#         now_ct = datetime.now(timezone.utc) - timedelta(hours=5)
#         if not weekday_only or now_ct.weekday() < 5:
#             try:
#                 fn()
#             except Exception as e:
#                 log.error(f"Scheduled task {fn.__name__} failed: {e}")
#         # Reschedule for tomorrow
#         threading.Timer(_seconds_until(hour_ct, minute_ct), _run).start()
#     threading.Timer(_seconds_until(hour_ct, minute_ct), _run).start()
#
# # In app.py _initialize_app() after _rec_tracker is built:
#
# from scheduled_reports import (
#     post_morning_briefing, post_eod_report,
#     post_weekly_digest, post_monthly_attribution,
# )
# from omega_small_fixes import summarize_book_greeks, format_book_greeks_report
#
# def _morning():
#     post_morning_briefing(
#         post_fn=post_to_telegram,
#         rec_tracker_store=_rec_tracker,
#         greeks_summary_fn=lambda: format_book_greeks_report(
#             summarize_book_greeks(_portfolio.list_open_positions())
#         ),
#         regime_fn=lambda: _regime_detector.get_regime_package(),
#         earnings_check_fn=_check_open_earnings,
#     )
#
# def _eod():
#     post_eod_report(post_to_telegram, _rec_tracker)
#
# def _weekly():
#     now_ct = datetime.now(timezone.utc) - timedelta(hours=5)
#     if now_ct.weekday() != 4:  # Friday only
#         return
#     post_weekly_digest(post_to_telegram, _rec_tracker)
#
# def _monthly():
#     now_ct = datetime.now(timezone.utc) - timedelta(hours=5)
#     if now_ct.day != 1:  # 1st of month only
#         return
#     post_monthly_attribution(post_to_telegram, _rec_tracker)
#
# _schedule_daily(_morning, 8, 0)
# _schedule_daily(_eod, 15, 15)
# _schedule_daily(_weekly, 15, 30, weekday_only=True)  # filters to Friday inside _weekly
# _schedule_daily(_monthly, 9, 0, weekday_only=False)  # filters to 1st inside _monthly
