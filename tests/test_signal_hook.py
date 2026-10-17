import asyncio, os, tempfile
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from signal_bot_hook import SignalHook, compose, describe
from oanda_signals.client import Oanda


class Boom:
    def get(self, *a, **k): raise AssertionError("network")
    post = put = get


SIG = "Sell Gold Now 4182.5 - 4187.5 TP 4180 TP 4178 TP 4175 TP 4172 SL 4190"
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def msg(text, age_min=0, reply=None, forum_only=False, mid=None):
    r = None
    if reply is not None:
        r = NS(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=1)
    async def get_reply(): return NS(raw_text=reply) if reply is not None else None
    return NS(raw_text=text, date=NOW - timedelta(minutes=age_min), reply_to=r, get_reply_message=get_reply, id=mid)


def mk(d, said):
    c = Oanda(token=None, account=None, dry_run=True, http=Boom())
    return SignalHook(d, notify=said.append, client=c, now_fn=lambda: NOW)


def run(c): return asyncio.run(c)


def test_signal_then_tp1_before_fill():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h(msg(SIG)))
        assert said[-1].startswith("DRY RUN WOULD PLACE SELL 40 XAU_USD limit 4187.5") and "made-up" not in said[-1], said
        run(h(msg("TP1 HIT", reply=SIG)))
        assert "CANCEL" in said[-1] and "missed" in said[-1], said


def test_old_message_ignored():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h(msg(SIG, age_min=11)))
        assert said == []
        run(h(msg(SIG, age_min=9)))
        assert len(said) == 1


def test_errors_are_contained():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h(NS(raw_text=SIG, date=None, reply_to=None)))       # broken message object
        assert said and said[-1].startswith("Signal bot ERROR") and "forwarder is unaffected" in said[-1]


def test_startup_message_without_token():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h.start_check())
        assert "DRY RUN" in said[0] and "No OANDA token" in said[0]


def test_compose_matches_reply_format():
    assert compose("TP1 HIT", "a\nb") == "TP1 HIT\n> a\n> b"

BUY_ML = "Buy Gold Now\n\n4146 - 4138\n\nTP 4148.5\nTP 4151\nTP 4153\nTP 4157\n\nSL 4135"
SELL_ML = "Sell Gold Now\n\n4133 - 4139\n\nTP 4130.5\nTP 4128\nTP 4126\nTP 4122\n\nSL 4145"


def test_real_multiline_format_is_read():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h(msg(BUY_ML)))
        assert said[-1].startswith("DRY RUN WOULD PLACE BUY") and "limit 4138" in said[-1], said


def test_signal_completed_by_an_edit_is_picked_up():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h(msg("Sell Gold Now", mid=7)))                   # first post: nothing to act on yet
        assert said == []
        run(h.edited(msg(SELL_ML, mid=7)))                    # the edit carries the numbers
        assert len(said) == 1 and said[0].startswith("(edited message) DRY RUN WOULD PLACE SELL") and "limit 4139" in said[0], said
        run(h.edited(msg(SELL_ML, mid=7)))                    # a second identical edit changes nothing
        assert len(said) == 1, said


def test_edit_that_changes_the_numbers_replaces_the_old_order():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h(msg(SELL_ML, mid=9)))
        assert "WOULD PLACE" in said[-1]
        run(h.edited(msg(SELL_ML.replace("SL 4145", "SL 4147"), mid=9)))
        assert any("CANCEL" in t for t in said) and "WOULD PLACE" in said[-1], said


def test_edited_updates_and_chat_and_old_messages_are_ignored():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        run(h.edited(msg("TP1 HIT", reply=SIG, mid=3)))
        run(h.edited(msg("Close partials / Set BE", mid=4)))
        run(h.edited(msg(SELL_ML, age_min=11, mid=5)))
        assert said == [], said
        run(h.edited(NS(raw_text=SELL_ML, date=None, reply_to=None, id=6)))   # a broken edit is contained too
        assert said and said[-1].startswith("Signal bot ERROR")



def test_two_edits_arriving_together_order_once():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        async def both():
            await asyncio.gather(h.edited(msg(SELL_ML, mid=21)), h.edited(msg(SELL_ML, mid=21)), h(msg(SELL_ML, mid=21)))
        run(both())
        assert sum("WOULD PLACE" in t for t in said) == 1, said



def test_failed_attempt_is_retried_by_the_next_edit():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        real = h.bot.handle
        calls = []
        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("OANDA said 401")
            return real(*a, **k)
        h.bot.handle = flaky
        run(h.edited(msg(SELL_ML, mid=31)))
        assert said and said[-1].startswith("Signal bot ERROR"), said
        run(h.edited(msg(SELL_ML, mid=31)))                   # the same text again must be tried again, not skipped
        assert any("WOULD PLACE" in t for t in said), said


class StubClient:
    """Enough of Oanda for start_check; self_test answers what it is told."""
    can_read = True
    def __init__(self, verdict, dry_run=False):
        self.dry_run, self.verdict, self.tests = dry_run, verdict, 0
    def summary(self): return dict(nav=10000.0, currency="GBP", margin_avail=9000.0, open_trades=0, pending=0)
    def instrument(self, n): return dict(price_dp=3, units_precision=1, min_units=1.0, margin_rate=0.05, max_units=0)
    def pricing(self, n, c): return dict(bid=4140.0, ask=4140.5, conv=1.3)
    def self_test(self, instrument, now=None):
        self.tests += 1
        return self.verdict, ["step one", "step two"]


def mkc(d, said, client, selftest):
    return SignalHook(d, notify=said.append, client=client, now_fn=lambda: NOW, selftest=selftest)


def test_selftest_off_changes_nothing():
    with tempfile.TemporaryDirectory() as d:
        said = []; c = StubClient("pass")
        run(mkc(d, said, c, False).start_check())
        assert c.tests == 0 and len(said) == 1 and not c.dry_run, said


def test_selftest_waits_for_the_dry_run_switch():
    with tempfile.TemporaryDirectory() as d:
        said = []; c = StubClient("pass", dry_run=True)
        run(mkc(d, said, c, True).start_check())
        assert c.tests == 0 and "DRY RUN" in said[-1] and "OANDA_DRY_RUN" in said[-1], said


def test_selftest_pass_goes_live_and_runs_only_once():
    with tempfile.TemporaryDirectory() as d:
        said = []; c = StubClient("pass")
        run(mkc(d, said, c, True).start_check())
        assert c.tests == 1 and not c.dry_run and "SELF-TEST PASSED" in said[-1] and "step two" in said[-1], said
        run(mkc(d, said, c, True).start_check())                  # a restart with SELFTEST still on
        assert c.tests == 1 and "passed earlier" in said[-1], said


def test_selftest_failure_or_skip_holds_real_orders_back():
    for verdict, word in (("fail", "FAILED"), ("skip", "NOT RUN")):
        with tempfile.TemporaryDirectory() as d:
            said = []; c = StubClient(verdict)
            h = mkc(d, said, c, True)
            run(h.start_check())
            assert c.dry_run and f"SELF-TEST {word}" in said[-1] and "HELD BACK" in said[-1], said
            assert not os.path.exists(os.path.join(d, "selftest_passed.json"))
            run(mkc(d, said, c, True).start_check())              # still not passed, so it tries again on the next start (here: dry run again, no test)


def test_describe_results_in_plain_words():
    t = dict(placed=2, filled=1, missed=1, closed=1, total_r=-1.02)
    assert describe(dict(kind="fill", side="buy", units=29.1, price=4155.48, entry=4155.5, sl=4151.0, tp3=4170.0)) == \
        "FILLED BUY 29.1 XAU_USD at 4155.48 (limit 4155.5); stop 4151, target 4170"
    s = describe(dict(kind="close", side="buy", by="stop-loss", close=4150.91, pl=-101.39, currency="GBP", r=-1.02, risk_cash=99.8, tally=t, dry_run=False))
    assert s.startswith("CLOSED BUY by stop-loss at 4150.91: -101.39 GBP = -1.02R") and "2 placed, 1 filled, 1 missed, 1 closed" in s and "-1.02R (missed counts as 0R)" in s, s
    assert "DRY RUN" not in describe(dict(kind="expired", entry=4155.5, tally=t, dry_run=True))


def test_track_once_posts_results_and_limits_error_noise():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mk(d, said)
        recs = [dict(kind="expired", entry=4155.5, tally=dict(placed=1, filled=0, missed=1, closed=0, total_r=0.0))]
        h.bot.track = lambda: recs
        h.bot.track_errors = []
        run(h.track_once())
        assert len(said) == 1 and said[0].startswith("ORDER EXPIRED"), said
        class Bad(Exception): pass
        def failing():
            h.bot.track_errors = [Bad()]
            return []
        h.bot.track = failing
        run(h.track_once()); run(h.track_once())                   # two failures in a row: one message only
        assert sum("Result tracking hit a problem" in t for t in said) == 1, said
        h.bot.track = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        run(h.track_once())                                         # even a crash is contained


class FakeBoard:
    def __init__(self): self.pubs = []; self.fail = None; self.sigs = set()
    def is_current(self, key, sig): return sig in self.sigs
    def publish(self, key, embed, png, sig):
        if self.fail: raise self.fail
        self.pubs.append((key, embed, png, sig)); self.sigs.add(sig); return "posted"


def mkb(d, said, env=None):
    c = Oanda(token=None, account=None, dry_run=True, http=Boom())
    return SignalHook(d, notify=said.append, client=c, now_fn=lambda: NOW, env=env or {})


def test_boards_are_off_without_their_settings():
    with tempfile.TemporaryDirectory() as d:
        h = mkb(d, [])
        assert h._board is None and h._gold is None
        h = mkb(d, [], env={"BOT_WEBHOOK_URL": "https://hook/x", "SCOREBOARD": "0"})
        assert h._board is None
        h = mkb(d, [], env={"BOT_WEBHOOK_URL": "https://hook/x"})
        assert h._board is not None and h._gold is None                         # signal board needs only the existing webhook
        h = mkb(d, [], env={"BOT_WEBHOOK_URL": "https://hook/x", "DISCORD_BOT_TOKEN": "t", "GOLD_CHANNEL_ID": "5"})
        assert h._gold is None                                                  # gold board needs all three settings
        h = mkb(d, [], env={"DISCORD_BOT_TOKEN": "t", "GOLD_CHANNEL_ID": "5", "GOLD_BOARD_WEBHOOK_URL": "https://hook/g"})
        assert h._gold is not None and h._gold_board is not None and h._board is None


def test_signal_board_updates_only_when_something_changed():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mkb(d, said, env={"BOT_WEBHOOK_URL": "https://hook/x"})
        h._board = fb = FakeBoard()
        h.bot.track = lambda: []; h.bot.track_errors = []
        run(h.track_once()); run(h.track_once())
        assert len(fb.pubs) == 1, fb.pubs                                      # second pass: nothing new, no edit
        key, embed, png, sig = fb.pubs[0]
        assert key == "oanda" and "Pass mark" in str(embed["fields"]) and png[:4] == b"\x89PNG", embed
        h.bot.st["signals"]["a"] = dict(status="closed", order="123", trade="124", plan=dict(risk_cash=100.0), ts=NOW.isoformat(),
                                        result=dict(pl=-101.39, r=-1.02, closed_at="2026-10-14T08:30:00.000000000Z"))
        run(h.track_once())
        assert len(fb.pubs) == 2 and "-1.02R" in fb.pubs[1][1]["description"], fb.pubs[1][1]["description"]


def test_board_problems_are_reported_once_an_hour_and_never_leak_the_link():
    with tempfile.TemporaryDirectory() as d:
        said = []; clock = [NOW]
        c = Oanda(token=None, account=None, dry_run=True, http=Boom())
        h = SignalHook(d, notify=said.append, client=c, now_fn=lambda: clock[0], env={"BOT_WEBHOOK_URL": "https://hook/SECRETTOKEN"})
        h._board = fb = FakeBoard(); h.bot.track = lambda: []; h.bot.track_errors = []
        fb.fail = ValueError("https://hook/SECRETTOKEN refused")
        run(h.track_once()); run(h.track_once())
        assert len(said) == 1 and "Scoreboard hit a problem" in said[0] and "SECRETTOKEN" not in said[0], said
        clock[0] = NOW + timedelta(hours=2)
        run(h.track_once())
        assert len(said) == 2
        fb.fail = RuntimeError("Discord is hiding message text: turn on Message Content Intent for the bot")
        clock[0] = NOW + timedelta(hours=4)
        run(h.track_once())
        assert "Message Content Intent" in said[-1], said[-1]                   # our own wording is shown


def test_gold_loop_reads_posts_and_publishes():
    with tempfile.TemporaryDirectory() as d:
        said = []; h = mkb(d, said, env={"DISCORD_BOT_TOKEN": "t", "GOLD_CHANNEL_ID": "5", "GOLD_BOARD_WEBHOOK_URL": "https://hook/g"})
        h._gold_board = fb = FakeBoard()
        h._gold.st["trades"] = [["2026-10-14T09:00:00+00:00", 2.0], ["2026-10-14T10:00:00+00:00", -1.0]]
        h._gold.poll = lambda: False
        async def one():
            import signal_bot_hook as m
            m.GOLD_POLL_S = 0.01
            t = asyncio.create_task(h._gold_loop()); await asyncio.sleep(0.1); t.cancel()
            try: await t
            except asyncio.CancelledError: pass
        run(one())
        assert len(fb.pubs) == 1 and fb.pubs[0][0] == "gold" and "+1.00R" in fb.pubs[0][1]["description"], fb.pubs
        def bad(): raise RuntimeError("Discord is hiding message text")
        h._gold.poll = bad; h._soft_err.clear()
        run(one())
        assert sum("Gold scoreboard hit a problem" in t for t in said) == 1, said     # loop survives and reports once


def test_every_signal_board_needs_all_its_settings_and_the_oanda_token():
    with tempfile.TemporaryDirectory() as d:
        base = {"DISCORD_BOT_TOKEN": "t", "SOURCE_CHANNEL_ID": "7", "SOURCE_BOARD_WEBHOOK_URL": "https://hook/i"}
        assert mkb(d, [], env=base)._chan is not None and mkb(d, [], env=base)._chan.tp_n == 1
        assert mkb(d, [], env=dict(base, SOURCE_TARGET_TP="2"))._chan.tp_n == 2
        assert mkb(d, [], env=dict(base, SOURCE_TARGET_TP="abc"))._chan.tp_n == 1
        assert mkb(d, [], env=base)._chan.entry_mode == "near"
        assert mkb(d, [], env=dict(base, SOURCE_ENTRY=" Bottom "))._chan.entry_mode == "bottom"
        assert mkb(d, [], env=dict(base, SOURCE_ENTRY="nonsense"))._chan.entry_mode == "near"
        for k in base:
            e = dict(base); e.pop(k)
            assert mkb(d, [], env=e)._chan is None, k
        assert mkb(d, [], env=dict(base, SCOREBOARD="0"))._chan is None


def test_every_signal_loop_publishes_and_reports_oanda_trouble_once():
    with tempfile.TemporaryDirectory() as d:
        said = []
        h = mkb(d, said, env={"DISCORD_BOT_TOKEN": "t", "SOURCE_CHANNEL_ID": "7", "SOURCE_BOARD_WEBHOOK_URL": "https://hook/i"})
        h._chan_board = fb = FakeBoard()
        calls = []
        h._chan.poll = lambda: calls.append("poll") or False
        h._chan.score = lambda: (False, ValueError("OANDA said no"))
        async def spin():
            import signal_bot_hook as m
            m.SOURCE_POLL_S = 0.01
            t = asyncio.create_task(h._chan_loop()); await asyncio.sleep(0.15); t.cancel()
            try: await t
            except asyncio.CancelledError: pass
        run(spin())
        assert len(calls) > 2 and len(fb.pubs) == 1 and fb.pubs[0][0] == "source", (len(calls), fb.pubs)
        assert "No closed trades yet" in fb.pubs[0][1]["description"], fb.pubs[0][1]["description"]
        assert sum("Signal scoring hit a problem" in t for t in said) == 1, said
        h._chan.poll = lambda: (_ for _ in ()).throw(RuntimeError("Discord is hiding message text"))
        run(spin())
        assert any("Signal channel scoreboard hit a problem" in t for t in said), said


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
