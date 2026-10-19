# Icon Countdown

This project was created by the **Icon Countdown** app. It changes your Roblox game icons on a countdown before an update (24h, 12h, 6h, 4h, 3h, 2h, 1h, then the update icon) and notifies you on every change.

You don't need to edit anything here. Use the app instead:

**https://YOUR-GITHUB-NAME.github.io/roblox-icon-countdown/**

What's in here:

- `docs/` is the app itself.
- `games/` holds each game's settings and countdown icons. The app writes these.
- `updater.py` is the engine. `.github/workflows/countdown-tick.yml` runs it every 5 minutes while a countdown is active, then switches itself off.
- `state.json` is what the engine has done so far. The engine writes it.
- Your Roblox keys, ntfy topic and Discord webhook are stored as encrypted secrets under Settings, Secrets and variables, Actions.
