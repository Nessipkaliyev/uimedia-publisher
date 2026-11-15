"""Cloud-friendly publishing: a queue of posts with UTC times, run on a cron.

Build (on Mac, uploads covers to Cloudinary once):
  python3 run_queue.py --build ../posts/batch1 2026-11-16 queue.json
Run (GitHub Actions, hourly): publishes everything due and not yet published.
  python3 run_queue.py queue.json published.json
Env for run: IG_USER_ID, IG_ACCESS_TOKEN, IG_API_BASE (optional).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

LOCAL_UTC_OFFSET_HOURS = 5  # Kazakhstan

log = logging.getLogger("run_queue")


def build_queue(posts: list[tuple[str, dict]], day: date, slots: list[tuple[int, int]],
                utc_offset_hours: int = LOCAL_UTC_OFFSET_HOURS) -> list[dict]:
    if len(posts) > len(slots):
        raise ValueError(f"{len(posts)} posts but only {len(slots)} slots")
    local_tz = timezone(timedelta(hours=utc_offset_hours))
    queue = []
    for (post_id, post), (h, m) in zip(posts, slots):
        at = datetime(day.year, day.month, day.day, h, m, tzinfo=local_tz).astimezone(timezone.utc)
        queue.append({"id": post_id, "at_utc": at.isoformat(), "post": post})
    return queue


def _approved(item: dict, entry: dict) -> bool:
    return not item.get("needs_approval") or entry.get("approval") == "approved"


def due_items(queue: list[dict], state: dict, now: datetime) -> list[dict]:
    return [q for q in queue
            if datetime.fromisoformat(q["at_utc"]) <= now
            and "media_id" not in state.get(q["id"], {})
            and _approved(q, state.get(q["id"], {}))]


def publish_due(queue: list[dict], state: dict, now: datetime, publish_one: Callable[[dict], str]) -> dict:
    """Return a new state dict; one failure never blocks the rest."""
    new_state = dict(state)
    for item in due_items(queue, state, now):
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            new_state[item["id"]] = {"media_id": publish_one(item), "at": stamp}
            log.info("%s published", item["id"])
        except Exception as err:  # recorded and retried next run
            new_state[item["id"]] = {"error": str(err), "at": stamp}
            log.error("%s failed: %s", item["id"], err)
    return new_state


def _build(batch_dir: Path, day: date, out: Path) -> int:
    from cloud_upload import CloudConfig, upload
    from ig_publish import load_env
    from schedule_batch import SLOTS

    config = CloudConfig.from_env(load_env(Path(__file__).with_name(".env")))
    posts = []
    for post_file in sorted(batch_dir.glob("[0-9][0-9].json")):
        raw = json.loads(post_file.read_text())
        urls = [upload(batch_dir / f, config) for f in raw.get("media_files", [])]
        post = {"kind": raw["kind"], "media_urls": raw.get("media_urls", []) + urls, "caption": raw["caption"]}
        posts.append((post_file.stem, post))
    out.write_text(json.dumps(build_queue(posts, day, SLOTS), ensure_ascii=False, indent=2))
    log.info("Queue with %d posts written to %s", len(posts), out)
    return 0


def _run(queue_path: Path, state_path: Path) -> int:
    from ig_publish import DEFAULT_API_BASE, Post, make_http, publish

    user_id, token = os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN")
    if not user_id or not token:
        log.error("IG_USER_ID and IG_ACCESS_TOKEN must be set")
        return 1
    http = make_http(token, os.environ.get("IG_API_BASE") or DEFAULT_API_BASE)

    def publish_one(item: dict) -> str:
        p = item["post"]
        return publish(Post(kind=p["kind"], media_urls=tuple(p["media_urls"]), caption=p["caption"]), user_id, http)

    queue = json.loads(queue_path.read_text())
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    bot = _make_bot()
    if bot:
        state = _sync_approvals(bot, queue, state)
    new_state = publish_due(queue, state, datetime.now(timezone.utc), publish_one)
    if bot:
        _report(bot, queue, state, new_state)
    state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2))
    failed = [k for k, v in new_state.items() if isinstance(v, dict) and "error" in v and k != "_tg"]
    return 1 if failed else 0


def _make_bot():
    """Telegram client if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; publishing works without it."""
    from telegram_bot import TelegramClient

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    return TelegramClient(token, int(chat)) if token and chat else None


def _local_time(item: dict) -> str:
    local = datetime.fromisoformat(item["at_utc"]).astimezone(timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS)))
    return local.strftime("%d.%m %H:%M")


def _sync_approvals(bot, queue: list[dict], state: dict) -> dict:
    """Read button presses, then send new approval requests. Telegram errors never block publishing."""
    from telegram_bot import TelegramError, apply_callbacks, items_to_ask

    try:
        updates = bot.updates(state.get("_tg", {}).get("offset", 0))
        state, acks = apply_callbacks(state, updates, allowed_chat_id=bot.chat_id)
        for ack in acks:
            bot.acknowledge(ack)
        for item in items_to_ask(queue, state):
            bot.ask_approval(item, _local_time(item))
            state = {**state, item["id"]: {**state.get(item["id"], {}), "asked": True}}
    except TelegramError as err:
        log.error("Telegram sync failed: %s", err)
    return state


def _report(bot, queue: list[dict], before: dict, after: dict) -> None:
    from telegram_bot import TelegramError

    titles = {q["id"]: q["post"]["caption"].split("\n", 1)[0] for q in queue}
    for item_id, entry in after.items():
        if item_id == "_tg" or entry == before.get(item_id):
            continue
        text = (f"✅ Вышел пост {item_id}: {titles.get(item_id, '')}" if "media_id" in entry
                else f"❌ Ошибка в посте {item_id}: {entry.get('error', '')[:300]}\nПовторю через час.")
        try:
            bot.send(text)
        except TelegramError as err:
            log.error("Telegram report failed: %s", err)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv and argv[0] == "--build":
        return _build(Path(argv[1]).resolve(), date.fromisoformat(argv[2]), Path(argv[3]))
    return _run(Path(argv[0]), Path(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
