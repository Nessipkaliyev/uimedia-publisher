"""Telegram digests for ҮЙ: real-estate news found in KZ media, and the content plan.

  python3 digests.py news news_seen.json            # send new real-estate headlines
  python3 digests.py plan queue.json published.json  # send tomorrow's plan
  python3 digests.py plan queue.json published.json --all   # send every planned day
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FEEDS = {
    "Tengrinews": "https://tengrinews.kz/news.rss",
    "Kapital.kz": "https://kapital.kz/feed",
    "Inbusiness": "https://inbusiness.kz/ru/rss",
    "LSM.kz": "https://lsm.kz/rss",
}
# Word-start anchors avoid hits like "безопаСНОСти"; generic words (участок, строительство) are
# only counted next to housing context.
KEYWORDS = re.compile(
    r"\b(жиль|жилищ|жилой|жилые|недвиж|ипотек|квартир|аренд|новострой|застрой|девелоп|риелт|снос|изъят|"
    r"отбасы|ЖК\b|земельн\w* участ|частн\w* дом|строительств\w* жил)",
    re.IGNORECASE,
)
TELEGRAM_LIMIT = 4096
SEEN_KEEP = 1000
FETCH_TIMEOUT_SECONDS = 20
LOCAL_TZ = timezone(timedelta(hours=5))
STATUS = {
    "published": "✅ вышел",
    "approved": "👍 одобрено",
    "rejected": "❌ пропущено",
    "pending": "🕒 ждёт одобрения",
    "queued": "⏳ в очереди",
    "error": "⚠️ ошибка, повтор",
}

log = logging.getLogger("digests")


def parse_rss(xml_text: str, source: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter("item"):
        title, link = (node.findtext("title") or "").strip(), (node.findtext("link") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "source": source})
    return items


def new_relevant(items: list[dict], seen: set[str]) -> list[dict]:
    result, links = [], set(seen)
    for item in items:
        if item["link"] in links or not KEYWORDS.search(item["title"]):
            continue
        links.add(item["link"])
        result.append(item)
    return result


def format_news(items: list[dict]) -> str:
    lines = [f"📰 Новости недвижимости — {len(items)}\n"]
    lines += [f"• {i['title']} — {i['source']}\n{i['link']}\n" for i in items]
    return "\n".join(lines)


def chunk_messages(blocks: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    parts, current = [], ""
    for block in blocks:
        while len(block) > limit:
            parts, current = parts + ([current] if current else []) + [block[:limit]], ""
            block = block[limit:]
        if current and len(current) + len(block) + 1 > limit:
            parts, current = parts + [current], ""
        current = f"{current}\n{block}" if current else block
    return parts + ([current] if current else [])


def _status(item: dict, entry: dict) -> str:
    if "media_id" in entry:
        return STATUS["published"]
    if "error" in entry:
        return STATUS["error"]
    if item.get("needs_approval"):
        return STATUS.get(entry.get("approval", ""), STATUS["pending"])
    return STATUS["queued"]


def format_plan(queue: list[dict], state: dict, day: date) -> str:
    rows = []
    for item in sorted(queue, key=lambda q: q["at_utc"]):
        local = datetime.fromisoformat(item["at_utc"]).astimezone(LOCAL_TZ)
        if local.date() != day:
            continue
        title = item["post"]["caption"].split("\n", 1)[0][:70]
        rows.append(f"{local:%H:%M}  {title}\n          {_status(item, state.get(item['id'], {}))}")
    header = f"🗓 План на {day:%d.%m}"
    return f"{header}\n\n" + ("\n".join(rows) if rows else "Очередь пуста — постов на этот день нет.")


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 uimedia-news/1.0"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _bot():
    from telegram_bot import TelegramClient

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    return TelegramClient(token, int(chat))


def run_news(seen_path: Path) -> int:
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else []
    items = []
    for source, url in FEEDS.items():
        try:
            items += parse_rss(_fetch(url), source)
        except Exception as err:  # one dead feed must not block the others
            log.error("Feed %s failed: %s", source, err)
    fresh = new_relevant(items, set(seen))
    if fresh:
        bot = _bot()
        for part in chunk_messages(format_news(fresh).split("\n\n")):
            bot.send(part)
    seen_path.write_text(json.dumps((seen + [i["link"] for i in fresh])[-SEEN_KEEP:], ensure_ascii=False))
    log.info("Sent %d new headlines", len(fresh))
    return 0


def run_plan(queue_path: Path, state_path: Path, all_days: bool) -> int:
    queue = json.loads(queue_path.read_text())
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    tomorrow = datetime.now(LOCAL_TZ).date() + timedelta(days=1)
    days = sorted({datetime.fromisoformat(q["at_utc"]).astimezone(LOCAL_TZ).date() for q in queue}) if all_days else [tomorrow]
    bot = _bot()
    for day in days or [tomorrow]:
        for part in chunk_messages([format_plan(queue, state, day)]):
            bot.send(part)
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv[:1] == ["news"]:
        return run_news(Path(argv[1]))
    if argv[:1] == ["plan"]:
        return run_plan(Path(argv[1]), Path(argv[2]), "--all" in argv)
    log.error(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
