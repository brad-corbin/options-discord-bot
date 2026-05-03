# Phase 3 — Read-only Views

Live data on Command Center and Trading view. Built on Phase 1 + Phase 2.

## What ships in this phase

**Command Center** (`/dashboard`):
- Income hero — year + month realized income from closed/expired/assigned/rolled options
- Goal pace bar — average of completed-month income, current month progress
- Status strip (7 cells) — Open positions / Wheel / Spreads / Intraday / Holdings / API credits / Scanner
- Open positions panel — grouped by Intraday → Wheel → Spreads → Shares; live spot + P/L on shares
- Today's alerts feed — pulled from scanner's recent signals
- Holdings sentiment panel — bullish/neutral/bearish buckets + 4-cell footer with portfolio P&L
- Capital progression — placeholder until Phase 4 starts tracking starting balances

**Trading View** (`/trading`):
- Live alerts feed — full width, fresh row highlighted with accent border
- Watch map grid (3 columns) — one card per active ticker (tier_a + active trades + pulled)
- Each card: ticker / spot / bias / GEX / regime tags / above-spot levels / spot row / below-spot levels / triggers / active trade banner
- Quick-pull form to add any ticker on demand
- Pulled tickers persist via cookie, dismissible individually with `×`, "Clear pulls" when 3+

**System status header** (across all pages):
- Scanner LIVE / PAUSED dot
- VIX + ADX regime tag
- API credit usage
- Active account label

## File drop

Replace your existing `dashboard/` folder. Three new things vs Phase 2:

```
your-bot/
└── dashboard/
    ├── __init__.py            (unchanged)
    ├── routes.py              ← updated (live data routes + pull/dismiss/clear)
    ├── durability.py          (unchanged)
    ├── scheduler.py           (unchanged)
    ├── data.py                ← NEW: read-only data layer
    ├── templates/dashboard/
    │   ├── base.html          ← updated (live system-status header)
    │   ├── login.html         (unchanged)
    │   ├── command_center.html  ← rewritten (live data)
    │   ├── trading.html       ← rewritten (watch map + alerts)
    │   ├── portfolio.html     (unchanged — Phase 4 territory)
    │   ├── diagnostic.html    (unchanged — Phase 6 territory)
    │   └── restore.html       (unchanged)
    └── static/
        └── omega.css          ← updated (Phase 3 panel/grid styles appended)
```

## No `app.py` changes needed

Same as Phase 2 — the data layer late-binds to `app.py` internals. Drop and go.

## What's reading from where

The data layer (`dashboard/data.py`) reads from:

- `portfolio.py` — holdings, options, spreads, cash; P/L calcs
- `app._cached_md` — spot prices (with 30-second per-ticker cache to avoid hammering the API)
- `app._scanner` — watchlist, regime label, recent signals
- `app._thesis_engine` (or `_thesis_monitor_engine`) — ThesisContext + MonitorState for watch map cards
- `options_map` — `build_watch_levels` / `build_watch_triggers` for the level structures on each card
- `sentiment_report` — per-ticker sentiment for the holdings buckets (if loaded)

If any of those modules aren't loaded yet (e.g. cold start), the page degrades cleanly — empty panels with helpful messages, never broken.

## Account model

Same UI accounts as Phase 1, mapped to underlying portfolio keys:

| UI view | Underlying | Status today |
|---|---|---|
| Combined | brad + mom | Live data |
| Mine | brad | Live data |
| Mom | mom | Live data |
| Partnership | (none yet) | Placeholder until Phase 4 |
| Kyleigh | (none yet) | Placeholder until Phase 4 |

When you switch to Partnership or Kyleigh today, you'll see the "No data yet" placeholder with a note. Phase 4 introduces those account keys when you start entering Day Trades and Kyleigh transfers.

## Spot price strategy

Every page render needs spot prices for share P/L and watch map cards. To avoid 15-30 fresh API calls per page load:

- Each ticker has a 30-second TTL in an in-memory cache in `data.py`
- The first request per ticker hits `_cached_md.get_spot()` (which itself caches)
- Subsequent requests within 30 seconds reuse the cached value
- After 30 seconds, the next page render refreshes that ticker

For a typical 10-ticker portfolio, refreshing the dashboard repeatedly costs 0 fresh API calls within the 30s window. The Command Center is safe to leave open and refresh at will.

## What's not in this phase (deliberately)

Held strictly to scope:

- Charts — out of scope (you have TradingView)
- Capital progression bars — needs starting balance tracking from Phase 4
- True ROC / goal projections — needs starting balance from Phase 4
- Position entry / edits / closes — Phase 4
- Holdings digest Telegram — Phase 5
- Diagnostic shadow signals / category P&L — Phase 6
- Real-time auto-refresh — phase 7+ (page refresh is the model for now)
- Time-travel through snapshots — out of scope

## Verify after deploy

Hit health:
```
https://options-discord-bot.onrender.com/dashboard/health
```

Should report `"phase": 3` and the durability flags from Phase 2 still all `true`.

Then log in and check:

1. **Header status** should show real values: scanner LIVE/PAUSED, regime tag with VIX/ADX, API credits used. If all show "—" or "OFFLINE", the bot's globals aren't populated yet (cold start) — give it a minute and refresh.

2. **Command Center** for the Combined or Mine view: income year + month should show realized $$ from your closed wheel options. Open positions section shows your current open contracts.

3. **Trading view**: watch map grid should populate from scanner's tier_a + any active trades. The quick-pull box accepts any ticker — try `SPY` and watch a card render with levels and triggers.

4. **Account switching**: click any account chip, see the accent color shift and the data filter to that account.

If something looks empty that you expected to have data, check the bot's logs — usually the answer is "scanner hasn't run yet" or "thesis monitor hasn't loaded that ticker yet". The dashboard reads what the bot already computes; it doesn't compute anything new.

## What's next

**Phase 4 — Portfolio Writes.** This is the big one. Manual entry forms for:
- Cash deposits + withdrawals (starting balances per account)
- Holdings (shares + cost basis with auto-tagging)
- Options (CSP / CC / Long / Spread legs)
- Rolls (single net-credit/debit event)
- Kyleigh transfers (`/transfertokyleigh $X from {acct}`)
- Campaign rollup at assignment (checkboxes for which CSPs fold into adjusted basis)

Every write goes through the audit log built in Phase 2. Once Phase 4 ships, the dashboard becomes truly self-sufficient — no more manual spreadsheet entry, the bot's data model becomes the source of truth.
