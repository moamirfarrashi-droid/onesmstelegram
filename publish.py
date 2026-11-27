import argparse
import json
import os
import re
import secrets
import subprocess
import time
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
POSTS = BASE / "posts.json"
STATE = BASE / "services-state.json"
TEHRAN = ZoneInfo("Asia/Tehran")
POST_TIMES = ("10:30", "14:30", "17:30")
POST_KINDS = ("photo", "text", "text")
DAYS = 60
SLOTS_PER_DAY = len(POST_TIMES)
WINDOW_START, WINDOW_END = clock.fromisoformat(POST_TIMES[0]), clock(19)
CHAT = re.compile(r"@[a-zA-Z0-9_]+|-100\d+")
TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]+")


class TelegramRejected(RuntimeError):
    pass


class UncertainDelivery(RuntimeError):
    pass


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_posts():
    posts = read_json(POSTS)
    expected_count = DAYS * SLOTS_PER_DAY
    if not isinstance(posts, list) or len(posts) != expected_count:
        raise ValueError(f"posts.json must contain exactly {expected_count} posts.")
    messages = set()
    for index, post in enumerate(posts):
        zero_day, zero_slot = divmod(index, SLOTS_PER_DAY)
        day, slot = zero_day + 1, zero_slot + 1
        kind = POST_KINDS[zero_slot]
        expected = (f"onesms-services-d{day:02}-s{slot}", day, slot, kind, POST_TIMES[zero_slot])
        if (post["id"], post["day"], post["slot"], post["kind"], post["time"]) != expected:
            raise ValueError(f"Invalid post order, type or hour at day {day} slot {slot}.")
        body = post["text"]
        limit = 1024 if kind == "photo" else 4096
        if not 1 <= len(body.encode("utf-16-le")) // 2 <= limit or body in messages:
            raise ValueError(f"Empty, duplicate or oversized text at day {day} slot {slot}.")
        messages.add(body)
        if kind == "photo":
            name = post["media"]
            asset = BASE / name
            if name != f"onesms-day-{day:02}.png" or not asset.is_file() or not 8 < asset.stat().st_size <= 10_000_000:
                raise ValueError(f"Missing or invalid photo for day {day}.")
    return posts


def repository_state():
    if STATE.exists():
        return read_json(STATE)
    return {"channel": None, "start_date": None, "sent": {}, "pending": None}


def save_state(state):
    updated = STATE.with_suffix(".tmp")
    updated.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    updated.replace(STATE)
    for args in (["git", "add", "--", STATE.name],
                 ["git", "commit", "-m", "Update OneSMS services publication state"],
                 ["git", "push", "origin", "HEAD"]):
        result = subprocess.run(args, cwd=BASE, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"State sync failed ({args[1]}). Check workflow write access and branch rules.")


def telegram(method, token, fields, photo=None):
    if photo:
        boundary = secrets.token_hex(24)
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                      f'filename="{photo.name}"\r\nContent-Type: image/png\r\n\r\n').encode())
        parts += [photo.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
        body = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
    else:
        body = json.dumps(fields, ensure_ascii=False).encode()
        content_type = "application/json"
    request = Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                      headers={"Content-Type": content_type}, method="POST")
    try:
        with urlopen(request, timeout=25) as result:
            response = json.load(result)
    except HTTPError as error:
        if 400 <= error.code < 500:
            raise TelegramRejected(f"Telegram rejected {method} with HTTP {error.code}.") from None
        raise UncertainDelivery(f"Telegram returned HTTP {error.code} for {method}.") from None
    except (URLError, OSError, ValueError):
        raise UncertainDelivery(f"No reliable Telegram response for {method}.") from None
    if isinstance(response, dict) and response.get("ok") is False:
        detail = str(response.get("description", "")).replace(token, "[REDACTED]").splitlines()
        raise TelegramRejected(f"Telegram rejected {method}. {detail[0][:200] if detail else ''}")
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise UncertainDelivery(f"Unexpected answer for {method}.")
    return response["result"]


def send(post, channel, token):
    fields = {"chat_id": channel}
    if post["kind"] == "photo":
        fields["caption"] = post["text"]
        result = telegram("sendPhoto", token, fields, BASE / post["media"])
    else:
        fields.update(text=post["text"], link_preview_options={"is_disabled": True})
        result = telegram("sendMessage", token, fields)
    message_id = result.get("message_id")
    if type(message_id) is not int or message_id <= 0:
        raise UncertainDelivery("Telegram did not return a usable message ID.")
    return message_id


def get_channel(name, required=True):
    channel = os.environ.get(name, "").strip().lower()
    if not channel and not required:
        return None
    if not CHAT.fullmatch(channel):
        raise ValueError(f"Set {name} to @channel_username or the channel's numeric -100 ID.")
    return channel


def get_token():
    value = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not TOKEN.fullmatch(value):
        raise ValueError("Set TELEGRAM_BOT_TOKEN in Actions secrets.")
    return value


def render(posts, day, slot):
    return [post for post in posts if post["day"] == day and (slot == 0 or slot == post["slot"])]


def show_preview(posts):
    for post in posts:
        print(f"{post['time']} | {post['category']} | {post['id']}")
        if post["kind"] == "photo":
            print(f"Image: {post['media']}")
        print(post["text"])
        print()


def test_day(posts):
    test_channel = get_channel("TELEGRAM_TEST_CHANNEL_ID")
    live_channel = get_channel("TELEGRAM_CHANNEL_ID", required=False)
    token = get_token()
    test_chat = telegram("getChat", token, {"chat_id": test_channel})
    if test_chat.get("type") != "channel" or type(test_chat.get("id")) is not int:
        raise ValueError("TELEGRAM_TEST_CHANNEL_ID must identify a separate Telegram channel.")
    if live_channel:
        live_chat = telegram("getChat", token, {"chat_id": live_channel})
        if type(live_chat.get("id")) is not int or test_chat["id"] == live_chat["id"]:
            raise ValueError("Set a test channel different from the production channel.")
    elif repository_state()["channel"] == test_channel:
        raise ValueError("The test channel is the recorded production channel.")
    for index, post in enumerate(posts):
        if index:
            time.sleep(3)
        print(f"{post['id']} sent to test. Message ID {send(post, test_channel, token)}")


def due_posts(posts, state, now, start):
    if not start:
        return []
    start = date.fromisoformat(start)
    return [post for post in posts if post["id"] not in state["sent"]
            and datetime.combine(start + timedelta(days=post["day"] - 1),
                                 clock.fromisoformat(post["time"]), TEHRAN) <= now]


def show_status(posts, state, now):
    start = state["start_date"] or os.environ.get("SERVICE_CAMPAIGN_START_DATE", "").strip()
    due = due_posts(posts, state, now, start)
    today = [receipt for receipt in state["sent"].values()
             if datetime.fromisoformat(receipt["at"]).astimezone(TEHRAN).date() == now.date()]
    print(f"Start date: {start or 'unset'} | Sent: {len(state['sent'])}/{len(posts)} | Due: {len(due)} | Sent today: {len(today)}")
    if state["pending"]:
        print(f"Delivery needs review: {state['pending']['id']}")
    if due:
        print(f"Oldest due post: {due[0]['id']}")
    for receipt in today:
        print(f"{receipt['at']} | {receipt['id']} | Telegram message ID {receipt['message_id']}")


def publish_next(posts, state, now):
    if os.environ.get("ENABLE_POSTING", "").strip().lower() != "true":
        print("Automatic posting is disabled. Set the Actions variable ENABLE_POSTING to true when ready.")
        return
    if not WINDOW_START <= now.time() < WINDOW_END:
        print("Outside 10:30–19:00 Tehran. Scheduled posts stay in the queue.")
        return
    if state["pending"]:
        raise RuntimeError(f"Inspect Telegram before retrying pending post {state['pending']['id']}.")
    channel = get_channel("TELEGRAM_CHANNEL_ID")
    start = state["start_date"] or os.environ.get("SERVICE_CAMPAIGN_START_DATE", "").strip()
    if not start:
        raise ValueError("Set SERVICE_CAMPAIGN_START_DATE (YYYY-MM-DD, Tehran local date) in Actions variables.")
    if state["channel"] and state["channel"] != channel:
        raise ValueError("Production channel differs from the saved channel. Restore the original channel ID before continuing.")
    if len(state["sent"]) == len(posts):
        print(f"All {len(posts)} posts have been sent.")
        return
    recent = sorted(datetime.fromisoformat(entry["at"]).astimezone(TEHRAN) for entry in state["sent"].values())
    if len([stamp for stamp in recent if stamp.date() == now.date()]) >= SLOTS_PER_DAY:
        print("Daily cap reached. Unsent posts remain queued.")
        return
    if recent and now - recent[-1] < timedelta(minutes=20):
        print("Minimum spacing between posts has not elapsed.")
        return
    due = due_posts(posts, state, now, start)
    if not due:
        print("No post due yet.")
        return
    post = due[0]
    token = get_token()
    now = datetime.now(TEHRAN)
    if not WINDOW_START <= now.time() < WINDOW_END:
        print("Posting window just closed. The post remains queued.")
        return
    state.update(channel=channel, start_date=start, pending={"id": post["id"], "at": now.isoformat()})
    save_state(state)
    if not WINDOW_START <= datetime.now(TEHRAN).time() < WINDOW_END:
        state["pending"] = None
        save_state(state)
        print("Posting window closed while recording state. The post remains queued.")
        return
    try:
        message_id = send(post, channel, token)
    except TelegramRejected:
        state["pending"] = None
        save_state(state)
        raise
    state["sent"][post["id"]] = {**state["pending"], "message_id": message_id}
    state["pending"] = None
    save_state(state)
    print(f"{post['id']} published. Telegram message ID {message_id}.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["validate", "preview", "test", "publish", "status"], default="preview")
    parser.add_argument("--day", type=int, choices=range(1, DAYS + 1), default=1, metavar="1..60")
    parser.add_argument("--slot", type=int, choices=range(SLOTS_PER_DAY + 1), default=0, metavar="0..3")
    args = parser.parse_args()
    try:
        posts = validate_posts()
        if args.mode == "validate":
            print(f"{len(posts)} posts and {DAYS} photo files validated.")
        elif args.mode == "preview":
            show_preview(render(posts, args.day, args.slot))
        elif args.mode == "test":
            test_day(render(posts, args.day, args.slot))
        elif args.mode == "status":
            show_status(posts, repository_state(), datetime.now(TEHRAN))
        else:
            publish_next(posts, repository_state(), datetime.now(TEHRAN))
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"OneSMS: {error}\n")


if __name__ == "__main__":
    main()
