"""Desktop app: shows the panel in its own window and hides to the system tray on close.

The window is drawn by Windows' WebView2 (Edge) component and the tray icon uses the standard
Windows notification area API. The app never touches games or any other process.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import urllib.request
import webbrowser
import winreg
from pathlib import Path

import pystray
import webview
from PIL import Image

import main as core
from i18n import t
from miner import Miner
from version import APP_ID, APP_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = APP_ID
LEGACY_RUN_VALUES = ["TwitchDropMiner"]

log = logging.getLogger("miner")


# ------------------------------------------------------------ start with Windows
def autostart_command() -> str:
    if core.FROZEN:
        return f'"{sys.executable}" --tray'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{Path(__file__).resolve()}" --tray'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def migrate_autostart() -> None:
    """Replaces the start-up entry of an earlier build (old app name) with the current one."""
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            for name in LEGACY_RUN_VALUES:
                try:
                    winreg.QueryValueEx(key, name)
                    winreg.DeleteValue(key, name)
                    found = True
                except OSError:
                    pass
    except OSError:
        return
    if found and core.FROZEN:
        set_autostart(True)


# --------------------------------------------------------------------- the app
class DesktopApp:
    def __init__(self, port: int, debug: bool, start_hidden: bool):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.debug = debug
        self.start_hidden = start_hidden
        self.loop = asyncio.new_event_loop()
        self.miner: Miner | None = None
        self.window: webview.Window | None = None
        self.tray: pystray.Icon | None = None
        self.quitting = False
        self.tray_hint_shown = False
        self.ready = threading.Event()
        self.server_error: BaseException | None = None
        self._task: asyncio.Task | None = None

    # ---- background: web server + miner
    def _server_thread(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001
            self.server_error = exc
            log.exception("Server stopped: %s", exc)
        finally:
            self.ready.set()
            self.loop.close()

    async def _serve(self) -> None:
        self._task = asyncio.current_task()
        try:
            await core.serve(self.miner, self.port, on_show=self.show, on_ready=self.ready.set)
        except asyncio.CancelledError:
            pass

    def _call(self, fn, *args) -> None:
        """Calls a miner method on its own event loop."""
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn, *args)

    # ---- window
    def show(self) -> None:
        if self.window:
            self.window.show()
            self.window.restore()

    def hide(self) -> None:
        if self.window:
            self.window.hide()
        if not self.tray_hint_shown and self.tray:
            self.tray_hint_shown = True
            self._notify(APP_NAME, t("tray.hidden_hint"))

    def _on_closing(self) -> bool:
        if self.quitting:
            return True
        # the close button does not quit the app, it hides it to the tray
        threading.Thread(target=self.hide, daemon=True).start()
        return False

    # ---- tray
    def _notify(self, title: str, body: str) -> None:
        if self.tray:
            try:
                self.tray.notify(body, title)
            except Exception:  # noqa: BLE001
                pass

    def _on_event(self, ev: dict) -> None:
        self._notify(ev["title"], ev["body"])
        if self.tray:
            self.tray.update_menu()

    def _toggle_pause(self, *_):
        if self.miner:
            self._call(self.miner.set_paused, not self.miner.paused)

    def _toggle_autostart(self, *_):
        try:
            set_autostart(not autostart_enabled())
        except OSError as exc:
            log.warning("Could not change the start-up setting: %s", exc)

    def _open_data(self, *_):
        webbrowser.open(core.DATA_DIR.as_uri())

    def _open_update(self, *_):
        if self.miner and self.miner.update:
            webbrowser.open(self.miner.update["url"])

    def quit(self, *_):
        self.quitting = True
        if self._task:
            self.loop.call_soon_threadsafe(self._task.cancel)
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    def _make_tray(self) -> pystray.Icon:
        try:
            image = Image.open(core.ASSETS_DIR / "icon.png")
        except OSError:
            image = Image.new("RGBA", (64, 64), (145, 70, 255, 255))
        has_update = lambda _: bool(self.miner and self.miner.update)  # noqa: E731
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: t("tray.open"), lambda *_: self.show(), default=True),
            pystray.MenuItem(lambda _: t("tray.resume") if self.miner and self.miner.paused else t("tray.pause"),
                             self._toggle_pause),
            pystray.MenuItem(lambda _: t("tray.update", version=self.miner.update["version"])
                             if has_update(_) else "", self._open_update, visible=has_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: t("tray.autostart"), self._toggle_autostart,
                             checked=lambda _: autostart_enabled()),
            pystray.MenuItem(lambda _: t("tray.data_folder"), self._open_data),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: t("tray.quit"), self.quit),
        )
        return pystray.Icon(APP_ID, image, APP_NAME, menu)

    def _tray_status_loop(self) -> None:
        # keep the current status in the tray icon tooltip
        while not self.quitting:
            if self.tray and self.miner:
                self.tray.title = f"{APP_NAME}\n{self.miner.status}"[:127]
            threading.Event().wait(5)

    # ---- run
    def run(self) -> None:
        core.migrate_legacy_data()
        migrate_autostart()
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.miner = Miner(core.DATA_DIR)
        self.miner.desktop = True
        self.miner.event_listeners.append(self._on_event)
        core.setup_logging(self.miner, self.debug)
        threading.Thread(target=self._server_thread, name="miner", daemon=True).start()
        self.ready.wait(15)
        if self.server_error:
            raise SystemExit(f"Could not start: {self.server_error}")

        self.tray = self._make_tray()
        self.tray.run_detached()
        threading.Thread(target=self._tray_status_loop, daemon=True).start()

        self.window = webview.create_window(APP_NAME, self.url, width=1280, height=860,
                                            min_size=(900, 600), hidden=self.start_hidden,
                                            background_color="#0e0e10")
        self.window.events.closing += self._on_closing
        storage = core.DATA_DIR.parent / "webview"
        webview.start(private_mode=False, storage_path=str(storage))
        # the window loop has ended: shut everything down
        if not self.quitting:
            self.quit()


def show_existing(port: int) -> bool:
    """Tries to bring the window of the running copy to the front."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/app/show", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tray", action="store_true", help="start hidden in the tray")
    parser.add_argument("--debug", action="store_true", help="verbose log")
    args = parser.parse_args()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # if two copies run with the same account, Twitch only counts one of them
    if asyncio.run(core.port_in_use(args.port)):
        if not args.tray and not show_existing(args.port):
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        return
    DesktopApp(args.port, args.debug, args.tray).run()


if __name__ == "__main__":
    main()
