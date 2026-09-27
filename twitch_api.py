"""Client for Twitch's internal GQL API: login, campaigns, stream info and the watch signal."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import string
from base64 import b64encode
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import aiohttp

from i18n import t

log = logging.getLogger("miner")

# Twitch clients that support the device-code login flow (tried in order)
CLIENTS = [
    {   # Android TV app
        "id": "ue6666qo983tsx6so1t0vnawi233wa",
        "url": "https://android.tv.twitch.tv",
        "ua": ("Mozilla/5.0 (Linux; Android 7.1; Smart Box C1) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"),
    },
    {   # mobile website
        "id": "r8s4dac0uhzifbpu9sjdiwzctle17ff",
        "url": "https://m.twitch.tv",
        "ua": ("Mozilla/5.0 (Linux; Android 16; SM-A205U) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/138.0.7204.158 Mobile Safari/537.36"),
    },
]
CLIENT_URL = "https://www.twitch.tv"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
GQL_URL = "https://gql.twitch.tv/gql"

# progress(stage, done, total) — reported while campaigns are being discovered
Progress = Callable[[str, int, int], None]


def _pq(name: str, sha: str, variables: dict | None = None) -> dict:
    op: dict[str, Any] = {
        "operationName": name,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
    }
    if variables is not None:
        op["variables"] = variables
    return op


# Persisted queries used by the Twitch website.
QUERIES: dict[str, dict] = {
    "GetStreamInfo": _pq(
        "VideoPlayerStreamInfoOverlayChannel",
        "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
        {"channel": None},
    ),
    "ClaimCommunityPoints": _pq(
        "ClaimCommunityPoints",
        "46aaeebe02c99afdf4fc97c7c0cba964124bf6b0af229395f1f6d1feed05b3d0",
        {"input": {"claimID": None, "channelID": None}},
    ),
    "ClaimDrop": _pq(
        "DropsPage_ClaimDropRewards",
        "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
        {"input": {"dropInstanceID": None}},
    ),
    "ChannelPointsContext": _pq(
        "ChannelPointsContext",
        "374314de591e69925fce3ddc2bcf085796f56ebb8cad67a0daa3165c03adc345",
        {"channelLogin": None},
    ),
    "Inventory": _pq(
        "Inventory",
        "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
        {"fetchRewardCampaigns": False},
    ),
    "CurrentDrop": _pq(
        "DropCurrentSessionContext",
        "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        {"channelID": None, "channelLogin": ""},
    ),
    "Campaigns": _pq(
        "ViewerDropsDashboard",
        "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb",
        {"fetchRewardCampaigns": False},
    ),
    "CampaignDetails": _pq(
        "DropCampaignDetails",
        "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1",
        {"channelLogin": None, "dropID": None},
    ),
    "PlaybackAccessToken": _pq(
        "PlaybackAccessToken",
        "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
        {"isLive": True, "isVod": False, "login": None, "platform": "web",
         "playerType": "site", "vodID": ""},
    ),
    "GameDirectory": _pq(
        "DirectoryPage_Game",
        "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd",
        {
            "limit": 30,
            "slug": None,
            "imageWidth": 50,
            "includeCostreaming": False,
            "options": {
                "broadcasterLanguages": [],
                "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "VIEWER_COUNT",
                "systemFilters": [],
                "tags": [],
                "requestID": "JIRA-VXP-2397",
            },
            "sortTypeIsRecency": False,
        },
    ),
}


CAMPAIGN_FIELDS = (
    "id name description status startAt endAt imageURL detailsURL accountLinkURL "
    "owner{name} self{isAccountConnected} game{id slug displayName name boxArtURL} "
    "allow{isEnabled channels{id name displayName}} "
    "timeBasedDrops{id name startAt endAt requiredMinutesWatched requiredSubs preconditionDrops{id} "
    "benefitEdges{entitlementLimit benefit{id name imageAssetURL distributionType}} "
    "self{currentMinutesWatched isClaimed dropInstanceID}}"
)


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def query(name: str, variables: dict | None = None) -> dict:
    op = deepcopy(QUERIES[name])
    if variables:
        op.setdefault("variables", {})
        _merge(op["variables"], variables)
    return op


def slugify(name: str) -> str:
    s = re.sub(r"'", "", name.lower())
    s = re.sub(r"\W+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TwitchError(Exception):
    pass


class LoginRequired(TwitchError):
    pass


class Twitch:
    def __init__(self, data_dir: Path):
        self.auth_path = data_dir / "auth.json"
        self.cache_path = data_dir / "campaign_cache.json"
        self.session: aiohttp.ClientSession | None = None
        self.access_token: str | None = None
        self.device_id: str | None = None
        self.user_id: str | None = None
        self.login: str | None = None
        self.client = CLIENTS[0]
        self.session_id = "".join(random.choices("0123456789abcdef", k=16))
        self._gql_limit = asyncio.Semaphore(5)
        # bulk (aliased) queries: too many at once and Twitch silently returns empty lists
        self._bulk_limit = asyncio.Semaphore(2)
        self._spade_url: str | None = None
        self._load_auth()

    # ------------------------------------------------------------------ session
    def _load_auth(self) -> None:
        try:
            data = json.loads(self.auth_path.read_text("utf8"))
        except (OSError, ValueError):
            return
        self.access_token = data.get("access_token")
        self.device_id = data.get("device_id")
        self.client = next((c for c in CLIENTS if c["id"] == data.get("client_id")), CLIENTS[0])

    def _save_auth(self) -> None:
        self.auth_path.write_text(json.dumps({
            "access_token": self.access_token,
            "device_id": self.device_id,
            "client_id": self.client["id"],
        }), "utf8")

    async def start(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": WEB_USER_AGENT},
            )
        if not self.device_id:
            self.device_id = await self._fetch_device_id()
            self._save_auth()

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _fetch_device_id(self) -> str:
        assert self.session is not None
        try:
            async with self.session.get(CLIENT_URL, headers={"User-Agent": WEB_USER_AGENT}) as r:
                await r.read()
            for cookie in self.session.cookie_jar:
                if cookie.key == "unique_id":
                    return cookie.value
        except aiohttp.ClientError:
            pass
        return "".join(random.choices(string.ascii_letters + string.digits, k=32))

    def _headers(self, gql: bool = False) -> dict[str, str]:
        h = {
            "Accept": "*/*",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Client-Id": self.client["id"],
            "Client-Session-Id": self.session_id,
            "User-Agent": self.client["ua"],
        }
        if self.device_id:
            h["X-Device-Id"] = self.device_id
        if gql:
            h["Origin"] = self.client["url"]
            h["Referer"] = self.client["url"]
            if self.access_token:
                h["Authorization"] = f"OAuth {self.access_token}"
        return h

    async def validate(self) -> bool:
        """Fills in the user info if the saved token is still valid."""
        assert self.session is not None
        if not self.access_token:
            return False
        async with self.session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {self.access_token}"},
        ) as r:
            if r.status == 401:
                self.access_token = None
                self._save_auth()
                return False
            if r.status != 200:
                raise TwitchError(t("error.token_validate", status=r.status))
            data = await r.json()
        if data.get("client_id") != self.client["id"]:
            self.access_token = None
            self._save_auth()
            return False
        self.user_id = str(data["user_id"])
        self.login = data["login"]
        return True

    async def _request_device_code(self) -> dict:
        assert self.session is not None
        last = None
        for client in CLIENTS:
            self.client = client
            headers = self._headers()
            headers.update({"Accept": "application/json", "Origin": client["url"], "Referer": client["url"]})
            async with self.session.post(
                "https://id.twitch.tv/oauth2/device",
                headers=headers,
                data={"client_id": client["id"], "scopes": ""},
            ) as r:
                data = await r.json(content_type=None)
            if "device_code" in data:
                return data
            last = data
            log.debug("Device code rejected (%s): %s", client["id"], data)
        raise TwitchError(t("error.device_code", detail=last))

    async def device_login(self, on_code) -> None:
        """Device-code login. Calls on_code(user_code, verification_uri, expires_in)."""
        assert self.session is not None
        while True:
            data = await self._request_device_code()
            headers = self._headers()
            headers.update({"Accept": "application/json", "Origin": self.client["url"],
                            "Referer": self.client["url"]})
            expires = asyncio.get_running_loop().time() + data["expires_in"]
            interval = data.get("interval", 5)
            on_code(data["user_code"], data["verification_uri"], data["expires_in"])
            payload = {
                "client_id": self.client["id"],
                "device_code": data["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
            while asyncio.get_running_loop().time() < expires:
                await asyncio.sleep(interval)
                async with self.session.post(
                    "https://id.twitch.tv/oauth2/token", headers=headers, data=payload
                ) as r:
                    if r.status != 200:
                        continue
                    token = await r.json(content_type=None)
                self.access_token = token["access_token"]
                self._save_auth()
                if await self.validate():
                    return
                raise TwitchError(t("error.token_invalid"))
            # the code expired, request a new one

    def logout(self) -> None:
        self.access_token = None
        self.user_id = None
        self.login = None
        self._save_auth()

    # --------------------------------------------------------------------- GQL
    async def gql(self, ops: dict | list[dict], partial: bool = False) -> Any:
        assert self.session is not None
        if not self.access_token:
            raise LoginRequired()
        delay = 2.0
        for attempt in range(5):
            async with self._gql_limit:
                try:
                    async with self.session.post(
                        GQL_URL, json=ops, headers=self._headers(gql=True)
                    ) as r:
                        if r.status == 401:
                            raise LoginRequired()
                        resp = await r.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.debug("GQL network error: %s", exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
            items = resp if isinstance(resp, list) else [resp]
            if partial and isinstance(resp, dict) and resp.get("data"):
                # with aliased queries some fields failing is normal
                return resp
            retry = False
            for item in items:
                for err in item.get("errors") or []:
                    msg = err.get("message", "")
                    if msg in ("service error", "service timeout", "service unavailable",
                               "context deadline exceeded", "request cancelled",
                               "PersistedQueryNotFound"):
                        retry = True
                    elif msg == "server error":
                        continue  # the affected field comes back null, keep going
                    else:
                        op = (item.get("extensions") or {}).get("operationName", "?")
                        raise TwitchError(t("error.gql", op=op, msg=msg))
            if retry and attempt < 4:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if retry:
                raise TwitchError(t("error.gql_rejected"))
            return resp
        raise TwitchError(t("error.connect"))

    async def gql_batch(self, ops: list[dict], size: int = 20) -> list[dict]:
        results: list[dict] = []
        chunks = [ops[i:i + size] for i in range(0, len(ops), size)]
        for res in await asyncio.gather(*(self.gql(c) for c in chunks)):
            results.extend(res)
        return results

    async def raw(self, q: str, variables: dict | None = None) -> dict:
        """Free-form GQL query; returns the 'data' part."""
        resp = await self.gql({"query": q, "variables": variables or {}}, partial=True)
        return resp.get("data") or {}

    async def aliased(self, parts: dict[str, str], chunk: int = 25, retries: int = 3,
                      on_chunk: Callable[[int], None] | None = None) -> dict[str, Any]:
        """Bulk aliased query. parts: {alias: field}. Empty/failed aliases are retried.
        on_chunk(n) is called as each first-pass chunk of n aliases finishes."""
        out: dict[str, Any] = {}
        pending = list(parts)
        for attempt in range(retries + 1):
            chunks = [pending[i:i + chunk] for i in range(0, len(pending), chunk)]

            async def one(keys: list[str]) -> None:
                q = "query{" + " ".join(f"{k}:{parts[k]}" for k in keys) + "}"
                try:
                    async with self._bulk_limit:
                        data = await self.raw(q)
                except TwitchError as exc:
                    log.debug("Bulk query error: %s", exc)
                    data = {}
                for k in keys:
                    if data.get(k) is not None:
                        out[k] = data[k]
                if on_chunk and attempt == 0:
                    on_chunk(len(keys))

            await asyncio.gather(*(one(c) for c in chunks))
            pending = [k for k in pending if k not in out]
            if not pending:
                break
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        if pending:
            log.debug("%d query parts got no answer", len(pending))
        return out

    # ---------------------------------------------------------------- drops
    async def _discover_drop_channels(self, extra_games: list[str]) -> dict[str, str]:
        """Finds streams with drops enabled. Returns {channel_id: login}."""
        streams = "streams(first:3,options:{systemFilters:[DROPS_ENABLED]}){edges{node{broadcaster{id login}}}}"
        channels: dict[str, str] = {}

        def collect(game: dict | None) -> None:
            for e in ((game or {}).get("streams") or {}).get("edges") or []:
                b = (e.get("node") or {}).get("broadcaster")
                if b:
                    channels[str(b["id"])] = b["login"]

        # most watched games (2 pages = 200 games)
        after = None
        for _ in range(2):
            data = await self.raw(
                "query($after:Cursor){games(first:100,after:$after){pageInfo{hasNextPage} "
                "edges{cursor node{id " + streams + "}}}}", {"after": after})
            games = data.get("games") or {}
            edges = games.get("edges") or []
            for e in edges:
                collect(e.get("node"))
            if not edges or not (games.get("pageInfo") or {}).get("hasNextPage"):
                break
            after = edges[-1]["cursor"]
        # games from the priority list / inventory (even if they are not popular)
        names = list(dict.fromkeys(n for n in extra_games if n))
        data = await self.aliased(
            {f"g{j}": f"game(name:{json.dumps(n)}){{id {streams}}}" for j, n in enumerate(names)}, retries=1)
        for g in data.values():
            collect(g)
        return channels

    async def channels_campaigns(self, channel_ids: list[str], fields: str = CAMPAIGN_FIELDS,
                                 on_chunk: Callable[[int], None] | None = None) -> dict[str, list[dict]]:
        """Drop campaigns that can be earned on each channel."""
        ids = list(dict.fromkeys(channel_ids))
        data = await self.aliased(
            {f"c{j}": f'channel(id:"{cid}"){{viewerDropCampaigns{{{fields}}}}}' for j, cid in enumerate(ids)},
            on_chunk=on_chunk)
        return {cid: (data.get(f"c{j}") or {}).get("viewerDropCampaigns") or []
                for j, cid in enumerate(ids) if f"c{j}" in data}

    def _load_cache(self) -> dict[str, dict]:
        try:
            return json.loads(self.cache_path.read_text("utf8"))
        except (OSError, ValueError):
            return {}

    def _save_cache(self, cache: dict[str, dict]) -> None:
        try:
            self.cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf8")
        except OSError as exc:
            log.debug("Could not write the campaign cache: %s", exc)

    async def fetch_all_campaigns(self, extra_games: list[str] | None = None,
                                  progress: Progress | None = None) -> tuple[dict[str, dict], list[dict]]:
        """Merges campaigns from the inventory, discovered streams and the cache.
        Returns (campaigns, claimed rewards from the inventory)."""
        def report(stage: str, done: int = 0, total: int = 0) -> None:
            if progress:
                progress(stage, done, total)

        report("inventory")
        inv_resp = await self.gql(query("Inventory"))
        inventory = ((inv_resp.get("data") or {}).get("currentUser") or {}).get("inventory") or {}
        in_progress = {c["id"]: c for c in inventory.get("dropCampaignsInProgress") or []}
        rewards = inventory.get("gameEventDrops") or []
        claimed_benefits: dict[str, str] = {b["id"]: b.get("lastAwardedAt") for b in rewards}
        extra_games = [g for g in (extra_games or []) if g]
        games = extra_games + [
            (c.get("game") or {}).get("name") for c in in_progress.values()
            if c.get("status") == "ACTIVE"
        ]
        # pass 1: a few drop-enabled streams in popular games
        report("games")
        channels = await self._discover_drop_channels(games)
        done = 0
        total = len(channels)

        def tick(n: int) -> None:
            nonlocal done
            done += n
            report("channels", min(done, total), total)

        report("channels", 0, total)
        per_channel = await self.channels_campaigns(list(channels), on_chunk=tick)
        found: dict[str, dict] = {}

        def seen(camp: dict, login: str) -> None:
            lst = camp.setdefault("_seen_channels", [])
            if login not in lst:
                lst.append(login)

        for cid, camps in per_channel.items():
            for camp in camps:
                if camp.get("game"):
                    seen(found.setdefault(camp["id"], camp), channels[cid])

        # pass 2: scan more streams in games that have campaigns (for streamer-specific drops)
        prio = {g.lower() for g in extra_games}
        depth: dict[str, int] = {}
        for camp in found.values():
            g = camp["game"]
            acl = bool((camp.get("allow") or {}).get("channels"))
            n = 100 if acl or (g.get("displayName") or g.get("name") or "").lower() in prio else 30
            depth[str(g["id"])] = max(depth.get(str(g["id"]), 0), n)
        more: dict[str, str] = {}
        gids = list(depth)
        report("deep", 0, 0)
        data = await self.aliased({
            f"g{j}": (f'game(id:"{gid}"){{streams(first:{depth[gid]},options:{{systemFilters:[DROPS_ENABLED]}})'
                      f'{{edges{{node{{broadcaster{{id login}}}}}}}}}}')
            for j, gid in enumerate(gids)}, chunk=10)
        for g in data.values():
            for e in ((g.get("streams") or {}).get("edges")) or []:
                br = (e.get("node") or {}).get("broadcaster")
                if br and str(br["id"]) not in channels:
                    more[str(br["id"])] = br["login"]
        log.debug("Pass 2: %d games scanned deeper, %d new streams; depth=%s", len(gids), len(more), depth)
        if more:
            done, total = 0, len(more)

            def tick_deep(n: int) -> None:
                nonlocal done
                done += n
                report("deep", min(done, total), total)

            ids_only = await self.channels_campaigns(list(more), fields="id", on_chunk=tick_deep)
            need: dict[str, str] = {}   # new campaign -> a channel that offers it
            for ch_id, camps in ids_only.items():
                for camp in camps:
                    if camp["id"] in found:
                        seen(found[camp["id"]], more[ch_id])
                    else:
                        need.setdefault(camp["id"], ch_id)
            log.debug("Pass 2: %d new campaigns found", len(need))
            if need:
                full = await self.channels_campaigns(list(dict.fromkeys(need.values())))
                for ch_id, camps in full.items():
                    for camp in camps:
                        if camp.get("game") and camp["id"] not in found:
                            found[camp["id"]] = camp
                        if camp["id"] in found:
                            seen(found[camp["id"]], more[ch_id])
                for ch_id, camps in ids_only.items():
                    for camp in camps:
                        if camp["id"] in found:
                            seen(found[camp["id"]], more[ch_id])

        report("finalize")
        # cache: campaigns seen before whose streams are offline right now
        now = datetime.now(timezone.utc)
        cache = self._load_cache()
        for cid, camp in found.items():
            cache[cid] = {k: v for k, v in camp.items() if k != "_claimed_benefits"}
        for camp in cache.values():
            # drop progress (self) goes stale in the cache; it only comes from the inventory
            for d in camp.get("timeBasedDrops") or []:
                d.pop("self", None)
        cache = {cid: c for cid, c in cache.items()
                 if (parse_time(c.get("endAt")) or now) > now - timedelta(days=1)}
        self._save_cache(cache)

        result: dict[str, dict] = {}
        for cid in dict.fromkeys(list(in_progress) + list(found) + list(cache)):
            if cid in found:
                camp = found[cid]
            elif cid in in_progress:
                camp = in_progress[cid]
                camp["_offline"] = True
            else:
                camp = deepcopy(cache[cid])
                camp["_offline"] = True
                if (parse_time(camp.get("endAt")) or now) <= now:
                    continue
            inv = in_progress.get(cid)
            if camp is not inv:
                # The inventory is the only reliable source of drop progress (self): discovery
                # and cache data can show even claimed drops as "0 min, not claimed". Drops that
                # are not in the inventory get their state from gameEventDrops (claimed rewards).
                inv_drops = {d["id"]: d for d in (inv or {}).get("timeBasedDrops") or []}
                for d in camp.get("timeBasedDrops") or []:
                    extra = inv_drops.get(d["id"])
                    if extra and extra.get("self"):
                        d["self"] = extra["self"]
                    else:
                        d.pop("self", None)
            if inv and camp is not inv:
                if inv.get("self"):
                    camp["self"] = {**(camp.get("self") or {}), **inv["self"]}
                for key in ("allow", "game"):
                    if not camp.get(key) and inv.get(key):
                        camp[key] = inv[key]
            if not camp.get("game"):
                continue
            camp["_claimed_benefits"] = claimed_benefits
            result[cid] = camp
        log.info(t("log.discovery", streams=len(channels) + len(more), campaigns=len(result),
                   offline=sum(1 for c in result.values() if c.get("_offline"))))
        return result, rewards

    async def current_drop(self, channel_id: str) -> dict | None:
        resp = await self.gql(query("CurrentDrop", {"channelID": str(channel_id)}))
        return ((resp.get("data") or {}).get("currentUser") or {}).get("dropCurrentSession")

    async def claim_drop(self, instance_id: str) -> str | None:
        """Returns Twitch's status (e.g. ELIGIBLE_FOR_ALL, DROP_INSTANCE_ALREADY_CLAIMED)."""
        resp = await self.gql(query("ClaimDrop", {"input": {"dropInstanceID": instance_id}}))
        data = resp.get("data") or {}
        res = data.get("claimDropRewards")
        return res.get("status") if res else None

    # ------------------------------------------------------------------ channels
    @staticmethod
    def _parse_stream_user(user: dict | None) -> dict | None:
        if not user:
            return None
        stream = user.get("stream")
        settings = user.get("broadcastSettings") or {}
        game = settings.get("game")
        return {
            "id": str(user["id"]),
            "login": user["login"],
            "display_name": user.get("displayName") or user["login"],
            "online": bool(stream),
            "broadcast_id": str(stream["id"]) if stream else None,
            "viewers": (stream or {}).get("viewersCount", 0),
            "title": settings.get("title") or "",
            "game": {"id": str(game["id"]), "name": game.get("displayName") or game.get("name"),
                     "slug": game.get("slug")} if game else None,
        }

    async def stream_info(self, login: str) -> dict | None:
        resp = await self.gql(query("GetStreamInfo", {"channel": login.lower()}))
        return self._parse_stream_user((resp.get("data") or {}).get("user"))

    async def streams_info(self, logins: list[str]) -> list[dict]:
        if not logins:
            return []
        resps = await self.gql_batch([query("GetStreamInfo", {"channel": l.lower()}) for l in logins])
        out = []
        for r in resps:
            info = self._parse_stream_user((r.get("data") or {}).get("user"))
            if info:
                out.append(info)
        return out

    async def game_streams(self, slug: str, drops_only: bool = True, limit: int = 30) -> list[dict]:
        resp = await self.gql(query("GameDirectory", {
            "limit": limit, "slug": slug,
            "options": {"systemFilters": ["DROPS_ENABLED"] if drops_only else []},
        }))
        game = (resp.get("data") or {}).get("game")
        if not game:
            return []
        out = []
        for edge in (game.get("streams") or {}).get("edges") or []:
            node = edge.get("node") or {}
            b = node.get("broadcaster")
            if not b:
                continue
            g = node.get("game") or {}
            out.append({
                "id": str(b["id"]), "login": b["login"],
                "display_name": b.get("displayName") or b["login"],
                "online": True, "broadcast_id": str(node.get("id")),
                "viewers": node.get("viewersCount", 0), "title": node.get("title") or "",
                "game": {"id": str(g.get("id", game.get("id"))),
                         "name": g.get("displayName") or g.get("name") or game.get("displayName"),
                         "slug": slug},
            })
        return out

    # ------------------------------------------------------------------ watching
    async def _get_spade_url(self, login: str) -> str:
        assert self.session is not None
        if self._spade_url:
            return self._spade_url
        settings_pat = r'src="(https://[\w.]+/config/settings\.[0-9a-f]{32}\.js)"'
        spade_pat = r'"spade_?url": ?"(https://[.\w\-/]+)"'
        async with self.session.get(f"{CLIENT_URL}/{login}",
                                    headers={"User-Agent": WEB_USER_AGENT}) as r:
            html = await r.text("utf8")
        m = re.search(spade_pat, html, re.I)
        if not m:
            m2 = re.search(settings_pat, html, re.I)
            if not m2:
                raise TwitchError("spade_url not found (step 1)")
            async with self.session.get(m2.group(1),
                                        headers={"User-Agent": WEB_USER_AGENT}) as r:
                js = await r.text("utf8")
            m = re.search(spade_pat, js, re.I)
            if not m:
                raise TwitchError("spade_url not found (step 2)")
        self._spade_url = m.group(1)
        return self._spade_url

    async def _send_spade(self, ch: dict) -> bool:
        assert self.session is not None
        game = ch.get("game") or {}
        payload = [{
            "event": "minute-watched",
            "properties": {
                "broadcast_id": ch["broadcast_id"],
                "channel_id": ch["id"],
                "channel": ch["login"],
                "client_time": datetime.now(timezone.utc).isoformat(),
                "game": game.get("name") or "",
                "game_id": game.get("id") or "",
                "hidden": False,
                "is_live": True,
                "live": True,
                "logged_in": True,
                "minutes_logged": 1,
                "muted": False,
                "user_id": int(self.user_id) if self.user_id else 0,
            },
        }]
        data = b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        url = await self._get_spade_url(ch["login"])
        async with self.session.post(url, data={"data": data}, headers=self._headers()) as r:
            if r.status >= 400:
                self._spade_url = None
            return r.status == 204

    async def _send_hls(self, ch: dict) -> bool:
        """Sends a HEAD request for the newest chunk of the stream's lowest quality playlist."""
        assert self.session is not None
        tok = await self.gql(query("PlaybackAccessToken", {"login": ch["login"]}))
        token = ((tok.get("data") or {}).get("streamPlaybackAccessToken")) or {}
        if not token:
            return False
        params = {"sig": token["signature"], "token": token["value"]}
        async with self.session.get(
            f"https://usher.ttvnw.net/api/channel/hls/{ch['login']}.m3u8", params=params
        ) as r:
            if r.status >= 400:
                return False
            master = (await r.text()).strip().split("\n")
        playlist_url = master[-1]
        if not playlist_url.startswith("http"):
            return False
        async with self.session.get(playlist_url, headers={"Connection": "close"}) as r:
            if r.status >= 400:
                return False
            chunks = (await r.text()).strip().split("\n")
        chunk = chunks[-1] if chunks[-1] != "#EXT-X-ENDLIST" else chunks[-2]
        if not chunk.startswith("http"):
            return False
        async with self.session.head(chunk) as r:
            return r.status == 200

    async def send_watch(self, ch: dict) -> bool:
        """Sends the watch signal (spade 'minute-watched' + an HLS chunk request)."""
        ok = False
        try:
            ok = await self._send_spade(ch)
        except (aiohttp.ClientError, asyncio.TimeoutError, TwitchError) as exc:
            log.debug("spade error: %s", exc)
        try:
            ok = await self._send_hls(ch) or ok
        except (aiohttp.ClientError, asyncio.TimeoutError, TwitchError, IndexError) as exc:
            log.debug("HLS error: %s", exc)
        return ok

    # ------------------------------------------------------------ channel points
    async def claim_points_bonus(self, ch: dict) -> int | None:
        resp = await self.gql(query("ChannelPointsContext", {"channelLogin": ch["login"]}))
        channel = (((resp.get("data") or {}).get("community") or {}).get("channel")) or {}
        cp = ((channel.get("self") or {}).get("communityPoints")) or {}
        claim = cp.get("availableClaim")
        if not claim:
            return None
        await self.gql(query("ClaimCommunityPoints", {
            "input": {"claimID": claim["id"], "channelID": str(ch["id"])}
        }))
        return cp.get("balance")


def new_id() -> str:
    return secrets.token_hex(6)
