"""Turns one parsed signal into one order plan: where, how many, or why not. Pure functions, no network."""
import math
from datetime import timedelta
from . import rules


def plan_order(sig, nav, conv, bid=None, ask=None, margin_avail=None, margin_rate=None, units_precision=0, price_dp=3, now=None):
    """nav: account value in account currency. conv: account currency per 1 USD of loss (1.0 for a USD account).
    bid/ask: current prices when known. Returns dict(decision='place'|'skip', reason, ...)."""
    out = dict(decision="skip", reason="", units=0, entry=None, sl=None, tp=None, risk_cash=0.0, risk_pct=0.0, capped_by_margin=False, expiry=None)
    if not sig or not sig.get("ok"):
        out["reason"] = (sig or {}).get("reason") or "not a valid signal"
        return out
    d, entry, sl, tp = sig["dir"], sig["best_entry"], sig["sl"], sig["tps"][rules.TP_INDEX]
    dist = abs(entry - sl)
    if dist <= 0:
        out["reason"] = "zero stop distance"
        return out
    if bid is not None and ask is not None:
        mkt = ask if d > 0 else bid
        if (d > 0 and mkt <= sl) or (d < 0 and mkt >= sl):
            out["reason"] = "market is already beyond the stop"
            return out
        if (d > 0 and mkt <= entry) or (d < 0 and mkt >= entry):
            out["reason"] = "price is already at or past the best entry"
            return out
        if (d > 0 and mkt >= tp) or (d < 0 and mkt <= tp):
            out["reason"] = "price is already at or past TP3"
            return out
    risk_cash = nav * rules.RISK_PCT / 100.0
    step = 10 ** (-units_precision)
    units = round(math.floor(risk_cash / (dist * conv) / step + 1e-9) * step, units_precision)
    capped = False
    if margin_avail is not None:
        rate = margin_rate or rules.DEFAULT_MARGIN_RATE
        cap = round(math.floor(rules.MARGIN_USE_CAP * margin_avail / (entry * rate * conv) / step + 1e-9) * step, units_precision)
        if cap < units:
            units, capped = cap, True
    if units < max(step, 1 if units_precision == 0 else step):
        out["reason"] = "size rounds to less than the minimum trade" + (" after the margin cap" if capped else "")
        return out
    actual_risk = units * dist * conv
    out.update(decision="place", reason="capped by margin" if capped else "", units=d * units, entry=round(entry, price_dp), sl=round(sl, price_dp),
               tp=round(tp, price_dp), risk_cash=actual_risk, risk_pct=100.0 * actual_risk / nav, capped_by_margin=capped)
    if now is not None:
        out["expiry"] = (now + timedelta(hours=rules.EXPIRY_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    return out
