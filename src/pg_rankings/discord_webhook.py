from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import certifi

from .history import RankingHistory, ranking_period_start
from .models import RankingSnapshot, history_pilot_key
from .ranking_card import render_ranking_card, render_weekly_activity_card

LOGGER = logging.getLogger(__name__)
RESET_TIMEZONE = ZoneInfo("Europe/Berlin")
STATE_VERSION = 3
RANKING_MESSAGE_KEY = "ranking"
ACTIVITY_MESSAGE_KEY = "activity"
CARD_FILENAME = "ranking.png"
ACTIVITY_CARD_FILENAME = "ranking-activity.png"


class DiscordError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _validate_webhook_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    if parsed.scheme != "https" or parsed.hostname not in {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }:
        raise DiscordError("Discord webhook URL has an unexpected origin")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "api" or parts[1] != "webhooks":
        raise DiscordError("Discord webhook URL has an unexpected path")
    return value.rstrip("/")


def next_evaluation_at(reference: datetime) -> datetime:
    if reference.tzinfo is None:
        raise ValueError("evaluation reference must include a timezone")
    local = reference.astimezone(RESET_TIMEZONE)
    days_until_monday = (-local.weekday()) % 7
    candidate_date = (local + timedelta(days=days_until_monday)).date()
    candidate = datetime.combine(candidate_date, time.min, tzinfo=RESET_TIMEZONE)
    if candidate <= local:
        candidate = datetime.combine(
            candidate_date + timedelta(days=7), time.min, tzinfo=RESET_TIMEZONE
        )
    return candidate


def stabilize_snapshot_names(
    snapshot: RankingSnapshot,
    previous: RankingSnapshot | None,
) -> RankingSnapshot:
    if previous is None or snapshot.captured_at < previous.captured_at:
        return snapshot
    if ranking_period_start(snapshot.captured_at) != ranking_period_start(previous.captured_at):
        return snapshot

    previous_counts = Counter(history_pilot_key(entry.name) for entry in previous.entries)
    current_counts = Counter(history_pilot_key(entry.name) for entry in snapshot.entries)
    previous_by_key = {history_pilot_key(entry.name): entry for entry in previous.entries}
    entries = tuple(
        replace(entry, name=previous_by_key[key].name)
        if previous_counts[key] == 1
        and current_counts[key] == 1
        and key in previous_by_key
        and entry.score >= previous_by_key[key].score
        else entry
        for entry in snapshot.entries
        for key in (history_pilot_key(entry.name),)
    )
    return replace(snapshot, entries=entries)


def build_payload(
    snapshot: RankingSnapshot,
    *,
    username: str,
    reward_slots: int,
    display_top_n: int,
    stale_reason: str | None = None,
) -> dict[str, Any]:
    display_count = min(display_top_n, len(snapshot.entries))
    color = 0xC0392B if stale_reason else 0xD4AF37
    description = (
        f"{snapshot.server} Division {snapshot.division}: "
        f"Pilot-Rangliste Top {display_count}, "
        f"Goldene Schädel für Plätze 1 bis {reward_slots}."
    )
    return {
        "username": username,
        "allowed_mentions": {"parse": []},
        "attachments": [
            {
                "id": 0,
                "filename": CARD_FILENAME,
                "description": description,
            }
        ],
        "embeds": [
            {
                "color": color,
                "image": {"url": f"attachment://{CARD_FILENAME}"},
            }
        ],
    }


def build_activity_payload(
    snapshot: RankingSnapshot,
    *,
    username: str,
    display_top_n: int,
    stale_reason: str | None = None,
) -> dict[str, Any]:
    display_count = min(display_top_n, len(snapshot.entries))
    color = 0xC0392B if stale_reason else 0xD4AF37
    description = (
        f"{snapshot.server} Division {snapshot.division}: "
        f"KW-Aktivität der aktuellen Wertungswoche für die Top {display_count}. "
        "Die Stundenfelder werden aus validierten Fünf-Minuten-Snapshots abgeleitet."
    )
    return {
        "username": username,
        "allowed_mentions": {"parse": []},
        "attachments": [
            {
                "id": 0,
                "filename": ACTIVITY_CARD_FILENAME,
                "description": description,
            }
        ],
        "embeds": [
            {
                "color": color,
                "image": {"url": f"attachment://{ACTIVITY_CARD_FILENAME}"},
            }
        ],
    }


def _multipart_body(
    payload: dict[str, Any],
    attachment: bytes,
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    boundary = boundary or f"PirateGalaxyRankings-{secrets.token_hex(16)}"
    try:
        filename = str(payload["attachments"][0]["filename"])
    except (KeyError, IndexError, TypeError):
        filename = CARD_FILENAME
    if Path(filename).name != filename or not filename.isascii():
        raise DiscordError("Discord attachment filename is invalid")
    payload_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body = bytearray()

    def line(value: str) -> None:
        body.extend(value.encode("ascii"))
        body.extend(b"\r\n")

    line(f"--{boundary}")
    line('Content-Disposition: form-data; name="payload_json"')
    line("Content-Type: application/json; charset=utf-8")
    line("")
    body.extend(payload_json)
    body.extend(b"\r\n")
    line(f"--{boundary}")
    line(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"')
    line("Content-Type: image/png")
    line("")
    body.extend(attachment)
    body.extend(b"\r\n")
    line(f"--{boundary}--")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


@dataclass(slots=True)
class MessagePublisherState:
    message_id: str | None = None
    stale_published: bool = False


@dataclass(slots=True)
class TargetPublisherState:
    messages: dict[str, MessagePublisherState] = field(default_factory=dict)


@dataclass(slots=True)
class PublisherState:
    last_snapshot: dict[str, Any] | None
    targets: dict[str, TargetPublisherState]


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    key: str
    payload: dict[str, Any]
    attachment: bytes


class DiscordPublisher:
    def __init__(
        self,
        *,
        webhook_urls: tuple[tuple[str, str], ...],
        state_path: Path,
        history_path: Path,
        username: str,
        display_top_n: int,
        reward_slots: int,
        timeout_seconds: float = 20,
    ) -> None:
        if not webhook_urls:
            raise DiscordError("at least one Discord webhook target is required")
        self.webhook_urls = tuple(
            (target_id, _validate_webhook_url(webhook_url))
            for target_id, webhook_url in webhook_urls
        )
        self.state_path = state_path
        self.history = RankingHistory(history_path)
        self.username = username
        self.display_top_n = display_top_n
        self.reward_slots = reward_slots
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.state = self._load_state()

    def _load_state(self) -> PublisherState:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PublisherState(last_snapshot=None, targets={})
        except (OSError, json.JSONDecodeError) as error:
            raise DiscordError(f"cannot read publisher state: {error}") from error
        if raw.get("version") == STATE_VERSION and isinstance(raw.get("targets"), dict):
            targets = {
                str(target_id): TargetPublisherState(
                    messages={
                        str(message_key): MessagePublisherState(
                            message_id=(
                                str(message_state["message_id"])
                                if message_state.get("message_id")
                                else None
                            ),
                            stale_published=bool(message_state.get("stale_published", False)),
                        )
                        for message_key, message_state in target_state.get("messages", {}).items()
                        if isinstance(message_state, dict)
                    }
                )
                for target_id, target_state in raw["targets"].items()
                if isinstance(target_state, dict)
            }
            return PublisherState(last_snapshot=raw.get("last_snapshot"), targets=targets)

        if raw.get("version") == 2 and isinstance(raw.get("targets"), dict):
            targets = {
                str(target_id): TargetPublisherState(
                    messages={
                        RANKING_MESSAGE_KEY: MessagePublisherState(
                            message_id=(
                                str(target_state["message_id"])
                                if target_state.get("message_id")
                                else None
                            ),
                            stale_published=bool(target_state.get("stale_published", False)),
                        )
                    }
                )
                for target_id, target_state in raw["targets"].items()
                if isinstance(target_state, dict)
            }
            return PublisherState(last_snapshot=raw.get("last_snapshot"), targets=targets)

        primary_id = self.webhook_urls[0][0]
        legacy_target = TargetPublisherState(
            messages={
                RANKING_MESSAGE_KEY: MessagePublisherState(
                    message_id=str(raw["message_id"]) if raw.get("message_id") else None,
                    stale_published=bool(raw.get("stale_published", False)),
                )
            }
        )
        return PublisherState(
            last_snapshot=raw.get("last_snapshot"),
            targets={primary_id: legacy_target},
        )

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "version": STATE_VERSION,
                "last_snapshot": self.state.last_snapshot,
                "targets": {
                    target_id: {
                        "messages": {
                            message_key: {
                                "message_id": message.message_id,
                                "stale_published": message.stale_published,
                            }
                            for message_key, message in target.messages.items()
                        }
                    }
                    for target_id, target in self.state.targets.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_path.parent, prefix=".state-", suffix=".json"
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            with suppress(OSError):
                os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.state_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        attachment: bytes | None = None,
    ) -> dict[str, Any]:
        if attachment is None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        else:
            body, content_type = _multipart_body(payload, attachment)
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": content_type,
                "User-Agent": "PirateGalaxyRankings/0.1",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            error.read()
            raise DiscordError(
                f"Discord webhook returned HTTP {error.code}", status=error.code
            ) from error
        except urllib.error.URLError as error:
            raise DiscordError(f"Discord webhook request failed: {error.reason}") from error
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise DiscordError("Discord returned invalid JSON") from error

    def _create(self, webhook_url: str, payload: dict[str, Any], attachment: bytes) -> str:
        separator = "&" if "?" in webhook_url else "?"
        response = self._request(
            "POST",
            f"{webhook_url}{separator}wait=true",
            payload,
            attachment,
        )
        message_id = str(response.get("id", ""))
        if not message_id.isdigit():
            raise DiscordError("Discord did not return a message ID")
        return message_id

    def _edit(
        self,
        webhook_url: str,
        message_id: str,
        payload: dict[str, Any],
        attachment: bytes,
    ) -> None:
        self._request(
            "PATCH",
            f"{webhook_url}/messages/{message_id}",
            payload,
            attachment,
        )

    def _publish_message(
        self,
        webhook_url: str,
        state: MessagePublisherState,
        payload: dict[str, Any],
        attachment: bytes,
    ) -> str:
        if state.message_id:
            try:
                self._edit(webhook_url, state.message_id, payload, attachment)
            except DiscordError as error:
                if error.status != 404:
                    raise
                state.message_id = self._create(webhook_url, payload, attachment)
        else:
            state.message_id = self._create(webhook_url, payload, attachment)
        return state.message_id

    def _publish_messages(
        self,
        rendered_messages: tuple[RenderedMessage, ...],
        *,
        stale: bool,
        skip_stale_or_missing: bool = False,
    ) -> dict[str, str]:
        message_ids: dict[str, str] = {}
        errors: dict[str, DiscordError] = {}
        attempted = 0
        for target_id, webhook_url in self.webhook_urls:
            target_state = self.state.targets.setdefault(target_id, TargetPublisherState())
            for rendered in rendered_messages:
                message_state = target_state.messages.setdefault(
                    rendered.key, MessagePublisherState()
                )
                if skip_stale_or_missing and (
                    message_state.stale_published or message_state.message_id is None
                ):
                    continue
                attempted += 1
                result_key = f"{target_id}/{rendered.key}"
                try:
                    message_ids[result_key] = self._publish_message(
                        webhook_url,
                        message_state,
                        rendered.payload,
                        rendered.attachment,
                    )
                    message_state.stale_published = stale
                except DiscordError as error:
                    errors[result_key] = error
                    LOGGER.error(
                        "Discord target %s message %s could not be updated: %s",
                        target_id,
                        rendered.key,
                        error,
                    )
        self._save_state()
        if attempted and not message_ids:
            failed = ", ".join(sorted(errors))
            raise DiscordError(f"all attempted Discord targets failed: {failed}")
        return message_ids

    def publish(self, snapshot: RankingSnapshot) -> dict[str, str]:
        rendered_at = datetime.now(UTC)
        previous = (
            snapshot_from_dict(self.state.last_snapshot)
            if self.state.last_snapshot is not None
            else None
        )
        snapshot = stabilize_snapshot_names(snapshot, previous)
        history_updated = False
        try:
            deltas = self.history.record_and_deltas(snapshot)
        except (OSError, sqlite3.Error) as error:
            LOGGER.error("ranking history could not be updated: %s", error)
            deltas = {}
        else:
            history_updated = True
        payload = build_payload(
            snapshot,
            username=self.username,
            reward_slots=self.reward_slots,
            display_top_n=self.display_top_n,
        )
        attachment = render_ranking_card(
            snapshot,
            reward_slots=self.reward_slots,
            display_top_n=self.display_top_n,
            evaluation_at=next_evaluation_at(rendered_at),
            score_deltas=deltas,
        )
        rendered_messages = [
            RenderedMessage(RANKING_MESSAGE_KEY, payload, attachment),
        ]
        if history_updated:
            try:
                weekly_activity = self.history.weekly_activity(
                    snapshot,
                    reward_slots=self.reward_slots,
                )
                activity_attachment = render_weekly_activity_card(
                    snapshot,
                    weekly_activity,
                    display_top_n=self.display_top_n,
                    reward_slots=self.reward_slots,
                    rendered_at=rendered_at,
                )
            except (OSError, sqlite3.Error, ValueError) as error:
                LOGGER.error("weekly activity card could not be rendered: %s", error)
            else:
                rendered_messages.append(
                    RenderedMessage(
                        ACTIVITY_MESSAGE_KEY,
                        build_activity_payload(
                            snapshot,
                            username=self.username,
                            display_top_n=self.display_top_n,
                        ),
                        activity_attachment,
                    )
                )
        self.state.last_snapshot = snapshot_to_dict(snapshot)
        return self._publish_messages(tuple(rendered_messages), stale=False)

    def publish_stale(self, snapshot: RankingSnapshot, *, reason: str) -> dict[str, str]:
        rendered_at = datetime.now(UTC)
        try:
            deltas = self.history.deltas(snapshot)
        except (OSError, sqlite3.Error) as error:
            LOGGER.error("ranking history could not be read: %s", error)
            deltas = {}
        payload = build_payload(
            snapshot,
            username=self.username,
            reward_slots=self.reward_slots,
            display_top_n=self.display_top_n,
            stale_reason=reason,
        )
        attachment = render_ranking_card(
            snapshot,
            reward_slots=self.reward_slots,
            display_top_n=self.display_top_n,
            evaluation_at=next_evaluation_at(rendered_at),
            score_deltas=deltas,
            stale_reason=reason,
        )
        rendered_messages = [
            RenderedMessage(RANKING_MESSAGE_KEY, payload, attachment),
        ]
        try:
            weekly_activity = self.history.weekly_activity(
                snapshot,
                reward_slots=self.reward_slots,
            )
            activity_attachment = render_weekly_activity_card(
                snapshot,
                weekly_activity,
                display_top_n=self.display_top_n,
                reward_slots=self.reward_slots,
                rendered_at=rendered_at,
                stale_reason=reason,
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            LOGGER.error("stale weekly activity card could not be rendered: %s", error)
        else:
            rendered_messages.append(
                RenderedMessage(
                    ACTIVITY_MESSAGE_KEY,
                    build_activity_payload(
                        snapshot,
                        username=self.username,
                        display_top_n=self.display_top_n,
                        stale_reason=reason,
                    ),
                    activity_attachment,
                )
            )
        return self._publish_messages(
            tuple(rendered_messages),
            stale=True,
            skip_stale_or_missing=True,
        )


def snapshot_to_dict(snapshot: RankingSnapshot) -> dict[str, Any]:
    return {
        "server": snapshot.server,
        "division": snapshot.division,
        "captured_at": snapshot.captured_at.isoformat(),
        "anchor_confidence": snapshot.anchor_confidence,
        "entries": [
            {
                "rank": entry.rank,
                "name": entry.name,
                "score": entry.score,
                "confidence": entry.confidence,
            }
            for entry in snapshot.entries
        ],
    }


def snapshot_from_dict(raw: dict[str, Any]) -> RankingSnapshot:
    from .models import RankingEntry

    captured_at = datetime.fromisoformat(str(raw["captured_at"]))
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    return RankingSnapshot(
        server=str(raw["server"]),
        division=int(raw["division"]),
        captured_at=captured_at,
        anchor_confidence=float(raw["anchor_confidence"]),
        entries=tuple(
            RankingEntry(
                rank=int(entry["rank"]),
                name=str(entry["name"]),
                score=int(entry["score"]),
                confidence=float(entry["confidence"]),
            )
            for entry in raw["entries"]
        ),
    )
