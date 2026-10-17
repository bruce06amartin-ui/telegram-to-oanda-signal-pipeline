"""Reads the group's gold signals. Pure functions, no network. Format seen in the channel:
   Buy Gold Now 4181 - 4173 TP 4183.5 TP 4185.5 TP 4188 TP 4190 TP open SL 4171
   Sell Gold Now 4177 -4182 TP 4174.5 TP 4172 TP 4170 TP 4165 SL 4184
Anything that does not match this shape is not a signal, and the bot ignores it."""
import re

_NUM = r"(\d+(?:\.\d+)?)"
SIGNAL = re.compile(r"\b(Buy|Sell)\s+(?:Gold|XAUUSD)\s+(?:Now\s+)?" + _NUM + r"\s*-\s*" + _NUM + r"(.*?)\bSL\s*" + _NUM, re.I | re.S)
TP_NUM = re.compile(r"\bTP\s*" + _NUM, re.I)           # "TP open" has no number, so it is not counted
HIT = re.compile(r"\bTP\s*([1-9])\b[^\n]{0,12}?\b(?:hit|tapped)\b", re.I)
CORRECTED = re.compile(r"\bnumbers?\s+corrected\b", re.I)


def parse_signal(text):
    """Returns a dict, or None when the text is not a signal. A signal that is malformed (stop on the wrong side, fewer than 3 numbered
    take-profits, a take-profit on the wrong side) comes back with ok=False and a reason, so the bot logs why it was skipped."""
    if not text:
        return None
    m = SIGNAL.search(text)
    if not m:
        return None
    side = m.group(1).lower()
    a, b, sl = float(m.group(2)), float(m.group(3)), float(m.group(5))
    lo, hi = min(a, b), max(a, b)
    tps = [float(x) for x in TP_NUM.findall(m.group(4))]
    d = 1 if side == "buy" else -1
    best = lo if d > 0 else hi                 # best entry: lowest price for a buy, highest for a sell
    sig = dict(side=side, dir=d, lo=lo, hi=hi, best_entry=best, tps=tps, sl=sl)
    sig["key"] = f"{side}|{lo:g}|{hi:g}|{sl:g}|" + ",".join(f"{t:g}" for t in tps)
    bad = None
    if d > 0 and not sl < lo:
        bad = "stop is not below a buy zone"
    elif d < 0 and not sl > hi:
        bad = "stop is not above a sell zone"
    elif len(tps) < 3:
        bad = "fewer than 3 numbered take-profits"
    elif any((t - best) * d <= 0 for t in tps):
        bad = "a take-profit is on the wrong side of the entry"
    elif any((tps[i + 1] - tps[i]) * d <= 0 for i in range(len(tps) - 1)):
        bad = "take-profits are not in order"
    sig["ok"] = bad is None
    sig["reason"] = bad
    return sig


def parse_hit(text):
    """'TP1 HIT', 'TP1 tapped', "TP1 'was' HIT" -> 1. Otherwise None."""
    m = HIT.search(text or "")
    return int(m.group(1)) if m else None


def is_correction(text):
    return bool(CORRECTED.search(text or ""))
