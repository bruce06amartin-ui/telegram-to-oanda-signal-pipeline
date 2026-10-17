import asyncio, json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oanda_signals import scoreboard as sb

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
ok = 0
def check(c, msg):
    global ok
    assert c, msg
    ok += 1

# ---- time parsing
check(sb.parse_time("2026-10-14T14:19:03.123456789Z") == datetime(2026, 10, 14, 14, 19, 3, 123456, tzinfo=UTC), "ns trimmed")
check(sb.parse_time("2026-10-14T14:19:03.5+00:00").second == 3, "short fraction")
check(sb.parse_time("rubbish") is None and sb.parse_time(None) is None, "bad time")

# ---- stats
t = lambda d, h=10: datetime(2026, 10, d, h, tzinfo=UTC)
st = sb.stats([(t(13), 2.0), (t(14), -1.0), (t(15, 9), -1.0), (t(12), 2.0)], NOW)   # Mon 12 .. Thu 15
check(st["n"] == 4 and st["wins"] == 2 and st["losses"] == 2, "counts")
check(abs(st["total"] - 2.0) < 1e-9, "total")
check(st["curve"] == [0.0, 2.0, 4.0, 3.0, 2.0], f"curve {st['curve']}")
check(abs(st["max_dd"] - (-2.0)) < 1e-9, "max dd")
check(st["today_n"] == 1 and abs(st["today_r"] + 1.0) < 1e-9, "today")
check(st["week_n"] == 4, "week")
e = sb.stats([], NOW)
check(e["n"] == 0 and e["curve"] == [0.0] and e["avg"] == 0 and e["winrate"] == 0, "empty stats")
# previous week not counted in 'this week'
p = sb.stats([(t(8), 2.0)], NOW)
check(p["week_n"] == 0 and p["total"] == 2.0, "previous week excluded")

# ---- chart
for curve in ([0.0], [0.0, 2.0], [0.0, 2.0, 1.0, -3.0], [0.0, -1.02], [0.0] + [0.5] * 80, [0.0, 0.0, 0.0]):
    png = sb.chart_png(curve, "test")
    check(png and png[:8] == b"\x89PNG\r\n\x1a\n", f"png for {curve[:4]}")

# ---- embed + signature
em = sb.build_embed("T", st, [dict(name="x", value="y", inline=False)], NOW, True)
check(em["image"]["url"] == "attachment://chart.png" and len(em["fields"]) == 6, "embed")
check("image" not in sb.build_embed("T", st, [], NOW, False), "no image when no png")
check("+2.00R" in em["description"], "desc total")
s1 = sb.signature("T", st, []); s2 = sb.signature("T", sb.stats([(t(13), 2.0)], NOW + timedelta(minutes=5)), [])
check(s1 == sb.signature("T", st, []) and s1 != s2, "signature stable and sensitive")
check(sb.signature("T", st, []) == sb.signature("T", sb.stats([(t(13), 2.0), (t(14), -1.0), (t(15, 9), -1.0), (t(12), 2.0)], NOW + timedelta(hours=1)), []), "time alone does not change signature")

# ---- DiscordBoard with a fake http
class Resp:
    def __init__(s, code=200, js=None): s.status_code, s._js = code, js or {}
    def json(s): return s._js
    def raise_for_status(s):
        if s.status_code >= 400:
            import requests; e = requests.HTTPError(f"{s.status_code} for url https://secret"); e.response = s; raise e
class Http:
    def __init__(s): s.calls = []; s.patch_code = 200; s.n = 0
    def post(s, url, **kw): s.n += 1; s.calls.append(("post", url, kw)); return Resp(200, {"id": f"10{s.n}"})
    def patch(s, url, **kw): s.calls.append(("patch", url, kw)); return Resp(s.patch_code)
d = tempfile.mkdtemp()
h = Http(); b = sb.DiscordBoard("https://hook/x", os.path.join(d, "b.json"), http=h)
em = sb.build_embed("T", st, [], NOW, True)
check(b.publish("k", em, b"PNGDATA", "s1") == "posted", "first post")
check(h.calls[0][2]["params"] == {"wait": "true"} and "files" in h.calls[0][2], "wait=true and file sent")
check(b.publish("k", em, b"PNGDATA", "s1") == "unchanged" and len(h.calls) == 1, "unchanged -> no call")
check(b.is_current("k", "s1") and not b.is_current("k", "s2"), "is_current")
check(b.publish("k", em, b"PNGDATA", "s2") == "edited", "edit")
c = h.calls[-1]; check(c[0] == "patch" and c[1] == "https://hook/x/messages/101", "patch url")
check(json.loads(c[2]["data"]["payload_json"])["attachments"] == [{"id": 0, "filename": "chart.png"}], "attachments replace")
check(json.loads(c[2]["data"]["payload_json"])["allowed_mentions"] == {"parse": []}, "no mentions")
h.patch_code = 404
check(b.publish("k", em, b"PNGDATA", "s3") == "posted", "deleted message -> repost")
check(h.calls[-1][0] == "post" and "attachments" not in json.loads(h.calls[-1][2]["data"]["payload_json"]), "repost has no attachments field")
b2 = sb.DiscordBoard("https://hook/x", os.path.join(d, "b.json"), http=h)
h.patch_code = 200
check(b2.publish("k", em, b"P", "s4") == "edited" and h.calls[-1][1].endswith("/messages/102"), "id remembered across restarts")
h.patch_code = 500
try: b2.publish("k", em, b"P", "s5"); check(False, "should raise")
except Exception: check(True, "server error raised")
check(b2.is_current("k", "s4"), "failed edit leaves old signature")
h2 = Http(); b3 = sb.DiscordBoard("https://hook/y", os.path.join(d, "c.json"), http=h2)
b3.publish("k", em, None, "a"); check("json" in h2.calls[0][2] and "files" not in h2.calls[0][2], "no png -> plain json")

# ---- GoldTracker with real post formats
class Gh:
    def __init__(s, msgs): s.msgs = msgs; s.gets = []
    def get(s, url, params=None, headers=None, timeout=None):
        s.gets.append(dict(params=params, headers=headers))
        after = int(params["after"]); out = [m for m in s.msgs if int(m["id"]) > after][:params["limit"]]
        return Resp(200, list(reversed(out)))        # Discord returns newest first
BASE = int(sb.snowflake(datetime(2026, 10, 12, tzinfo=sb.LONDON)))
def msg(i, text, ts): return {"id": str(BASE + i), "content": text, "timestamp": ts}
M = [msg(1000, "S&D zone trade opened: LONG @ 4155.48 | SL 4150.91 | TP 4164.62", "2026-10-12T08:00:00.000000+00:00"),
     msg(1001, "AT ENTRY NOW gold", "2026-10-12T08:01:00+00:00"),
     msg(1002, "S&D zone trade closed: WIN @ 4164.62", "2026-10-12T09:00:00+00:00"),
     msg(1003, "S&D zone trade opened: SHORT @ 4170.00 | SL 4175.00 | TP 4160.00", "2026-10-13T08:00:00+00:00"),
     msg(1004, "S&D zone trade closed: LOSS @ 4175.00", "2026-10-13T09:00:00+00:00"),
     msg(1005, "S&D zone trade closed: WIN @ 4100", "2026-10-14T09:00:00+00:00"),     # no opening message
     msg(1006, "S&D zone trade opened: LONG @ 4100.00 | SL 4095.00 | TP 4110.00", "2026-10-15T08:00:00+00:00")]
g = sb.GoldTracker("TOKEN", "555", os.path.join(d, "g.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), http=Gh(M))
check(g.poll() is True, "first poll changed")
check(g.http.gets[0]["headers"]["Authorization"] == "Bot TOKEN", "auth header")
check([r for _, r in g.st["trades"]] == [2.0, -1.0, 2.0], f"trades {g.st['trades']}")
check(g.st["unpaired"] == 1 and g.st["open"]["e"] == 4100.0, "unpaired and open")
check(g.poll() is False, "second poll nothing new")
gst, ex = g.inputs(NOW)
check(gst["n"] == 3 and abs(gst["total"] - 3.0) < 1e-9, "gold stats")
check({f["name"] for f in ex} == {"Open now", "Note"}, "extra fields")
g2 = sb.GoldTracker("TOKEN", "555", os.path.join(d, "g.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), http=Gh(M))
check(g2.st["last_id"] == str(BASE + 1006) and len(g2.st["trades"]) == 3, "state restored")
# non-2:1 trade: win worth TP distance / stop distance
g3 = sb.GoldTracker("T", "5", os.path.join(d, "g3.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON),
                    http=Gh([msg(2000, "S&D zone trade opened: LONG @ 100 | SL 99 | TP 103", "2026-10-12T08:00:00+00:00"), msg(2001, "S&D zone trade closed: WIN @ 103", "2026-10-12T09:00:00+00:00")]))
g3.poll(); check(g3.st["trades"][0][1] == 3.0, "win uses real RR")
# pagination: 250 messages
big = []
for i in range(125):
    big.append(msg(3000 + 2 * i, "S&D zone trade opened: LONG @ 100 | SL 99 | TP 102", "2026-10-12T08:00:00+00:00"))
    big.append(msg(3001 + 2 * i, "S&D zone trade closed: LOSS @ 99", "2026-10-12T09:00:00+00:00"))
g4 = sb.GoldTracker("T", "5", os.path.join(d, "g4.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), http=Gh(big))
g4.poll(); check(len(g4.st["trades"]) == 125 and len(g4.http.gets) == 3, f"pagination {len(g4.st['trades'])} {len(g4.http.gets)}")
# message content hidden
hid = [{"id": str(BASE + 5000 + i), "content": "", "timestamp": "2026-10-12T08:00:00+00:00"} for i in range(6)]
g5 = sb.GoldTracker("T", "5", os.path.join(d, "g5.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), http=Gh(hid))
try: g5.poll(); check(False, "should raise")
except RuntimeError as ex_: check("Message Content Intent" in str(ex_), "intent error")
# chatter only: pointer still advances and is saved
g6 = sb.GoldTracker("T", "5", os.path.join(d, "g6.json"), datetime(2026, 10, 12, tzinfo=sb.LONDON), http=Gh([msg(6000, "hello", "2026-10-12T08:00:00+00:00")]))
check(g6.poll() is False and json.load(open(os.path.join(d, "g6.json")))["last_id"] == str(BASE + 6000), "pointer saved on chatter")
check(sb.snowflake(datetime(2026, 10, 12, tzinfo=UTC)).isdigit(), "snowflake")
print("scoreboard tests passed:", ok)
