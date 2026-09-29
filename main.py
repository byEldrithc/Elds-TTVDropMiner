"""Eld's TTVDropMiner — launcher with a local web panel (browser mode)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from aiohttp import web

import i18n
from discord_rpc import DiscordPresence
from miner import Miner, MemoryLogHandler
from version import APP_ID, APP_NAME, __version__

FROZEN = getattr(sys, "frozen", False)   # packaged .exe (PyInstaller)
# in the packaged build web/, assets/ and locales/ live under _MEIPASS
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_DIR = BASE_DIR / "web"
ASSETS_DIR = BASE_DIR / "assets"
_APPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home())
# the installed build may not be able to write next to the exe, so data lives in the user profile
DATA_DIR = (Path(os.environ["TDM_DATA_DIR"]) if os.environ.get("TDM_DATA_DIR")
            else _APPDATA / APP_ID / "data" if FROZEN
            else Path(__file__).resolve().parent / "data")
LEGACY_DATA_DIRS = [_APPDATA / "TwitchDropMiner"]   # data folders of earlier builds


def migrate_legacy_data() -> None:
    """Moves the data folder of an earlier build (old app name) to the current location."""
    if not FROZEN or os.environ.get("TDM_DATA_DIR") or DATA_DIR.exists():
        return
    for old in LEGACY_DATA_DIRS:
        if (old / "data").is_dir():
            try:
                DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(old / "data", DATA_DIR)
                shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
            return


def setup_logging(miner: Miner, debug: bool) -> None:
    logger = logging.getLogger("miner")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(DATA_DIR / "log.txt", maxBytes=2_000_000, backupCount=2, encoding="utf8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout:  # a windowed .exe has no stdout
        try:  # redirected output would otherwise use the ANSI code page and choke on emoji
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    mem = MemoryLogHandler(miner.logs)
    mem.setLevel(logging.INFO)
    logger.addHandler(mem)


def make_app(miner: Miner, on_show: Callable[[], None] | None = None) -> web.Application:
    routes = web.RouteTableDef()

    async def body(request: web.Request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001
            return {}

    def ok(**extra) -> web.Response:
        return web.json_response({"ok": True, **extra})

    def fail(msg: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": msg}, status=status)

    @routes.get("/")
    async def index(_):
        # revalidate every load, or the WebView keeps showing the old page after an update
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/api/state")
    async def state(_):
        return web.json_response(miner.state_view())

    @routes.get("/api/i18n/{lang}")
    async def strings(request):
        return web.json_response({"lang": request.match_info["lang"],
                                  "strings": i18n.catalog(request.match_info["lang"])})

    @routes.get("/api/campaigns")
    async def campaigns(_):
        return web.json_response({"version": miner.campaigns_version,
                                  "campaigns": miner.campaigns_view()})

    @routes.get("/api/history")
    async def history(_):
        return web.json_response({"version": miner.history_version, "history": miner.history})

    @routes.post("/api/settings")
    async def settings(request):
        miner.update_settings(await body(request))
        return ok()

    @routes.post("/api/pin")
    async def pin(request):
        data = await body(request)
        try:
            miner.pin_drop(data["drop_id"], bool(data.get("pinned", True)))
        except (KeyError, ValueError) as exc:
            return fail(str(exc))
        return ok()

    @routes.post("/api/claim")
    async def claim(request):
        data = await body(request)
        try:
            res = await miner.manual_claim(data["drop_id"])
        except Exception as exc:  # noqa: BLE001
            return fail(str(exc))
        return ok(claimed=res)

    @routes.post("/api/refresh")
    async def refresh(_):
        miner.request_refresh()
        return ok()

    @routes.post("/api/pause")
    async def pause(request):
        miner.set_paused(bool((await body(request)).get("paused", True)))
        return ok()

    @routes.post("/api/switch")
    async def switch(_):
        miner.switch_channel()
        return ok()

    @routes.post("/api/logout")
    async def logout(_):
        miner.logout()
        miner.wake()
        return ok()

    @routes.post("/api/custom/add")
    async def custom_add(request):
        data = await body(request)
        try:
            item = miner.add_custom(str(data.get("channel", "")), int(data.get("minutes", 0)))
        except (ValueError, TypeError) as exc:
            return fail(str(exc))
        return ok(item=item)

    @routes.post("/api/custom/remove")
    async def custom_remove(request):
        miner.remove_custom(str((await body(request)).get("id", "")))
        return ok()

    @routes.post("/api/custom/move")
    async def custom_move(request):
        data = await body(request)
        miner.move_custom(str(data.get("id", "")), int(data.get("direction", 0)))
        return ok()

    # desktop app: a second launch brings the running window to the front
    @routes.post("/api/app/show")
    async def app_show(_):
        if on_show is None:
            return fail("no window", 404)
        on_show()
        return ok()

    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static/", WEB_DIR)
    app.router.add_static("/assets/", ASSETS_DIR)
    return app


async def port_in_use(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    return True


async def serve(miner: Miner, port: int, on_show: Callable[[], None] | None = None,
                on_ready: Callable[[], None] | None = None) -> None:
    """Serves the panel and runs the miner until cancelled."""
    runner = web.AppRunner(make_app(miner, on_show), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    logging.getLogger("miner").info(i18n.t("log.ui_ready", name=APP_NAME, version=__version__,
                                           url=f"http://127.0.0.1:{port}"))
    if on_ready:
        on_ready()
    presence = asyncio.create_task(DiscordPresence(miner).run())
    try:
        await miner.run()
    finally:
        presence.cancel()
        await miner.tw.close()
        await runner.cleanup()


async def main_async(port: int, open_browser: bool, debug: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    # if two copies run with the same account, Twitch only counts one of them
    if await port_in_use(port):
        print(f"{APP_NAME} is already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    miner = Miner(DATA_DIR)
    setup_logging(miner, debug)
    await serve(miner, port, on_ready=(lambda: webbrowser.open(url)) if open_browser else None)


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--debug", action="store_true", help="verbose log")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_async(args.port, not args.no_browser, args.debug))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
