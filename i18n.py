"""Translations: every user-facing string lives in locales/<code>.json.

Keys are dotted paths ("log.watching"). Missing keys fall back to English, then to the key
itself, so a partially translated locale never breaks the app. To add a language, copy
locales/en.json to locales/<code>.json, translate the values and set "_meta.name".
"""
from __future__ import annotations

import ctypes
import json
import locale
import sys
from pathlib import Path

LOCALES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "locales"
DEFAULT = "en"

_catalogs: dict[str, dict[str, str]] = {}
_names: dict[str, str] = {}
_current = DEFAULT


def _flatten(tree: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in tree.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = str(v)
    return out


def load() -> None:
    _catalogs.clear()
    _names.clear()
    for path in sorted(LOCALES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf8"))
        except (OSError, ValueError):
            continue
        code = path.stem
        _catalogs[code] = _flatten(data)
        _names[code] = (data.get("_meta") or {}).get("name", code)


def languages() -> list[dict[str, str]]:
    if not _catalogs:
        load()
    order = [DEFAULT] + sorted(c for c in _catalogs if c != DEFAULT)
    return [{"code": c, "name": _names[c]} for c in order if c in _catalogs]


def system_language() -> str:
    """Best match for the Windows display language, or English."""
    if not _catalogs:
        load()
    tag = ""
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        tag = locale.windows_locale.get(lcid, "")
    except (AttributeError, OSError):
        tag = (locale.getlocale()[0] or "")
    tag = tag.replace("_", "-")
    if tag in _catalogs:
        return tag
    base = tag.split("-")[0].lower()
    for code in _catalogs:
        if code.split("-")[0].lower() == base:
            return code
    return DEFAULT


def resolve(code: str | None) -> str:
    """Setting value -> actual locale code ("" means follow the system language)."""
    if not _catalogs:
        load()
    return code if code in _catalogs else system_language()


def set_language(code: str | None) -> str:
    global _current
    _current = resolve(code)
    return _current


def current() -> str:
    return _current


def t(key: str, **params) -> str:
    if not _catalogs:
        load()
    text = _catalogs.get(_current, {}).get(key) or _catalogs.get(DEFAULT, {}).get(key) or key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def catalog(code: str) -> dict[str, str]:
    """Flat string table for the web UI (English merged under the chosen language)."""
    if not _catalogs:
        load()
    return {**_catalogs.get(DEFAULT, {}), **_catalogs.get(code, {})}
