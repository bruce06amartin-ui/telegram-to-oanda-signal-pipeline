"""Every number the bot uses, in one place. Frozen before the practice run started (see docs/DESIGN.md). Changing any of these is a deliberate, dated decision."""
INSTRUMENT = "XAU_USD"
RISK_PCT = 1.0           # percent of NAV risked per signal (at the actual units placed)
DAY_CAP_PCT = 3.0        # percent of NAV; total risk of orders placed per UTC day
MAX_OPEN = 5             # open plus pending orders at once
EXPIRY_HOURS = 4         # unfilled limit order is cancelled after this long
TP_INDEX = 2             # the third numbered take-profit (TP3), zero-based
CORRECTION_WINDOW_MIN = 30
MARGIN_USE_CAP = 0.8     # never use more than this share of free margin on one order
DEFAULT_MARGIN_RATE = 0.05   # UNVERIFIED default; the bot reads the real one from OANDA when it has a token
MAX_AGE_MIN = 10        # a message older than this when we see it is ignored (restart catch-up, replayed backlog). Added 2026-10-13, before any order.
