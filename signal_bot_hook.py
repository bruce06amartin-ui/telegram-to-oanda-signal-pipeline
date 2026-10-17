"""Glue between the Telegram forwarder and the signal bot. Switched on only by SIGNAL_BOT=1; with it off, nothing here runs.
Any failure here is caught and reported, and never stops the forwarder."""
import asyncio
import logging
import os
from datetime import datetime, timezone

from oanda_signals import rules
from oanda_signals import scoreboard as sb
from oanda_signals.signal_channel import SignalChannel
from oanda_signals.bot import Bot
from oanda_signals.client import Oanda
from oanda_signals.signal_parser import parse_hit, parse_signal

log = logging.getLogger("signal_bot")
TRACK_EVERY_S = 45
GOLD_POLL_S = 60
OANDA_BOARD_TITLE = "Signal bot: practice results"
GOLD_BOARD_TITLE = "Gold strategy (Xau bot): results"
SOURCE_BOARD_TITLE = "Signal channel: every signal, scored as if taken"
SOURCE_POLL_S = 120


def compose(own, reply_text):
    """Own text first, then the message it answers as '> ' lines (the format the bot's reply splitter reads)."""
    out = (own or "").strip()
    if reply_text:
        out += "\n" + "\n".join("> " + ln for ln in reply_text.splitlines())
    return out


def tally_text(t):
    return (f"Practice orders so far: {t['placed']} placed, {t['filled']} filled, {t['missed']} missed, {t['closed']} closed. "
            f"Closed trades total {t['total_r']:+.2f}R (missed counts as 0R).")


def describe(rec):
    k, dry = rec["kind"], "DRY RUN " if rec.get("dry_run") else ""
    if k == "fill":
        return f"FILLED {rec['side'].upper()} {abs(rec['units'])} XAU_USD at {rec['price']:g} (limit {rec['entry']:g}); stop {rec['sl']:g}, target {rec['tp3']:g}"
    if k == "close":
        return (f"CLOSED {rec['side'].upper()} by {rec['by']} at {rec['close']:g}: {rec['pl']:+.2f} {rec.get('currency', '')}".rstrip() + f" = {rec['r']:+.2f}R "
                f"(planned risk {rec['risk_cash']:.2f}). " + tally_text(rec["tally"]))
    if k == "expired":
        return f"ORDER EXPIRED unfilled (limit {rec['entry']:g}); counted as missed, 0R. " + tally_text(rec["tally"])
    if k in ("would_place", "placed"):
        side = "BUY" if rec["units"] > 0 else "SELL"
        return (f"{dry}{'WOULD PLACE' if k == 'would_place' else 'PLACED'} {side} {abs(rec['units'])} XAU_USD limit {rec['entry']}  SL {rec['sl']}  TP3 {rec['tp3']}  "
                f"risk {rec['risk_pct']}%" + ("  (account figures assumed, no token read)" if rec.get("assumed_account") else "") + (f"  [{rec['reason']}]" if rec.get("reason") else ""))
    if k == "skip":
        return f"{dry}SKIPPED: {rec['reason']}"
    if k == "cancel":
        return f"{dry}CANCEL: {rec['reason']}"
    if k == "hit":
        return f"Group reports TP{rec['n']} hit"
    if k == "duplicate":
        return "Duplicate signal ignored"
    return f"{k}: {rec.get('note', '')}".strip()


class SignalHook:
    def __init__(self, data_dir, notify=None, client=None, now_fn=None, selftest=None, env=None):
        os.makedirs(data_dir, exist_ok=True)
        env = os.environ if env is None else env
        self.data_dir = data_dir
        self.selftest = (os.environ.get("SELFTEST") == "1") if selftest is None else selftest     # opt-in start-up check of the order format
        self.client = client or Oanda()
        self.notify = notify or (lambda text: None)
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.bot = Bot(self.client, os.path.join(data_dir, "bot_state.json"), os.path.join(data_dir, "decisions.jsonl"),
                       os.path.join(data_dir, "KILL"), now_fn=self.now)
        self._lock = asyncio.Lock()               # one message handled at a time, so a signal can never be ordered twice
        self._msg_keys = {}                       # Telegram message id -> the signal it last read as (memory only)
        self._track_err_at = None                 # when a tracking problem was last reported (so a bad patch of OANDA does not spam the channel)
        self._track_task = None
        self._soft_err = {}                       # when each scoreboard problem was last reported
        self._board = self._gold = self._gold_board = self._gold_task = None
        self._chan = self._chan_board = self._chan_task = None
        if env.get("SCOREBOARD", "1") != "0":
            url = env.get("SCOREBOARD_WEBHOOK_URL") or env.get("BOT_WEBHOOK_URL")
            if url:
                self._board = sb.DiscordBoard(url, os.path.join(data_dir, "scoreboard_oanda.json"))
            tok, chan, gurl = env.get("DISCORD_BOT_TOKEN"), env.get("GOLD_CHANNEL_ID"), env.get("GOLD_BOARD_WEBHOOK_URL")
            if tok and chan and gurl:
                since = datetime.fromisoformat(env.get("GOLD_SINCE", "2026-10-12")).replace(tzinfo=sb.LONDON)
                self._gold = sb.GoldTracker(tok, chan, os.path.join(data_dir, "gold_state.json"), since)
                self._gold_board = sb.DiscordBoard(gurl, os.path.join(data_dir, "scoreboard_gold.json"))
            cchan, curl = env.get("SOURCE_CHANNEL_ID"), env.get("SOURCE_BOARD_WEBHOOK_URL")
            if tok and cchan and curl:
                since = datetime.fromisoformat(env.get("SOURCE_SINCE", "2026-10-12")).replace(tzinfo=sb.LONDON)
                try:
                    tp_n = int(env.get("SOURCE_TARGET_TP", "1"))
                except ValueError:
                    tp_n = 1
                entry_mode = (env.get("SOURCE_ENTRY") or "near").strip().lower()      # near / far / top / bottom / mid; anything else = near
                self._chan = SignalChannel(tok, cchan, os.path.join(data_dir, "source_channel.json"), since, self.client, self.now, tp_n=tp_n, entry_mode=entry_mode)
                self._chan_board = sb.DiscordBoard(curl, os.path.join(data_dir, "scoreboard_source.json"))

    async def _say(self, text):
        try:
            await asyncio.to_thread(self.notify, text[:1900])
        except Exception as e:
            log.warning("could not post to the bot channel: %s", type(e).__name__)

    async def start_check(self):
        """Read-only check of the OANDA connection at start-up, reported in plain words. Places nothing."""
        mode = "DRY RUN (nothing will be ordered)" if self.client.dry_run else "LIVE PRACTICE ORDERS ON"
        if self._gold and self._gold_task is None:
            self._gold_task = asyncio.create_task(self._gold_loop())      # the gold-channel scoreboard needs no OANDA token
        if self._chan and self._chan_task is None and self.client.can_read:
            self._chan_task = asyncio.create_task(self._chan_loop())      # every-signal board: needs the OANDA token for price history
        if not self.client.can_read:
            await self._say(f"Signal bot online, {mode}. No OANDA token or account id set, so it will use made-up account figures.")
            return
        try:
            s = await asyncio.to_thread(self.client.summary)
            i = await asyncio.to_thread(self.client.instrument, rules.INSTRUMENT)
            p = await asyncio.to_thread(self.client.pricing, rules.INSTRUMENT, s["currency"])
            await self._say(f"Signal bot online, {mode}. OANDA connected: {s['currency']} {s['nav']:,.2f}, gold {p['bid']}/{p['ask']}, margin rate {i['margin_rate']}, "
                            f"price dp {i['price_dp']}, units precision {i['units_precision']}.")
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", "")
            await self._say(f"Signal bot online, {mode}, but the OANDA check FAILED ({type(e).__name__} {code}). Check the token and account id.")
            log.error("OANDA start-up check failed: %s %s", type(e).__name__, code)
        if self.selftest:
            await self._self_test()
        if self._track_task is None:
            self._track_task = asyncio.create_task(self._track_loop())      # reports fills, closes and expiries as they happen

    async def _track_loop(self):
        while True:
            await asyncio.sleep(TRACK_EVERY_S)
            await self.track_once()

    def _publish_sync(self, board, key, title, st, extra, now):
        sig = sb.signature(title, st, extra)
        if board.is_current(key, sig):
            return "unchanged"
        png = sb.chart_png(st["curve"], title)
        return board.publish(key, sb.build_embed(title, st, extra, now, png is not None), png, sig)

    async def _report_soft(self, name, e):
        """A scoreboard problem is reported at most once an hour. Only our own RuntimeError text is shown: an HTTP error's text can contain a webhook link."""
        now = self.now()
        log.error("%s problem: %s", name, type(e).__name__)
        last = self._soft_err.get(name)
        if last is None or (now - last).total_seconds() > 3600:
            self._soft_err[name] = now
            code = getattr(getattr(e, "response", None), "status_code", "")
            why = f": {str(e)[:150]}" if type(e) is RuntimeError else ""
            await self._say(f"{name} hit a problem ({type(e).__name__} {code}){why}. The trading side is unaffected; it will keep trying.")

    async def refresh_board(self):
        """Updates the signal bot's scoreboard message if what it shows has changed."""
        if not self._board:
            return
        try:
            now = self.now()
            async with self._lock:
                st, extra = sb.oanda_inputs(self.bot, now)
            await asyncio.to_thread(self._publish_sync, self._board, "oanda", OANDA_BOARD_TITLE, st, extra, now)
        except Exception as e:
            await self._report_soft("Scoreboard", e)

    async def _gold_loop(self):
        while True:
            try:
                await asyncio.to_thread(self._gold.poll)
                now = self.now()
                st, extra = self._gold.inputs(now)
                await asyncio.to_thread(self._publish_sync, self._gold_board, "gold", GOLD_BOARD_TITLE, st, extra, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._report_soft("Gold scoreboard", e)
            await asyncio.sleep(GOLD_POLL_S)

    async def _chan_loop(self):
        while True:
            try:
                await asyncio.to_thread(self._chan.poll)
                _, err = await asyncio.to_thread(self._chan.score)
                if err is not None:
                    await self._report_soft("Signal scoring", err)
                now = self.now()
                st, extra = self._chan.inputs(now)
                await asyncio.to_thread(self._publish_sync, self._chan_board, "source", SOURCE_BOARD_TITLE, st, extra, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._report_soft("Signal channel scoreboard", e)
            await asyncio.sleep(SOURCE_POLL_S)

    async def track_once(self):
        """One look at OANDA for what became of our orders. Read-only; any failure is reported at most once an hour and never reaches the forwarder."""
        try:
            async with self._lock:
                recs = await asyncio.to_thread(self.bot.track)
            errs = list(self.bot.track_errors)
            for rec in recs:
                await self._say(describe(rec))
            if errs:
                e, now = errs[0], self.now()
                log.error("tracking problem: %s", type(e).__name__)
                if self._track_err_at is None or (now - self._track_err_at).total_seconds() > 3600:
                    self._track_err_at = now
                    code = getattr(getattr(e, "response", None), "status_code", "")
                    await self._say(f"Result tracking hit a problem ({type(e).__name__} {code}); it will keep trying. Check OANDA by hand if this repeats.")
        except Exception as e:
            log.exception("tracking failed")
        await self.refresh_board()

    async def _self_test(self):
        """Opt-in (SELFTEST=1) and runs before any signal is handled. Places one far-from-market 1-unit practice order, reads it back, cancels it.
        Until it has passed once, real orders are held back (the bot stays in dry run), so a wrong order format can never leave a position unprotected."""
        marker = os.path.join(self.data_dir, "selftest_passed.json")
        if self.client.dry_run:
            await self._say("SELFTEST is on, but the bot is in DRY RUN, so no test order was placed. It runs when OANDA_DRY_RUN is set to 0.")
            return
        if os.path.exists(marker):
            await self._say("Self-test passed earlier: OANDA accepts our order format. Not repeated; SELFTEST can go back to 0.")
            return
        verdict, steps = await asyncio.to_thread(self.client.self_test, rules.INSTRUMENT, self.now())
        detail = "\n".join("- " + t for t in steps)
        if verdict == "pass":
            try:
                with open(marker, "w") as f:
                    f.write('{"passed": "%s"}' % self.now().isoformat(timespec="seconds"))
            except OSError as e:
                log.warning("could not save the self-test result: %s", type(e).__name__)
            await self._say("SELF-TEST PASSED: OANDA accepts our order format, the stop, target and expiry were kept, and the test order was cancelled. Practice orders are live.\n" + detail)
            return
        self.client.dry_run = True
        await self._say(f"SELF-TEST {'FAILED' if verdict == 'fail' else 'NOT RUN'}. Real orders are HELD BACK: the bot is in DRY RUN until a restart passes the test. Nothing was ordered for a signal.\n" + detail)

    def _when(self, msg):
        return msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)

    async def _report_error(self, e):
        code = getattr(getattr(e, "response", None), "status_code", "")
        log.exception("signal bot failed on a message")
        await self._say(f"Signal bot ERROR on a message: {type(e).__name__} {code}. The forwarder is unaffected.")

    def _remember(self, msg, text):
        mid, sig = getattr(msg, "id", None), parse_signal(text)
        if mid is not None and sig:
            self._msg_keys[mid] = sig["key"]
            if len(self._msg_keys) > 500:         # keep it small
                self._msg_keys.pop(next(iter(self._msg_keys)))

    async def __call__(self, msg):
        try:
            when = self._when(msg)
            age = (self.now() - when).total_seconds() / 60
            if age > rules.MAX_AGE_MIN:
                log.info("signal bot: ignored a message %.0f minutes old", age)
                return
            reply_text = None
            from forwarder import is_real_reply
            if is_real_reply(msg):
                r = await msg.get_reply_message()
                reply_text = getattr(r, "raw_text", None) if r else None
            text = compose(getattr(msg, "raw_text", None) or "", reply_text)
            async with self._lock:
                recs = await asyncio.to_thread(self.bot.handle, text, when)
                self._remember(msg, getattr(msg, "raw_text", None) or "")     # only after it worked, so a failed attempt can be retried by a later edit
            for rec in recs:
                await self._say(describe(rec))
        except Exception as e:
            await self._report_error(e)

    async def edited(self, msg):
        """The group often posts a signal and fixes it a minute later, so the full numbers arrive as an EDIT.
        An edited message is acted on only when it now reads as a signal: edited TP updates and chat are ignored, a signal
        already handled is ignored quietly, and if an edit changes a signal's numbers the old unfilled order is replaced.
        The 10-minute age limit counts from when the message was first posted."""
        try:
            when = self._when(msg)
            if (self.now() - when).total_seconds() / 60 > rules.MAX_AGE_MIN:
                return
            own = getattr(msg, "raw_text", None) or ""
            sig = parse_signal(own)
            if parse_hit(own) or not sig:
                return
            mid = getattr(msg, "id", None)
            async with self._lock:
                before = self._msg_keys.get(mid) if mid is not None else None
                if before == sig["key"]:
                    return                        # cosmetic edit, already handled
                recs = await asyncio.to_thread(self.bot.handle, own, when, before is not None)
                self._remember(msg, own)
            for rec in recs:
                if rec["kind"] != "duplicate":
                    await self._say("(edited message) " + describe(rec))
        except Exception as e:
            await self._report_error(e)


def make_hook(env=None):
    env = os.environ if env is None else env
    from forwarder import DiscordSender
    url = env.get("BOT_WEBHOOK_URL")
    sender = DiscordSender(url) if url else None
    notify = (lambda t: sender.post(t, None, "Signal bot")) if sender else None
    return SignalHook(env.get("DATA_DIR", "/tmp"), notify=notify, env=env)
