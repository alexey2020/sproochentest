"""Learner state in the site's Firestore, one document per sync code.

Documents look exactly like the ones index.html writes (fields v, s, t; s is the
packed state), so a code works both in the bot and on the site.

Every Telegram user gets their own code derived from their id with BOT_SECRET.
When a user links a code they already use on the site, their own document keeps
only {"link": "<that code>"} and the bot reads and writes the linked one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import zlib

import aiohttp

from plan import merge, normalize, now_ms

CODE_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[0-9]{4}$")


class StoreError(Exception):
    pass


def pack(state: dict) -> str:
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return "z:" + base64.b64encode(c.compress(raw) + c.flush()).decode()


def unpack(s: str) -> dict:
    if s.startswith("j:"):
        return json.loads(s[2:])
    return json.loads(zlib.decompress(base64.b64decode(s[2:]), -15))


def clean_code(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[\s_—–]+", "-", text.strip().lower()))


class Store:
    def __init__(self, data: dict, secret: bytes):
        self.base = data["sync"]["base"]
        self.key = data["sync"]["key"]
        self.words = data["sync"]["words"]
        self.legacy = data["plan"]["legacy_cfg"]
        self.secret = secret
        self.codes: dict[int, str] = {}  # telegram id -> code in use
        self.locks: dict[int, asyncio.Lock] = {}
        self.session: aiohttp.ClientSession | None = None

    def lock(self, uid: int) -> asyncio.Lock:
        return self.locks.setdefault(uid, asyncio.Lock())

    def own_code(self, uid: int) -> str:
        h = hmac.new(self.secret, str(uid).encode(), hashlib.sha256).digest()
        w = self.words
        return f"{w[h[0] % len(w)]}-{w[h[1] % len(w)]}-{w[h[2] % len(w)]}-{int.from_bytes(h[3:6], 'big') % 10000:04d}"

    async def _http(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self.session

    async def get_raw(self, code: str) -> dict | None:
        try:
            http = await self._http()
            async with http.get(self.base + code, params={"key": self.key}) as r:
                if r.status == 404:
                    return None
                if r.status != 200:
                    raise StoreError(f"сервер ответил {r.status}")
                j = await r.json()
        except aiohttp.ClientError as e:
            raise StoreError(str(e)) from e
        return unpack(j["fields"]["s"]["stringValue"])

    async def put_raw(self, code: str, state: dict) -> None:
        body = {"fields": {"v": {"integerValue": "1"}, "s": {"stringValue": pack(state)}, "t": {"integerValue": str(now_ms())}}}
        try:
            http = await self._http()
            async with http.patch(self.base + code, params={"key": self.key}, json=body) as r:
                if r.status != 200:
                    raise StoreError(f"сервер ответил {r.status}")
        except aiohttp.ClientError as e:
            raise StoreError(str(e)) from e

    async def code_for(self, uid: int) -> str:
        if uid not in self.codes:
            own = self.own_code(uid)
            raw = await self.get_raw(own)
            self.codes[uid] = raw["link"] if raw and raw.get("link") else own
        return self.codes[uid]

    async def load(self, uid: int) -> dict:
        code = await self.code_for(uid)
        return normalize(await self.get_raw(code), self.legacy)

    async def save(self, uid: int, state: dict) -> None:
        """Merges with what is stored now, so marks made on the site meanwhile survive."""
        code = await self.code_for(uid)
        remote = await self.get_raw(code)
        merged = merge(normalize(remote, self.legacy), state) if remote else state
        await self.put_raw(code, merged)

    async def link(self, uid: int, code: str) -> bool:
        """Switches the user to a code from the site, carrying over what they did in the bot."""
        remote = await self.get_raw(code)
        if remote is None:
            return False
        own = self.own_code(uid)
        current = await self.code_for(uid)
        if code != current:
            mine = await self.get_raw(current)
            merged = normalize(remote, self.legacy)
            if mine and not mine.get("link"):
                merged = merge(merged, normalize(mine, self.legacy))
            await self.put_raw(code, merged)
        await self.put_raw(own, {"link": code} if code != own else normalize(remote, self.legacy))
        self.codes[uid] = code
        return True
