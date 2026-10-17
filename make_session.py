"""Run this ONCE on your own computer to create the Telegram login ("session string") the forwarder uses.

Before running: get an API id and hash from https://my.telegram.org (log in, "API development tools", create an app).
Then:  python -m pip install telethon   and   python make_session.py
Telegram will send a login code to your Telegram app. Type it in here, not anywhere else.

The string it prints is as powerful as your Telegram password. Paste it into Render as the TG_SESSION environment variable and nowhere else.
Do not post it in chat, email or screenshots, and do not share it with anyone, including Claude.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()                      # asks for your phone number, the login code and (if set) your 2-step password
    print("\nYour session string (keep it secret):\n")
    print(client.session.save())
    print("\nIf you ever suspect it leaked: Telegram > Settings > Devices > terminate that session.")
    await client.disconnect()


asyncio.run(main())
