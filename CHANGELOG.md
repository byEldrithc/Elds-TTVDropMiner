# Changelog

All notable changes to Eld's TTVDropMiner are listed here.
The project uses [semantic versioning](https://semver.org/).

## [1.1.1] - 2026-09-27

### Added
- **Discord Rich Presence.** While Discord is running, your profile shows the game and drop
  being mined, its progress and how long it has been running, with the game's cover art.
  You can turn it off under Priorities → Options.
- **Buy me a coffee.** A support link is now in the dashboard header, in the tray menu and
  in the README.

## [1.1.0] - 2026-09-27

### Added
- **Copy button for the login code.** The Twitch sign-in card now has a "Copy code" button,
  and clicking the code itself copies it too. A short confirmation appears once it's on the
  clipboard.
- **Priority-only reminder on the dashboard.** When "Only the games / drops I picked" is
  selected but no game or drop has been picked yet, the dashboard says so and offers a
  button that opens the Priorities tab.

### Changed
- **New installs start in "Only the games / drops I picked" mode.** Nothing is mined until
  you choose what you want, so the app no longer spends time on drops you don't care about.
  Existing installs keep the mode they already have.

### Fixed
- The login code couldn't be selected or copied in the desktop window. It can now be
  selected, and the sign-in card no longer redraws every few seconds, which used to clear
  the selection.

## [1.0.0] - 2026-09-27

First public release.

[1.1.1]: https://github.com/byEldrithc/Elds-TTVDropMiner/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/byEldrithc/Elds-TTVDropMiner/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/byEldrithc/Elds-TTVDropMiner/releases/tag/v1.0.0
