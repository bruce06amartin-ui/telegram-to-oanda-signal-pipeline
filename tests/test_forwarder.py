"""Offline tests with fake Telegram messages and a fake Discord. Run:  python -m unittest test_forwarder -v"""
import asyncio, json, os, tempfile, unittest
import forwarder as F


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body or {}, headers or {}
    def json(self): return self._b
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)


class FakeHttp:
    """Records requests; replays scripted responses, else returns a new message id."""
    def __init__(self, script=None): self.calls, self.script, self.n = [], list(script or []), 0
    def request(self, method, url, json=None, data=None, files=None, timeout=None):
        self.calls.append(dict(method=method, url=url, json=json, data=data, files=files))
        if self.script: return self.script.pop(0)
        self.n += 1
        return FakeResp(204 if method == "DELETE" else 200, {"id": str(1000 + self.n)})


class FakeFile:
    def __init__(self, name, size, ext=""): self.name, self.size, self.ext = name, size, ext


class FakeMsg:
    def __init__(self, id, text="", sender="Alice", **kw):
        self.id, self.text, self.raw_text, self._sender = id, text, text, sender
        self.grouped_id = self.reply_to = self.fwd_from = self.poll = self.geo = self.contact = self.file = self.media = None
        self.reply = None
        self.__dict__.update(kw)
    async def get_sender(self):
        from telethon.tl.types import User
        if self._sender is None: return None
        return User(id=1, first_name=self._sender)
    async def get_reply_message(self): return self.reply


class FakeClient:
    def __init__(self, data=b"x" * 10): self.data, self.downloads = data, 0
    async def download_media(self, m, file=bytes): self.downloads += 1; return self.data


def run(c): return asyncio.run(c)


class Helpers(unittest.TestCase):
    def test_markdown_italic_converted_bold_kept(self):
        self.assertEqual(F.tg_to_discord_markdown("**bold** and __ital__ and `code`"), "**bold** and *ital* and `code`")

    def test_split_respects_limit_and_loses_nothing(self):
        text = "\n".join(["line %d " % i + "word " * 40 for i in range(60)])
        parts = F.split_message(text, 1900)
        self.assertTrue(all(len(p) <= 1900 for p in parts)); self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join("".join(parts).split()), " ".join(text.split()))
        self.assertTrue(all(len(p) <= 100 for p in F.split_message("x" * 1000, 100)))

    def test_chunk_files(self):
        b = F.chunk_files([("f%d" % i, b"x") for i in range(25)]); self.assertEqual([len(x) for x in b], [10, 10, 5])
        b = F.chunk_files([("a", b"x" * 15_000_000), ("b", b"x" * 15_000_000)]); self.assertEqual(len(b), 2)

    def test_username_cleaned(self):
        self.assertNotIn("discord", F.clean_username("My Discord Signals").lower()); self.assertEqual(F.clean_username(""), "Telegram")

    def test_render_layout(self):
        out = F.render("Group", "Alice", "hi __there__", reply="old", fwd="Bob", notes=["note"], edited=True)
        self.assertEqual(out.split("\n"), ["**Alice**", "↪ forwarded from Bob", "> old", "hi *there*", "note", "*(edited)*"])
        self.assertEqual(F.render("Chan", "Chan", "post"), "post")      # channel posts get no sender header


class Sender(unittest.TestCase):
    def test_no_mass_pings_and_wait_flag(self):
        h = FakeHttp(); s = F.DiscordSender("https://discord.com/api/webhooks/1/abc?thread=1", http=h)
        ids = s.post("@everyone hello", None, "Group")
        self.assertEqual(ids, ["1001"]); c = h.calls[0]
        self.assertEqual(c["json"]["allowed_mentions"], {"parse": []}); self.assertTrue(c["url"].endswith("?wait=true"))
        self.assertEqual(c["json"]["username"], "Group")

    def test_long_text_becomes_several_messages(self):
        h = FakeHttp(); s = F.DiscordSender("https://x/api/webhooks/1/a", http=h)
        self.assertEqual(len(s.post("a " * 2500)), 3 if len("a " * 2500) > 3800 else 2)

    def test_files_sent_multipart(self):
        h = FakeHttp(); s = F.DiscordSender("https://x/api/webhooks/1/a", http=h)
        s.post("cap", [("p.jpg", b"123")], "G"); c = h.calls[0]
        self.assertIn("files[0]", c["files"]); self.assertEqual(json.loads(c["data"]["payload_json"])["content"], "cap")

    def test_rate_limit_retried(self):
        waits = []; h = FakeHttp([FakeResp(429, {"retry_after": 1.5}), FakeResp(200, {"id": "9"})])
        s = F.DiscordSender("https://x/api/webhooks/1/a", http=h, sleep=waits.append)
        self.assertEqual(s.post("hi"), ["9"]); self.assertEqual(len(h.calls), 2); self.assertAlmostEqual(waits[0], 1.6)

    def test_edit_and_delete_urls(self):
        h = FakeHttp(); s = F.DiscordSender("https://x/api/webhooks/1/a?wait=true", http=h)
        self.assertTrue(s.edit("55", "new")); self.assertTrue(s.delete("55"))
        self.assertEqual([c["url"] for c in h.calls], ["https://x/api/webhooks/1/a/messages/55"] * 2)
        self.assertEqual([c["method"] for c in h.calls], ["PATCH", "DELETE"])


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.http = FakeHttp(); self.client = FakeClient()
        self.cfg = dict(max_bytes=8_000_000, delete_mode="delete", forward_edits=True)
        self.store = F.Store(os.path.join(self.tmp, "m.json"))
        self.fw = F.Forwarder(self.client, F.DiscordSender("https://x/api/webhooks/1/a", http=self.http), self.store, self.cfg)

    def test_text_then_edit_then_delete(self):
        run(self.fw.forward([FakeMsg(1, "buy **now**")], -100, "Signals"))
        self.assertEqual(self.http.calls[0]["json"]["content"], "**Alice**\nbuy **now**")
        run(self.fw.edited(FakeMsg(1, "buy later"), -100, "Signals"))
        self.assertEqual(self.http.calls[1]["method"], "PATCH"); self.assertIn("*(edited)*", self.http.calls[1]["json"]["content"])
        run(self.fw.deleted([1], -100)); self.assertEqual(self.http.calls[2]["method"], "DELETE")
        self.assertIsNone(self.store.get(-100, 1))

    def test_replay_is_not_duplicated(self):
        m = FakeMsg(1, "hi"); run(self.fw.forward([m], -100, "S")); run(self.fw.forward([m], -100, "S")); self.assertEqual(len(self.http.calls), 1)

    def test_photo_forwarded_and_big_file_skipped(self):
        run(self.fw.forward([FakeMsg(2, "chart", file=FakeFile("c.png", 100), media=object())], -100, "S"))
        self.assertEqual(self.client.downloads, 1); self.assertIn("files[0]", self.http.calls[0]["files"])
        run(self.fw.forward([FakeMsg(3, "video", file=FakeFile("v.mp4", 50_000_000), media=object())], -100, "S"))
        self.assertEqual(self.client.downloads, 1)                       # never downloaded
        self.assertIn("too large", self.http.calls[1]["json"]["content"])

    def test_album_is_one_discord_post(self):
        album = [FakeMsg(10, "", file=FakeFile("a.jpg", 5), media=object()), FakeMsg(11, "caption", file=FakeFile("b.jpg", 5), media=object())]
        run(self.fw.forward(album, -100, "S")); self.assertEqual(len(self.http.calls), 1); self.assertEqual(len(self.http.calls[0]["files"]), 2)
        self.assertIsNotNone(self.store.get(-100, 10)); self.assertIsNotNone(self.store.get(-100, 11))

    def test_link_preview_not_downloaded(self):
        class MessageMediaWebPage: pass
        run(self.fw.forward([FakeMsg(4, "see https://x.com", file=FakeFile("p.jpg", 5), media=MessageMediaWebPage())], -100, "S"))
        self.assertEqual(self.client.downloads, 0)

    def test_poll_location_reply(self):
        class A:
            def __init__(s, t): s.text = t
        class P:
            question = "Up or down?"; answers = [A("Up"), A("Down")]
        class Poll: poll = P()
        class G: lat, long = 51.5, -0.12
        run(self.fw.forward([FakeMsg(5, "", poll=Poll())], -100, "S")); self.assertIn("📊 Poll: Up or down?\n• Up\n• Down", self.http.calls[0]["json"]["content"])
        run(self.fw.forward([FakeMsg(6, "", geo=G())], -100, "S")); self.assertIn("maps.google.com/?q=51.5,-0.12", self.http.calls[1]["json"]["content"])
        run(self.fw.forward([FakeMsg(7, "yes", reply_to=True, reply=FakeMsg(5, "question?"))], -100, "S")); self.assertIn("> question?", self.http.calls[2]["json"]["content"])

    def test_mark_mode_strikes_instead_of_deleting(self):
        self.cfg["delete_mode"] = "mark"; run(self.fw.forward([FakeMsg(1, "oops")], -100, "S")); run(self.fw.deleted([1], -100))
        self.assertEqual(self.http.calls[1]["method"], "PATCH"); self.assertIn("~~", self.http.calls[1]["json"]["content"])

    def test_delete_without_chat_id_only_if_unambiguous(self):
        run(self.fw.forward([FakeMsg(8, "a")], -100, "S")); run(self.fw.forward([FakeMsg(8, "b")], -200, "T"))
        n = len(self.http.calls); run(self.fw.deleted([8], None)); self.assertEqual(len(self.http.calls), n)   # two matches: left alone

    def test_map_survives_restart(self):
        run(self.fw.forward([FakeMsg(1, "hi")], -100, "S")); again = F.Store(os.path.join(self.tmp, "m.json"))
        self.assertEqual(again.get(-100, 1)["ids"], ["1001"])


class Topics(unittest.TestCase):
    class R:
        def __init__(s, **k): s.forum_topic = k.get("forum_topic", False); s.reply_to_top_id = k.get("top"); s.reply_to_msg_id = k.get("msg")

    def test_topic_ids(self):
        R = self.R
        self.assertEqual(F.topic_of(FakeMsg(1, reply_to=R(forum_topic=True, msg=1234))), 1234)           # plain post in a topic
        self.assertEqual(F.topic_of(FakeMsg(2, reply_to=R(forum_topic=True, top=1234, msg=9000))), 1234)  # reply inside a topic
        self.assertEqual(F.topic_of(FakeMsg(3)), 1)                                                        # General

    def test_topic_post_is_not_a_reply(self):
        R = self.R
        self.assertFalse(F.is_real_reply(FakeMsg(1, reply_to=R(forum_topic=True, msg=1234))))
        self.assertTrue(F.is_real_reply(FakeMsg(2, reply_to=R(forum_topic=True, top=1234, msg=9000))))
        self.assertTrue(F.is_real_reply(FakeMsg(3, reply_to=R(msg=5))))

    def test_config_topics(self):
        c = F.load_config(dict(TG_API_ID="1", TG_API_HASH="h", TG_SESSION="s", TG_CHATS="x", DISCORD_WEBHOOK_URL="u", TG_TOPICS="1234, 12"))
        self.assertEqual(c["topics"], {1234, 12})


class Config(unittest.TestCase):
    def test_missing_vars_named_but_values_not_printed(self):
        with self.assertRaises(SystemExit) as e: F.load_config({"TG_API_ID": "1"})
        self.assertIn("TG_API_HASH", str(e.exception)); self.assertNotIn("=1", str(e.exception))

    def test_parsing(self):
        c = F.load_config(dict(TG_API_ID="12", TG_API_HASH="h", TG_SESSION="s", TG_CHATS="@a, -100123 ,My Group", DISCORD_WEBHOOK_URL="u", MAX_FILE_MB="5"))
        self.assertEqual(c["chats"], ["@a", "-100123", "My Group"]); self.assertEqual(c["max_bytes"], 5_000_000)


if __name__ == "__main__":
    unittest.main()
