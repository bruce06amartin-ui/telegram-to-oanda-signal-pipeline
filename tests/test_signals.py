import json, os, tempfile
from datetime import datetime, timezone, timedelta
from oanda_signals.signal_parser import parse_signal, parse_hit, is_correction
from oanda_signals.planner import plan_order
from oanda_signals.client import Oanda
from oanda_signals.bot import Bot, shadow_r, split_reply

# the 12 captured signals: (text, highest TP reported)
S = [
 ("Buy Gold Now 4181 - 4173 TP 4183.5 TP 4185.5 TP 4188 TP 4190 TP open SL 4171", 4),
 ("Sell Gold Now 4182.5 - 4187.5 TP 4180 TP 4178 TP 4175 TP 4172 SL 4190", 3),
 ("Sell Gold Now 4177 - 4182 TP 4174.5 TP 4172 TP 4170 TP 4165 SL 4184", 3),
 ("Sell Gold Now 4168.5 - 4173.5 TP 4166 TP 4164 TP 4162 TP 4160 TP open SL 4178", 4),
 ("Sell Gold Now 4169.5 - 4173.5 TP 4167 TP 4165 TP 4163 TP 4160 TP 4144 SL 4176.5", 4),
 ("Sell Gold Now 4191.5 - 4197.5 TP 4189 TP 4187 TP 4185 TP 4182 SL 4201", 3),
 ("Sell Gold Now 4190.5 - 4195.5 TP 4188 TP 4186 TP 4184 TP 4182 TP open SL 4198.5", 3),
 ("Sell Gold Now 4182.5 - 4187.5 TP 4180 TP 4178 TP 4176 TP 4174 TP 4170 SL 4192.5", 2),
 ("Sell Gold Now 4181 - 4187 TP 4178 TP 4176 TP 4173 TP 4169 TP open SL 4192", 3),
 ("Sell Gold Now 4177 - 4184 TP 4174 TP 4172 TP 4170 TP 4168 TP open SL 4187", 1),
 ("Sell Gold Now 4170 - 4177 TP 4167.5 TP 4165 TP 4163 TP 4160 SL 4183", 3),
 ("Sell Gold Now 4157 - 4162 TP 4155 TP 4153 TP 4151 TP 4145 SL 4167", 3),
]
EXPECT_R = [7.50, 5.00, 6.00, 2.56, 3.50, 3.57, 3.83, 2.30, 2.80, 4.67, 2.33, 2.20]


class Boom:  # any network call fails the test
    def get(self, *a, **k): raise AssertionError("network GET in dry run")
    def post(self, *a, **k): raise AssertionError("network POST in dry run")
    def put(self, *a, **k): raise AssertionError("network PUT in dry run")


def mk(tmp, **kw):
    c = Oanda(token=None, account=None, dry_run=True, http=Boom())
    return Bot(c, os.path.join(tmp, "s.json"), os.path.join(tmp, "d.jsonl"), os.path.join(tmp, "KILL"), **kw), c


def test_parse_all():
    tot = 0
    for (t, _), e in zip(S, EXPECT_R):
        s = parse_signal(t); assert s and s["ok"], t
        r = abs(s["tps"][2] - s["best_entry"]) / abs(s["best_entry"] - s["sl"])
        assert round(r, 2) == e, (t, r, e); tot += r
    assert round(tot, 2) == 46.26
    print("sum of best-entry TP3 R over all 12 (as if every one filled and reached TP3):", round(tot, 2))


def test_best_entry_side():
    assert parse_signal(S[0][0])["best_entry"] == 4173      # buy: lowest
    assert parse_signal(S[1][0])["best_entry"] == 4187.5    # sell: highest


def test_malformed():
    assert parse_signal("hello") is None
    assert not parse_signal("Buy Gold Now 4181 - 4173 TP 4183 TP 4185 TP 4188 SL 4180")["ok"]            # stop above zone bottom
    assert not parse_signal("Sell Gold Now 4182 - 4187 TP 4180 TP 4178 SL 4190")["ok"]                    # only 2 TPs
    assert not parse_signal("Sell Gold Now 4182 - 4187 TP 4180 TP 4182 TP 4175 SL 4190")["ok"]            # out of order
    assert not parse_signal("Sell Gold Now 4182 - 4187 TP 4188 TP 4178 TP 4175 SL 4190")["ok"]            # TP on wrong side


def test_hit_and_reply_split():
    assert parse_hit("TP1 HIT") == 1 and parse_hit("tp3 hit :fire:") == 3 and parse_hit("TP open") is None
    own, q = split_reply("TP1 HIT\n> " + S[1][0])
    assert own == "TP1 HIT" and parse_signal(q)["best_entry"] == 4187.5
    assert is_correction("Numbers corrected")


def test_sizing():
    s = parse_signal(S[0][0])
    p = plan_order(s, 10000, 1.0)           # risk 100, dist 2 -> 50 units
    assert p["decision"] == "place" and p["units"] == 50 and p["tp"] == 4188 and abs(p["risk_pct"] - 1) < 1e-9
    p = plan_order(parse_signal(S[1][0]), 10000, 1.0)   # sell, dist 2.5 -> 40 units, negative
    assert p["units"] == -40
    p = plan_order(s, 10000, 0.79)          # GBP account: loss per $ is 0.79
    assert p["units"] == 63 and p["risk_pct"] <= 1.0   # rounds down, never over 1%
    p = plan_order(s, 10000, 1.0, margin_avail=500, margin_rate=0.05)   # margin cap
    assert p["capped_by_margin"] and p["risk_pct"] < 1
    p = plan_order(s, 10000, 1.0, bid=4170, ask=4170.4)                  # market already below the buy limit? price<=entry -> skip
    assert p["decision"] == "skip"
    p = plan_order(s, 10000, 1.0, bid=4165, ask=4165.4)                  # beyond stop
    assert p["decision"] == "skip"
    p = plan_order(s, 1, 1.0)
    assert p["decision"] == "skip"


def test_fractional_units_have_no_float_junk():
    s = parse_signal(S[1][0])
    for conv in (1.0, 0.75, 0.7381):
        p = plan_order(s, 10000, conv, units_precision=1, margin_avail=10000, margin_rate=0.05)
        assert p["units"] == round(p["units"], 1), p["units"]
        assert str(abs(p["units"])).count(".") <= 1 and len(str(abs(p["units"])).split(".")[-1]) <= 1, p["units"]
    from oanda_signals.client import Oanda
    c = Oanda(token=None, account=None, dry_run=True, http=Boom())
    body = c.place_limit("XAU_USD", -50.9, 4187.5, 4190, 4175, "x", "id")["body"]
    assert body["order"]["units"] == "-50.9"
    assert c.place_limit("XAU_USD", 66.0, 4173, 4171, 4188, "x", "id")["body"]["order"]["units"] == "66"


def test_dry_run_blocks_everything():
    with tempfile.TemporaryDirectory() as d:
        b, c = mk(d)
        t0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        for i, (t, _) in enumerate(S[:3]):
            r = b.handle(t, t0 + timedelta(minutes=i))
            assert r[0]["kind"] == "would_place" and r[0]["dry_run"] is True and r[0]["order"].startswith("DRY-")
        # Boom would have raised on any network call. Also check the live path is shut:
        res = c.place_limit("XAU_USD", 10, 1, 0.5, 2, "x", "id")
        assert res["dry"] is True
        assert Oanda(token="t", account="a", dry_run=True, http=Boom()).place_limit("XAU_USD", 10, 1, 0.5, 2, "x", "id")["dry"]


def test_daily_cap_and_kill():
    with tempfile.TemporaryDirectory() as d:
        b, _ = mk(d)
        t0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        kinds = [b.handle(t, t0 + timedelta(minutes=i))[0] for i, (t, _) in enumerate(S[:5])]
        assert [k["kind"] for k in kinds] == ["would_place"] * 3 + ["skip"] * 2, [k["kind"] for k in kinds]
        assert "daily risk cap" in kinds[3]["reason"]
        # next UTC day resets
        assert b.handle(S[5][0], t0 + timedelta(days=1))[0]["kind"] == "would_place"
        open(os.path.join(d, "KILL"), "w").close()
        assert b.handle(S[6][0], t0 + timedelta(days=1, minutes=1))[0]["reason"].startswith("kill")


def test_duplicate_and_cancel_on_tp1_before_fill():
    with tempfile.TemporaryDirectory() as d:
        b, _ = mk(d)
        t0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        assert b.handle(S[1][0], t0)[0]["kind"] == "would_place"
        assert b.handle(S[1][0], t0 + timedelta(minutes=1))[0]["kind"] == "duplicate"
        r = b.handle("TP1 HIT\n> " + S[1][0], t0 + timedelta(minutes=20))
        assert r[0]["kind"] == "cancel" and "missed" in r[0]["reason"]
        key = parse_signal(S[1][0])["key"]
        assert b.st["signals"][key]["status"] == "missed"
        assert b.handle("TP2 HIT\n> Sell Gold Now 1 - 2 TP 3 TP 4 TP 5 SL 6", t0)[0]["kind"] == "hit_unmatched"


def test_numbers_corrected_replaces_unfilled():
    with tempfile.TemporaryDirectory() as d:
        b, _ = mk(d)
        t0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        b.handle(S[3][0], t0)
        r = b.handle("Numbers corrected\n" + S[4][0], t0 + timedelta(minutes=5))
        assert any(x["kind"] == "cancel" for x in r) and any(x["kind"] == "would_place" for x in r)
        k3, k4 = parse_signal(S[3][0])["key"], parse_signal(S[4][0])["key"]
        assert b.st["signals"][k3]["status"] == "superseded" and b.st["signals"][k4]["status"] == "placed"
        # without the correction marker the second one would be a separate signal
    with tempfile.TemporaryDirectory() as d:
        b, _ = mk(d)
        b.handle(S[3][0], t0); b.handle(S[4][0], t0 + timedelta(minutes=5))
        assert b.st["signals"][parse_signal(S[3][0])["key"]]["status"] == "placed"


def test_shadow():
    b = None
    with tempfile.TemporaryDirectory() as d:
        b, _ = mk(d)
        t0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)
        b.handle(S[0][0], t0)
        b.handle("TP2 HIT\n> " + S[0][0], t0)   # fill is unknown in dry run: cancel path; shadow numbers still come from reported hit
        rec = b.st["signals"][parse_signal(S[0][0])["key"]]
        assert shadow_r(rec, 1) == 5.25 and shadow_r(rec, 2) == 6.25 and shadow_r(rec, 3) is None



class FakeResp:
    def __init__(self, code, body=None, text=""): self.status_code, self._b, self.text = code, body or {}, text
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            e = requests.HTTPError(f"{self.status_code}"); e.response = self; raise e
    def json(self): return self._b


class FlakyHttp:
    """GET answers the given codes in turn; POST/PUT count their calls."""
    def __init__(self, codes): self.codes, self.gets, self.posts = list(codes), 0, 0
    def get(self, *a, **k):
        self.gets += 1
        c = self.codes.pop(0) if self.codes else 200
        return FakeResp(c, {"account": {"NAV": "10000", "currency": "GBP", "marginAvailable": "9000"}})
    def post(self, *a, **k):
        self.posts += 1; return FakeResp(401)
    put = post


def _raises(fn):
    try:
        fn()
    except Exception as e:
        return str(e)
    return None


def test_reads_retry_but_orders_never_repeat():
    h = FlakyHttp([401, 200])
    c = Oanda(token="t", account="a", dry_run=False, http=h, sleep=lambda s: None)
    assert c.summary()["nav"] == 10000.0 and h.gets == 2
    h = FlakyHttp([401, 401, 401, 401])
    c = Oanda(token="t", account="a", dry_run=False, http=h, sleep=lambda s: None)
    err = _raises(c.summary)
    assert err and "401" in err and h.gets == 3                              # gives up after 3 tries
    h = FlakyHttp([])
    c = Oanda(token="t", account="a", dry_run=False, http=h, sleep=lambda s: None)
    assert _raises(lambda: c.place_limit("XAU_USD", 10, 4138.0, 4135.0, 4152.0, "2026-10-15T17:00:00.000000000Z", "sig-x")) and h.posts == 1   # one attempt, no repeat
    assert _raises(lambda: c.cancel("123")) and h.posts == 2


class FakeOanda:
    """A tiny pretend OANDA for the self-test. keep_sl=False pretends it silently dropped the stop; reject=True answers the order with a 400."""
    def __init__(self, tradeable=True, keep_sl=True, reject=False):
        self.tradeable, self.keep_sl, self.reject = tradeable, keep_sl, reject
        self.orders, self.posts, self.puts, self.body = {}, 0, 0, None
    def get(self, url, params=None, **k):
        if url.endswith("/instruments"):
            return FakeResp(200, {"instruments": [{"displayPrecision": 3, "tradeUnitsPrecision": 1, "minimumTradeSize": "1", "marginRate": "0.05"}]})
        if url.endswith("/pricing"):
            return FakeResp(200, {"prices": [{"tradeable": self.tradeable, "bids": [{"price": "4140.000"}], "asks": [{"price": "4140.500"}]}]})
        oid = url.rsplit("/", 1)[1]
        return FakeResp(200, {"order": self.orders[oid]})
    def post(self, url, json=None, **k):
        self.posts += 1
        if self.reject:
            return FakeResp(400, {"errorMessage": "Invalid value specified for 'order.gtdTime'"}, text='{"errorMessage":"Invalid value specified for gtdTime"}')
        o = dict(json["order"]); self.body = o
        o.update(id="900", state="PENDING")
        if not self.keep_sl: o.pop("stopLossOnFill")
        self.orders["900"] = o
        return FakeResp(201, {"orderCreateTransaction": {"id": "900"}})
    def put(self, url, **k):
        self.puts += 1; self.orders[url.split("/")[-2]]["state"] = "CANCELLED"
        return FakeResp(200, {})


def test_self_test_passes_and_cleans_up():
    f = FakeOanda()
    c = Oanda(token="t", account="a", dry_run=False, http=f, sleep=lambda s: None)
    v, steps = c.self_test("XAU_USD", datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc))
    b = f.body
    assert v == "pass", steps
    assert b["type"] == "LIMIT" and b["units"] == "1" and float(b["price"]) == 3726.0 and b["timeInForce"] == "GTD" and b["gtdTime"] == "2026-10-15T14:10:00.000000000Z"
    assert float(b["stopLossOnFill"]["price"]) < float(b["price"]) < float(b["takeProfitOnFill"]["price"]) and b["clientExtensions"]["tag"] == "signal-selftest"
    assert f.posts == 1 and f.puts == 1 and f.orders["900"]["state"] == "CANCELLED"


def test_self_test_fails_when_the_stop_was_not_kept():
    f = FakeOanda(keep_sl=False)
    c = Oanda(token="t", account="a", dry_run=False, http=f, sleep=lambda s: None)
    v, steps = c.self_test("XAU_USD")
    assert v == "fail" and any("stop loss was not kept" in t for t in steps) and f.puts == 1, steps       # still cancelled


def test_self_test_rejected_order_is_one_attempt_and_shows_why():
    f = FakeOanda(reject=True)
    c = Oanda(token="t", account="a", dry_run=False, http=f, sleep=lambda s: None)
    v, steps = c.self_test("XAU_USD")
    assert v == "fail" and f.posts == 1 and f.puts == 0 and "gtdTime" in steps[-1] and "400" in steps[-1], steps


def test_self_test_skips_when_market_closed_or_dry_run():
    f = FakeOanda(tradeable=False)
    c = Oanda(token="t", account="a", dry_run=False, http=f, sleep=lambda s: None)
    v, steps = c.self_test("XAU_USD")
    assert v == "skip" and f.posts == 0, steps
    assert Oanda(token="t", account="a", dry_run=True, http=Boom()).self_test("XAU_USD")[0] == "skip"     # dry run: no network at all


class FakeBook:
    """Pretend OANDA orders and trades for the result tracker. Counts every write so the tests can prove tracking only reads."""
    def __init__(self):
        self.orders, self.trades, self.writes, self.broken = {}, {}, 0, set()
    def get(self, url, params=None, **k):
        kind, _id = url.rsplit("/", 2)[-2:]
        if _id in self.broken:
            return FakeResp(500)
        if url.endswith("/summary"):
            return FakeResp(200, {"account": {"NAV": "9898.61", "currency": "GBP", "marginAvailable": "9000"}})
        book = self.orders if kind == "orders" else self.trades
        return FakeResp(200, {"order" if kind == "orders" else "trade": book[_id]})
    def post(self, *a, **k): self.writes += 1; return FakeResp(500)
    put = post


BUY = "Buy Gold Now 4155.5 - 4160 TP 4162 TP 4166 TP 4170 TP 4175 SL 4151"


def tracked(tmp, book, order="7", status="placed"):
    c = Oanda(token="t", account="a", dry_run=False, http=book, sleep=lambda s: None)
    b = Bot(c, os.path.join(tmp, "s.json"), os.path.join(tmp, "d.jsonl"), os.path.join(tmp, "KILL"))
    sig = parse_signal(BUY)
    b.st["signals"][sig["key"]] = dict(sig=sig, ts="2026-10-15T13:19:00+00:00", day="2026-10-15", status=status, order=order, hit=0, risk_pct=0.998,
                                       plan=dict(units=29.1, entry=4155.5, sl=4151.0, tp=4170.0, risk_cash=99.8))
    return b, sig["key"]


def test_tracker_reports_fill_then_close_once():
    with tempfile.TemporaryDirectory() as d:
        bk = FakeBook(); b, key = tracked(d, bk)
        bk.orders["7"] = {"state": "PENDING"}
        assert b.track() == []                                                       # still waiting
        bk.orders["7"] = {"state": "FILLED", "tradeOpenedID": "8"}
        bk.trades["8"] = {"state": "OPEN", "price": "4155.48000", "initialUnits": "29.1"}
        recs = b.track()
        assert [r["kind"] for r in recs] == ["fill"] and recs[0]["price"] == 4155.48 and b.st["signals"][key]["status"] == "filled"
        assert b.track() == []                                                       # nothing new, nothing repeated
        bk.trades["8"] = {"state": "CLOSED", "price": "4155.48000", "realizedPL": "-101.39340", "averageClosePrice": "4150.91000", "closeTime": "x"}
        recs = b.track()
        assert [r["kind"] for r in recs] == ["close"], recs
        r = recs[0]
        assert r["by"] == "stop-loss" and r["pl"] == -101.39 and r["r"] == -1.02 and r["currency"] == "GBP" and r["tally"]["closed"] == 1 and r["tally"]["total_r"] == -1.02
        assert b.st["signals"][key]["status"] == "closed" and b.track() == [] and bk.writes == 0       # reported once; reads only
        assert b._open_count() == 0                                                  # a closed trade no longer counts as open


def test_tracker_fill_and_close_found_in_one_look_and_target_wording():
    with tempfile.TemporaryDirectory() as d:
        bk = FakeBook(); b, key = tracked(d, bk)
        bk.orders["7"] = {"state": "FILLED", "fillingTransactionID": "8"}            # older field name is the fallback
        bk.trades["8"] = {"state": "CLOSED", "price": "4155.50000", "realizedPL": "300.0", "averageClosePrice": "4170.0", "closeTime": "x"}
        recs = b.track()
        assert [r["kind"] for r in recs] == ["fill", "close"] and recs[1]["by"] == "take-profit" and recs[1]["r"] == 3.01, recs


def test_tracker_expired_order_is_missed_and_frees_the_slot():
    with tempfile.TemporaryDirectory() as d:
        bk = FakeBook(); b, key = tracked(d, bk)
        bk.orders["7"] = {"state": "CANCELLED"}
        recs = b.track()
        assert [r["kind"] for r in recs] == ["expired"] and b.st["signals"][key]["status"] == "expired" and b._open_count() == 0
        assert recs[0]["tally"]["missed"] == 1 and b.track() == []


def test_tracker_ignores_dry_run_orders_and_survives_one_bad_order():
    with tempfile.TemporaryDirectory() as d:
        b, _ = tracked(d, Boom(), order="DRY-1")
        assert b.track() == []                                                       # no network at all for dry-run ids
    with tempfile.TemporaryDirectory() as d:
        bk = FakeBook(); b, key = tracked(d, bk)
        sig2 = parse_signal("Sell Gold Now 4182.5 - 4187.5 TP 4180 TP 4178 TP 4175 TP 4172 SL 4190")
        b.st["signals"][sig2["key"]] = dict(b.st["signals"][key], sig=sig2, order="9", plan=dict(units=40, entry=4187.5, sl=4190.0, tp=4175.0, risk_cash=99.0))
        bk.broken.add("7"); bk.orders["9"] = {"state": "CANCELLED"}
        recs = b.track()
        assert [r["kind"] for r in recs] == ["expired"] and len(b.track_errors) == 1                  # the good one was still reported


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
