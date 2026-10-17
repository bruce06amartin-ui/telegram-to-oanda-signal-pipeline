"""Offline demo: feeds SYNTHETIC signals to the bot in dry-run mode. No network, no tokens, nothing is ordered.

    python examples/demo_dry_run.py

Writes ./demo_out/decisions.jsonl (one JSON line per decision) and prints it. Create ./demo_out/KILL and run again to see the kill switch.
"""
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oanda_signals.bot import Bot
from oanda_signals.client import Oanda

out = "demo_out"
os.makedirs(out, exist_ok=True)
log = os.path.join(out, "decisions.jsonl")
for f in (log, os.path.join(out, "bot_state.json")):
    if os.path.exists(f):
        os.remove(f)

client = Oanda(token=None, account=None, dry_run=True)          # no credentials: made-up account figures, dry run
bot = Bot(client, os.path.join(out, "bot_state.json"), log, os.path.join(out, "KILL"), assumed_nav=10000.0)

now = datetime.now(timezone.utc)
SIGNALS = [
    "Buy Gold Now 2410 - 2406 TP 2412 TP 2414 TP 2417 TP 2420 TP open SL 2403",    # valid
    "Sell Gold Now 2430 - 2434 TP 2428 TP 2426 TP 2423 TP 2420 SL 2437",           # valid
    "Buy Gold Now 2410 - 2406 TP 2412 TP 2414 TP 2417 TP 2420 TP open SL 2403",    # same signal reposted: ignored as duplicate
    "Buy Gold Now 2410 - 2406 TP 2412 TP 2414 TP 2417 SL 2415",                    # stop on the wrong side: rejected with a reason
    "good morning everyone",                                                         # not a signal: ignored
]
for text in SIGNALS:
    bot.handle(text, ts=now)

print(open(log).read())
