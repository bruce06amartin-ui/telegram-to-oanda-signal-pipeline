"""OANDA client. PRACTICE ENDPOINT ONLY: there is no code path to the live endpoint. DRY_RUN is on unless OANDA_DRY_RUN=0 is set by the human.
Secrets (OANDA_API_TOKEN, OANDA_ACCOUNT_ID) are read from the environment and never printed or logged.
Field names follow OANDA's v20 REST documentation; unit tests use a fake HTTP session, so confirm against a real practice account in dry run first."""
import os
import time
from datetime import datetime, timedelta, timezone
import requests

BASE = "https://api-fxpractice.oanda.com/v3"      # practice only, on purpose
RETRY_STATUS = (401, 429, 500, 502, 503, 504)       # OANDA sometimes answers a good request with one of these; reads are simply asked again
READ_TRIES = 3


class Oanda:
    def __init__(self, token=None, account=None, dry_run=None, http=requests, sleep=time.sleep):
        self.token = token if token is not None else os.environ.get("OANDA_API_TOKEN")
        self.account = account if account is not None else os.environ.get("OANDA_ACCOUNT_ID")
        self.dry_run = (os.environ.get("OANDA_DRY_RUN", "1") != "0") if dry_run is None else dry_run
        self.http = http
        self.sleep = sleep
        self._n = 0

    @property
    def can_read(self):
        return bool(self.token and self.account)

    def _hdr(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _get(self, path, params=None):
        """Reads only: asked again (up to READ_TRIES) on a flaky answer. Orders and cancels are never repeated."""
        for attempt in range(1, READ_TRIES + 1):
            try:
                url = f"{BASE}{path}" if path.startswith("/instruments/") else f"{BASE}/accounts/{self.account}{path}"       # price history is not under the account
                r = self.http.get(url, headers=self._hdr(), params=params, timeout=20)
                if r.status_code in RETRY_STATUS and attempt < READ_TRIES:
                    self.sleep(0.7 * attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= READ_TRIES:
                    raise
                self.sleep(0.7 * attempt)

    # ---- reads (allowed in dry run when a token is set)
    def summary(self):
        a = self._get("/summary")["account"]
        return dict(nav=float(a["NAV"]), currency=a["currency"], margin_avail=float(a["marginAvailable"]), open_trades=int(a.get("openTradeCount", 0)), pending=int(a.get("pendingOrderCount", 0)))

    def instrument(self, name):
        i = self._get("/instruments", {"instruments": name})["instruments"][0]
        return dict(price_dp=int(i["displayPrecision"]), units_precision=int(i["tradeUnitsPrecision"]), min_units=float(i["minimumTradeSize"]),
                    margin_rate=float(i["marginRate"]), max_units=float(i.get("maximumOrderUnits", 0) or 0))

    def pricing(self, name, currency):
        p = self._get("/pricing", {"instruments": name, "includeHomeConversions": "true"})
        pr = p["prices"][0]
        bid, ask = float(pr["bids"][0]["price"]), float(pr["asks"][0]["price"])
        conv = 1.0
        if currency != "USD":
            conv = next(float(c["accountLoss"]) for c in p.get("homeConversions", []) if c["currency"] == "USD")
        return dict(bid=bid, ask=ask, conv=conv)

    def order_state(self, order_id):
        if self.dry_run and str(order_id).startswith("DRY-"):
            return "PENDING"
        return self._get(f"/orders/{order_id}")["order"]["state"]

    def candles(self, instrument, start, now, max_pages=6):
        """One-minute bid/ask candles from `start` up to `now` (complete candles only), oldest first. Read-only.
        Each: dict(t=aware datetime, bid_h, bid_l, ask_h, ask_l). Raises if OANDA's answer is not in the expected shape."""
        out, cur = [], start
        for _ in range(max_pages):
            j = self._get(f"/instruments/{instrument}/candles", {"granularity": "M1", "price": "BA", "count": 5000, "from": cur.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")})
            cs = j["candles"]
            for c in cs:
                if not c.get("complete", True):
                    continue
                t = datetime.fromisoformat(c["time"][:19] + "+00:00")
                out.append(dict(t=t, bid_h=float(c["bid"]["h"]), bid_l=float(c["bid"]["l"]), ask_h=float(c["ask"]["h"]), ask_l=float(c["ask"]["l"])))
            if len(cs) < 5000 or not cs:
                break
            cur = datetime.fromisoformat(cs[-1]["time"][:19] + "+00:00") + timedelta(minutes=1)
            if cur >= now:
                break
        return out

    def order_detail(self, order_id):
        return self._get(f"/orders/{order_id}")["order"]

    def trade_detail(self, trade_id):
        return self._get(f"/trades/{trade_id}")["trade"]

    # ---- writes (blocked while dry_run)
    def place_limit(self, instrument, units, price, sl, tp, expiry, client_id, dp=3, tag="signal-bot"):
        body = {"order": {"type": "LIMIT", "instrument": instrument, "units": f"{float(units):.10g}",
                          "price": f"{price:.{dp}f}", "timeInForce": "GTD", "gtdTime": expiry, "positionFill": "DEFAULT",
                          "stopLossOnFill": {"price": f"{sl:.{dp}f}", "timeInForce": "GTC"}, "takeProfitOnFill": {"price": f"{tp:.{dp}f}"},
                          "clientExtensions": {"id": client_id[:120], "tag": tag}}}
        if self.dry_run or not self.can_read:
            self._n += 1
            return dict(id=f"DRY-{self._n}", dry=True, body=body)
        r = self.http.post(f"{BASE}/accounts/{self.account}/orders", headers=self._hdr(), json=body, timeout=20)
        r.raise_for_status()
        j = r.json()
        oid = (j.get("orderCreateTransaction") or {}).get("id")
        if not oid:
            raise RuntimeError("OANDA did not confirm the order: " + str({k: j.get(k) for k in ("orderRejectTransaction", "errorMessage")}))
        return dict(id=oid, dry=False, body=body)

    def cancel(self, order_id):
        if self.dry_run or str(order_id).startswith("DRY-") or not self.can_read:
            return dict(id=order_id, dry=True)
        r = self.http.put(f"{BASE}/accounts/{self.account}/orders/{order_id}/cancel", headers=self._hdr(), timeout=20)
        r.raise_for_status()
        return dict(id=order_id, dry=False)

    # ---- one-off self-test: never called by the signal path, only by the opt-in start-up check (SELFTEST=1)
    def self_test(self, instrument, now=None):
        """Proves OANDA accepts our order format before any real signal uses it. Places ONE 1-unit BUY LIMIT 10% below the market
        (so it cannot fill) with a stop, a target and a 10-minute expiry, reads it back, cancels it, reads it back again.
        Returns (verdict, [steps in plain words]); verdict is 'pass', 'fail' or 'skip' (not run). Practice endpoint only, like everything here."""
        if self.dry_run or not self.can_read:
            return "skip", ["needs a token, an account id and OANDA_DRY_RUN=0"]
        steps, oid = [], None
        try:
            inst = self.instrument(instrument)
            dp = inst["price_dp"]
            q = self._get("/pricing", {"instruments": instrument})["prices"][0]
            if not q.get("tradeable", True):
                return "skip", ["gold is not tradeable right now (market closed), so no test order was placed"]
            bid = float(q["bids"][0]["price"])
            price = round(bid * 0.9, dp)
            sl, tp = round(price - 20, dp), round(price + 20, dp)
            units = max(1.0, inst["min_units"])
            when = (now or datetime.now(timezone.utc)) + timedelta(minutes=10)
            r = self.place_limit(instrument, units, price, sl, tp, when.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
                                 "selftest-" + when.strftime("%H%M%S"), dp, tag="signal-selftest")
            oid = r["id"]
            steps.append(f"OANDA accepted order {oid}: BUY {units:g} limit {price} (market {bid}), stop {sl}, target {tp}, expires in 10 min")
            o = self._get(f"/orders/{oid}")["order"]
            def num(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None
            problems = []
            if o.get("state") != "PENDING": problems.append(f"state is {o.get('state')}, not PENDING")
            if o.get("type") != "LIMIT": problems.append(f"type is {o.get('type')}, not LIMIT")
            if o.get("timeInForce") != "GTD" or not o.get("gtdTime"): problems.append("the 10-minute expiry was not kept")
            if num((o.get("stopLossOnFill") or {}).get("price")) != sl: problems.append("the stop loss was not kept")
            if num((o.get("takeProfitOnFill") or {}).get("price")) != tp: problems.append("the take-profit was not kept")
            if problems:
                steps.append("read back: " + "; ".join(problems))
                verdict = "fail"
            else:
                steps.append("read back: pending LIMIT order with the stop, target and expiry all kept")
                verdict = "pass"
            self.cancel(oid)
            done, oid = oid, None                     # cancelled; nothing left to clean up if the read-back below fails
            st = self._get(f"/orders/{done}")["order"]["state"]
            steps.append(f"cancelled; state now {st}")
            if st != "CANCELLED":
                verdict = "fail"
            return verdict, steps
        except Exception as e:
            resp = getattr(e, "response", None)
            text = getattr(resp, "text", "")
            body = text[:300] if isinstance(text, str) else ""
            steps.append(f"{type(e).__name__} {getattr(resp, 'status_code', '')} {body}".strip())
            if oid:
                try:
                    self.cancel(oid)
                    steps.append(f"order {oid} cancelled afterwards")
                except Exception:
                    steps.append(f"could not cancel order {oid}; it is far below the market and expires on its own within 10 minutes")
            return "fail", steps
