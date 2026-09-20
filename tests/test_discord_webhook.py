import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pg_rankings.discord_webhook import (
    ACTIVITY_CARD_FILENAME,
    CARD_FILENAME,
    STATE_VERSION,
    DiscordError,
    DiscordPublisher,
    _multipart_body,
    build_activity_payload,
    build_payload,
    next_evaluation_at,
    stabilize_snapshot_names,
)
from pg_rankings.models import RankingEntry, RankingSnapshot


def snapshot() -> RankingSnapshot:
    return RankingSnapshot(
        server="Example Server",
        division=1,
        captured_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        anchor_confidence=91.0,
        entries=(
            RankingEntry(1, "Pilot.Alpha", 1_692_889, 90.0),
            RankingEntry(2, "Pilot Beta", 1_657_607, 88.0),
            RankingEntry(3, "Pilot Gamma", 1_595_715, 92.0),
            RankingEntry(4, "Pilot Delta", 1_420_592, 89.0),
            RankingEntry(5, "DJan", 1_291_935, 87.0),
            RankingEntry(6, "Pilot 6", 900_000, 87.0),
            RankingEntry(7, "Pilot 7", 800_000, 87.0),
            RankingEntry(8, "Pilot 8", 700_000, 87.0),
            RankingEntry(9, "Pilot 9", 600_000, 87.0),
            RankingEntry(10, "Pilot 10", 500_000, 87.0),
            RankingEntry(11, "Pilot 11", 400_000, 87.0),
        ),
    )


def test_payload_marks_four_reward_slots() -> None:
    payload = build_payload(snapshot(), username="Tracker", reward_slots=4, display_top_n=7)
    embed = payload["embeds"][0]
    assert embed == {
        "color": 0xD4AF37,
        "image": {"url": f"attachment://{CARD_FILENAME}"},
    }
    assert payload["attachments"] == [
        {
            "id": 0,
            "filename": CARD_FILENAME,
            "description": (
                "Example Server Division 1: Pilot-Rangliste Top 7, "
                "Goldene Schädel für Plätze 1 bis 4."
            ),
        }
    ]
    assert payload["allowed_mentions"] == {"parse": []}


def test_stale_payload_uses_compact_status_field() -> None:
    payload = build_payload(
        snapshot(),
        username="Tracker",
        reward_slots=4,
        display_top_n=7,
        stale_reason="OcrError",
    )

    embed = payload["embeds"][0]
    assert embed["color"] == 0xC0392B
    assert embed["image"]["url"] == f"attachment://{CARD_FILENAME}"


def test_activity_payload_uses_separate_attachment() -> None:
    payload = build_activity_payload(snapshot(), username="Tracker", display_top_n=7)

    assert payload["attachments"][0]["filename"] == ACTIVITY_CARD_FILENAME
    assert payload["embeds"][0]["image"]["url"] == (f"attachment://{ACTIVITY_CARD_FILENAME}")


def test_multipart_body_contains_payload_and_png() -> None:
    payload = build_payload(snapshot(), username="Tracker", reward_slots=4, display_top_n=7)
    png = b"\x89PNG\r\n\x1a\ncard"

    body, content_type = _multipart_body(payload, png, boundary="test-boundary")

    assert content_type == "multipart/form-data; boundary=test-boundary"
    assert b'name="payload_json"' in body
    assert b'name="files[0]"' in body
    assert f'filename="{CARD_FILENAME}"'.encode() in body
    assert json.dumps(payload, ensure_ascii=False).encode() in body
    assert png in body
    assert body.endswith(b"--test-boundary--\r\n")


def test_evaluation_rolls_to_following_week_at_monday_midnight() -> None:
    berlin = ZoneInfo("Europe/Berlin")

    assert next_evaluation_at(datetime(2026, 8, 30, 23, 59, tzinfo=berlin)) == datetime(
        2026, 8, 31, 0, 0, tzinfo=berlin
    )
    assert next_evaluation_at(datetime(2026, 8, 31, 0, 0, tzinfo=berlin)) == datetime(
        2026, 9, 7, 0, 0, tzinfo=berlin
    )


def test_evaluation_keeps_berlin_midnight_across_dst_change() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    result = next_evaluation_at(datetime(2026, 10, 23, 12, 0, tzinfo=UTC))

    assert result == datetime(2026, 10, 26, 0, 0, tzinfo=berlin)
    assert result.astimezone(UTC) == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)


def test_stabilizes_common_name_ocr_variant_within_ranking_week() -> None:
    previous = RankingSnapshot(
        server="Example Server",
        division=1,
        captured_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        anchor_confidence=90,
        entries=(RankingEntry(7, "Stellar PilotO1", 49_588, 85),),
    )
    current = RankingSnapshot(
        server="Example Server",
        division=1,
        captured_at=datetime(2026, 9, 1, 10, 5, tzinfo=UTC),
        anchor_confidence=90,
        entries=(RankingEntry(6, "Stellar Pilot01", 50_000, 85),),
    )

    stabilized = stabilize_snapshot_names(current, previous)

    assert stabilized.entries[0].name == "Stellar PilotO1"
    assert stabilized.entries[0].rank == 6
    assert stabilized.entries[0].score == 50_000


def test_publisher_creates_then_edits_two_persistent_messages(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    publisher = DiscordPublisher(
        webhook_urls=(("primary", "https://discord.com/api/webhooks/123/token"),),
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.sqlite3",
        username="Tracker",
        display_top_n=7,
        reward_slots=4,
    )

    def request(
        method: str,
        url: str,
        payload: object,
        attachment: bytes | None = None,
    ) -> dict[str, str]:
        calls.append((method, url))
        assert attachment is not None
        assert attachment.startswith(b"\x89PNG\r\n\x1a\n")
        filename = payload["attachments"][0]["filename"]  # type: ignore[index]
        if method != "POST":
            return {}
        return {"id": "999" if filename == CARD_FILENAME else "1000"}

    publisher._request = request  # type: ignore[method-assign]
    assert publisher.publish(snapshot()) == {
        "primary/ranking": "999",
        "primary/activity": "1000",
    }
    assert publisher.publish(snapshot()) == {
        "primary/ranking": "999",
        "primary/activity": "1000",
    }
    assert [method for method, _ in calls] == ["POST", "POST", "PATCH", "PATCH"]
    assert publisher.state_path.exists()


def test_publisher_fans_out_and_isolates_one_failed_target(tmp_path: Path) -> None:
    publisher = DiscordPublisher(
        webhook_urls=(
            ("primary", "https://discord.com/api/webhooks/123/primary"),
            ("clan", "https://discord.com/api/webhooks/456/clan"),
        ),
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.sqlite3",
        username="Tracker",
        display_top_n=7,
        reward_slots=4,
    )

    def request(
        method: str,
        url: str,
        payload: object,
        attachment: bytes | None = None,
    ) -> dict[str, str]:
        assert method == "POST"
        assert attachment is not None
        if "/456/" in url:
            raise DiscordError("revoked", status=401)
        filename = payload["attachments"][0]["filename"]  # type: ignore[index]
        return {"id": "111" if filename == CARD_FILENAME else "112"}

    publisher._request = request  # type: ignore[method-assign]

    assert publisher.publish(snapshot()) == {
        "primary/ranking": "111",
        "primary/activity": "112",
    }
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == STATE_VERSION
    assert state["targets"]["primary"]["messages"]["ranking"]["message_id"] == "111"
    assert state["targets"]["primary"]["messages"]["activity"]["message_id"] == "112"
    assert state["targets"]["clan"]["messages"]["ranking"]["message_id"] is None


def test_publisher_migrates_version_two_target_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "last_snapshot": None,
                "targets": {
                    "primary": {
                        "message_id": "777",
                        "stale_published": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    publisher = DiscordPublisher(
        webhook_urls=(("primary", "https://discord.com/api/webhooks/123/token"),),
        state_path=state_path,
        history_path=tmp_path / "history.sqlite3",
        username="Tracker",
        display_top_n=7,
        reward_slots=4,
    )
    calls: list[tuple[str, str]] = []

    def request(
        method: str,
        url: str,
        payload: object,
        attachment: bytes | None = None,
    ) -> dict[str, str]:
        calls.append((method, url))
        assert attachment is not None
        if method == "POST":
            assert payload["attachments"][0]["filename"] == ACTIVITY_CARD_FILENAME  # type: ignore[index]
            return {"id": "778"}
        return {}

    publisher._request = request  # type: ignore[method-assign]

    assert publisher.publish(snapshot()) == {
        "primary/ranking": "777",
        "primary/activity": "778",
    }
    assert calls[0][0] == "PATCH"
    migrated = json.loads(state_path.read_text(encoding="utf-8"))
    assert migrated["version"] == STATE_VERSION
    assert migrated["targets"]["primary"]["messages"]["ranking"]["message_id"] == "777"
    assert migrated["targets"]["primary"]["messages"]["activity"]["message_id"] == "778"


def test_publisher_raises_if_all_targets_fail(tmp_path: Path) -> None:
    publisher = DiscordPublisher(
        webhook_urls=(("primary", "https://discord.com/api/webhooks/123/token"),),
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.sqlite3",
        username="Tracker",
        display_top_n=7,
        reward_slots=4,
    )

    def request(
        _method: str,
        _url: str,
        _payload: object,
        attachment: bytes | None = None,
    ) -> dict[str, str]:
        assert attachment is not None
        raise DiscordError("revoked", status=401)

    publisher._request = request  # type: ignore[method-assign]

    with pytest.raises(DiscordError, match="all attempted Discord targets failed"):
        publisher.publish(snapshot())


def test_activity_render_failure_does_not_block_ranking_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = DiscordPublisher(
        webhook_urls=(("primary", "https://discord.com/api/webhooks/123/token"),),
        state_path=tmp_path / "state.json",
        history_path=tmp_path / "history.sqlite3",
        username="Tracker",
        display_top_n=7,
        reward_slots=4,
    )

    def fail_activity_render(*_args: object, **_kwargs: object) -> bytes:
        raise ValueError("invalid activity fixture")

    monkeypatch.setattr(
        "pg_rankings.discord_webhook.render_weekly_activity_card", fail_activity_render
    )

    def request(
        method: str,
        _url: str,
        payload: object,
        attachment: bytes | None = None,
    ) -> dict[str, str]:
        assert method == "POST"
        assert attachment is not None
        assert payload["attachments"][0]["filename"] == CARD_FILENAME  # type: ignore[index]
        return {"id": "999"}

    publisher._request = request  # type: ignore[method-assign]

    assert publisher.publish(snapshot()) == {"primary/ranking": "999"}
