#!/usr/bin/env python3
"""Send weekly bin notices without receiving updates or changing bot settings.

Commands: run, preview, status, verify. Only run can publish weekly notices.
Run accepts only Monday at or after noon in Europe/London. There is no initial,
reset, force-send, or resend command. An uncertain delivery needs manual review.

BINS_STATE_PATH defaults to /data/state.json.
BINS_CACHE_PATH defaults to /data/council-cache.json.
BINS_PROPERTY_PATH defaults to /etc/bins/property.json.
TELEGRAM_BOT_TOKEN_FILE defaults to /var/run/acorn/token.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import date, datetime, timedelta, timezone
import fcntl
import html
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from council import (CouncilError, HOLIDAY_URL, LONDON, fetch_schedule,
                     load_property, property_identity)
from storage import atomic_json, strict_json

CHAT_ID = -1004424243504
TOPIC_ID = 45
SENDER = "acorn-care-bot"
DESTINATION = f"{CHAT_ID}:{TOPIC_ID}"
FINDER_URL = "https://www.royalgreenwich.gov.uk/info/200171/recycling_and_rubbish/100/bin_collection_days"


class BotError(RuntimeError):
    """A fixed public-safe error message."""


class TelegramError(BotError):
    def __init__(self, text: str, *, uncertain: bool = False):
        super().__init__(text)
        self.uncertain = uncertain


def _positive(value) -> bool:
    return type(value) is int and value > 0


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BotError("Telegram redirects are not accepted.")


class Telegram:
    METHODS = frozenset({"getMe", "getChatMember", "sendMessage", "sendRichMessage",
                         "pinChatMessage", "unpinChatMessage", "deleteMessage"})
    _KEYS = {
        "getMe": set(), "getChatMember": {"chat_id", "user_id"},
        "sendMessage": {"chat_id", "message_thread_id", "text", "parse_mode", "link_preview_options"},
        "sendRichMessage": {"chat_id", "message_thread_id", "rich_message"},
        "pinChatMessage": {"chat_id", "message_id", "disable_notification"},
        "unpinChatMessage": {"chat_id", "message_id"},
        "deleteMessage": {"chat_id", "message_id"},
    }

    def __init__(self):
        self._gateway = None
        if "TELEGRAM_GATEWAY_SOCKET" in os.environ:
            from gateway_client import UnixClient
            socket_path = os.environ["TELEGRAM_GATEWAY_SOCKET"]
            if not socket_path or not os.path.isabs(socket_path):
                raise BotError("An absolute Telegram gateway socket is required.")
            self._gateway = UnixClient(socket_path)
            me = self.call("getMe", {})
            if not isinstance(me, dict) or not _positive(me.get("id")) or me.get("is_bot") is not True:
                raise BotError("The gateway returned an invalid bot identity.")
            self._bot_id = me["id"]
            return
        path = Path(os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "/var/run/acorn/token"))
        try:
            with path.open("r", encoding="utf-8") as stream:
                raw_token = stream.read(513)
                token = raw_token.strip()
        except Exception:
            raise BotError("The mounted Telegram token file is not readable.") from None
        if len(raw_token) > 512 or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise BotError("The mounted Telegram token has an invalid format.")
        self._token = token
        self._bot_id = int(token.split(":", 1)[0])

    def __repr__(self):
        return "Telegram(<mounted token>)"

    def call(self, method: str, payload: dict):
        if not isinstance(method, str) or method not in self.METHODS:
            raise BotError("This Telegram method is not allowed.")
        if not isinstance(payload, dict) or set(payload) - self._KEYS[method]:
            raise BotError("This Telegram payload is not allowed.")
        if method != "getMe" and (type(payload.get("chat_id")) is not int
                                   or payload["chat_id"] != CHAT_ID):
            raise BotError("This Telegram destination is not allowed.")
        if method in {"sendMessage", "sendRichMessage"}:
            if type(payload.get("message_thread_id")) is not int or payload["message_thread_id"] != TOPIC_ID:
                raise BotError("Messages must stay in the bin topic.")
            if method == "sendMessage" and (payload.get("parse_mode") != "HTML"
                                             or not isinstance(payload.get("text"), str)):
                raise BotError("Alert messages must use HTML text.")
            if method == "sendRichMessage" and (not isinstance(payload.get("rich_message"), dict)
                    or set(payload["rich_message"]) != {"html"}
                    or not isinstance(payload["rich_message"]["html"], str)):
                raise BotError("Bin notices must use rich HTML.")
        if method in {"pinChatMessage", "unpinChatMessage", "deleteMessage"} and not _positive(payload.get("message_id")):
            raise BotError("A recorded message identifier is required for pin changes or cleanup.")
        if method == "getChatMember" and not _positive(payload.get("user_id")):
            raise BotError("A bot identifier is required for permission checks.")
        if self._gateway is not None:
            from telegram_api import TelegramError as GatewayError
            try:
                return self._gateway.call(method, payload)
            except GatewayError as error:
                raise TelegramError("The gateway did not confirm the request. Automatic send retries are blocked.",
                                    uncertain=error.uncertain) from None
        request = Request("https://api.telegram.org/bot" + self._token + "/" + method,
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=30) as response:
                envelope = strict_json(response.read(1_000_001).decode("utf-8"))
        except HTTPError as error:
            try:
                envelope = strict_json(error.read(1_000_001).decode("utf-8"))
            except Exception:
                raise TelegramError("Telegram did not confirm the request. Automatic send retries are blocked.",
                                    uncertain=True) from None
            if not isinstance(envelope, dict) or envelope.get("ok") is not False:
                raise TelegramError("Telegram did not confirm the request. Automatic send retries are blocked.",
                                    uncertain=True) from None
        except Exception:
            raise TelegramError("Telegram did not confirm the request. Automatic send retries are blocked.",
                                uncertain=True) from None
        if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
            raise TelegramError("Telegram returned an invalid response. Do not resend automatically.", uncertain=True)
        if not envelope["ok"]:
            # Never include Telegram's description, request URL, or exception text.
            raise TelegramError("Telegram rejected the request. Automatic send retries are blocked.")
        if "result" not in envelope:
            raise TelegramError("Telegram omitted the result. Do not resend automatically.", uncertain=True)
        return envelope["result"]

    def verify(self):
        me = self.call("getMe", {})
        if (not isinstance(me, dict) or not _positive(me.get("id"))
                or me["id"] != self._bot_id or me.get("is_bot") is not True):
            raise BotError("The sender does not match the required bot.")
        member = self.call("getChatMember", {"chat_id": CHAT_ID, "user_id": me["id"]})
        user = member.get("user") if isinstance(member, dict) else None
        if (not isinstance(user, dict) or user.get("id") != me["id"]
                or not (member.get("status") == "creator" or (
                    member.get("status") == "administrator" and member.get("can_pin_messages") is True))):
            raise BotError("The required bot needs permission to pin messages in the configured chat.")
        result = {"can_pin_messages": True,
                  "can_delete_messages": member.get("can_delete_messages") is True}
        username = me.get("username")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            result["username"] = username
        return result


def week_key(now: datetime) -> str:
    local = now.astimezone(LONDON)
    return (local.date() - timedelta(days=local.weekday())).isoformat()


def _identity(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_state(state: dict, identity: str | None, bot_id: int | None = None) -> None:
    try:
        if (not isinstance(state, dict) or state["version"] != 2 or state["destination"] != DESTINATION
                or state["sender"] != SENDER or not _positive(state["bot_id"])
                or (bot_id is not None and state["bot_id"] != bot_id)
                or not _identity(state["property_identity"])
                or (identity is not None and state["property_identity"] != identity)
                or not isinstance(state["publications"], dict) or not isinstance(state["alerts"], dict)):
            raise ValueError
        ids = set()
        for key, entry in state["publications"].items():
            if (date.fromisoformat(key).isoformat() != key or date.fromisoformat(key).weekday() != 0
                    or not isinstance(entry, dict) or entry["target_start"] != key
                    or entry["status"] not in {"sending", "needs_review", "sent", "complete", "superseded"}):
                raise ValueError
            if "message_id" in entry:
                if (not _positive(entry["message_id"]) or not _positive(entry["date"])
                        or entry["message_id"] in ids or entry["status"] not in {"sent", "complete", "superseded"}):
                    raise ValueError
                ids.add(entry["message_id"])
            elif entry["status"] not in {"sending", "needs_review"}:
                raise ValueError
            if entry["status"] == "complete" and entry.get("pinned") is not True:
                raise ValueError
        for key, entry in state["alerts"].items():
            if (date.fromisoformat(key).isoformat() != key or date.fromisoformat(key).weekday() != 0
                    or not isinstance(entry, dict) or entry["status"] not in {"sending", "needs_review", "sent"}):
                raise ValueError
            if entry["status"] == "sent":
                if not _positive(entry.get("message_id")) or entry["message_id"] in ids:
                    raise ValueError
                ids.add(entry["message_id"])
            elif "message_id" in entry:
                raise ValueError
        if state["current"] is not None and not _own_record(state, state["current"]):
            raise ValueError
        for entry in state["publications"].values():
            if entry.get("previous") is not None and not _own_record(state, entry["previous"]):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BotError("The saved state is invalid or belongs to another destination, bot, or property. No message was changed.") from None


def _own_record(state: dict, reference: dict) -> bool:
    if not isinstance(reference, dict):
        return False
    record = state["publications"].get(reference.get("target_start"))
    return (isinstance(record, dict) and record.get("status") == "complete"
            and _positive(reference.get("message_id"))
            and record.get("message_id") == reference["message_id"]
            and record.get("date") == reference.get("date"))


@contextlib.contextmanager
def locked_state(path: Path, identity: str | None = None, bot_id: int | None = None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    state = strict_json(path.read_text(encoding="utf-8"))
                except Exception:
                    raise BotError("The saved state cannot be read. No message was changed.") from None
                _validate_state(state, identity, bot_id)
            else:
                state = {"version": 2, "destination": DESTINATION, "sender": SENDER,
                         "bot_id": bot_id, "property_identity": identity, "publications": {}, "alerts": {}, "current": None}
            yield state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def date_label(value: str) -> str:
    value_date = date.fromisoformat(value)
    return f"{value_date:%A} {value_date.day} {value_date:%B %Y}"


def render_rich(schedule: dict) -> str:
    """Use native headings and lists. Keep confirmed dates and warnings."""
    start = date.fromisoformat(schedule["target_start"])
    end = date.fromisoformat(schedule["target_end"])
    if start.year != end.year:
        period = f"{start.day} {start:%B %Y} – {end.day} {end:%B %Y}"
    elif start.month != end.month:
        period = f"{start.day} {start:%B} – {end.day} {end:%B %Y}"
    else:
        period = f"{start.day}–{end.day} {end:%B %Y}"
    lines = ["<h1>🗓️ Bins next week</h1>", f"<p>{period}</p>"]
    labels = {"recycling": "♻️ Recycling · blue-top bin", "food": "🥬 Food waste · green-top bin",
              "general": "🗑️ General waste · black-top bin"}
    groups, unknown = {}, []
    for item in schedule["collections"]:
        if item.get("date"):
            groups.setdefault(item["date"], []).append(item)
        else:
            unknown.append(item)
    for day, items in sorted(groups.items()):
        lines.extend([f"<h2>🚛 {date_label(day)}</h2>", "<ul>"])
        for item in items:
            body = labels[item["kind"]]
            if day != item["usual_date"]:
                body += f". Changed from {date_label(item['usual_date'])}."
            lines.append(f"<li>{body}</li>")
        lines.append("</ul>")
    if unknown:
        lines.extend(["<h2>⚠️ Collection date not confirmed</h2>", "<ul>"])
        for item in unknown:
            body = labels[item["kind"]] + f". Usual date: {date_label(item['usual_date'])}."
            if item.get("note"):
                body += " " + html.escape(item["note"])
            lines.append(f"<li>{body}</li>")
        lines.append("</ul>")
    if not schedule["general_this_week"]:
        lines.append("<p>🗑️ No general waste collection next week.</p>")
        next_date = schedule.get("general_next_date")
        lines.append(f"<p>Next black-top collection: {date_label(next_date)}.</p>" if next_date
                     else "<p>The next black-top date is not confirmed.</p>")
    for warning in schedule.get("warnings", []):
        lines.append("<p>⚠️ " + html.escape(warning) + "</p>")
    if schedule["collections"] and not unknown:
        lines.extend(["<p>⏰ Put your bins out by 6 am on collection day.</p>",
                      "<p>If you use bin bags, put them out that morning, not the night before.</p>"])
    fetched = datetime.fromisoformat(schedule["fetched_at"]).astimezone(LONDON)
    lines.extend([f'<p><a href="{FINDER_URL}">Greenwich collection checker</a> · '
                  f'<a href="{HOLIDAY_URL}">Holiday changes</a></p>',
                  f"<footer>Checked {fetched.day} {fetched:%B %Y at %H:%M %Z}.</footer>"])
    return "\n".join(lines)


def check_receipt(result, *, rich=False):
    if (not isinstance(result, dict) or not isinstance(result.get("chat"), dict)
            or type(result["chat"].get("id")) is not int or result["chat"]["id"] != CHAT_ID
            or type(result.get("message_thread_id")) is not int or result["message_thread_id"] != TOPIC_ID
            or not _positive(result.get("message_id")) or not _positive(result.get("date"))
            or (rich and not isinstance(result.get("rich_message"), dict))):
        raise TelegramError("Telegram did not confirm the bin topic and message format. Do not resend automatically.", uncertain=True)


def send_notice(client, text: str, *, rich=False):
    payload = {"chat_id": CHAT_ID, "message_thread_id": TOPIC_ID}
    if rich:
        payload["rich_message"] = {"html": text}
    else:
        payload.update({"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
    result = client.call("sendRichMessage" if rich else "sendMessage", payload)
    check_receipt(result, rich=rich)
    return result


def _blocked(state: dict) -> None:
    if any(not entry.get("message_id") for entry in state["publications"].values()):
        raise BotError("A previous delivery result is unknown. Review the topic and state manually. Automatic sends are blocked.")


def cleanup_previous(client, state: dict, entry: dict, path: Path, now: datetime) -> None:
    """Retry recorded cleanup without blocking a later week."""
    previous = entry.get("previous")
    if not previous or previous["message_id"] == entry["message_id"] or entry.get("cleanup_done"):
        return
    if not _own_record(state, previous):
        raise BotError("Cleanup requires this sender's completed delivery record.")
    try:
        if not entry.get("previous_unpinned"):
            if client.call("unpinChatMessage", {"chat_id": CHAT_ID, "message_id": previous["message_id"]}) is not True:
                raise TelegramError("Telegram did not confirm the unpin operation.", uncertain=True)
            entry["previous_unpinned"] = True
            atomic_json(path, state)
        age = now.timestamp() - previous["date"]
        if 0 <= age < 48 * 60 * 60:
            try:
                if client.call("deleteMessage", {"chat_id": CHAT_ID, "message_id": previous["message_id"]}) is not True:
                    raise TelegramError("Telegram did not confirm message cleanup.", uncertain=True)
                entry["previous_deleted"] = True
            except TelegramError as error:
                if error.uncertain:
                    raise
                entry["cleanup_note"] = "Telegram rejected deletion. The recorded previous notice is unpinned."
        else:
            entry["cleanup_note"] = "The previous notice is unpinned. Deletion is limited to messages less than 48 hours old."
        entry["cleanup_done"] = True
        entry.pop("cleanup_pending", None)
    except TelegramError:
        entry["cleanup_pending"] = True
        entry["cleanup_note"] = "Telegram did not confirm previous-notice cleanup. The new notice is pinned. Cleanup needs review."
    atomic_json(path, state)


def publication_result(entry: dict, status: str) -> dict:
    result = {"status": status, "message_id": entry["message_id"], "pinned": entry.get("pinned") is True,
              "target_start": entry["target_start"]}
    if entry.get("cleanup_pending"):
        result.update(cleanup_pending=True, cleanup_note=entry["cleanup_note"])
    return result


def finish_publication(client, state: dict, entry: dict, path: Path, now: datetime) -> dict:
    message_id = entry["message_id"]
    if not entry.get("pinned"):
        if client.call("pinChatMessage", {"chat_id": CHAT_ID, "message_id": message_id,
                                          "disable_notification": True}) is not True:
            raise TelegramError("Telegram did not confirm the pin operation.", uncertain=True)
        entry["pinned"] = True
    # Persist the replacement pin before optional cleanup. Later weeks can proceed.
    if entry["status"] != "complete":
        entry["status"] = "complete"
        state["current"] = {"message_id": message_id, "date": entry["date"], "target_start": entry["target_start"]}
        atomic_json(path, state)
    cleanup_previous(client, state, entry, path, now)
    return publication_result(entry, "published")


def publish(client, schedule: dict, now: datetime, path: Path) -> dict:
    key = (date.fromisoformat(week_key(now)) + timedelta(days=7)).isoformat()
    if schedule.get("target_start") != key or not _identity(schedule.get("property_identity")):
        raise BotError("The schedule does not match the next collection week or a configured property.")
    text = render_rich(schedule)
    with locked_state(path, schedule["property_identity"], client._bot_id) as state:
        _blocked(state)
        entry = state["publications"].get(key)
        if entry:
            if entry["status"] in {"complete", "superseded"}:
                return publication_result(entry, "already_published")
            return finish_publication(client, state, entry, path, now)
        if any(item["status"] not in {"complete", "superseded"} for item in state["publications"].values()):
            raise BotError("Finish the recorded publication before creating another notice.")
        entry = {"status": "sending", "claimed_at": now.isoformat(), "target_start": key,
                 "format": "rich_html", "previous": state["current"]}
        state["publications"][key] = entry
        atomic_json(path, state)
        try:
            result = send_notice(client, text, rich=True)
            if any(record.get("message_id") == result["message_id"]
                   for record in [*state["publications"].values(), *state["alerts"].values()]):
                raise TelegramError("Telegram returned an already recorded message identifier. Do not resend automatically.",
                                    uncertain=True)
        except Exception:
            entry["status"] = "needs_review"
            atomic_json(path, state)
            raise
        entry.update({"status": "sent", "message_id": result["message_id"], "date": result["date"]})
        atomic_json(path, state)
        return finish_publication(client, state, entry, path, now)


def resume(client, now: datetime, path: Path, identity: str) -> dict | None:
    with locked_state(path, identity, client._bot_id) as state:
        _blocked(state)
        key = (date.fromisoformat(week_key(now)) + timedelta(days=7)).isoformat()
        recovered = None
        for target, entry in sorted(state["publications"].items()):
            if entry["status"] not in {"complete", "superseded"}:
                try:
                    result = finish_publication(client, state, entry, path, now)
                except TelegramError as error:
                    if target >= key or error.uncertain:
                        raise
                    # The send receipt is known. Never resend an obsolete notice.
                    # A definite rejected pin must not block a newer target week.
                    entry["status"] = "superseded"
                    entry["pin_note"] = "Telegram rejected the obsolete notice's pin. Its confirmed send record is retained."
                    atomic_json(path, state)
                    continue
                if target == key:
                    recovered = result
            elif entry["status"] == "complete":
                cleanup_previous(client, state, entry, path, now)
        if recovered:
            return recovered
        entry = state["publications"].get(key)
        if entry:
            return publication_result(entry, "already_published")
    # Recovering an older pin does not consume this week's scheduled publication.
    return None


def alert(client, now: datetime, path: Path, identity: str) -> bool:
    """Send one fixed alert per publication week. Never include exception text."""
    key = week_key(now)
    with locked_state(path, identity, client._bot_id) as state:
        _blocked(state)
        if key in state["alerts"]:
            return state["alerts"][key].get("status") == "sent"
        state["alerts"][key] = {"status": "sending", "at": now.isoformat()}
        atomic_json(path, state)
        try:
            result = send_notice(client, "⚠️ <b>The bin update needs attention</b>\n\n"
                                 "Council data could not be verified. No unverified collection dates were published. "
                                 "The last confirmed pin stays in place.\n\n"
                                 + f'<a href="{FINDER_URL}">Check Greenwich Council</a> · @LionZXY')
            if any(record.get("message_id") == result["message_id"]
                   for record in [*state["publications"].values(), *state["alerts"].values()]):
                raise TelegramError("Telegram returned an already recorded message identifier.", uncertain=True)
        except Exception:
            state["alerts"][key]["status"] = "needs_review"
            atomic_json(path, state)
            return False
        state["alerts"][key].update({"status": "sent", "message_id": result["message_id"]})
        atomic_json(path, state)
        return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "preview", "status", "verify"])
    parser.add_argument("--at", help="Preview only. Use an ISO timestamp with an explicit time zone.")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    path = Path(os.environ.get("BINS_STATE_PATH", "/data/state.json"))
    cache = Path(os.environ.get("BINS_CACHE_PATH", "/data/council-cache.json"))
    try:
        if args.at:
            if args.command != "preview":
                raise BotError("A simulated clock is allowed only for a preview.")
            try:
                now = datetime.fromisoformat(args.at)
            except ValueError:
                raise BotError("The preview timestamp is invalid.") from None
            if now.tzinfo is None or now.utcoffset() is None:
                raise BotError("The preview timestamp needs an explicit time zone.")
        if args.command == "status":
            with locked_state(path) as state:
                result = {"status": "ok", "destination": DESTINATION, "sender": SENDER,
                          "publications": {key: {name: entry[name] for name in ("status", "message_id", "pin_note", "cleanup_pending", "cleanup_note") if name in entry}
                                           for key, entry in state["publications"].items()},
                          "alerts": {key: entry["status"] for key, entry in state["alerts"].items()}}
        elif args.command == "verify":
            config = load_property()
            with locked_state(path, property_identity(config)):
                pass
            result = {"status": "verified", **Telegram().verify()}
        elif args.command == "run" and (now.astimezone(LONDON).weekday() != 0 or now.astimezone(LONDON).hour < 12):
            result = {"status": "not_due"}
        else:
            config = load_property()
            identity = property_identity(config)
            if args.command == "preview":
                # A preview neither reads a token nor changes the publication state or cache.
                schedule = fetch_schedule(now, config)
                result = {"status": "preview", "schedule": schedule, "rich_html": render_rich(schedule)}
            else:
                # Validate local state before any network request.
                with locked_state(path, identity) as state:
                    _blocked(state)
                client = Telegram()
                client.verify()
                result = resume(client, now, path, identity)
                if result is None:
                    try:
                        schedule = fetch_schedule(now, config, cache_path=cache, force=True)
                    except CouncilError:
                        notified = alert(client, now, path, identity)
                        print(json.dumps({"status": "error", "description": "Council data could not be verified.",
                                          "alert_sent": notified}))
                        return 1
                    result = publish(client, schedule, now, path)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        # Unknown exceptions, URLs, Telegram descriptions, and private paths stay out of output.
        description = (str(error) if isinstance(error, (BotError, CouncilError))
                       else "The bin operation failed. No automatic send retry was attempted.")
        print(json.dumps({"status": "error", "description": description}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
