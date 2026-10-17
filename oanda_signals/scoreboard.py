"""Live scoreboards for Discord channels: ONE message per board that is edited in place (total R, win rate, drawdown and an equity-curve chart).
Nothing here touches orders. Reads results from the bot's own records (OANDA board) or from the Xau bot's posts in a channel (gold board).
Secrets (the Discord bot token and the webhook links) come from the environment and are never printed or logged."""
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:                                   # no time-zone data: fall back to UTC rather than fail
    LONDON = timezone.utc

DISCORD_EPOCH_MS = 1420070400000
API = "https://discord.com/api/v10"


def fmt_r(x):
    return f"{x:+.2f}R"


def parse_time(s):
    """OANDA and Discord timestamps (nanosecond or microsecond fractions, Z or +00:00) to an aware UTC datetime. None if unreadable."""
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", str(s)).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------- numbers
def stats(trades, now):
    """trades: list of (closed_at datetime, r). Returns the figures the board shows."""
    ts = sorted(trades, key=lambda x: x[0])
    rs = [r for _, r in ts]
    curve, run, peak, dd = [0.0], 0.0, 0.0, 0.0
    for r in rs:
        run += r
        curve.append(round(run, 4))
        peak = max(peak, run)
        dd = min(dd, run - peak)
    local = now.astimezone(LONDON)
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=day0.weekday())
    today = [r for t, r in ts if t.astimezone(LONDON) >= day0]
    week = [r for t, r in ts if t.astimezone(LONDON) >= week0]
    wins, losses = sum(1 for r in rs if r > 0), sum(1 for r in rs if r < 0)
    return dict(n=len(rs), wins=wins, losses=losses, total=round(run, 4), avg=(run / len(rs) if rs else 0.0), winrate=(100.0 * wins / len(rs) if rs else 0.0),
                max_dd=round(dd, 4), today_r=sum(today), today_n=len(today), week_r=sum(week), week_n=len(week), curve=curve)


# ---------------------------------------------------------------- chart
def chart_png(curve, title):
    """Equity curve in R, one point per closed trade. Returns PNG bytes, or None when Pillow is not installed."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    W, H, L, R_, T, B = 900, 340, 64, 24, 52, 40
    bg, grid, txt, dim = (17, 20, 26), (44, 50, 62), (214, 219, 228), (128, 136, 150)
    up, down = (74, 201, 120), (240, 98, 98)
    try:
        f, fs, fb = ImageFont.load_default(size=15), ImageFont.load_default(size=13), ImageFont.load_default(size=20)
    except TypeError:                                # old Pillow: only the small built-in font
        f = fs = fb = ImageFont.load_default()
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    d.text((L, 14), title, fill=txt, font=f)
    n = len(curve) - 1
    if n < 1:
        d.text((W // 2 - 90, H // 2 - 8), "No closed trades yet", fill=dim, font=f)
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()
    lo, hi = min(0.0, min(curve)), max(0.0, max(curve))
    span = max(hi - lo, 1.0)
    step = next(s for s in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500) if span / s <= 6)
    lo_t, hi_t = (lo // step) * step, -((-hi) // step) * step
    if hi_t == lo_t:
        hi_t = lo_t + step
    def X(i): return L + (W - L - R_) * (i / n)
    def Y(v): return T + (H - T - B) * (1 - (v - lo_t) / (hi_t - lo_t))
    v = lo_t
    while v <= hi_t + 1e-9:
        d.line([(L, Y(v)), (W - R_, Y(v))], fill=(grid if abs(v) > 1e-9 else (96, 104, 120)), width=1)
        d.text((8, Y(v) - 8), f"{v:+g}R" if abs(v) > 1e-9 else "0R", fill=dim, font=fs)
        v += step
    color = up if curve[-1] >= 0 else down
    pts = [(X(i), Y(c)) for i, c in enumerate(curve)]
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).polygon(pts + [(X(n), Y(0)), (X(0), Y(0))], fill=color + (46,))
    im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(im)
    d.line(pts, fill=color, width=3, joint="curve")
    if n <= 60:
        for x, y in pts[1:]:
            d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=color)
    d.text((W - R_ - 8 - 92, 10), f"{curve[-1]:+.2f}R", fill=color, font=fb)
    d.text((L, H - 28), "0", fill=dim, font=fs)
    d.text((W - R_ - 100, H - 28), f"trade {n}", fill=dim, font=fs)
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


# ---------------------------------------------------------------- the Discord message
def build_embed(title, st, extra_fields, now, has_image):
    local = now.astimezone(LONDON)
    desc = f"**{fmt_r(st['total'])}** over {st['n']} closed trade{'s' if st['n'] != 1 else ''}" if st["n"] else "No closed trades yet."
    fields = [
        dict(name="Win rate", value=f"{st['winrate']:.0f}%  ({st['wins']}W / {st['losses']}L)" if st["n"] else "-", inline=True),
        dict(name="Average per trade", value=fmt_r(st["avg"]) if st["n"] else "-", inline=True),
        dict(name="Worst dip", value=fmt_r(st["max_dd"]) if st["n"] else "-", inline=True),
        dict(name="Today", value=f"{fmt_r(st['today_r'])}  ({st['today_n']} trade{'s' if st['today_n'] != 1 else ''})", inline=True),
        dict(name="This week", value=f"{fmt_r(st['week_r'])}  ({st['week_n']} trade{'s' if st['week_n'] != 1 else ''})", inline=True),
    ] + list(extra_fields)
    e = dict(title=title, description=desc, color=(0x2ECC71 if st["total"] >= 0 else 0xE74C3C), fields=fields,
             footer=dict(text=f"Updated {local:%a %d %b %H:%M} UK time. R = profit or loss divided by the amount risked."))
    if has_image:
        e["image"] = dict(url="attachment://chart.png")
    return e


def signature(title, st, extra_fields):
    """Changes only when what the board SHOWS changes, so an unchanged board is never edited."""
    core = json.dumps([title, st["n"], st["wins"], st["losses"], round(st["total"], 2), round(st["max_dd"], 2), round(st["today_r"], 2), st["today_n"],
                       round(st["week_r"], 2), st["week_n"], [(f["name"], f["value"]) for f in extra_fields]], sort_keys=True)
    return hashlib.sha1(core.encode()).hexdigest()


class DiscordBoard:
    """Posts the board once through a channel webhook, then edits that same message. Remembers the message id in a small file."""
    def __init__(self, webhook_url, state_path, http=requests):
        self.url, self.path, self.http = webhook_url, state_path, http

    def _load(self):
        try:
            return json.load(open(self.path))
        except Exception:
            return {}

    def _save(self, st):
        json.dump(st, open(self.path, "w"))

    @staticmethod
    def _payload(body, files):
        """A picture goes up as a multipart form; without one it is plain JSON."""
        return dict(data={"payload_json": json.dumps(body)}, files=files) if files else dict(json=body)

    def is_current(self, key, sig):
        cur = self._load().get(key) or {}
        return bool(cur.get("id")) and cur.get("sig") == sig

    def publish(self, key, embed, png, sig):
        """Returns 'unchanged', 'posted' or 'edited'."""
        st = self._load()
        cur = st.get(key) or {}
        if cur.get("id") and cur.get("sig") == sig:
            return "unchanged"
        body = dict(embeds=[embed], allowed_mentions=dict(parse=[]))
        files = {"files[0]": ("chart.png", png, "image/png")} if png else None
        if cur.get("id"):
            body["attachments"] = [dict(id=0, filename="chart.png")] if png else []
            r = self.http.patch(f"{self.url}/messages/{cur['id']}", timeout=30, **self._payload(body, files))
            if r.status_code != 404:                  # 404: someone deleted the message, so post a fresh one below
                r.raise_for_status()
                st[key] = dict(id=cur["id"], sig=sig)
                self._save(st)
                return "edited"
        body.pop("attachments", None)
        body["username"] = "Scoreboard"
        r = self.http.post(self.url, params={"wait": "true"}, timeout=30, **self._payload(body, files))
        r.raise_for_status()
        st[key] = dict(id=str(r.json()["id"]), sig=sig)
        self._save(st)
        return "posted"


# ---------------------------------------------------------------- the OANDA signal bot's board
def oanda_inputs(bot, now):
    """Closed trades and the extra fields for the signal bot's board, from the bot's own records."""
    sigs = [r for r in bot.st["signals"].values() if bot._real(r)]
    trades = []
    for r in sigs:
        if r.get("result"):
            t = parse_time(r["result"].get("closed_at")) or parse_time(r.get("ts")) or now
            trades.append((t, r["result"]["r"]))
    tl = bot.tally()
    open_n = tl["filled"] - tl["closed"]
    st = stats(trades, now)
    extra = [
        dict(name="Practice orders", value=f"{tl['placed']} placed, {tl['filled']} filled, {tl['missed']} missed, {max(open_n, 0)} open", inline=False),
        dict(name="Pass mark (judged once, after 30 signals)", inline=False, value="\n".join([
            f"Signals so far: **{tl['placed']} / 30**",
            f"Total R: **{fmt_r(st['total'])}**  (needs above +5.00R; missed counts as 0R)",
            f"Average per filled trade: **{fmt_r(st['avg'])}**  (needs +0.25R or better)",
            f"Filled: **{tl['filled']} of {tl['placed']}**  (needs at least 15 of 30)",
            f"Worst dip: **{fmt_r(st['max_dd'])}**  (must not be worse than -8.00R)",
            "Not a verdict until 30 signals."])),
    ]
    return st, extra


# ---------------------------------------------------------------- the gold strategy channel (Xau bot posts)
OPEN_RE = re.compile(r"trade opened: (LONG|SHORT) @ ([0-9.]+) \| SL ([0-9.]+) \| TP ([0-9.]+)")
CLOSE_RE = re.compile(r"trade closed: (WIN|LOSS) @ ([0-9.]+)")


def snowflake(dt):
    return str((int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22)


class GoldTracker:
    """Reads the 'trade opened / trade closed' posts of the Xau bot with a read-only Discord bot token and keeps a list of results.
    A win is worth the posted reward-to-risk (TP distance over stop distance, 2.0 on every trade seen); a loss is -1R.
    A close with no opening message is still counted (win 2.0R, loss -1R) and flagged."""
    def __init__(self, token, channel_id, state_path, since, http=requests):
        self.token, self.channel, self.path, self.since, self.http = token, channel_id, state_path, since, http
        try:
            self.st = json.load(open(state_path))
        except Exception:
            self.st = dict(last_id=None, open=None, trades=[], unpaired=0)

    def _page(self, after):
        r = self.http.get(f"{API}/channels/{self.channel}/messages", params={"limit": 100, "after": after},
                          headers={"Authorization": f"Bot {self.token}"}, timeout=30)
        r.raise_for_status()
        return sorted(r.json(), key=lambda m: int(m["id"]))

    def poll(self):
        """Reads new messages, updates the list. Returns True if anything changed."""
        changed, read, after = False, False, self.st["last_id"] or snowflake(self.since)
        while True:
            page = self._page(after)
            if not page:
                break
            read = True
            if len(page) >= 5 and not any((m.get("content") or m.get("embeds")) for m in page):
                raise RuntimeError("Discord is hiding message text: turn on Message Content Intent for the bot")
            for m in page:
                changed |= self._take(m)
            after = page[-1]["id"]
            self.st["last_id"] = after
            if len(page) < 100:
                break
        if read:                                     # remember how far we got even when nothing was a trade
            json.dump(self.st, open(self.path, "w"))
        return changed

    def _take(self, m):
        text = m.get("content") or ""
        mo, mc = OPEN_RE.search(text), CLOSE_RE.search(text)
        if mo:
            side, e, sl, tp = mo.group(1), float(mo.group(2)), float(mo.group(3)), float(mo.group(4))
            self.st["open"] = dict(side=side, e=e, sl=sl, tp=tp)
            return True
        if mc:
            win, op = mc.group(1) == "WIN", self.st.get("open")
            if op and abs(op["e"] - op["sl"]) > 0:
                r = abs(op["tp"] - op["e"]) / abs(op["e"] - op["sl"]) if win else -1.0
            else:
                r = 2.0 if win else -1.0
                self.st["unpaired"] = self.st.get("unpaired", 0) + 1
            self.st["open"] = None
            self.st["trades"].append([m["timestamp"], round(r, 3)])
            return True
        return False

    def inputs(self, now):
        trades = [(parse_time(t) or now, r) for t, r in self.st["trades"]]
        st = stats(trades, now)
        extra = []
        if self.st.get("open"):
            o = self.st["open"]
            extra.append(dict(name="Open now", value=f"{o['side']} @ {o['e']:g}  (stop {o['sl']:g}, target {o['tp']:g})", inline=False))
        if self.st.get("unpaired"):
            extra.append(dict(name="Note", value=f"{self.st['unpaired']} close(s) had no opening message; counted as win +2.00R or loss -1.00R.", inline=False))
        return st, extra
