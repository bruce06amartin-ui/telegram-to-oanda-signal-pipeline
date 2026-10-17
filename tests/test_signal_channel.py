import json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oanda_signals import scoreboard as sb
from oanda_signals.signal_channel import SignalChannel, simulate
from oanda_signals.signal_parser import parse_signal
from oanda_signals.client import Oanda

UTC = timezone.utc
ok = 0
def check(c, msg):
    global ok
    assert c, msg
    ok += 1

T0 = datetime(2026, 10, 15, 14, 15, 10, tzinfo=UTC)
def C(minute, bid_l, bid_h=None, ask_l=None, ask_h=None, spread=0.4):
    """A one-minute candle; minute counted from 14:00."""
    bid_h = bid_h if bid_h is not None else bid_l
    ask_l = ask_l if ask_l is not None else bid_l + spread
    ask_h = ask_h if ask_h is not None else bid_h + spread
    return dict(t=datetime(2026, 10, 15, 14, 0, tzinfo=UTC) + timedelta(minutes=minute), bid_l=bid_l, bid_h=bid_h, ask_l=ask_l, ask_h=ask_h)
def flat(lo, n=30, start=0, hi=None):
    return [C(start + i, lo, hi if hi is not None else lo + 0.5) for i in range(n)]

BUY = parse_signal("Buy Gold Now 4161.50 - 4155.50 TP 4164 TP 4167 TP 4170 TP 4173 TP 4182 SL 4151")      # edge entry 4161.5, stop 4151, TP1 4164
SELL = parse_signal("Sell Gold Now 4156 - 4162 TP 4153 TP 4150 TP 4147 TP 4144 SL 4167")                   # edge entry 4156, stop 4167, TP1 4153
NOW = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)
risk, rew = 10.5, 2.5          # edge entry 4161.5, stop 4151, TP1 4164

# ---- simulate
cs = flat(4165, 20, 16) ; cs = [C(16, 4155.0, 4156.0)] + [C(17, 4160, 4162)] + [C(18, 4168, 4171)]       # fills 14:16, rises to TP3 at 14:18
r = simulate(BUY, T0, None, cs, NOW)
check(r["status"] == "win" and abs(r["r"] - round(rew / risk, 3)) < 1e-9, f"buy win {r}")
r = simulate(BUY, T0, None, [C(16, 4155.0, 4156.0), C(17, 4150.5, 4157)], NOW)
check(r["status"] == "loss" and r["r"] == -1.0, "buy loss on later minute")
r = simulate(BUY, T0, None, [C(16, 4150.0, 4171.0)], NOW)
check(r["status"] == "loss", "stop in the entry minute is a loss")
r = simulate(BUY, T0, None, [C(16, 4155.0, 4175.0), C(17, 4160, 4161)], NOW)
check(r["status"] == "open", f"target in the entry minute not counted: {r}")
r = simulate(BUY, T0, None, [C(16, 4155.0, 4156.0), C(17, 4160, 4161)], NOW)
check(r["status"] == "open" and r["fill_at"], "filled and open")
r = simulate(BUY, T0, None, flat(4165, 10, 16), T0 + timedelta(minutes=10))
check(r["status"] == "pending", "waiting for entry")
r = simulate(BUY, T0, None, flat(4165, 10, 16), T0 + timedelta(hours=5))
check(r["status"] == "expired" and r["r"] == 0.0, "never reached within 4h")
r = simulate(BUY, T0, T0 + timedelta(minutes=2), [C(16, 4165, 4166), C(17, 4165, 4166), C(18, 4155.0, 4156.0)], NOW)
check(r["status"] == "missed", "group reported a target before entry -> cancelled")
r = simulate(BUY, T0, T0 + timedelta(minutes=3), [C(16, 4155.0, 4156.0), C(17, 4160, 4161), C(19, 4150, 4151)], NOW)
check(r["status"] == "loss", "a target report AFTER the fill does not cancel it")
r = simulate(BUY, T0, T0 + timedelta(minutes=2), [], T0 + timedelta(minutes=5))
check(r["status"] == "missed", "hit report with no candles yet still cancels")
# sell side: fills on bid >= 4156, stop when ask >= 4167, target when ask <= 4147
S0 = datetime(2026, 10, 15, 14, 30, 0, tzinfo=UTC)
r = simulate(SELL, S0, None, [C(31, 4161.0, 4162.5), C(32, 4150, 4151), C(33, 4146.0, 4148)], NOW)
check(r["status"] == "win" and abs(r["r"] - round(3 / 11, 3)) < 1e-9, f"sell win {r}")     # edge entry 4156, stop 4167, TP1 4153
r = simulate(SELL, S0, None, [C(31, 4161.0, 4162.5), C(32, 4165, 4166.8)], NOW)
check(r["status"] == "loss", "sell loss via the ask")
r = simulate(SELL, S0, None, [C(31, 4150.0, 4151.0)], S0 + timedelta(minutes=3))
check(r["status"] == "pending", "sell never reached")

# ---- the channel reader
class Resp:
    def __init__(s, js): s.status_code, s._js = 200, js
    def json(s): return s._js
    def raise_for_status(s): pass
BASE = int(sb.snowflake(datetime(2026, 10, 15, 14, 0, tzinfo=UTC)))     # real-looking ids, so the 6-hour re-read window behaves as in life
def M(i, text, ts): return {"id": str(BASE + i), "content": text, "timestamp": ts}
class Disc:
    def __init__(s, msgs): s.msgs = msgs; s.gets = 0
    def get(s, url, params=None, headers=None, timeout=None):
        s.gets += 1
        out = [m for m in s.msgs if int(m["id"]) > int(params["after"])][:params["limit"]]
        return Resp(list(reversed(out)))
class FakeOanda:
    def __init__(s, candles): s.candles_list, s.err, s.calls = candles, None, 0
    def candles(s, instrument, start, now):
        s.calls += 1
        if s.err: raise s.err
        return [c for c in s.candles_list if c["t"] >= start]

SIG1 = "**Dave H**\nBuy Gold Now 4161.50 - 4155.50 TP 4164 TP 4167 TP 4170 TP 4173 TP 4182 SL 4151"
msgs = [
    M(1, "**Dave H**\nBuy Gold Now", "2026-10-15T14:15:00.000000+00:00"),                                              # incomplete, completed by an edit later
    M(2, "**Dave H**\n> Buy Gold Now 4145 - 4138 TP 4147.5 TP 4149.5 TP 4152 TP 4157 SL 4135\nTP1 HIT", "2026-10-15T14:16:00+00:00"),  # reply with no matching signal yet: ignored
    M(3, "**Dave H**\nSell Gold Now 4156 - 4162 TP 4153 TP 4150 TP 4147 TP 4144 SL 4167", "2026-10-15T14:30:05+00:00"),
    M(4, "**Dave H**\n> Sell Gold Now 4156 - 4162 TP 4153 TP 4150 TP 4147 TP 4144 SL 4167\nTP1 HIT", "2026-10-15T14:45:00+00:00"),
    M(5, "**Dave H**\nBuy Gold Now 4100 - 4090 TP 4102 TP 4104 SL 4095", "2026-10-15T16:00:00+00:00"),                      # only 2 targets and a bad stop: unusable
    M(6, "Telegram forwarder is online.", "2026-10-15T16:01:00+00:00"),
]
candles = [C(16, 4155.0, 4156.0), C(17, 4160, 4161), C(18, 4168, 4171)] + [C(31, 4161, 4162.5), C(32, 4150, 4151), C(33, 4146, 4148)]
d = tempfile.mkdtemp(); path = os.path.join(d, "ch.json")
disc = Disc(msgs); ora = FakeOanda(candles); clock = [NOW]
ch = SignalChannel("TOK", "9", path, datetime(2026, 10, 12, tzinfo=sb.LONDON), ora, lambda: clock[0], http=disc)
check(ch.poll() is True, "first poll")
check(len(ch.st["signals"]) == 2 + 0 or len(ch.st["signals"]) == 3, f"signals {list(ch.st['signals'])}")
ch.score()
res = {mid: r["status"] for mid, r in ch.st["results"].items()}
check(res == {str(BASE + 3): "win"}, f"only the sell is scored at first (buy is still incomplete): {res}")
check(ch.st["results"][str(BASE + 3)]["r"] == round(3 / 11, 3), "sell: TP1 report came AFTER the fill, so no cancel")
# the first message is edited into the full signal -> picked up by the re-read, and scored
msgs[0] = M(1, SIG1, "2026-10-15T14:15:00.000000+00:00")
check(ch.poll() is True, "edit picked up")
ch.score()
check(ch.st["results"][str(BASE + 1)]["status"] == "win", f"buy now a win: {ch.st['results']}")
# same numbers posted again later: one signal
msgs.append(M(7, SIG1, "2026-10-15T17:00:00+00:00")); ch.poll()
check(len(ch._unique_signals()) == 3, "repost counted once")
st, extra = ch.inputs(NOW)
check(st["n"] == 2 and st["wins"] == 2 and abs(st["total"] - (round(rew / risk, 3) + round(3 / 11, 3))) < 1e-9, f"stats {st['n']} {st['total']}")
check("3 seen" in extra[1]["value"] and "1 unusable" in extra[1]["value"] and "2 won" in extra[1]["value"] and "0 never filled" in extra[1]["value"], extra[1]["value"])
# a hit that quotes different (partial) numbers goes to the latest earlier signal in that direction
msgs.append(M(8, "**Dave H**\n> Buy Gold Now 4161.50 TP 4164\nTP1 HIT fast", "2026-10-15T14:15:30+00:00"))
# a fresh channel where that partial-quote report precedes the entry: cancelled (the plan's rule), not a trade
d5 = tempfile.mkdtemp()
ch5 = SignalChannel("T", "9", os.path.join(d5, "c.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda(candles), lambda: clock[0], http=Disc(msgs))
ch5.poll(); ch5.score()
check(ch5.st["results"][str(BASE + 1)]["status"] == "missed", f"early TP report with different quote still attaches: {ch5.st['results'][str(BASE + 1)]}")
st5, ex5 = ch5.inputs(NOW)
check("1 never filled" in ex5[1]["value"] and st5["n"] == 1, ex5[1]["value"])
check("BUY @4161.5 SL 4151: cancelled (target reported first)" in ex5[0]["value"] and "SELL @4156 SL 4167: won +0.27R" in ex5[0]["value"], ex5[0]["value"])
# state survives a restart and finished signals are not simulated again
ch2 = SignalChannel("TOK", "9", path, datetime(2026, 10, 12, tzinfo=sb.LONDON), ora, lambda: clock[0], http=Disc(msgs))
n_before = ora.calls; ch2.poll(); ch2.score()
check(ora.calls == n_before + 1 or ora.calls == n_before, "no needless candle requests")
check(ch2.st["results"][str(BASE + 1)]["status"] == "win", "restored result kept")
# OANDA problem is returned, not raised; channel reading continues
d3 = tempfile.mkdtemp()
ora3 = FakeOanda(candles); ora3.err = ValueError("boom")
ch3 = SignalChannel("TOK", "9", os.path.join(d3, "c.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), ora3, lambda: clock[0], http=Disc(msgs))
ch3.poll(); changed, err = ch3.score()
check(isinstance(err, ValueError) and changed is False, "candle failure returned")
st3, ex3 = ch3.inputs(NOW)
check("not scored yet" in ex3[1]["value"] and "not scored yet" in ex3[0]["value"], ex3[0]["value"])
# hidden message text
hid = [{"id": str(BASE + 100 + i), "content": "", "timestamp": "2026-10-15T14:00:00+00:00"} for i in range(6)]
ch4 = SignalChannel("T", "9", os.path.join(d3, "h.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: clock[0], http=Disc(hid))
try: ch4.poll(); check(False, "should raise")
except RuntimeError as e: check("Message Content Intent" in str(e), "intent error")

# ---- the client's candle reader against OANDA's documented shape
class H:
    def __init__(s, pages): s.pages, s.calls = pages, []
    def get(s, url, headers=None, params=None, timeout=None):
        s.calls.append((url, params)); return Resp(s.pages.pop(0))
def oc(t, complete=True): return {"complete": complete, "time": t, "volume": 5, "bid": {"o": "1", "h": "4156.1", "l": "4155.2", "c": "1"}, "ask": {"o": "1", "h": "4156.5", "l": "4155.6", "c": "1"}}
h = H([{"candles": [oc("2026-10-15T14:16:00.000000000Z"), oc("2026-10-15T14:17:00.000000000Z"), oc("2026-10-15T14:18:00.000000000Z", False)]}])
cl = Oanda(token="T", account="A", dry_run=True, http=h)
out = cl.candles("XAU_USD", datetime(2026, 10, 15, 14, 16, tzinfo=UTC), NOW)
check(len(out) == 2 and out[0]["t"] == datetime(2026, 10, 15, 14, 16, tzinfo=UTC) and out[0]["ask_l"] == 4155.6 and out[0]["bid_h"] == 4156.1, "candle parsing")
check(h.calls[0][0] == "https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles", h.calls[0][0])
check(h.calls[0][1]["price"] == "BA" and h.calls[0][1]["granularity"] == "M1" and h.calls[0][1]["from"] == "2026-10-15T14:16:00.000000000Z", h.calls[0][1])
check(Oanda(token="T", account="A", dry_run=True, http=H([{"accounts": 1}]))._get("/summary") == {"accounts": 1}, "account paths unchanged")

# ---- edge entry: a buy fills as soon as price touches the TOP of the zone (the old far-edge rule would not have filled here)
r = simulate(BUY, T0, None, [C(16, 4160.0, 4161.0), C(17, 4165, 4166), C(18, 4169, 4171)], NOW)
check(r["status"] == "win" and r["fill_at"].startswith("2026-10-15T14:16"), f"edge fill {r}")
# a state file scored under the old rule is scored again
sd = tempfile.mkdtemp(); sp = os.path.join(sd, "old.json")
json.dump(dict(last_id=None, msgs={}, signals={}, hits={}, results={"1": {"status": "loss", "r": -1.0, "at": "x", "fill_at": "x"}}), open(sp, "w"))
chx = SignalChannel("T", "9", sp, datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]))
check(chx.st["results"] == {} and chx.st["rule"] == "edge-tp1-v1", "old results dropped")
# a signal whose TP3 is not beyond the edge entry is unusable
weird = parse_signal("Buy Gold Now 4150 - 4140 TP 4141 TP 4142 TP 4143 SL 4130")
check(weird["ok"], "parser itself accepts it (entry would be 4140)")
chw = SignalChannel("T", "9", os.path.join(sd, "w.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]))
chw.st["signals"]["5"] = dict(ts="2026-10-15T14:00:00+00:00", sig=weird)
check(chw._unique_signals()[0][1]["sig"]["ok"] is False, "TP1 inside the zone -> unusable")

# the target is a setting: TP2 / TP3 give the bigger rewards, and a changed setting rescores stored results
for n, (buy_r, sell_r) in {2: (round(5.5 / 10.5, 3), round(6 / 11, 3)), 3: (round(8.5 / 10.5, 3), round(9 / 11, 3))}.items():
    cn = SignalChannel("T", "9", os.path.join(tempfile.mkdtemp(), "n.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda(candles), lambda: NOW, http=Disc([m for m in msgs if m["id"] != str(BASE + 8)]), tp_n=n)
    cn.poll(); cn.score()
    check(cn.rule == f"edge-tp{n}-v1" and cn.st["results"][str(BASE + 3)]["r"] == sell_r, f"TP{n} sell {cn.st['results'][str(BASE + 3)]}")
    check(cn.st["results"][str(BASE + 1)]["r"] == buy_r, f"TP{n} buy {cn.st['results'][str(BASE + 1)]}")
    check(f"TP{n} as the target" in cn.inputs(NOW)[1][2]["value"], "board text names the target")
check(SignalChannel("T", "9", os.path.join(tempfile.mkdtemp(), "z.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]), tp_n=7).tp_n == 1, "bad setting falls back to TP1")

# the entry point is a setting too (SOURCE_ENTRY): where the limit order sits inside the zone
from oanda_signals.signal_channel import entry_of, ENTRY_MODES
check([entry_of(BUY, m) for m in ("near", "far", "top", "bottom", "mid")] == [4161.5, 4155.5, 4161.5, 4155.5, 4158.5], "buy entries")
check([entry_of(SELL, m) for m in ("near", "far", "top", "bottom", "mid")] == [4156, 4162, 4162, 4156, 4159], "sell entries")
check(entry_of(BUY) == 4161.5 and entry_of(SELL) == 4156, "default is the near edge")
# a buy with entry at the bottom of the zone (4155.5, stop 4151, risk 4.5, TP2 4167 = 11.5/4.5R): price must dip to the bottom first
dip = [C(16, 4160.0, 4161.0), C(17, 4155.0, 4160), C(18, 4163, 4168)]
r = simulate(BUY, T0, None, dip, NOW, 2, "bottom")
check(r["status"] == "win" and r["fill_at"].startswith("2026-10-15T14:17") and r["r"] == round(11.5 / 4.5, 3), f"bottom entry {r}")
r = simulate(BUY, T0, None, [C(16, 4160.0, 4161.0), C(17, 4165, 4166), C(18, 4169, 4171)], NOW, 2, "bottom")
check(r["status"] == "expired" and r["fill_at"] is None, f"bottom entry never reached -> not filled {r}")
for m in ENTRY_MODES:
    cm = SignalChannel("T", "9", os.path.join(tempfile.mkdtemp(), "m.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]), entry_mode=m)
    check(cm.entry_mode == m and cm.rule == ("edge-tp1-v1" if m == "near" else f"{m}-tp1-v1"), f"rule for {m}: {cm.rule}")
    check("Entry at" in cm.inputs(NOW)[1][2]["value"], "text builds")
check(SignalChannel("T", "9", os.path.join(tempfile.mkdtemp(), "q.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]), entry_mode="sideways").entry_mode == "near", "bad entry setting falls back to near")
# changing the setting rescores stored results
sp2 = os.path.join(tempfile.mkdtemp(), "e.json")
ca = SignalChannel("T", "9", sp2, datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]))
ca.st["results"]["1"] = dict(status="win", r=1.0, at="x", fill_at="x"); ca._save()
cb = SignalChannel("T", "9", sp2, datetime(2026, 10, 12, tzinfo=sb.LONDON), FakeOanda([]), lambda: NOW, http=Disc([]), entry_mode="bottom")
check(cb.st["results"] == {} and cb.st["rule"] == "bottom-tp1-v1", "entry change rescored")
cb.st["signals"]["5"] = dict(ts="2026-10-15T14:00:00+00:00", sig=parse_signal("Sell Gold Now 4156 - 4162 TP 4153 TP 4150 TP 4147 SL 4155"))
check(cb._unique_signals()[0][1]["sig"]["ok"] is False, "stop on the wrong side of the entry -> unusable")
print("signal channel tests passed:", ok)
