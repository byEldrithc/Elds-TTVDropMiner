"""Drop mining engine: campaign selection, channel finding, watching, claiming, custom watch."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import aiohttp

import i18n
from i18n import t
from twitch_api import LoginRequired, Twitch, TwitchError, new_id, parse_time, query, slugify
from version import DONATE_URL, GITHUB_OWNER, GITHUB_REPO, __version__, github_configured, releases_url

log = logging.getLogger("miner")

WATCH_INTERVAL = 59            # seconds between watch signals
INVENTORY_REFRESH = 10 * 60    # seconds between full campaign discoveries
NO_STREAM_RETRY = 5 * 60       # seconds before retrying a campaign that had no stream
BAD_CHANNEL_TIME = 30 * 60     # seconds a channel without progress stays excluded
STALL_LIMIT = 5                # minutes without progress before switching channel
UPDATE_CHECK_INTERVAL = 12 * 3600
HISTORY_LIMIT = 5000

DEFAULT_SETTINGS = {
    "priority_games": [],       # priority game names (ordered)
    "excluded_games": [],       # games that are never watched
    "priority_drops": [],       # [{id, name, game, campaign}] (ordered)
    "mode": "priority_only",    # priority_first | priority_only
    "allow_unlinked": False,    # also watch campaigns whose game account is not linked
    "auto_claim": True,
    "claim_points": True,
    "custom_fallback_drops": True,  # keep mining drops while the custom-watch streamer is offline
    "language": "",             # "" = follow the Windows language
    "notifications": True,      # desktop / browser notifications
    "check_updates": True,      # look for new releases on GitHub
    "discord_presence": True,   # show the mining status on the Discord profile
}

LOADING_STAGES = ["login", "inventory", "games", "channels", "deep", "finalize"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


class MemoryLogHandler(logging.Handler):
    def __init__(self, buffer: deque):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append({
            "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


class Miner:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.settings_path = data_dir / "settings.json"
        self.custom_path = data_dir / "custom_watch.json"
        self.history_path = data_dir / "history.json"
        self.tw = Twitch(data_dir)
        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()
        i18n.set_language(self.settings["language"])
        self.logs: deque = deque(maxlen=400)
        self.auth: dict = {"state": "starting"}
        self._status: tuple[str, dict] = ("status.starting", {})
        self.paused = False
        self.desktop = False    # set by the desktop app (notifications go through the tray)
        self.campaigns: dict[str, dict] = {}
        self.campaigns_version = 0
        self.campaigns_updated: str | None = None
        self.loading = {"active": True, "first": True, "stage": "login", "done": 0, "total": 0}
        self.current: dict | None = None   # channel being watched + context
        self.custom_queue: list[dict] = []
        self.custom_history: list[dict] = []
        self.claimed_session: list[dict] = []
        self.points_claimed = 0
        self.history: list[dict] = []
        self.history_version = 0
        self.update: dict | None = None     # {"version", "url"} when a newer release exists
        self.events: deque = deque(maxlen=30)
        self._event_id = 0
        self.event_listeners: list[Callable[[dict], None]] = []
        self._load_custom()
        self._load_history()
        self._wake = asyncio.Event()
        self._need_refresh = True
        self._refresh_at = 0.0
        self._no_stream: dict[str, float] = {}      # campaign_id -> retry time
        self._bad_channels: dict[str, float] = {}   # login -> end of exclusion
        self._stall = 0
        self._inv_minutes: dict[str, int] = {}      # drop_id -> last minutes seen in the inventory
        self._last_tick: float | None = None
        self._last_tick_login: str | None = None
        self._force_switch = False
        self._claimed_ids: set[str] = set()         # drops claimed in this session
        self._unknown_drops: set[str] = set()       # drops reported by CurrentDrop but not in our list
        self._update_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- status
    @property
    def status(self) -> str:
        key, params = self._status
        return t(key, **params)

    def set_status(self, key: str, **params) -> None:
        self._status = (key, params)

    # ---------------------------------------------------------------- persistence
    def _load_settings(self) -> None:
        try:
            data = json.loads(self.settings_path.read_text("utf8"))
            for k in DEFAULT_SETTINGS:
                if k in data:
                    self.settings[k] = data[k]
        except (OSError, ValueError):
            pass

    def save_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), "utf8")

    def _load_custom(self) -> None:
        try:
            data = json.loads(self.custom_path.read_text("utf8"))
            self.custom_queue = data.get("queue", [])
            self.custom_history = data.get("history", [])[-30:]
        except (OSError, ValueError):
            pass

    def save_custom(self) -> None:
        self.custom_path.write_text(json.dumps(
            {"queue": self.custom_queue, "history": self.custom_history[-30:]},
            ensure_ascii=False, indent=2), "utf8")

    def _load_history(self) -> None:
        try:
            self.history = json.loads(self.history_path.read_text("utf8"))
        except (OSError, ValueError):
            self.history = []

    def _save_history(self) -> None:
        self.history.sort(key=lambda h: h.get("time") or "", reverse=True)
        del self.history[HISTORY_LIMIT:]
        self.history_version += 1
        try:
            self.history_path.write_text(json.dumps(self.history, ensure_ascii=False, indent=1), "utf8")
        except OSError as exc:
            log.debug("Could not write history: %s", exc)

    def _import_rewards(self, rewards: list[dict]) -> None:
        """Adds rewards claimed outside this app (or before it existed) to the history,
        using the inventory's gameEventDrops list."""
        if not rewards:
            return
        benefit_info: dict[str, tuple[dict, dict]] = {}
        for c in self.campaigns.values():
            for d in c["drops"]:
                for b in d["benefits"]:
                    benefit_info.setdefault(b["id"], (c, d))
        known: dict[str, list[datetime]] = {}
        for h in self.history:
            ts = parse_time(h.get("time"))
            for bid in h.get("benefit_ids") or []:
                known.setdefault(bid, []).append(ts)
        added = 0
        for r in rewards:
            bid, ts = r.get("id"), parse_time(r.get("lastAwardedAt"))
            if not bid or not ts:
                continue
            # the same reward already recorded (by us, or by an earlier import) within two days
            if any(k and abs((k - ts).total_seconds()) < 2 * 86400 for k in known.get(bid, [])):
                continue
            c, d = benefit_info.get(bid, (None, None))
            game = ((r.get("game") or {}).get("displayName") or (r.get("game") or {}).get("name")
                    or (c["game"]["name"] if c else None))
            self.history.append({
                "id": new_id(), "time": iso(ts), "source": "twitch",
                "game": game, "campaign": c["name"] if c else None, "drop": d["name"] if d else None,
                "rewards": [{"name": r.get("name"), "image": r.get("imageURL")}],
                "benefit_ids": [bid], "count": r.get("totalCount") or 1,
            })
            known.setdefault(bid, []).append(ts)
            added += 1
        if added:
            log.info(t("log.history_imported", count=added))
            self._save_history()

    # ---------------------------------------------------------------- events
    def emit_event(self, kind: str, title: str, body: str, force: bool = False) -> None:
        """Notification for the UI and desktop tray (respects the notifications setting)."""
        if not force and not self.settings["notifications"]:
            return
        self._event_id += 1
        ev = {"id": self._event_id, "kind": kind, "title": title, "body": body, "time": iso(now_utc())}
        self.events.append(ev)
        for listener in self.event_listeners:
            try:
                listener(ev)
            except Exception as exc:  # noqa: BLE001
                log.debug("Event listener error: %s", exc)

    def wake(self) -> None:
        self._wake.set()

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(1.0, seconds))
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    # ------------------------------------------------------------ campaigns
    def _normalize(self, raw: dict) -> dict:
        claimed_benefits: dict = raw.get("_claimed_benefits") or {}
        game = raw.get("game") or {}
        allow = raw.get("allow") or {}
        acl = []
        if allow.get("channels") and allow.get("isEnabled", True):
            acl = [{"id": str(c["id"]), "login": c.get("name"),
                    "display_name": c.get("displayName") or c.get("name")}
                   for c in allow["channels"] if c.get("name")]
        c_start = parse_time(raw.get("startAt"))
        c_end = parse_time(raw.get("endAt"))
        drops = []
        for d in raw.get("timeBasedDrops") or []:
            d_start = parse_time(d.get("startAt")) or c_start
            d_end = parse_time(d.get("endAt")) or c_end
            benefits = []
            for edge in d.get("benefitEdges") or []:
                b = edge.get("benefit") or {}
                benefits.append({
                    "id": b.get("id"), "name": b.get("name"),
                    "image": b.get("imageAssetURL"),
                    "type": b.get("distributionType"),
                    "limit": edge.get("entitlementLimit"),
                })
            me = d.get("self")
            claim_id = None
            is_claimed = False
            current = 0
            if me:
                claim_id = me.get("dropInstanceID")
                is_claimed = bool(me.get("isClaimed"))
                current = me.get("currentMinutesWatched") or 0
            else:
                stamps = [parse_time(claimed_benefits[b["id"]]) for b in benefits
                          if b["id"] in claimed_benefits and claimed_benefits[b["id"]]]
                if stamps and d_start and d_end and all(d_start <= s < d_end for s in stamps):
                    is_claimed = True
            required = d.get("requiredMinutesWatched") or 0
            if is_claimed:
                current = required
            drops.append({
                "id": d["id"], "name": d.get("name"),
                "starts_at": d_start, "ends_at": d_end,
                "required_minutes": required, "current_minutes": current,
                "required_subs": d.get("requiredSubs") or 0,
                "is_claimed": is_claimed, "claim_id": claim_id,
                "preconditions": [p["id"] for p in d.get("preconditionDrops") or []],
                "benefits": benefits,
            })
        linked = bool((raw.get("self") or {}).get("isAccountConnected"))
        badge_emote = any(b["type"] in ("BADGE", "EMOTE") for d in drops for b in d["benefits"])
        return {
            "id": raw["id"], "name": raw.get("name"),
            "description": raw.get("description") or "",
            "status": raw.get("status"),
            "game": {"id": str(game.get("id")),
                     "name": game.get("displayName") or game.get("name"),
                     "slug": game.get("slug") or slugify(game.get("displayName") or game.get("name") or ""),
                     "box_art": (game.get("boxArtURL") or "").replace("{width}", "144").replace("{height}", "192")},
            "image": raw.get("imageURL"),
            "owner": (raw.get("owner") or {}).get("name"),
            "details_url": raw.get("detailsURL"),
            "link_url": raw.get("accountLinkURL"),
            "linked": linked,
            "eligible_by_badge": badge_emote,
            "starts_at": c_start, "ends_at": c_end,
            "acl": acl,
            "seen_channels": raw.get("_seen_channels") or [],
            "offline": bool(raw.get("_offline")),
            "drops": drops,
        }

    def _progress(self, stage: str, done: int, total: int) -> None:
        self.loading.update(stage=stage, done=done, total=total)

    async def refresh_inventory(self) -> None:
        self.set_status("status.loading")
        self.loading.update(active=True, stage="inventory", done=0, total=0)
        extra = list(self.settings["priority_games"]) + [p.get("game") for p in self.settings["priority_drops"]]
        try:
            raw, rewards = await self.tw.fetch_all_campaigns(extra, progress=self._progress)
        finally:
            self.loading["active"] = False
        camps = {cid: self._normalize(c) for cid, c in raw.items()}
        # compare with the previous data: report drops that turned out to be claimed already
        fixed = [d["name"] for cid, c in camps.items() for d in c["drops"]
                 if d["is_claimed"] and cid in self.campaigns
                 and any(o["id"] == d["id"] and not o["is_claimed"] for o in self.campaigns[cid]["drops"])]
        if fixed:
            log.info(t("log.inventory_fixed", count=len(fixed),
                       names=", ".join(fixed[:8]) + (" ..." if len(fixed) > 8 else "")))
        # discovery/cache data may show a claimed drop as unclaimed; keep what we claimed this session
        for c in camps.values():
            for d in c["drops"]:
                if d["id"] in self._claimed_ids and not d["is_claimed"]:
                    d["is_claimed"] = True
                    d["current_minutes"] = d["required_minutes"]
        for cid, c in camps.items():
            old = self.campaigns.get(cid)
            if old is not None and old["linked"] != c["linked"]:
                log.info(t("log.link_on" if c["linked"] else "log.link_off",
                           game=c["game"]["name"], campaign=c["name"]))
        self.campaigns = camps
        self.campaigns_version += 1
        self.campaigns_updated = iso(now_utc())
        self.loading["first"] = False
        self._need_refresh = False
        self._refresh_at = time.monotonic() + INVENTORY_REFRESH
        self._no_stream.clear()
        self._import_rewards(rewards)
        active = [c for c in camps.values() if self._campaign_active(c)]
        linked = sum(1 for c in active if c["linked"])
        log.info(t("log.campaigns_updated", total=len(camps), active=len(active), linked=linked,
                   minable=len(self.ranked_campaigns())))

    # ------------------------------------------------------- eligibility
    @staticmethod
    def _campaign_active(c: dict, now: datetime | None = None) -> bool:
        now = now or now_utc()
        return (c["status"] != "EXPIRED" and c["starts_at"] is not None and c["ends_at"] is not None
                and c["starts_at"] <= now < c["ends_at"])

    def _campaign_eligible(self, c: dict) -> bool:
        return c["linked"] or c["eligible_by_badge"] or self.settings["allow_unlinked"]

    @staticmethod
    def _drop_map(c: dict) -> dict:
        return {d["id"]: d for d in c["drops"]}

    def drop_state(self, c: dict, d: dict, now: datetime | None = None) -> str:
        now = now or now_utc()
        if d["is_claimed"]:
            return "claimed"
        if d["claim_id"] and d["current_minutes"] >= d["required_minutes"] > 0:
            return "claimable"
        if d["required_minutes"] <= 0:
            return "sub_only" if d["required_subs"] else "unavailable"
        if d["ends_at"] and now >= d["ends_at"]:
            return "expired"
        if d["starts_at"] and now < d["starts_at"]:
            return "not_started"
        dm = self._drop_map(c)
        if not all(dm[p]["is_claimed"] for p in d["preconditions"] if p in dm):
            return "locked"
        if d["current_minutes"] >= d["required_minutes"]:
            return "pending_claim"
        return "in_progress" if d["current_minutes"] > 0 else "available"

    def _earnable_drops(self, c: dict) -> list[dict]:
        if not self._campaign_active(c) or not self._campaign_eligible(c):
            return []
        return [d for d in c["drops"] if self.drop_state(c, d) in ("available", "in_progress")]

    def _pinned_ids(self) -> list[str]:
        return [p["id"] for p in self.settings["priority_drops"]]

    def ranked_campaigns(self) -> list[dict]:
        """Minable campaigns in priority order."""
        pinned = self._pinned_ids()
        prio_games = [g.lower() for g in self.settings["priority_games"]]
        excluded = {g.lower() for g in self.settings["excluded_games"]}
        out = []
        for c in self.campaigns.values():
            earnable = self._earnable_drops(c)
            if not earnable:
                continue
            gname = (c["game"]["name"] or "").lower()
            pin_rank = min((pinned.index(d["id"]) for d in earnable if d["id"] in pinned), default=None)
            if pin_rank is None and gname in excluded:
                continue
            game_rank = prio_games.index(gname) if gname in prio_games else None
            if self.settings["mode"] == "priority_only" and pin_rank is None and game_rank is None:
                continue
            key = (
                0 if pin_rank is not None else 1,
                pin_rank if pin_rank is not None else 0,
                game_rank if game_rank is not None else 10_000,
                c["ends_at"],
            )
            out.append((key, c))
        out.sort(key=lambda x: x[0])
        return [c for _, c in out]

    def _channel_serves(self, ch: dict, c: dict) -> bool:
        if not ch or not ch.get("online") or not ch.get("game"):
            return False
        if ch["game"]["id"] != c["game"]["id"]:
            return False
        if c["acl"] and ch["login"].lower() not in {a["login"].lower() for a in c["acl"]}:
            return False
        return True

    def _is_bad(self, login: str) -> bool:
        until = self._bad_channels.get(login.lower())
        return bool(until and until > time.monotonic())

    async def _find_channel(self, c: dict) -> dict | None:
        if c["acl"]:
            infos = await self.tw.streams_info([a["login"] for a in c["acl"]][:100])
        else:
            infos = await self.tw.game_streams(c["game"]["slug"])
            known = {i["login"].lower() for i in infos}
            extra = [l for l in c["seen_channels"] if l.lower() not in known]
            if extra:
                infos += await self.tw.streams_info(extra[:10])
        cands = [i for i in infos if self._channel_serves(i, c) and not self._is_bad(i["login"])]
        cands.sort(key=lambda i: i["viewers"], reverse=True)
        if not cands:
            return None
        # ask Twitch whether the campaign can really be earned on these channels
        try:
            offered = await self.tw.channels_campaigns([i["id"] for i in cands[:10]], fields="id")
        except TwitchError as exc:
            log.debug("Channel verification error: %s", exc)
            return cands[0]
        for i in cands[:10]:
            if any(x["id"] == c["id"] for x in offered.get(i["id"], [])):
                log.info(t("log.channel_picked", count=len(cands), channel=i["display_name"]))
                return i
        log.info(t("log.channel_none", count=len(cands)))
        return None

    # --------------------------------------------------------------- selection
    def active_custom(self) -> dict | None:
        return self.custom_queue[0] if self.custom_queue else None

    async def _select(self) -> tuple[dict | None, dict | None, str]:
        """Returns (channel, campaign, mode)."""
        item = self.active_custom()
        if item:
            info = await self.tw.stream_info(item["channel"])
            if info:
                item["display_name"] = info["display_name"]
            if info and info["online"]:
                item["state"] = "watching"
                camp = next((c for c in self.ranked_campaigns() if self._channel_serves(info, c)), None)
                return info, camp, "custom"
            item["state"] = "offline" if info else "not_found"
            if not self.settings["custom_fallback_drops"]:
                return None, None, "custom_wait"

        ranked = self.ranked_campaigns()
        if not ranked:
            return None, None, "idle"

        cur = self.current.get("channel") if self.current and self.current.get("mode") == "drops" else None
        if cur and self._force_switch:
            self._bad_channels[cur["login"].lower()] = time.monotonic() + BAD_CHANNEL_TIME
            cur = None
        self._force_switch = False
        if cur:
            cur = await self.tw.stream_info(cur["login"])

        now_m = time.monotonic()
        for c in ranked:
            if cur and self._channel_serves(cur, c) and not self._is_bad(cur["login"]):
                return cur, c, "drops"
            if self._no_stream.get(c["id"], 0) > now_m:
                continue
            log.info(t("log.searching", game=c["game"]["name"], campaign=c["name"]))
            ch = await self._find_channel(c)
            if ch:
                return ch, c, "drops"
            log.info(t("log.no_stream", game=c["game"]["name"], minutes=NO_STREAM_RETRY // 60))
            self._no_stream[c["id"]] = now_m + NO_STREAM_RETRY
        return None, None, "no_stream"

    # --------------------------------------------------------------- progress
    def _find_drop(self, drop_id: str) -> tuple[dict | None, dict | None]:
        for c in self.campaigns.values():
            for d in c["drops"]:
                if d["id"] == drop_id:
                    return c, d
        return None, None

    async def _poll_inventory(self) -> dict[str, tuple[int | None, int]]:
        """Reads the inventory and updates link/progress/claim state.
        Returns {drop_id: (previous_minutes, new_minutes)}."""
        resp = await self.tw.gql(query("Inventory"))
        inv = ((resp.get("data") or {}).get("currentUser") or {}).get("inventory") or {}
        deltas: dict[str, tuple[int | None, int]] = {}
        changed = False
        for camp in inv.get("dropCampaignsInProgress") or []:
            c = self.campaigns.get(camp["id"])
            if c is None:
                if camp.get("status") == "ACTIVE":
                    self._need_refresh = True  # a campaign that just started
                continue
            linked = bool((camp.get("self") or {}).get("isAccountConnected"))
            if linked != c["linked"]:
                c["linked"] = linked
                changed = True
                log.info(t("log.link_on" if linked else "log.link_off",
                           game=c["game"]["name"], campaign=c["name"]))
            dm = self._drop_map(c)
            for d in camp.get("timeBasedDrops") or []:
                me, x = d.get("self"), dm.get(d["id"])
                if not me or x is None:
                    continue
                new = me.get("currentMinutesWatched") or 0
                prev = self._inv_minutes.get(d["id"])
                self._inv_minutes[d["id"]] = new
                deltas[d["id"]] = (prev, new)
                if me.get("dropInstanceID") and x["claim_id"] != me["dropInstanceID"]:
                    x["claim_id"] = me["dropInstanceID"]
                    changed = True
                if me.get("isClaimed") and not x["is_claimed"]:
                    x["is_claimed"] = True
                    x["current_minutes"] = x["required_minutes"]
                    changed = True
                elif not x["is_claimed"] and new != x["current_minutes"]:
                    x["current_minutes"] = new
                    changed = True
        if changed:
            self.campaigns_version += 1
        return deltas

    async def _update_progress(self, ch: dict, mode: str, camp: dict | None) -> None:
        """Every minute: active drop (CurrentDrop) + real progress check against the inventory.
        One stream can progress several drops at once (even from different campaigns),
        so every drop that grows in the inventory is tracked."""
        try:
            cur = await self.tw.current_drop(ch["id"])
        except TwitchError as exc:
            log.debug("CurrentDrop error: %s", exc)
            cur = None
        cur_id = None
        cur_minutes = None
        if cur and cur.get("dropID"):
            _, d = self._find_drop(cur["dropID"])
            if d is None:
                # unknown drop: ask for a full refresh once, not every minute
                if cur["dropID"] not in self._unknown_drops:
                    self._unknown_drops.add(cur["dropID"])
                    self._need_refresh = True
            else:
                cur_id = d["id"]
                cur_minutes = cur.get("currentMinutesWatched") or 0
                if not d["is_claimed"] and cur_minutes > d["current_minutes"]:
                    d["current_minutes"] = cur_minutes
                    self.campaigns_version += 1
        try:
            deltas = await self._poll_inventory()
        except TwitchError as exc:
            log.warning(t("log.inventory_error", error=exc))
            deltas = {}

        # ALL drops whose minutes grew in the inventory (from any campaign)
        gained: dict[str, int] = {did: n - p for did, (p, n) in deltas.items() if p is not None and n > p}
        camp_ids = {x["id"] for x in camp["drops"]} if camp else set()

        def remaining(did: str) -> int:
            _, x = self._find_drop(did)
            return max(x["required_minutes"] - x["current_minutes"], 0) if x else 10 ** 9

        # main drop: progressing in the selected campaign > CurrentDrop > other progressing > closest in campaign
        drop_id = None
        own = sorted((did for did in gained if did in camp_ids), key=remaining)
        other = sorted((did for did in gained if did not in camp_ids), key=remaining)
        if own:
            drop_id = own[0]
        elif cur_id:
            drop_id = cur_id
        elif other:
            drop_id = other[0]
        elif camp is not None:
            earn = [d for d in camp["drops"] if self.drop_state(camp, d) in ("in_progress", "available")]
            earn.sort(key=lambda d: d["required_minutes"] - d["current_minutes"])
            drop_id = earn[0]["id"] if earn else None
        extra_ids = [did for did in own + other if did != drop_id]
        if self.current is not None:
            self.current["drop_id"] = drop_id
            self.current["extra_drop_ids"] = extra_ids

        # if any drop is progressing, the channel is not stalled
        if gained or (cur_minutes and cur_id not in deltas):
            self._stall = 0
        else:
            self._stall += 1

        # claim sooner for every drop whose time is complete
        for did in [drop_id, *extra_ids]:
            c2, d2 = self._find_drop(did) if did else (None, None)
            if d2 is not None and d2["current_minutes"] >= d2["required_minutes"] > 0 and not d2["is_claimed"]:
                log.info(t("log.drop_complete", drop=d2["name"], game=c2["game"]["name"]))
                self._refresh_at = min(self._refresh_at, time.monotonic() + 45)

        who = f"{ch['display_name']} ({ch['game']['name'] if ch.get('game') else '-'})"
        c, d = self._find_drop(drop_id) if drop_id else (None, None)
        if d is not None:
            inv_val = self._inv_minutes.get(d["id"])
            rem = max(d["required_minutes"] - d["current_minutes"], 0)
            if gained.get(d["id"]):
                check = t("log.check_gain", minutes=gained[d["id"]])
            elif gained:
                check = t("log.check_other")
            elif inv_val is None:
                check = t("log.check_not_listed_early" if self._stall < 3 else "log.check_not_listed",
                          minutes=self._stall)
            else:
                check = t("log.check_none", minutes=self._stall)
            log.info(t("log.tick", who=who, drop=d["name"], current=d["current_minutes"],
                       required=d["required_minutes"], remaining=rem, check=check))
            if extra_ids:
                parts = []
                for did in extra_ids:
                    _, x = self._find_drop(did)
                    parts.append(f"{x['name']} {x['current_minutes']}/{x['required_minutes']}")
                log.info(t("log.tick_extra", drops=" · ".join(parts)))
        elif mode == "custom":
            log.info(t("log.tick_custom", who=who))
        else:
            log.info(t("log.tick_waiting", who=who, minutes=self._stall))

        if mode == "drops" and self._stall >= STALL_LIMIT:
            log.warning(t("log.stalled", channel=ch["display_name"], minutes=self._stall))
            self._bad_channels[ch["login"].lower()] = time.monotonic() + BAD_CHANNEL_TIME
            self._stall = 0

    async def claim_ready(self) -> None:
        if not self.settings["auto_claim"]:
            return
        for c in self.campaigns.values():
            for d in c["drops"]:
                if self.drop_state(c, d) == "claimable":
                    await self.claim(c, d)

    async def claim(self, c: dict, d: dict) -> bool:
        if not d["claim_id"]:
            return False
        try:
            status = await self.tw.claim_drop(d["claim_id"])
        except TwitchError as exc:
            log.error(t("log.claim_error", drop=d["name"], error=exc))
            return False
        ok = status in ("ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED")
        if ok:
            d["is_claimed"] = True
            d["current_minutes"] = d["required_minutes"]
            self._claimed_ids.add(d["id"])
            self.campaigns_version += 1
            if status == "DROP_INSTANCE_ALREADY_CLAIMED":
                log.debug("Already claimed: %s", d["name"])
            else:
                names = ", ".join(b["name"] for b in d["benefits"]) or d["name"]
                self.claimed_session.append({"time": iso(now_utc()), "game": c["game"]["name"],
                                             "drop": d["name"], "rewards": names})
                self.history.append({
                    "id": new_id(), "time": iso(now_utc()), "source": "app",
                    "game": c["game"]["name"], "campaign": c["name"], "drop": d["name"],
                    "rewards": [{"name": b["name"], "image": b["image"]} for b in d["benefits"]],
                    "benefit_ids": [b["id"] for b in d["benefits"]], "count": 1,
                })
                self._save_history()
                log.info(t("log.claimed", game=c["game"]["name"], rewards=names))
                self.emit_event("claimed", t("notify.claimed_title"),
                                t("notify.claimed_body", game=c["game"]["name"], rewards=names))
            self._unpin_if_done(d["id"])
        else:
            log.warning(t("log.claim_failed", drop=d["name"], status=status))
        return ok

    def _unpin_if_done(self, drop_id: str) -> None:
        before = len(self.settings["priority_drops"])
        self.settings["priority_drops"] = [p for p in self.settings["priority_drops"] if p["id"] != drop_id]
        if len(self.settings["priority_drops"]) != before:
            self.save_settings()

    # ------------------------------------------------------------ updates
    async def check_update(self) -> None:
        if not github_configured() or not self.settings["check_updates"]:
            return
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(url, headers={"Accept": "application/vnd.github+json"}) as r:
                    if r.status != 200:
                        log.debug("Update check: HTTP %s", r.status)
                        return
                    data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.debug("Update check failed: %s", exc)
            return
        latest = data.get("tag_name") or ""
        if version_tuple(latest) <= version_tuple(__version__):
            return
        first = self.update is None or self.update["version"] != latest
        self.update = {"version": latest.lstrip("v"), "url": data.get("html_url") or releases_url()}
        if first:
            log.info(t("log.update_available", version=self.update["version"]))
            self.emit_event("update", t("notify.update_title"),
                            t("notify.update_body", version=self.update["version"]))

    async def _update_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            await self.check_update()
            await asyncio.sleep(UPDATE_CHECK_INTERVAL)

    # ------------------------------------------------------------ main loop
    async def run(self) -> None:
        await self.tw.start()
        if self._update_task is None:
            self._update_task = asyncio.create_task(self._update_loop())
        try:
            while True:
                try:
                    await self._ensure_login()
                    await self._loop()
                except LoginRequired:
                    log.warning(t("log.session_invalid"))
                    self.tw.logout()
                    self.auth = {"state": "logged_out"}
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception(t("log.unexpected", error=exc))
                    self.set_status("status.error", error=exc)
                    await self._wait(30)
        finally:
            self._update_task.cancel()

    async def _ensure_login(self) -> None:
        self.loading.update(stage="login", done=0, total=0)
        if self.tw.access_token and await self.tw.validate():
            self.auth = {"state": "logged_in", "login": self.tw.login, "user_id": self.tw.user_id}
            log.info(t("log.logged_in", login=self.tw.login))
            return
        self.set_status("status.login_wait")

        def on_code(code: str, uri: str, expires_in: int) -> None:
            first = self.auth.get("state") != "code"
            self.auth = {"state": "code", "code": code, "uri": uri,
                         "expires_at": iso(now_utc() + timedelta(seconds=expires_in))}
            log.info(t("log.login_code", code=code, uri=uri))
            if first:
                self.emit_event("login", t("notify.login_title"), t("notify.login_body", code=code))

        await self.tw.device_login(on_code)
        self.auth = {"state": "logged_in", "login": self.tw.login, "user_id": self.tw.user_id}
        log.info(t("log.login_ok", login=self.tw.login))
        self._need_refresh = True

    async def _loop(self) -> None:
        while True:
            if self.auth.get("state") != "logged_in":
                return
            if self.paused:
                self.set_status("status.paused")
                self.current = None
                await self._wait(3600)
                continue
            if self._need_refresh or time.monotonic() >= self._refresh_at:
                await self.refresh_inventory()
            await self.claim_ready()

            tick_start = time.monotonic()
            self.set_status("status.searching")
            ch, camp, mode = await self._select()
            if ch is None:
                self.current = None
                self._last_tick = None
                if mode == "custom_wait":
                    self.set_status("status.custom_offline", channel=self.active_custom()["channel"])
                elif mode == "idle":
                    self.set_status("status.idle")
                else:
                    self.set_status("status.no_stream")
                await self._wait(60)
                continue

            if not self.current or self.current["channel"]["login"] != ch["login"] or self.current["mode"] != mode:
                self._stall = 0
                if mode == "custom":
                    log.info(t("log.custom_watching", channel=ch["display_name"]))
                else:
                    log.info(t("log.watching", channel=ch["display_name"],
                               game=ch["game"]["name"] if ch["game"] else "-",
                               campaign=camp["name"] if camp else "-"))
                self.current = {"channel": ch, "mode": mode, "since": iso(now_utc()),
                                "campaign_id": camp["id"] if camp else None, "drop_id": None}
            else:
                self.current["channel"] = ch
                self.current["campaign_id"] = camp["id"] if camp else None

            ok = await self.tw.send_watch(ch)
            self.current["last_watch_ok"] = ok
            self.current["last_watch"] = iso(now_utc())
            if not ok:
                log.warning(t("log.watch_failed", channel=ch["display_name"]))

            # count custom-watch time
            now_m = time.monotonic()
            if mode == "custom" and ok:
                item = self.active_custom()
                if self._last_tick and self._last_tick_login == ch["login"]:
                    item["watched_seconds"] = item.get("watched_seconds", 0) + min(now_m - self._last_tick, 90)
                if item["minutes"] and item["watched_seconds"] >= item["minutes"] * 60:
                    log.info(t("log.custom_done", channel=item["channel"], minutes=item["minutes"]))
                    item["state"] = "done"
                    item["finished_at"] = iso(now_utc())
                    self.custom_history.append(self.custom_queue.pop(0))
                self.save_custom()
            self._last_tick = now_m if ok else None
            self._last_tick_login = ch["login"]

            await self._update_progress(ch, mode, camp)
            await self.claim_ready()

            if self.settings["claim_points"]:
                try:
                    bal = await self.tw.claim_points_bonus(ch)
                    if bal is not None:
                        self.points_claimed += 1
                        log.info(t("log.points", channel=ch["display_name"], balance=bal))
                except TwitchError as exc:
                    log.debug("Channel points error: %s", exc)

            self.set_status("status.watching_custom" if mode == "custom" else "status.watching",
                            channel=ch["display_name"])
            await self._wait(WATCH_INTERVAL - (time.monotonic() - tick_start))

    # ------------------------------------------------------------ UI data
    def campaigns_view(self) -> list[dict]:
        now = now_utc()
        pinned = self._pinned_ids()
        ranked_ids = [c["id"] for c in self.ranked_campaigns()]
        out = []
        for c in self.campaigns.values():
            drops = []
            for d in c["drops"]:
                st = self.drop_state(c, d, now)
                remaining = max(d["required_minutes"] - d["current_minutes"], 0)
                mins_left = (d["ends_at"] - now).total_seconds() / 60 if d["ends_at"] else None
                drops.append({
                    **{k: v for k, v in d.items() if k not in ("starts_at", "ends_at")},
                    "starts_at": iso(d["starts_at"]), "ends_at": iso(d["ends_at"]),
                    "state": st, "remaining_minutes": remaining,
                    "achievable": mins_left is None or remaining <= mins_left,
                    "pinned": d["id"] in pinned,
                    "precondition_names": [dd["name"] for dd in c["drops"] if dd["id"] in d["preconditions"]],
                })
            if self._campaign_active(c, now):
                cstate = "active"
            elif c["starts_at"] and now < c["starts_at"]:
                cstate = "upcoming"
            else:
                cstate = "expired"
            out.append({
                **{k: v for k, v in c.items() if k not in ("starts_at", "ends_at", "drops")},
                "starts_at": iso(c["starts_at"]), "ends_at": iso(c["ends_at"]),
                "state": cstate, "eligible": self._campaign_eligible(c),
                "rank": ranked_ids.index(c["id"]) if c["id"] in ranked_ids else None,
                "drops": drops,
            })
        return out

    def state_view(self) -> dict:
        cur = None
        if self.current:
            cur = dict(self.current)
            if cur.get("drop_id"):
                c, d = self._find_drop(cur["drop_id"])
                if d:
                    cur["drop"] = {"id": d["id"], "name": d["name"], "current": d["current_minutes"],
                                   "required": d["required_minutes"], "campaign": c["name"],
                                   "game": c["game"]["name"], "box_art": c["game"]["box_art"],
                                   "benefits": d["benefits"], "ends_at": iso(d["ends_at"])}
            extras = []
            for did in cur.get("extra_drop_ids") or []:
                c2, d2 = self._find_drop(did)
                if d2:
                    extras.append({"id": d2["id"], "name": d2["name"], "current": d2["current_minutes"],
                                   "required": d2["required_minutes"], "campaign": c2["name"],
                                   "game": c2["game"]["name"]})
            cur["extra_drops"] = extras
            if cur.get("campaign_id") and cur["campaign_id"] in self.campaigns:
                c = self.campaigns[cur["campaign_id"]]
                cur["campaign"] = {"id": c["id"], "name": c["name"], "game": c["game"]["name"],
                                   "ends_at": iso(c["ends_at"])}
        ranked = self.ranked_campaigns()
        claimable = sum(1 for c in self.campaigns.values() for d in c["drops"]
                        if self.drop_state(c, d) == "claimable")
        games = sorted({c["game"]["name"] for c in self.campaigns.values() if c["game"]["name"]},
                       key=str.lower)
        return {
            "version": __version__,
            "lang": i18n.current(),
            "languages": i18n.languages(),
            "desktop": self.desktop,
            "donate_url": DONATE_URL,
            "auth": self.auth,
            "status": self.status,
            "paused": self.paused,
            "loading": {**self.loading, "stages": LOADING_STAGES},
            "update": self.update,
            "events": list(self.events),
            "current": cur,
            "settings": self.settings,
            "custom_queue": self.custom_queue,
            "custom_history": self.custom_history[-15:],
            "claimed_session": self.claimed_session[-30:],
            "points_claimed": self.points_claimed,
            "campaigns_version": self.campaigns_version,
            "campaigns_updated": self.campaigns_updated,
            "history_version": self.history_version,
            "history_count": len(self.history),
            "games": games,
            "stats": {
                "campaigns": len(self.campaigns),
                "active": sum(1 for c in self.campaigns.values() if self._campaign_active(c)),
                "minable": len(ranked),
                "claimable": claimable,
            },
            "queue_preview": [{"id": c["id"], "name": c["name"], "game": c["game"]["name"]}
                              for c in ranked[:8]],
            "logs": list(self.logs)[-150:],
        }

    # ------------------------------------------------------------ commands
    def add_custom(self, channel: str, minutes: int) -> dict:
        channel = channel.strip().lstrip("@").split("/")[-1].lower()
        if not channel:
            raise ValueError(t("error.channel_empty"))
        item = {"id": new_id(), "channel": channel, "display_name": channel,
                "minutes": max(int(minutes), 0), "watched_seconds": 0,
                "state": "queued", "added_at": iso(now_utc())}
        self.custom_queue.append(item)
        self.save_custom()
        log.info(t("log.custom_added", channel=channel,
                   duration=t("log.minutes", minutes=minutes) if minutes else t("log.unlimited")))
        self.wake()
        return item

    def remove_custom(self, item_id: str) -> None:
        for i, it in enumerate(self.custom_queue):
            if it["id"] == item_id:
                it["state"] = "stopped"
                it["finished_at"] = iso(now_utc())
                self.custom_history.append(self.custom_queue.pop(i))
                log.info(t("log.custom_stopped", channel=it["channel"]))
                break
        self.save_custom()
        self.wake()

    def move_custom(self, item_id: str, direction: int) -> None:
        q = self.custom_queue
        for i, it in enumerate(q):
            if it["id"] == item_id:
                j = i + direction
                if 0 <= j < len(q):
                    q[i], q[j] = q[j], q[i]
                break
        self.save_custom()
        self.wake()

    def update_settings(self, new: dict) -> None:
        for k, v in new.items():
            if k in DEFAULT_SETTINGS:
                self.settings[k] = v
        if "language" in new:
            i18n.set_language(self.settings["language"])
        self.save_settings()
        self._no_stream.clear()
        self.campaigns_version += 1
        self.wake()

    def pin_drop(self, drop_id: str, pinned: bool) -> None:
        drops = self.settings["priority_drops"]
        if pinned and drop_id not in self._pinned_ids():
            c, d = self._find_drop(drop_id)
            if d is None:
                raise ValueError(t("error.drop_not_found"))
            drops.append({"id": d["id"], "name": d["name"], "game": c["game"]["name"], "campaign": c["name"]})
            log.info(t("log.pinned", drop=d["name"], game=c["game"]["name"]))
        elif not pinned:
            self.settings["priority_drops"] = [p for p in drops if p["id"] != drop_id]
        self.update_settings({})

    def switch_channel(self) -> None:
        self._force_switch = True
        self.wake()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        log.info(t("log.paused" if paused else "log.resumed"))
        self.wake()

    def request_refresh(self) -> None:
        self._need_refresh = True
        self.wake()

    async def manual_claim(self, drop_id: str) -> bool:
        c, d = self._find_drop(drop_id)
        if d is None:
            raise ValueError(t("error.drop_not_found"))
        return await self.claim(c, d)

    def logout(self) -> None:
        self.tw.logout()
        self.auth = {"state": "logged_out"}
        self.campaigns = {}
        self.campaigns_version += 1
        self.current = None
        self.loading.update(first=True, stage="login", done=0, total=0)
        log.info(t("log.logged_out"))
