"""Single source of truth for the app name, version and GitHub repository."""

APP_NAME = "Eld's TTVDropMiner"
APP_ID = "EldsTTVDropMiner"      # file/folder/registry safe name
__version__ = "1.1.0"

# Used by the update checker. The check is skipped while these are placeholders.
GITHUB_OWNER = "byEldrithc"
GITHUB_REPO = "Elds-TTVDropMiner"


def github_configured() -> bool:
    return not GITHUB_OWNER.startswith("YOUR_") and not GITHUB_REPO.startswith("YOUR_")


def releases_url() -> str:
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
