import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / "settings.json"
GAMES_DIR = ROOT / "games"
STATE_PATH = ROOT / "state.json"
API = os.environ.get("ROBLOX_API_BASE", "https://apis.roblox.com").rstrip("/")
THUMBS = os.environ.get("ROBLOX_THUMBS_BASE", "https://thumbnails.roblox.com").rstrip("/")
ICON_SIZE = 512
WAIT_FOR_STAGE_SECONDS = 330
LOG_LIMIT = 60

try:
    SECRETS = json.loads(os.environ.get("ALL_SECRETS") or "{}")
except ValueError:
    SECRETS = {}


class RobloxError(Exception):
    pass


def secret(name):
    return str(SECRETS.get(name) or os.environ.get(name) or "").strip()


def now_utc():
    fake = os.environ.get("ICON_UPDATER_NOW", "").strip()
    if fake:
        return datetime.fromisoformat(fake).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return default
    return json.loads(text) if text else default


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def load_settings():
    settings = load_json(SETTINGS_PATH, {}) or {}
    settings.setdefault("timezone", "UTC")
    settings.setdefault("discord_announcements", True)
    settings.setdefault("discord_errors", False)
    settings.setdefault("accounts", [])
    return settings


def load_games():
    games = {}
    if not GAMES_DIR.exists():
        return games
    for folder in sorted(GAMES_DIR.iterdir()):
        path = folder / "game.json"
        if not path.is_file():
            continue
        try:
            game = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        stages = []
        for stage in game.get("stages") or []:
            try:
                stage["hours_before"] = float(stage["hours_before"])
            except (KeyError, TypeError, ValueError):
                continue
            stages.append(stage)
        game["stages"] = sorted(stages, key=lambda s: -s["hours_before"])
        game["_id"] = folder.name
        game["_dir"] = folder
        games[folder.name] = game
    return games


def local_text(settings, moment):
    try:
        zone = ZoneInfo(settings["timezone"])
    except Exception:
        zone = timezone.utc
    return moment.astimezone(zone).strftime("%a %d %b, %H:%M")


def account_key_name(account_id):
    return "ROBLOX_KEY_" + re.sub(r"[^A-Z0-9_]", "_", str(account_id or "MAIN").upper())


def account_label(settings, account_id):
    for account in settings.get("accounts", []):
        if account.get("id") == account_id:
            return account.get("label") or account_id
    return account_id or "MAIN"


def roblox(method, path, key, files=None, attempts=4):
    response = None
    for attempt in range(attempts):
        try:
            response = requests.request(method, API + path, headers={"x-api-key": key}, files=files, timeout=30)
        except requests.RequestException as error:
            if attempt == attempts - 1:
                raise RobloxError(f"Couldn't reach Roblox: {error}")
            time.sleep(2 ** attempt * 2)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < attempts - 1:
                time.sleep(2 ** attempt * 2)
                continue
        break
    if response.status_code in (401, 403):
        raise RobloxError(
            f"Roblox refused the key ({response.status_code}). Make sure the key has Assets read+write and Universe read, "
            f"isn't restricted to other games, and belongs to whoever owns this game (you or your group)."
        )
    if not response.ok:
        raise RobloxError(f"Roblox returned {response.status_code} for {method} {path.split('?')[0]}: {response.text[:300]}")
    return response.json() if response.text.strip() else {}


def public_get(url):
    try:
        response = requests.get(url, timeout=20)
        return response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def request_part(data):
    return {"request": (None, json.dumps(data))}


def asset_id_from(result):
    value = str(result.get("assetId") or "")
    if not value and str(result.get("path", "")).startswith("assets/"):
        value = result["path"].split("/", 1)[1]
    return value if value.isdigit() else None


def poll_operation(operation_path, key):
    result = roblox("GET", f"/assets/v1/{operation_path}", key)
    if result.get("error"):
        raise RobloxError(f"Roblox operation failed: {result['error'].get('message', result['error'])}")
    return (result.get("response") or {}) if result.get("done") else None


def refresh_icon_url(gs):
    data = public_get(f"{THUMBS}/v1/games/icons?universeIds={gs['universe_id']}&size=256x256&format=Png&isCircular=false")
    for item in data.get("data") or []:
        if item.get("state") == "Completed" and item.get("imageUrl"):
            gs["icon_url"] = item["imageUrl"]


def resolve(game, gs, key):
    universe = str(game.get("universe_id") or "").strip()
    place = str(game.get("place_id") or "").strip()
    wanted = f"{universe}:{place}"
    if gs.get("resolved_for") == wanted and gs.get("place_id") and gs.get("creator"):
        return
    if not universe:
        if not place.isdigit():
            raise RobloxError("This game has no link or ID. Open it in the app and add one.")
        universe = str(public_get(f"{API}/universes/v1/places/{place}/universe").get("universeId") or "")
        if not universe.isdigit():
            universe = place
    try:
        info = roblox("GET", f"/cloud/v2/universes/{universe}", key)
    except RobloxError as error:
        if "returned 404" in str(error) or "returned 400" in str(error):
            raise RobloxError("Couldn't find that game. Check the game link in the app.")
        raise
    root = str(info.get("rootPlace", "")).rsplit("/", 1)[-1]
    if not root.isdigit():
        raise RobloxError("Couldn't find the game's start place.")
    asset = roblox("GET", f"/assets/v1/assets/{root}", key)
    owner = (asset.get("creationContext") or {}).get("creator") or {}
    if owner.get("groupId"):
        creator = {"groupId": str(owner["groupId"])}
    elif owner.get("userId"):
        creator = {"userId": str(owner["userId"])}
    else:
        raise RobloxError("Couldn't tell who owns the game (you or a group).")
    if gs.get("universe_id") not in (None, universe):
        gs["assets"] = {}
    gs.update(
        resolved_for=wanted,
        universe_id=universe,
        place_id=root,
        creator=creator,
        game_name=info.get("displayName") or game.get("name") or "Your game",
    )
    refresh_icon_url(gs)


def current_icon(gs, key):
    return roblox("GET", f"/assets/v1/assets/{gs['place_id']}?readMask=icon", key).get("icon")


def prepared_png(path):
    with Image.open(path) as image:
        image = image.convert("RGBA")
        side = min(image.size)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        image = image.crop((left, top, left + side, top + side)).resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def stage_key(stage):
    return f"{stage['hours_before']:g}"


def stage_label(stage):
    hours = stage["hours_before"]
    if stage.get("name"):
        return stage["name"]
    if hours <= 0:
        return "Update is live"
    if hours < 1:
        return f"{round(hours * 60)} minutes left"
    return f"{hours:g} hour{'s' if hours != 1 else ''} left"


def stage_announcement(stage):
    if stage.get("announcement"):
        return stage["announcement"]
    hours = stage["hours_before"]
    if hours <= 0:
        return "The update is out now! Hop in!"
    if hours < 1:
        return f"{round(hours * 60)} minutes until the update!"
    return f"{hours:g} hour{'s' if hours != 1 else ''} until the update!"


def stage_time(gs, stage):
    return datetime.fromisoformat(gs["update_at"]) - timedelta(hours=stage["hours_before"])


def encode_header(value):
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode() + "?="


def send_ntfy(title, message, image=None, priority="default", tags=""):
    topic = secret("NTFY_TOPIC")
    if not topic:
        return
    server = (secret("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Title": encode_header(title), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        if image:
            headers["Message"] = encode_header(message)
            headers["Filename"] = "icon.png"
            requests.put(f"{server}/{topic}", data=image, headers=headers, timeout=30)
        else:
            requests.post(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=30)
    except requests.RequestException as error:
        print(f"ntfy failed: {error}")


def send_discord(title, message, image=None, color=0x5865F2):
    url = secret("DISCORD_WEBHOOK_URL")
    if not url:
        return
    embed = {"title": title[:250], "description": message[:4000], "color": color}
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    try:
        if image:
            embed["image"] = {"url": "attachment://icon.png"}
            requests.post(url, data={"payload_json": json.dumps(payload)}, files={"files[0]": ("icon.png", image, "image/png")}, timeout=30)
        else:
            requests.post(url, json=payload, timeout=30)
    except requests.RequestException as error:
        print(f"Discord failed: {error}")


class Notifier:
    def __init__(self, state, settings):
        self.state = state
        self.settings = settings

    def log(self, game_id, kind, title, message):
        entry = {"t": now_utc().isoformat(timespec="seconds"), "game": game_id, "kind": kind, "title": title, "message": message}
        log = self.state.setdefault("log", [])
        log.insert(0, entry)
        del log[LOG_LIMIT:]

    def send(self, game_id, title, message, image=None, priority="default", tags="", kind="info", announce=None):
        print(f"[{kind}] {title}: {message}")
        self.log(game_id, kind, title, message)
        send_ntfy(title, message, image, priority, tags)
        if announce and self.settings.get("discord_announcements", True):
            send_discord(announce[0], announce[1], image, announce[2])

    def problem(self, game_id, holder, message):
        print(f"[problem] {game_id}: {message}")
        if holder.get("last_problem") == message:
            return
        holder["last_problem"] = message
        holder["last_problem_at"] = now_utc().isoformat(timespec="seconds")
        name = holder.get("game_name") or "Icon countdown"
        self.log(game_id, "problem", f"Problem: {name}", message)
        send_ntfy(f"Problem: {name}", message, priority="high", tags="warning")
        if self.settings.get("discord_errors"):
            send_discord(f"Problem: {name}", message, color=0xED4245)

    def once(self, gs, key, *args, **kwargs):
        sent = gs.setdefault("sent", [])
        if key in sent:
            return
        sent.append(key)
        self.send(*args, **kwargs)


def upload_image(gs, key, path, digest, label):
    request = {
        "assetType": "Image",
        "displayName": f"Countdown icon {label}"[:50],
        "description": "Game icon countdown",
        "creationContext": {"creator": gs["creator"]},
    }
    files = request_part(request)
    files["fileContent"] = ("icon.png", prepared_png(path), "image/png")
    result = roblox("POST", "/assets/v1/assets", key, files=files)
    if result.get("error"):
        raise RobloxError(f"Roblox wouldn't accept the {label} icon: {result['error'].get('message', result['error'])}")
    entry = {"label": label, "status": "uploading"}
    found = asset_id_from(result.get("response") or {})
    if result.get("done") and found:
        entry.update(asset_id=found, status="moderating")
    elif str(result.get("path", "")).startswith("operations/"):
        entry["operation"] = result["path"]
    else:
        raise RobloxError(f"Unexpected upload reply from Roblox for the {label} icon")
    gs.setdefault("assets", {})[digest] = entry


def refresh_asset(gs, key, digest):
    entry = gs["assets"][digest]
    if entry["status"] == "uploading":
        response = poll_operation(entry["operation"], key)
        if response is None:
            return
        found = asset_id_from(response)
        if not found:
            raise RobloxError(f"Roblox finished uploading the {entry['label']} icon but sent no asset id")
        entry.update(asset_id=found, status="moderating")
        entry.pop("operation", None)
    if entry["status"] == "moderating":
        result = roblox("GET", f"/assets/v1/assets/{entry['asset_id']}", key)
        verdict = str((result.get("moderationResult") or {}).get("moderationState", "")).upper()
        if verdict.endswith("APPROVED"):
            entry["status"] = "approved"
        elif verdict.endswith("REJECTED"):
            entry["status"] = "rejected"


def prepare_images(game, gs, key, note):
    missing = []
    stage_assets = {}
    for stage in game["stages"]:
        path = game["_dir"] / stage["image"]
        if not path.exists():
            missing.append(stage_label(stage))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        stage_assets[stage_key(stage)] = digest
        if digest not in gs.setdefault("assets", {}):
            upload_image(gs, key, path, digest, stage_label(stage))
        refresh_asset(gs, key, digest)
    gs["stage_assets"] = stage_assets
    gs["missing"] = missing
    if missing:
        raise RobloxError("Pick an icon for: " + ", ".join(missing))
    cid = gs.get("countdown_id")
    for digest in set(stage_assets.values()):
        entry = gs["assets"][digest]
        if entry["status"] == "rejected":
            note.once(
                gs, f"rejected:{cid}:{digest}", game["_id"],
                f"Icon rejected: {gs['game_name']}",
                f"Roblox moderation rejected the '{entry['label']}' icon. Pick a different picture for it in the app and it will re-upload by itself.",
                priority="high", tags="x", kind="problem",
            )
    statuses = [gs["assets"][d]["status"] for d in set(stage_assets.values())]
    if statuses and all(s == "approved" for s in statuses):
        note.once(
            gs, f"ready:{cid}", game["_id"],
            f"Icons approved: {gs['game_name']}",
            f"Roblox approved all {len(statuses)} icons. The countdown is ready.",
            tags="white_check_mark", kind="good",
        )


def set_icon(gs, key, asset_id):
    place = gs["place_id"]
    result = roblox(
        "PATCH",
        f"/assets/v1/assets/{place}?updateMask=icon",
        key,
        files=request_part({"assetId": place, "icon": f"assets/{asset_id}"}),
    )
    if str(result.get("path", "")).startswith("operations/") and not result.get("done"):
        for _ in range(12):
            if poll_operation(result["path"], key) is not None:
                break
            time.sleep(5)
    for _ in range(8):
        if current_icon(gs, key) == f"assets/{asset_id}":
            return
        time.sleep(5)
    raise RobloxError("Roblox accepted the icon change but the game still shows the old icon. It will try again on the next check.")


def apply_stage(game, gs, key, stage, note, settings):
    entry = gs["assets"][gs["stage_assets"][stage_key(stage)]]
    label = stage_label(stage)
    if entry["status"] != "approved":
        note.once(
            gs, f"waiting:{gs.get('countdown_id')}:{stage_key(stage)}", game["_id"],
            f"Waiting for Roblox: {gs['game_name']}",
            f"It's time for '{label}' but Roblox hasn't approved that icon yet. It will switch as soon as it's approved.",
            priority="high", tags="hourglass", kind="warn",
        )
        return False
    set_icon(gs, key, entry["asset_id"])
    gs["applied_stage"] = stage_key(stage)
    gs["applied_at"] = now_utc().isoformat(timespec="seconds")
    gs.pop("last_problem", None)
    refresh_icon_url(gs)
    final = stage["hours_before"] <= 0
    note.send(
        game["_id"],
        f"{'Update is live' if final else label}: {gs['game_name']}",
        f"The game icon now shows '{label}'. Update time: {local_text(settings, datetime.fromisoformat(gs['update_at']))}.",
        image=prepared_png(game["_dir"] / stage["image"]),
        tags="rocket" if final else "hourglass_flowing_sand",
        kind="good",
        announce=(gs["game_name"], stage_announcement(stage), 0x57F287 if final else 0xFEE75C),
    )
    return True


def due_stage(game, gs, now):
    due = None
    for stage in game["stages"]:
        if stage_time(gs, stage) <= now:
            due = stage
    return due


def next_stage(game, gs, now):
    for stage in game["stages"]:
        if stage_time(gs, stage) > now:
            return stage
    return None


def start_countdown(game, gs, key, request, note, settings):
    gs["countdown_id"] = request["id"]
    try:
        update_at = datetime.fromisoformat(str(request.get("update_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise RobloxError("The update time couldn't be read. Set it again in the app.")
    if update_at <= now_utc():
        gs["active"] = False
        raise RobloxError(f"The update time {local_text(settings, update_at)} is already in the past. Pick a new time in the app.")
    if not game["stages"]:
        gs["active"] = False
        raise RobloxError("This game has no countdown stages.")
    for field in ("applied_stage", "applied_at", "finished_at", "sent", "last_problem"):
        gs.pop(field, None)
    gs.update(active=True, update_at=update_at.isoformat(), started_at=now_utc().isoformat(timespec="seconds"))
    gs["original_icon"] = current_icon(gs, key)
    lines = [f"{stage_label(s)}: {local_text(settings, stage_time(gs, s))}" for s in game["stages"]]
    note.send(
        game["_id"],
        f"Countdown scheduled: {gs['game_name']}",
        f"Update at {local_text(settings, update_at)}.\n" + "\n".join(lines),
        tags="calendar",
    )


def cancel_countdown(game, gs, key, restore, note):
    gs["active"] = False
    message = "Countdown cancelled. The icon was left as it is."
    if restore and gs.get("original_icon"):
        set_icon(gs, key, gs["original_icon"].split("/", 1)[-1])
        refresh_icon_url(gs)
        message = "Countdown cancelled and the old icon was put back."
    note.send(game["_id"], f"Cancelled: {gs['game_name']}", message, tags="stop_sign")


def finish_countdown(game, gs, note):
    gs["active"] = False
    gs["finished_at"] = now_utc().isoformat(timespec="seconds")
    note.send(game["_id"], f"Countdown finished: {gs['game_name']}", "All icons were shown and the update icon is set.", tags="checkered_flag", kind="good")


def tick_game(game, gs, key, note, settings, allow_wait):
    prepare_images(game, gs, key, note)
    stage = due_stage(game, gs, now_utc())
    if stage and gs.get("applied_stage") != stage_key(stage):
        apply_stage(game, gs, key, stage, note, settings)
    final = game["stages"][-1]
    if gs.get("applied_stage") == stage_key(final) and stage_time(gs, final) <= now_utc():
        finish_countdown(game, gs, note)
        return None
    upcoming = next_stage(game, gs, now_utc())
    if not upcoming or not allow_wait:
        return None
    digest = gs["stage_assets"].get(stage_key(upcoming))
    if not digest or gs["assets"][digest]["status"] != "approved":
        return None
    return (stage_time(gs, upcoming) - now_utc()).total_seconds()


def run_game(game, gs, settings, note, allow_wait=True):
    account = game.get("account") or "MAIN"
    key = secret(account_key_name(account))
    if not gs.get("game_name") and game.get("name"):
        gs["game_name"] = game["name"]
    if not key:
        raise RobloxError(f"No Roblox key saved for '{account_label(settings, account)}'. Add it in the app: Settings, Roblox keys.")
    resolve(game, gs, key)
    request = game.get("countdown") or {}
    if request.get("id") and request["id"] != gs.get("countdown_id"):
        if request.get("cancel"):
            gs["countdown_id"] = request["id"]
            gs["active"] = False
        else:
            start_countdown(game, gs, key, request, note, settings)
    elif gs.get("active") and (not request.get("id") or request.get("cancel")):
        cancel_countdown(game, gs, key, bool(request.get("restore")), note)
    if gs.get("active"):
        wait = tick_game(game, gs, key, note, settings, allow_wait)
        if gs.get("active"):
            refresh_icon_url(gs)
        gs.pop("last_problem", None)
        return wait
    gs.pop("last_problem", None)
    return None


def run_test(games, state, settings, note):
    lines = []
    for gid, game in games.items():
        gs = state["games"].setdefault(gid, {})
        account = game.get("account") or "MAIN"
        key = secret(account_key_name(account))
        name = gs.get("game_name") or game.get("name") or gid
        if not key:
            lines.append(f"{name}: no Roblox key saved for '{account_label(settings, account)}'")
            continue
        try:
            resolve(game, gs, key)
            current_icon(gs, key)
            lines.append(f"{gs['game_name']}: OK")
        except RobloxError as error:
            lines.append(f"{name}: {error}")
    if not games:
        lines.append("No games added yet.")
    channels = [label for label, name in (("phone (ntfy)", "NTFY_TOPIC"), ("Discord", "DISCORD_WEBHOOK_URL")) if secret(name)]
    lines.append("Notifications: " + (", ".join(channels) if channels else "none set up"))
    note.send("", "Test from Icon Countdown", "\n".join(lines), tags="mag")
    if settings.get("discord_announcements") and secret("DISCORD_WEBHOOK_URL"):
        send_discord("Icon Countdown test", "Discord notifications are working.", color=0x5865F2)


def write_output(state):
    target = os.environ.get("GITHUB_OUTPUT")
    active = any(g.get("active") for g in state.get("games", {}).values())
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"active={'true' if active else 'false'}\n")
    return active


def main():
    state = load_json(STATE_PATH, {}) or {}
    state.setdefault("games", {})
    settings = load_settings()
    games = load_games()
    note = Notifier(state, settings)
    for gid in list(state["games"]):
        if gid not in games:
            del state["games"][gid]
    if settings.get("test_request") and settings["test_request"] != state.get("test_done"):
        state["test_done"] = settings["test_request"]
        run_test(games, state, settings, note)
    waits = {}
    for gid, game in games.items():
        gs = state["games"].setdefault(gid, {})
        try:
            wait = run_game(game, gs, settings, note)
            if wait is not None:
                waits[gid] = wait
        except RobloxError as error:
            note.problem(gid, gs, str(error))
    soon = {gid: w for gid, w in waits.items() if 0 < w <= WAIT_FOR_STAGE_SECONDS}
    if soon and not os.environ.get("ICON_UPDATER_NOW"):
        save_state(state)
        time.sleep(max(soon.values()) + 2)
        for gid in soon:
            try:
                run_game(games[gid], state["games"][gid], settings, note, allow_wait=False)
            except RobloxError as error:
                note.problem(gid, state["games"][gid], str(error))
    save_state(state)
    write_output(state)


if __name__ == "__main__":
    main()
