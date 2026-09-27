"""Checks every locale against locales/en.json: missing/extra keys and mismatched {placeholders}.

Usage: python tools/check_locales.py      (exit code 1 if a problem is found)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "locales"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def flatten(tree: dict, prefix: str = "") -> dict[str, str]:
    out = {}
    for k, v in tree.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = str(v)
    return out


def main() -> int:
    base = flatten(json.loads((LOCALES / "en.json").read_text("utf8")))
    problems = 0
    for path in sorted(LOCALES.glob("*.json")):
        if path.stem == "en":
            continue
        try:
            loc = flatten(json.loads(path.read_text("utf8")))
        except ValueError as exc:
            print(f"{path.name}: invalid JSON: {exc}")
            problems += 1
            continue
        # "<key>_one" (singular form) is optional; each language adds it only if it needs it
        missing = sorted(k for k in set(base) - set(loc) if not k.endswith("_one"))
        extra = sorted(k for k in set(loc) - set(base) if not (k.endswith("_one") and k[:-4] in base))
        wrong = [k for k in base if k in loc and not k.endswith("_one")
                 and set(PLACEHOLDER.findall(base[k])) != set(PLACEHOLDER.findall(loc[k]))]
        for label, keys in (("missing", missing), ("extra", extra), ("placeholder mismatch", wrong)):
            for k in keys:
                print(f"{path.name}: {label}: {k}")
        problems += len(missing) + len(extra) + len(wrong)
        if not (missing or extra or wrong):
            print(f"{path.name}: OK ({len(loc)} strings)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
