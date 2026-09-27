"""Discord Rich Presence: shows what the miner is doing on the user's Discord profile.

Talks to the local Discord client over its IPC named pipe (no extra dependency). Nothing is sent
anywhere else, and nothing happens while Discord is closed or the setting is off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import uuid
from datetime import datetime

from i18n import t
from miner import Miner
from version import APP_NAME, DISCORD_CLIENT_ID, DONATE_URL, GITHUB_OWNER, GITHUB_REPO, __version__, repo_url

log = logging.getLogger("miner")

UPDATE_INTERVAL = 15    # seconds between presence checks (Discord allows 5 updates / 20 s)
RETRY_INTERVAL = 60     # seconds between connection attempts while Discord is closed
ICON_URL = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/assets/icon.png"

OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2
ACTIVITY_WATCHING = 3


def _text(s: str | None) -> str | None:
    """Discord rejects presence strings shorter than 2 or longer than 128 characters."""
    if not s:
        return None
    s = str(s)
    if len(s) < 2:
        s += " "
    return s if len(s) <= 128 else s[:127] + "…"


class DiscordIPC:
    """Blocking client for Discord's IPC pipe. Call it from a worker thread."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.pipe = None

    def connect(self) -> bool:
        for i in range(10):
            try:
                self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)  # noqa: SIM115
            except OSError:
                continue
            try:
                self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                op, data = self._recv()
                if op == OP_FRAME and data.get("evt") == "READY":
                    return True
                log.debug("Discord handshake rejected: %s", data)
            except OSError:
                pass
            self.close()
        return False

    def close(self) -> None:
        if self.pipe:
            try:
                self.pipe.close()
            except OSError:
                pass
        self.pipe = None

    def set_activity(self, activity: dict | None) -> None:
        self._send(OP_FRAME, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                              "args": {"pid": os.getpid(), "activity": activity}})
        op, data = self._recv()
        if op == OP_CLOSE:
            raise OSError(f"Discord closed the connection: {data}")
        if data.get("evt") == "ERROR":
            log.debug("Discord rejected the presence: %s", data.get("data"))

    def _send(self, op: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf8")
        self.pipe.write(struct.pack("<II", op, len(body)) + body)

    def _recv(self) -> tuple[int, dict]:
        op, length = struct.unpack("<II", self._read(8))
        return op, json.loads(self._read(length) or b"{}")

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.pipe.read(n - len(buf))
            if not chunk:
                raise OSError("Discord pipe closed")
            buf += chunk
        return buf


class DiscordPresence:
    def __init__(self, miner: Miner):
        self.miner = miner
        self.ipc = DiscordIPC(DISCORD_CLIENT_ID)
        self.connected = False
        self._last: dict | None = None

    def _activity(self) -> dict | None:
        m = self.miner
        if m.auth.get("state") != "logged_in":
            return None
        details, state, since = t("discord.idle"), None, None
        large, large_text = ICON_URL, APP_NAME
        cur = m.current
        if m.paused:
            details = t("discord.paused")
        elif cur:
            ch = cur["channel"]
            since = cur.get("since")
            game = ch["game"]["name"] if ch.get("game") else None
            if cur["mode"] == "custom":
                details = t("discord.watching", channel=ch["display_name"])
                state = game
            else:
                c, d = m._find_drop(cur["drop_id"]) if cur.get("drop_id") else (None, None)
                if c is None and cur.get("campaign_id"):
                    c = m.campaigns.get(cur["campaign_id"])
                game = c["game"]["name"] if c else game
                details = t("discord.mining", game=game or ch["display_name"])
                if d:
                    state = t("discord.progress", drop=d["name"], current=d["current_minutes"],
                              required=d["required_minutes"])
                if c and c["game"].get("box_art"):
                    large, large_text = c["game"]["box_art"], game
        activity = {
            "type": ACTIVITY_WATCHING,
            "details": _text(details),
            "state": _text(state),
            "assets": {"large_image": large, "large_text": _text(large_text)},
            "buttons": [{"label": t("discord.button_get")[:32], "url": repo_url()},
                        {"label": t("discord.button_donate")[:32], "url": DONATE_URL}],
        }
        if large != ICON_URL:   # the game art is the big picture, the app icon goes in the corner
            activity["assets"].update(small_image=ICON_URL, small_text=_text(f"{APP_NAME} v{__version__}"))
        if since:
            activity["timestamps"] = {"start": int(datetime.fromisoformat(since).timestamp())}
        return {k: v for k, v in activity.items() if v is not None}

    async def _apply(self) -> None:
        wanted = self.miner.settings.get("discord_presence") and self.miner.auth.get("state") == "logged_in"
        if not wanted:
            if self.connected:
                await asyncio.to_thread(self.ipc.close)   # closing the pipe clears the presence
                self.connected = False
                self._last = None
            return
        if not self.connected:
            self.connected = await asyncio.to_thread(self.ipc.connect)
            if not self.connected:
                return
            log.debug("Connected to Discord")
        activity = self._activity()
        if activity != self._last:
            await asyncio.to_thread(self.ipc.set_activity, activity)
            self._last = activity

    async def run(self) -> None:
        if not DISCORD_CLIENT_ID:
            return
        try:
            while True:
                try:
                    await self._apply()
                except (OSError, ValueError, struct.error) as exc:
                    log.debug("Discord presence error: %s", exc)
                    self.ipc.close()
                    self.connected = False
                    self._last = None
                await asyncio.sleep(UPDATE_INTERVAL if self.connected else RETRY_INTERVAL)
        finally:
            self.ipc.close()
