--- omega_dashboard/routes.py — Patch 11.3 instructions

Two edits, both small. Apply via str_replace patterns to match anchors in
the actual file.

═══════════════════════════════════════════════════════════════════════
EDIT 1 — Rename Diagnostic → Research in PAGE_TABS
═══════════════════════════════════════════════════════════════════════

ANCHOR (must match exactly, currently around line 71):

    {"key": "diagnostic", "label": "Diagnostic",   "endpoint": "dashboard.diagnostic"},

REPLACE WITH:

    {"key": "research",   "label": "Research",     "endpoint": "dashboard.research"},

═══════════════════════════════════════════════════════════════════════
EDIT 2 — Replace the diagnostic route handler with the research route
═══════════════════════════════════════════════════════════════════════

ANCHOR (currently around line 1492):

    @dashboard_bp.route("/diagnostic", methods=["GET"])
    @login_required
    def diagnostic():
        return render_page("dashboard/diagnostic.html", page_key="diagnostic")

REPLACE WITH:

    # Patch 11.3 — Diagnostic renamed to Research, now data-driven.
    @dashboard_bp.route("/research", methods=["GET"])
    @login_required
    def research():
        """Research tab — rebuild progress + per-ticker BotState.

        Shows what's been migrated to the canonical rebuild and the live
        state of each ticker through the new compute path. As canonical
        functions land in subsequent patches, more fields go from
        'pending' to lit values automatically.
        """
        try:
            from . import research_data
        except ImportError as e:
            # Module not yet deployed — render the page with no data
            log.warning("research_data module not available: %s", e)
            return render_page(
                "dashboard/research.html",
                page_key="research",
                page_data=_empty_research_payload(str(e)),
            )

        # Get the bot's data router. The exact attribute name may vary
        # depending on where the bot's _cached_md is exposed; this tries
        # the conventional places. If none match, page falls back to
        # 'unavailable' state — still renders cleanly.
        data_router = _get_bot_data_router()
        payload = research_data.research_data(data_router=data_router)

        return render_page(
            "dashboard/research.html",
            page_key="research",
            page_data=payload,
        )


    @dashboard_bp.route("/research/data", methods=["GET"])
    @login_required
    def research_data_json():
        """JSON feed for any future polling JS on the Research page.
        Right now the page server-renders on each visit (60s in-memory
        cache in research_data.py keeps Schwab cost flat)."""
        from flask import jsonify
        try:
            from . import research_data as rd
        except ImportError:
            return jsonify({"available": False, "error": "research_data unavailable"}), 503

        data_router = _get_bot_data_router()
        payload = rd.research_data(data_router=data_router)
        # Convert dataclass to dict for JSON
        from dataclasses import asdict
        resp = jsonify(asdict(payload) if hasattr(payload, '__dataclass_fields__') else payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp


    def _get_bot_data_router():
        """Locate the bot's DataRouter instance.

        The bot exposes it as `_cached_md` in app.py, set up by
        `build_data_router()`. We import it lazily to avoid module-load
        circular dependencies. Returns None if not available — caller
        renders a graceful 'unavailable' state.
        """
        try:
            import app
            return getattr(app, '_cached_md', None)
        except Exception as e:
            log.warning("Could not locate bot data_router: %s", e)
            return None


    def _empty_research_payload(error_msg: str):
        """Fallback payload when research_data module is unavailable."""
        from datetime import datetime, timezone
        from types import SimpleNamespace
        return SimpleNamespace(
            fetched_at_utc=datetime.now(timezone.utc),
            tickers_total=0,
            tickers_with_data=0,
            tickers_errored=0,
            fields_lit_avg=0.0,
            fields_total=0,
            canonical_status_summary={},
            snapshots=[],
            available=False,
            error=error_msg,
        )

═══════════════════════════════════════════════════════════════════════
EDIT 3 — Optional: keep /diagnostic alive as a redirect
═══════════════════════════════════════════════════════════════════════

If anyone has bookmarks or external links to /diagnostic, add right
after the new research routes:

    # Backward-compat: old /diagnostic URL → /research
    @dashboard_bp.route("/diagnostic", methods=["GET"])
    @login_required
    def diagnostic_redirect():
        return redirect(url_for("dashboard.research"))

Remove after a release or two once external links have updated.

═══════════════════════════════════════════════════════════════════════
DEPLOYMENT
═══════════════════════════════════════════════════════════════════════

1. Drop these new files into the repo:
     omega_dashboard/research_data.py
     omega_dashboard/templates/dashboard/research.html
     bot_state.py
     raw_inputs.py
     canonical_gamma_flip.py

2. Append the CSS additions in research_page_styles.css to the end of
     omega_dashboard/static/omega.css

3. Apply the routes.py edits above.

4. Restart Render. The Research tab in the top nav now points to the
   new page. Old /diagnostic URL redirects (if Edit 3 applied) or 404s.

5. Verify by logging in and clicking Research. You should see:
     - top: REBUILD PROGRESS + canonical compute status grid
     - middle: per-ticker BotState cards (gamma_flip lit, others pending)
     - bottom: REBUILD ROADMAP

If the data layer is unavailable (data_router missing), the top section
shows the error and the per-ticker grid is hidden, but the page still
renders with the roadmap visible.
