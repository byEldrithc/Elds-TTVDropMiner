"""Single source of truth for the app name, version and GitHub repository."""

APP_NAME = "Eld's TTVDropMiner"
APP_ID = "EldsTTVDropMiner"      # file/folder/registry safe name
__version__ = "1.1.1"

# Used by the update checker. The check is skipped while these are placeholders.
GITHUB_OWNER = "byEldrithc"
GITHUB_REPO = "Elds-TTVDropMiner"

DONATE_URL = "https://buymeacoffee.com/eldrithc_"

# Application ID from https://discord.com/developers/applications (its name is what Discord shows).
# Discord Rich Presence is skipped while this is empty.
DISCORD_CLIENT_ID = "1553725969217622036"


def github_configured() -> bool:
    return not GITHUB_OWNER.startswith("YOUR_") and not GITHUB_REPO.startswith("YOUR_")


def repo_url() -> str:
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"


def releases_url() -> str:
    return f"{repo_url()}/releases"
