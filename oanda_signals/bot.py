"""Signal bot: message text in, decisions out. Rules are in rules.py.
Feed it one message at a time with handle(text, ts). Replies quote the original signal as lines starting with '>', so the bot reads
the quoted part to find WHICH signal an update is about and the unquoted part for what the update says.
This module does not touch Telegram or Discord itself: messages are fed in by signal_bot_hook.py (or by hand in tests)."""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from . import rules
from .signal_parser import parse_signal, parse_hit, is_correction
from .planner import plan_order


def split_reply(text):
    quoted, own = [], []
    for ln in (text or "").splitlines():
        (quoted if ln.lstrip().startswith(">") else own).append(ln.lstrip("> ").rstrip() if ln.lstrip().startswith(">") else ln)
    return "\n".join(own).strip(), "\n".join(quoted).strip()


class Bot:
    def __init__(self, client, state_path="bot_state.json", log_path="decisions.jsonl", kill_path="KILL", assumed_nav=10000.0, now_fn=None):
        self.c, self.state_path, self.log_path, self.kill_path = client, state_path, log_path, kill_path
        self.assumed_nav = assumed_nav
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.st = json.load(open(state_path)) if os.path.exists(state_path) else {"signals": {}}
        self.track_errors = []

    # ---- plumbing
    def _save(self):
        json.dump(self.st, open(self.state_path, "w"), indent=1)

    def _log(self, kind, **kw):
        rec = dict(t=self.now().isoformat(timespec="seconds"), kind=kind, dry_run=self.c.dry_run, **kw)
        open(self.log_path, "a").write(json.dumps(rec) + "\n")
        return rec

    def _account(self):
        """Real numbers when a token is set, otherwise clearly-marked assumptions (so a dry run works offline)."""
        if self.c.can_read:
            s = self.c.summary()
            ins = self.c.instrument(rules.INSTRUMENT)
            pr = self.c.pricing(rules.INSTRUMENT, s["currency"])
            return dict(nav=s["nav"], conv=pr["conv"], bid=pr["bid"], ask=pr["ask"], margin_avail=s["margin_avail"], margin_rate=ins["margin_rate"],
                        units_precision=ins["units_precision"], price_dp=ins["price_dp"], assumed=False)
        return dict(nav=self.assumed_nav, conv=1.0, bid=None, ask=None, margin_avail=None, margin_rate=None, units_precision=0, price_dp=3, assumed=True)

    def _day_risk(self, day):
        return sum(r["risk_pct"] for r in self.st["signals"].values() if r.get("status") in ("placed", "filled", "closed") and r.get("day") == day)

    def _open_count(self):
        return sum(1 for r in self.st["signals"].values() if r.get("status") in ("placed", "filled"))

    # ---- main entry
    def handle(self, text, ts=None, correction=False):
        ts = ts or self.now()
        self.extra = []
        own, quoted = split_reply(text)
        out = []
        n = parse_hit(own)
        if n:
            ref = parse_signal(quoted)
            out.append(self._on_hit(ref["key"] if ref else None, n))
        sig = parse_signal(own)
        if sig:
            out.append(self._on_signal(sig, ts, correction or is_correction(own)))
        return self.extra + [o for o in out if o]

    def _on_signal(self, sig, ts, correction):
        key = sig["key"]
        if key in self.st["signals"]:
            return self._log("duplicate", key=key, note="same signal text seen before; ignored")
        if os.path.exists(self.kill_path):
            return self._log("skip", key=key, reason="kill switch file present")
        if correction:
            self.extra.extend(self._supersede(sig, ts))
        acct = self._account()
        day = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        plan = plan_order(sig, acct["nav"], acct["conv"], acct["bid"], acct["ask"], acct["margin_avail"], acct["margin_rate"], acct["units_precision"], acct["price_dp"], now=ts)
        rec = dict(sig=sig, ts=ts.isoformat(), day=day, plan=plan, hit=0, status="skipped", order=None, assumed_account=acct["assumed"])
        if plan["decision"] == "place":
            if self._day_risk(day) + plan["risk_pct"] > rules.DAY_CAP_PCT + 1e-9:
                plan.update(decision="skip", reason=f"daily risk cap {rules.DAY_CAP_PCT}% would be exceeded")
            elif self._open_count() >= rules.MAX_OPEN:
                plan.update(decision="skip", reason=f"{rules.MAX_OPEN} orders already open or pending")
        if plan["decision"] == "place":
            o = self.c.place_limit(rules.INSTRUMENT, plan["units"], plan["entry"], plan["sl"], plan["tp"], plan["expiry"], "sig-" + hashlib.sha1(key.encode()).hexdigest()[:12], acct["price_dp"])
            rec.update(status="placed", order=o["id"], risk_pct=plan["risk_pct"])
            self.st["signals"][key] = rec
            self._save()
            return self._log("would_place" if o.get("dry") else "placed", key=key, side=sig["side"], units=plan["units"], entry=plan["entry"], sl=plan["sl"], tp3=plan["tp"],
                             risk_pct=round(plan["risk_pct"], 3), reason=plan["reason"], assumed_account=acct["assumed"], order=o["id"])
        self.st["signals"][key] = rec
        self._save()
        return self._log("skip", key=key, reason=plan["reason"])

    def _supersede(self, new_sig, ts):
        done = []
        for k, r in self.st["signals"].items():
            if r["status"] != "placed" or r["sig"]["side"] != new_sig["side"]:
                continue
            if ts - datetime.fromisoformat(r["ts"]) <= timedelta(minutes=rules.CORRECTION_WINDOW_MIN) and self._is_pending(r):
                self.c.cancel(r["order"])
                r["status"] = "superseded"
                self._save()
                done.append(self._log("cancel", key=k, reason="replaced by a 'Numbers corrected' signal"))
        return done

    def _is_pending(self, r):
        return self.c.order_state(r["order"]) == "PENDING"

    def _on_hit(self, key, n):
        r = self.st["signals"].get(key) if key else None
        if not r:
            return self._log("hit_unmatched", n=n, note="could not tie this update to a known signal")
        r["hit"] = max(r.get("hit", 0), n)
        if r["status"] == "placed":
            if self._is_pending(r):
                self.c.cancel(r["order"])
                r["status"] = "missed"
                self._save()
                return self._log("cancel", key=key, reason=f"TP{n} reported before our order filled; counted as missed")
            r["status"] = "filled"
        self._save()
        return self._log("hit", key=key, n=n)


    # ---- follow our own real orders on OANDA (read-only): fills, closes and expiries
    def _real(self, r):
        return bool(r.get("order")) and not str(r["order"]).startswith("DRY-")

    def tally(self):
        real = [r for r in self.st["signals"].values() if self._real(r)]
        closed = [r["result"]["r"] for r in real if r.get("result")]
        return dict(placed=len(real), filled=sum(1 for r in real if r.get("trade")),
                    missed=sum(1 for r in real if r.get("status") in ("missed", "superseded", "expired")), closed=len(closed), total_r=round(sum(closed), 2))

    def track(self):
        """Asks OANDA what became of each order we placed and reports it once: filled, closed (with the result in R) or expired unfilled.
        R = profit or loss divided by the risk we planned for that signal. Reads only; never places or cancels. Returns log records.
        A problem with one order is noted in self.track_errors and does not stop the others."""
        out, self.track_errors = [], []
        for key, r in list(self.st["signals"].items()):
            if r.get("status") not in ("placed", "filled") or not self._real(r):
                continue
            try:
                out.extend(self._track_one(key, r))
            except Exception as e:
                self.track_errors.append(e)
        return out

    def _track_one(self, key, r):
        out, plan, side = [], r["plan"], r["sig"]["side"]
        if not r.get("trade"):
            o = self.c.order_detail(r["order"])
            if o.get("state") == "FILLED":
                r["trade"] = o.get("tradeOpenedID") or o.get("fillingTransactionID")
                r["status"] = "filled"
                self._save()
                t = self.c.trade_detail(r["trade"])
                out.append(self._log("fill", key=key, side=side, units=plan["units"], price=float(t["price"]), entry=plan["entry"], sl=plan["sl"], tp3=plan["tp"], order=r["order"], trade=r["trade"]))
            elif o.get("state") == "CANCELLED":
                r["status"] = "expired"
                self._save()
                return [self._log("expired", key=key, side=side, entry=plan["entry"], order=r["order"], tally=self.tally())]
            else:
                return out
        t = self.c.trade_detail(r["trade"])
        if t.get("state") == "CLOSED":
            pl, close = float(t["realizedPL"]), float(t["averageClosePrice"])
            risk = plan["risk_cash"] or 1.0
            by = "take-profit" if abs(close - plan["tp"]) <= abs(close - plan["sl"]) else "stop-loss"
            try:
                cur = self.c.summary()["currency"]
            except Exception:
                cur = ""
            r["status"] = "closed"
            r["result"] = dict(pl=pl, r=round(pl / risk, 2), close=close, by=by, closed_at=t.get("closeTime"))
            self._save()
            out.append(self._log("close", key=key, side=side, by=by, close=close, pl=round(pl, 2), currency=cur, r=r["result"]["r"], risk_cash=round(risk, 2), tally=self.tally()))
        return out


def shadow_r(rec, n):
    """What exiting at TPn would have paid for this signal, from the group's reported hits only. None = TPn not reported (outcome open or a loss)."""
    s = rec["sig"]
    if len(s["tps"]) < n or rec.get("hit", 0) < n:
        return None
    return round(abs(s["tps"][n - 1] - s["best_entry"]) / abs(s["best_entry"] - s["sl"]), 2)
