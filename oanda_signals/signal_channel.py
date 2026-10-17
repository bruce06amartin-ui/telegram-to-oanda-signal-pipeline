"""Scores EVERY signal posted in a Discord channel (the forwarded Telegram group), whether or not the OANDA bot traded it.
Read-only: it reads the channel with a Discord bot token and OANDA's one-minute price history. It never places or cancels anything.

Each signal is scored as the bot's plan would have been: one limit order at the chosen point of the zone (default: the edge nearest the market - top of a buy zone, bottom of a sell zone; set with SOURCE_ENTRY), the group's stop, the chosen take-profit (TP1, TP2 or TP3; default TP1) as the target,
cancelled if the group reports a take-profit before the order fills (0R) or if it is still unfilled after EXPIRY_HOURS (0R).
Win = the chosen take-profit reached first = its reward-to-risk in R; loss = stop reached first = -1R. Price history only shows one-minute highs and lows, so when the
stop and the entry fall in the same minute it is scored as a loss, and a target reached in the entry minute is not counted until the next minute.
Spread: buys fill and stop on the ask / exit on the bid (and the reverse for sells). Slippage and commission are not modelled."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import requests

from . import rules
from .bot import split_reply
from .scoreboard import API, LONDON, parse_time, snowflake, stats
from .signal_parser import parse_hit, parse_signal

REREAD_HOURS = 6          # recent messages are re-read every pass, so a signal completed by an edit is picked up
DEFAULT_TP = 1          # which take-profit this board scores against (1, 2 or 3); set with SOURCE_TARGET_TP. The practice bot keeps its frozen TP3 (rules.TP_INDEX)
STALE_DAYS = 3            # a filled trade still unresolved after this long is shown as open and counted as 0R in the totals


DEFAULT_ENTRY = "near"
ENTRY_MODES = ("near", "far", "top", "bottom", "mid")
ENTRY_WORDS = dict(
    near="the edge of the zone nearest the market (top of a buy zone, bottom of a sell zone)",
    far="the edge of the zone furthest from the market (bottom of a buy zone, top of a sell zone)",
    top="the top of the zone (its higher price, buy or sell)",
    bottom="the bottom of the zone (its lower price, buy or sell)",
    mid="the middle of the zone")


def entry_of(sig, mode=DEFAULT_ENTRY):
    """Entry price for this board, chosen with SOURCE_ENTRY. 'near' (default) is the edge nearest the market, which leaves the widest
    stop distance for a buy; 'far' is the opposite edge; 'top'/'bottom' are the zone's higher/lower price whatever the direction."""
    lo, hi, buy = sig["lo"], sig["hi"], sig["dir"] > 0
    if mode == "far":
        return lo if buy else hi
    if mode == "top":
        return hi
    if mode == "bottom":
        return lo
    if mode == "mid":
        return round((lo + hi) / 2, 5)
    return hi if buy else lo


def _h(text):
    return hashlib.sha1((text or "").encode()).hexdigest()[:12]


def simulate(sig, t0, hit_at, candles, now, tp_n=DEFAULT_TP, entry_mode=DEFAULT_ENTRY):
    """sig: parsed signal dict. t0: when it was posted. hit_at: when the group first reported a take-profit for it (or None).
    candles: oldest-first dicts from Oanda.candles. Returns dict(status, r, at, fill_at) with status one of
    'pending', 'open', 'win', 'loss', 'missed', 'expired'."""
    d, e, sl = sig["dir"], entry_of(sig, entry_mode), sig["sl"]
    tp = sig["tps"][tp_n - 1]
    risk = abs(e - sl)
    reward_r = round(abs(tp - e) / risk, 3)
    expiry = t0 + timedelta(hours=rules.EXPIRY_HOURS)
    t0m = t0.replace(second=0, microsecond=0)
    hitm = hit_at.replace(second=0, microsecond=0) if hit_at else None
    filled_at = None
    for c in candles:
        t = c["t"]
        if t < t0m:
            continue
        if filled_at is None:
            if hitm and t >= hitm:
                return dict(status="missed", r=0.0, at=t.isoformat(), fill_at=None)
            if t >= expiry:
                return dict(status="expired", r=0.0, at=t.isoformat(), fill_at=None)
            touched = c["ask_l"] <= e if d > 0 else c["bid_h"] >= e
            if not touched:
                continue
            filled_at = t
            stopped = c["bid_l"] <= sl if d > 0 else c["ask_h"] >= sl
            if stopped:
                return dict(status="loss", r=-1.0, at=t.isoformat(), fill_at=t.isoformat())
            continue                                            # a target reached in the entry minute is not counted
        stopped = c["bid_l"] <= sl if d > 0 else c["ask_h"] >= sl
        if stopped:
            return dict(status="loss", r=-1.0, at=t.isoformat(), fill_at=filled_at.isoformat())
        won = c["bid_h"] >= tp if d > 0 else c["ask_l"] <= tp
        if won:
            return dict(status="win", r=reward_r, at=t.isoformat(), fill_at=filled_at.isoformat())
    if filled_at is None:
        if hitm and now >= hitm:
            return dict(status="missed", r=0.0, at=hit_at.isoformat(), fill_at=None)
        if now >= expiry:
            return dict(status="expired", r=0.0, at=expiry.isoformat(), fill_at=None)
        return dict(status="pending", r=0.0, at=None, fill_at=None)
    return dict(status="open", r=0.0, at=None, fill_at=filled_at.isoformat())


class SignalChannel:
    def __init__(self, token, channel_id, state_path, since, oanda, now_fn, http=requests, tp_n=DEFAULT_TP, entry_mode=DEFAULT_ENTRY):
        self.tp_n = tp_n if tp_n in (1, 2, 3) else DEFAULT_TP
        self.entry_mode = entry_mode if entry_mode in ENTRY_MODES else DEFAULT_ENTRY
        self.rule = f"edge-tp{self.tp_n}-v1" if self.entry_mode == DEFAULT_ENTRY else f"{self.entry_mode}-tp{self.tp_n}-v1"      # stored results are recomputed whenever this changes
        self.token, self.channel, self.path, self.since, self.oanda, self.now, self.http = token, channel_id, state_path, since, oanda, now_fn, http
        try:
            self.st = json.load(open(state_path))
        except Exception:
            self.st = dict(last_id=None, msgs={}, signals={}, hits={}, results={})
        if self.st.get("rule") != self.rule:                       # scoring rule changed: old results were scored another way, so score again
            self.st["rule"], self.st["results"] = self.rule, {}

    # ------------------------------------------------------------ reading the channel
    def _page(self, after):
        r = self.http.get(f"{API}/channels/{self.channel}/messages", params={"limit": 100, "after": after},
                          headers={"Authorization": f"Bot {self.token}"}, timeout=30)
        r.raise_for_status()
        return sorted(r.json(), key=lambda m: int(m["id"]))

    def poll(self):
        """Reads new messages and re-reads the last few hours (edits). Returns True if anything changed."""
        now = self.now()
        start = snowflake(self.since)
        if self.st["last_id"]:
            start = str(min(int(self.st["last_id"]), int(snowflake(now - timedelta(hours=REREAD_HOURS)))))
            start = str(max(int(start), int(snowflake(self.since))))
        changed, after = False, start
        while True:
            page = self._page(after)
            if not page:
                break
            if len(page) >= 5 and not any((m.get("content") or m.get("embeds")) for m in page):
                raise RuntimeError("Discord is hiding message text: turn on Message Content Intent for the bot")
            for m in page:
                changed |= self._take(m)
            after = page[-1]["id"]
            if int(after) > int(self.st["last_id"] or 0):
                self.st["last_id"] = after
            if len(page) < 100:
                break
        self._save()
        return changed

    def _take(self, m):
        mid, text = str(m["id"]), m.get("content") or ""
        h = _h(text)
        if self.st["msgs"].get(mid) == h:
            return False
        self.st["msgs"][mid] = h
        own, quoted = split_reply(text)
        ts = (parse_time(m.get("timestamp")) or self.now()).isoformat()
        before = (json.dumps(self.st["signals"].get(mid)), json.dumps(self.st["hits"].get(mid)))
        sig = parse_signal(own)
        if sig and mid not in self.st["results"]:
            self.st["signals"][mid] = dict(ts=ts, sig=sig)
        elif sig is None and mid in self.st["signals"] and mid not in self.st["results"]:
            del self.st["signals"][mid]                       # edited into something that is not a signal
        n = parse_hit(own)
        if n:
            ref = parse_signal(quoted)
            self.st["hits"][mid] = dict(ts=ts, n=n, key=(ref["key"] if ref else None), dir=(ref["dir"] if ref else None))
        elif mid in self.st["hits"]:
            del self.st["hits"][mid]
        return before != (json.dumps(self.st["signals"].get(mid)), json.dumps(self.st["hits"].get(mid)))

    def _save(self):
        json.dump(self.st, open(self.path, "w"))

    # ------------------------------------------------------------ scoring
    def _unique_signals(self):
        """Signals in posting order; the same numbers posted again later are one signal (as the bot treats them)."""
        seen, out = set(), []
        for mid in sorted(self.st["signals"], key=int):
            rec = self.st["signals"][mid]
            if rec["sig"]["key"] in seen:
                continue
            seen.add(rec["sig"]["key"])
            sg = rec["sig"]
            if sg["ok"]:
                e = entry_of(sg, self.entry_mode)
                if (e - sg["sl"]) * sg["dir"] <= 0:                                        # the stop must lie beyond the entry
                    sg["ok"], sg["reason"] = False, "the stop is not beyond the entry price"
                elif (sg["tps"][self.tp_n - 1] - e) * sg["dir"] <= 0:                      # the target must lie beyond the entry
                    sg["ok"], sg["reason"] = False, f"TP{self.tp_n} is not beyond the entry price"
            out.append((mid, rec))
        return out

    def _first_hit(self, mid, rec, ordered):
        """When the group first reported a take-profit for this signal: a reply quoting its numbers, or an unquoted/unknown quote assigned to the
        latest earlier signal in the same direction within 8 hours."""
        t0, best = parse_time(rec["ts"]), None
        for hid, hit in self.st["hits"].items():
            th = parse_time(hit["ts"])
            if th is None or th < t0:
                continue
            if hit["key"] == rec["sig"]["key"]:
                ok = True
            elif hit["key"] is None:                       # the quote was cut short or edited: fall back to direction and time (a full quote of an unknown signal is ignored)
                same = [(i, r) for i, r in ordered if (hit["dir"] in (None, r["sig"]["dir"])) and parse_time(r["ts"]) <= th and th - parse_time(r["ts"]) <= timedelta(hours=8)]
                ok = bool(same) and same[-1][0] == mid
            else:
                ok = False
            if ok and (best is None or th < best):
                best = th
        return best

    def score(self):
        """Updates results for every signal not yet final. Returns (changed, error) - an OANDA problem is returned, not raised, so channel reading continues."""
        now, changed, err = self.now(), False, None
        ordered = self._unique_signals()
        todo = [(mid, rec) for mid, rec in ordered if rec["sig"]["ok"] and self.st["results"].get(mid, {}).get("status") not in ("win", "loss", "missed", "expired")]
        if not todo:
            return False, None
        try:
            t_from = min(parse_time(r["ts"]) for _, r in todo)
            candles = self.oanda.candles(rules.INSTRUMENT, t_from.replace(second=0, microsecond=0), now)
        except Exception as e:
            return False, e
        for mid, rec in todo:
            res = simulate(rec["sig"], parse_time(rec["ts"]), self._first_hit(mid, rec, ordered), candles, now, self.tp_n, self.entry_mode)
            if res["status"] == "open" and now - parse_time(rec["ts"]) > timedelta(days=STALE_DAYS):
                res["stale"] = True
            if self.st["results"].get(mid) != res:
                self.st["results"][mid] = res
                changed = True
        self._save()
        return changed, err

    # ------------------------------------------------------------ what the board shows
    def inputs(self, now, title_note="Every signal the group sends, scored as if taken"):
        ordered = self._unique_signals()
        trades, c = [], dict(win=0, loss=0, missed=0, expired=0, open=0, pending=0, bad=0, unscored=0)
        for mid, rec in ordered:
            if not rec["sig"]["ok"]:
                c["bad"] += 1
                continue
            res = self.st["results"].get(mid)
            if not res:
                c["unscored"] += 1
                continue
            s = res["status"]
            c[s] = c.get(s, 0) + 1
            if s in ("win", "loss"):
                trades.append((parse_time(res["at"]) or now, res["r"]))
        st = stats(trades, now)
        seen = len(ordered)
        filled = c["win"] + c["loss"] + c["open"]
        names = dict(win="won", loss="lost", missed="cancelled (target reported first)", expired="never reached entry", open="filled, still open", pending="waiting for entry")
        lines = []
        for mid, rec in ordered[-10:]:
            sg, res = rec["sig"], self.st["results"].get(mid)
            when = parse_time(rec["ts"]).astimezone(LONDON).strftime("%a %d %H:%M")
            if not sg["ok"]:
                what = "unusable: " + (sg.get("reason") or "bad numbers")
            elif not res:
                what = "not scored yet"
            else:
                what = names.get(res["status"], res["status"]) + (f" {res['r']:+.2f}R" if res["status"] in ("win", "loss") else "")
            lines.append(f"{when} {sg['side'].upper()} @{entry_of(sg, self.entry_mode):g} SL {sg['sl']:g}: {what}")
        extra = [
            dict(name="Latest signals (UK time)", value="\n".join(lines)[:1000] or "-", inline=False),
            dict(name="Signals", value=f"{seen} seen: {filled} filled ({c['win']} won, {c['loss']} lost, {c['open']} still open), "
                                       f"{c['missed'] + c['expired']} never filled, {c['pending']} waiting for entry"
                                       + (f", {c['bad']} unusable (bad numbers)" if c["bad"] else "")
                                       + (f", {c['unscored']} not scored yet" if c["unscored"] else ""), inline=False),
            dict(name="How this is counted", value=f"{title_note}. Entry at {ENTRY_WORDS[self.entry_mode]}, the group's stop, TP{self.tp_n} as the target, 1R = the distance to the stop. "
                                                  "Cancelled (0R) if the group reports a target before entry or the entry is not reached within 4 hours. "
                                                  "Not real orders. Scored from OANDA one-minute prices.", inline=False),
        ]
        return st, extra
