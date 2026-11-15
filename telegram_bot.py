"""Telegram bot for ҮЙ: approval buttons and publish reports.

No server needed: the cloud runner polls getUpdates on each run.
Find your chat id after pressing Start in the bot:
  python3 telegram_bot.py --chat-id
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

HTTP_TIMEOUT_SECONDS = 30
MAX_CAPTION_PREVIEW = 900  # Telegram photo caption limit is 1024

log = logging.getLogger("telegram_bot")

DECISIONS = {"approve": "approved", "reject": "rejected"}


class TelegramError(Exception):
    """Raised when the Telegram API call fails."""


def approval_keyboard(item_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"approve:{item_id}"},
        {"text": "❌ Пропустить", "callback_data": f"reject:{item_id}"},
    ]]}


def apply_callbacks(state: dict, updates: list[dict], allowed_chat_id: int) -> tuple[dict, list[dict]]:
    """Return (new_state, acks). Only button presses from the owner's chat count."""
    new_state = dict(state)
    offset = state.get("_tg", {}).get("offset", 0)
    acks = []
    for update in updates:
        offset = max(offset, update["update_id"] + 1)
        query = update.get("callback_query")
        if not query or query.get("message", {}).get("chat", {}).get("id") != allowed_chat_id:
            continue
        action, _, item_id = query.get("data", "").partition(":")
        decision = DECISIONS.get(action)
        if not decision or "media_id" in state.get(item_id, {}):
            continue
        new_state[item_id] = {**new_state.get(item_id, {}), "approval": decision}
        acks.append({"callback_id": query["id"], "item_id": item_id, "decision": decision,
                     "message_id": query["message"]["message_id"]})
    new_state["_tg"] = {**state.get("_tg", {}), "offset": offset}
    return new_state, acks


def items_to_ask(queue: list[dict], state: dict) -> list[dict]:
    return [q for q in queue if q.get("needs_approval") and not state.get(q["id"], {}).get("asked")]


class TelegramClient:
    def __init__(self, token: str, chat_id: int | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def call(self, method: str, params: dict) -> dict:
        request = urllib.request.Request(f"{self._base}/{method}", data=json.dumps(params).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            raise TelegramError(f"{method}: HTTP {err.code} {err.read().decode(errors='replace')[:200]}") from err
        except urllib.error.URLError as err:
            raise TelegramError(f"{method}: network error {err.reason}") from err
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body["result"]

    def send(self, text: str) -> None:
        self.call("sendMessage", {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})

    def ask_approval(self, item: dict, when_local: str) -> None:
        post = item["post"]
        caption = f"🕒 {when_local} — на одобрение\n\n{post['caption'][:MAX_CAPTION_PREVIEW]}"
        self.call("sendPhoto", {"chat_id": self.chat_id, "photo": post["media_urls"][0], "caption": caption,
                                "reply_markup": approval_keyboard(item["id"])})

    def updates(self, offset: int) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]})

    def acknowledge(self, ack: dict) -> None:
        label = "✅ Одобрено — выйдет по расписанию" if ack["decision"] == "approved" else "❌ Пропущено"
        self.call("answerCallbackQuery", {"callback_query_id": ack["callback_id"], "text": label})
        self.call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": ack["message_id"],
                                             "reply_markup": {"inline_keyboard": [[
                                                 {"text": label, "callback_data": "noop"}]]}})


def main(argv: list[str]) -> int:
    from ig_publish import load_env

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = load_env(Path(__file__).with_name(".env"))
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1
    if argv[:1] == ["--chat-id"]:
        updates = TelegramClient(token).call("getUpdates", {"timeout": 0})
        chats = {u["message"]["chat"]["id"] for u in updates if "message" in u}
        if not chats:
            log.error("No messages yet: open the bot in Telegram and press Start, then retry")
            return 1
        print("\n".join(str(c) for c in chats))
        return 0
    log.error("Usage: python3 telegram_bot.py --chat-id")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
