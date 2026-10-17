"""Telegram -> Discord forwarder.

Listens to one or more Telegram chats THROUGH YOUR OWN TELEGRAM ACCOUNT (a normal bot cannot read a chat you only belong to)
and mirrors every message to a Discord webhook: text, photos, files, albums, replies, polls, locations, edits and deletions.

Secrets (never put these in the code, never share them): TG_SESSION, TG_API_HASH, DISCORD_WEBHOOK_URL.
Run:  python forwarder.py                 start forwarding
      python forwarder.py --list-chats    print your chats with their ids, then exit
"""
import asyncio, json, logging, os, re, sys, time
from collections import OrderedDict

import requests

MAX_CONTENT = 1900          # Discord's limit is 2000 characters per message
MAX_FILES_PER_POST = 10
MAX_BYTES_PER_POST = 20_000_000
log = logging.getLogger("tg2discord")


# ----------------------------------------------------------------------------- pure helpers (unit tested)
def tg_to_discord_markdown(text):
    """Telethon markdown uses __italic__; Discord uses *italic* (and __x__ means underline). Everything else matches."""
    return re.sub(r"(?<!_)__(?!_)(.+?)(?<!_)__(?!_)", r"*\1*", text or "", flags=re.S)


def split_message(text, limit=MAX_CONTENT):
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            head, line = line[:cut], line[cut:].lstrip(" ")
            if cur:
                parts.append(cur); cur = ""
            parts.append(head)
        if len(cur) + len(line) + (1 if cur else 0) > limit:
            parts.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def chunk_files(files):
    """files = [(name, bytes)]. Group so that each Discord request has <= 10 files and <= ~20 MB."""
    batches, cur, size = [], [], 0
    for f in files:
        if cur and (len(cur) >= MAX_FILES_PER_POST or size + len(f[1]) > MAX_BYTES_PER_POST):
            batches.append(cur); cur, size = [], 0
        cur.append(f); size += len(f[1])
    if cur:
        batches.append(cur)
    return batches


def clean_username(name):
    name = re.sub(r"(?i)discord|clyde", "chat", name or "Telegram")[:80].strip()
    return name if len(name) >= 2 else "Telegram"


def poll_text(poll):
    p = getattr(poll, "poll", poll)
    txt = lambda x: getattr(x, "text", x)
    lines = [f"📊 Poll: {txt(p.question)}"] + [f"• {txt(a.text)}" for a in p.answers]
    return "\n".join(lines)


def render(chat_title, sender, text, reply=None, fwd=None, notes=(), edited=False):
    lines = []
    if sender and sender != chat_title:
        lines.append(f"**{sender}**")
    if fwd:
        lines.append(f"↪ forwarded from {fwd}")
    if reply:
        lines.append("> " + reply.replace("\n", " ")[:200])
    if text:
        lines.append(tg_to_discord_markdown(text))
    lines.extend(notes)
    if edited:
        lines.append("*(edited)*")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- forum topics
def topic_of(msg):
    """Topic id of a message in a forum group (1 = the General topic); None if the chat has no topics."""
    r = getattr(msg, "reply_to", None)
    if not r:
        return 1
    if getattr(r, "forum_topic", False):
        return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)
    return 1


def is_real_reply(msg):
    """True when the message answers another message (not merely sits inside a topic)."""
    r = getattr(msg, "reply_to", None)
    if not r:
        return False
    if getattr(r, "forum_topic", False) and not getattr(r, "reply_to_top_id", None):
        return False
    return True


# ----------------------------------------------------------------------------- Discord webhook client
class DiscordSender:
    def __init__(self, webhook_url, http=requests, sleep=time.sleep):
        base = webhook_url.split("?")[0].rstrip("/")
        self.base, self.http, self.sleep = base, http, sleep
        self.post_url = base + "?wait=true"

    def _request(self, method, url, payload, files=None):
        for attempt in range(6):
            if files:
                r = self.http.request(method, url, data={"payload_json": json.dumps(payload)},
                                      files={f"files[{i}]": (n, d) for i, (n, d) in enumerate(files)}, timeout=60)
            else:
                r = self.http.request(method, url, json=payload, timeout=30)
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except Exception:
                    wait = float(r.headers.get("Retry-After", 2))
                self.sleep(min(wait, 30) + 0.1); continue
            if r.status_code >= 500:
                self.sleep(2 ** attempt); continue
            return r
        raise RuntimeError("Discord kept rate-limiting or failing; message dropped")

    def post(self, content, files=None, username=None):
        chunks = split_message(content) if content else []
        batches = chunk_files(list(files or []))
        ids = []
        for i in range(max(len(chunks), len(batches))):
            payload = {"allowed_mentions": {"parse": []}}      # never let forwarded text ping @everyone or roles
            c = chunks[i] if i < len(chunks) else ""
            if c:
                payload["content"] = c
            if username:
                payload["username"] = clean_username(username)
            batch = batches[i] if i < len(batches) else []
            if not c and not batch:
                continue
            r = self._request("POST", self.post_url, payload, batch)
            r.raise_for_status()
            ids.append(str(r.json()["id"]))
        return ids

    def edit(self, message_id, content):
        r = self._request("PATCH", f"{self.base}/messages/{message_id}", {"content": content[:2000], "allowed_mentions": {"parse": []}})
        return r.status_code < 300

    def delete(self, message_id):
        r = self._request("DELETE", f"{self.base}/messages/{message_id}", None)
        return r.status_code in (200, 204, 404)


# ----------------------------------------------------------------------------- telegram id -> discord id map (so edits and deletes can be mirrored)
class Store:
    def __init__(self, path, cap=5000):
        self.path, self.cap, self.d = path, cap, OrderedDict()
        try:
            with open(path) as fh:
                self.d = OrderedDict(json.load(fh))
        except Exception:
            pass

    @staticmethod
    def key(chat_id, msg_id):
        return f"{chat_id}:{msg_id}"

    def get(self, chat_id, msg_id):
        return self.d.get(self.key(chat_id, msg_id))

    def put(self, chat_id, msg_id, ids, content):
        self.d[self.key(chat_id, msg_id)] = {"ids": ids, "content": content}
        while len(self.d) > self.cap:
            self.d.popitem(last=False)
        self._save()

    def find(self, msg_id):
        return [(k, v) for k, v in self.d.items() if k.split(":")[-1] == str(msg_id)]

    def pop(self, key):
        self.d.pop(key, None); self._save()

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.d, fh)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("could not save id map: %s", e)


# ----------------------------------------------------------------------------- the forwarding logic
class Forwarder:
    def __init__(self, client, sender, store, cfg):
        self.client, self.sender, self.store, self.cfg = client, sender, store, cfg

    async def _media(self, m):
        files, notes = [], []
        if getattr(m, "poll", None):
            return files, [poll_text(m.poll)]
        geo = getattr(m, "geo", None)
        if geo:
            return files, [f"📍 https://maps.google.com/?q={geo.lat},{geo.long}"]
        c = getattr(m, "contact", None)
        if c:
            return files, [f"👤 Contact: {c.first_name} {c.last_name or ''} {c.phone_number}".strip()]
        f = getattr(m, "file", None)
        if f is None or type(getattr(m, "media", None)).__name__ == "MessageMediaWebPage":
            return files, notes                      # plain text, or just a link preview
        name = f.name or f"file_{m.id}{f.ext or ''}"
        size = f.size or 0
        if size > self.cfg["max_bytes"]:
            return files, [f"[not forwarded, too large: {name} ({size / 1e6:.1f} MB)]"]
        data = await self.client.download_media(m, file=bytes)
        if data and len(data) <= self.cfg["max_bytes"]:
            files.append((name, data))
        elif data:
            notes.append(f"[not forwarded, too large: {name}]")
        return files, notes

    async def _build(self, msgs, chat_title, edited=False, with_media=True):
        first = msgs[0]
        sender = None
        try:
            s = await first.get_sender()
            if s is not None:
                from telethon import utils
                sender = utils.get_display_name(s)
        except Exception:
            pass
        text = "\n".join(dict.fromkeys([m.text for m in msgs if getattr(m, "text", None)]))
        reply = None
        if is_real_reply(first):
            try:
                r = await first.get_reply_message()
                reply = (r.raw_text or "[media]") if r else None
            except Exception:
                pass
        fwd = None
        f = getattr(first, "fwd_from", None)
        if f:
            fwd = getattr(f, "from_name", None) or "another chat"
        files, notes = [], []
        if with_media:
            for m in msgs:
                fs, ns = await self._media(m)
                files += fs; notes += ns
        return render(chat_title, sender or chat_title, text, reply, fwd, notes, edited), files

    async def forward(self, msgs, chat_id, chat_title):
        if self.store.get(chat_id, msgs[0].id):
            return                                    # already forwarded (e.g. replay after a restart)
        content, files = await self._build(msgs, chat_title)
        if not content and not files:
            return
        ids = await asyncio.to_thread(self.sender.post, content, files, chat_title)
        for m in msgs:
            self.store.put(chat_id, m.id, ids, content)

    async def edited(self, msg, chat_id, chat_title):
        rec = self.store.get(chat_id, msg.id)
        if not rec or not rec["ids"] or not self.cfg.get("forward_edits", True):
            return
        content, _ = await self._build([msg], chat_title, edited=True, with_media=False)
        content = content if len(content) <= MAX_CONTENT else content[:MAX_CONTENT] + "…"
        if await asyncio.to_thread(self.sender.edit, rec["ids"][0], content):
            self.store.put(chat_id, msg.id, rec["ids"], content)

    async def deleted(self, msg_ids, chat_id):
        for mid in msg_ids:
            if chat_id is not None:
                key = Store.key(chat_id, mid); rec = self.store.d.get(key)
                hits = [(key, rec)] if rec else []
            else:
                hits = self.store.find(mid)
                if len(hits) != 1:
                    continue                          # ambiguous without a chat id: leave it alone
            for key, rec in hits:
                ids = rec["ids"]
                if self.cfg.get("delete_mode", "delete") == "mark" and ids:
                    await asyncio.to_thread(self.sender.edit, ids[0], f"~~{rec['content'][:1800]}~~\n*(deleted in Telegram)*")
                    for extra in ids[1:]:
                        await asyncio.to_thread(self.sender.delete, extra)
                else:
                    for i in ids:
                        await asyncio.to_thread(self.sender.delete, i)
                self.store.pop(key)


# ----------------------------------------------------------------------------- wiring
def load_config(env=None):
    env = os.environ if env is None else env
    need = ["TG_API_ID", "TG_API_HASH", "TG_SESSION", "TG_CHATS", "DISCORD_WEBHOOK_URL"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    return dict(api_id=int(env["TG_API_ID"]), api_hash=env["TG_API_HASH"], session=env["TG_SESSION"],
                chats=[c.strip() for c in env["TG_CHATS"].split(",") if c.strip()],
                topics={int(t) for t in env.get("TG_TOPICS", "").split(",") if t.strip()}, webhook=env["DISCORD_WEBHOOK_URL"],
                max_bytes=int(float(env.get("MAX_FILE_MB", "8")) * 1_000_000), delete_mode=env.get("DELETE_MODE", "delete"),
                forward_edits=env.get("FORWARD_EDITS", "1") == "1", data_dir=env.get("DATA_DIR", "/tmp"),
                startup_notice=env.get("STARTUP_NOTICE", "1") == "1")


async def resolve_chat(client, spec):
    """spec: numeric id, @username, t.me link, or (part of) the chat's name as it shows in your Telegram."""
    try:
        return await client.get_entity(int(spec) if re.fullmatch(r"-?\d+", spec) else spec)
    except Exception:
        pass
    async for d in client.iter_dialogs():
        if spec.lower() in (d.name or "").lower():
            return d.entity
    raise SystemExit(f"Could not find a chat matching {spec!r}. Run with --list-chats to see the names and ids.")


async def main():
    from telethon import TelegramClient, events, utils
    from telethon.sessions import StringSession
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    client = TelegramClient(StringSession(cfg["session"]), cfg["api_id"], cfg["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        log.error("The Telegram session is not valid any more (it was revoked or expired). Create a new one with make_session.py.")
        sys.exit(2)
    if "--list-chats" in sys.argv:
        async for d in client.iter_dialogs():
            print(f"{d.id}\t{d.name}")
        return
    chats = [await resolve_chat(client, s) for s in cfg["chats"]]
    titles = {utils.get_peer_id(e): utils.get_display_name(e) for e in chats}
    log.info("Forwarding from: %s%s", ", ".join(titles.values()), (" (topics %s only)" % sorted(cfg["topics"])) if cfg["topics"] else "")
    sender = DiscordSender(cfg["webhook"])
    fwd = Forwarder(client, sender, Store(os.path.join(cfg["data_dir"], "idmap.json")), cfg)
    hook = None
    if os.environ.get("SIGNAL_BOT") == "1":           # off unless asked for; see signal_bot_hook.py
        try:
            from signal_bot_hook import make_hook
            hook = make_hook()
            await hook.start_check()
        except Exception as e:
            log.error("signal bot could not start (forwarding continues): %s", type(e).__name__)
            hook = None
    title = lambda cid: titles.get(cid, "Telegram")

    def guarded(fn):
        async def run(event):
            try:
                await fn(event)
            except Exception as e:                    # one bad message must not stop the forwarder
                log.exception("failed on a message: %s", type(e).__name__)
        return run

    def wanted(msg):
        return not cfg["topics"] or topic_of(msg) in cfg["topics"]

    @client.on(events.NewMessage(chats=chats))
    @guarded
    async def _new(event):
        if not wanted(event.message):
            return
        if event.message.grouped_id:
            return                                    # albums are handled in one go below
        if hook:                                      # first: the order is time-critical, the Discord copy is not
            await hook(event.message)
        await fwd.forward([event.message], event.chat_id, title(event.chat_id))

    @client.on(events.Album(chats=chats))
    @guarded
    async def _album(event):
        if not wanted(event.messages[0]):
            return
        await fwd.forward(list(event.messages), event.chat_id, title(event.chat_id))

    @client.on(events.MessageEdited(chats=chats))
    @guarded
    async def _edit(event):
        if not wanted(event.message):
            return
        if hook:                                      # signals are often posted, then completed by an edit
            await hook.edited(event.message)
        await fwd.edited(event.message, event.chat_id, title(event.chat_id))

    @client.on(events.MessageDeleted())
    @guarded
    async def _del(event):
        if event.chat_id is not None and event.chat_id not in titles:
            return
        await fwd.deleted(event.deleted_ids, event.chat_id)

    try:
        await client.catch_up()                       # pick up anything missed while the service was restarting
    except Exception as e:
        log.warning("catch_up skipped: %s", type(e).__name__)
    if cfg["startup_notice"]:
        await asyncio.to_thread(sender.post, "✅ Telegram forwarder is online.", None, "Forwarder")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
