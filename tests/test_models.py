from publicador.models import (
    Spec,
    Post,
    PlatformAttempt,
    AttemptState,
    PostState,
    new_post_id,
    now_iso,
    parse_iso,
    CAPTION_LIMITS,
)
from datetime import datetime, timezone, timedelta
import os
import pytest


def test_new_post_id_shape():
    pid = new_post_id()
    assert len(pid) == 8
    assert pid.islower()
    assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in pid)


def test_new_post_id_uniqueness():
    ids = {new_post_id() for _ in range(1000)}
    assert len(ids) >= 999


def test_spec_validate_happy(media_fixture_path):
    spec = Spec(
        caption="hello world",
        media=media_fixture_path,
        platforms=["instagram", "tiktok"],
    )
    assert spec.validate() == []


def test_spec_validate_missing_caption(media_fixture_path):
    spec = Spec(caption="", media=media_fixture_path, platforms=["instagram"])
    problems = spec.validate()
    assert any("caption is missing or empty" in p for p in problems)


def test_spec_validate_missing_media():
    spec = Spec(caption="x", media="/nonexistent/path.mp4", platforms=["instagram"])
    problems = spec.validate()
    assert any("media file not found" in p for p in problems)


def test_spec_validate_media_is_folder(tmp_path):
    spec = Spec(caption="x", media=str(tmp_path), platforms=["instagram"])
    problems = spec.validate()
    assert any("folder, not a file" in p for p in problems)


def test_spec_validate_empty_platforms(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=[])
    problems = spec.validate()
    assert any("platforms list is missing or empty" in p for p in problems)


def test_spec_validate_bad_schedule(media_fixture_path):
    spec = Spec(
        caption="x",
        media=media_fixture_path,
        platforms=["instagram"],
        schedule_at="not a date",
    )
    problems = spec.validate()
    assert any("not a valid ISO 8601" in p for p in problems)


def test_spec_validate_good_schedule(media_fixture_path):
    spec = Spec(
        caption="x",
        media=media_fixture_path,
        platforms=["instagram"],
        schedule_at="2026-12-31T23:59:00+00:00",
    )
    assert spec.validate() == []


def test_caption_too_long_for_instagram(media_fixture_path):
    long_caption = "x" * (CAPTION_LIMITS["instagram"] + 1)
    spec = Spec(caption=long_caption, media=media_fixture_path, platforms=["instagram"])
    limit = spec.caption_too_long_for("instagram")
    assert limit == CAPTION_LIMITS["instagram"]


def test_caption_at_exact_limit(media_fixture_path):
    exact_caption = "x" * CAPTION_LIMITS["instagram"]
    spec = Spec(caption=exact_caption, media=media_fixture_path, platforms=["instagram"])
    assert spec.caption_too_long_for("instagram") is None


def test_caption_one_over_limit(media_fixture_path):
    over_caption = "x" * (CAPTION_LIMITS["instagram"] + 1)
    spec = Spec(caption=over_caption, media=media_fixture_path, platforms=["instagram"])
    assert spec.caption_too_long_for("instagram") is not None


def test_caption_within_limit(media_fixture_path):
    spec = Spec(caption="short", media=media_fixture_path, platforms=["instagram"])
    assert spec.caption_too_long_for("instagram") is None


def test_caption_limit_unknown_platform(media_fixture_path):
    spec = Spec(caption="x" * 99999, media=media_fixture_path, platforms=["mastodon"])
    assert spec.caption_too_long_for("mastodon") is None


def test_spec_round_trip(media_fixture_path):
    spec = Spec(
        caption="round trip",
        media=media_fixture_path,
        platforms=["instagram", "tiktok"],
        schedule_at="2026-12-31T23:59:00+00:00",
        extra={"tags": ["test"]},
    )
    d = spec.to_dict()
    back = Spec.from_dict(d)
    assert back.caption == spec.caption
    assert back.media == spec.media
    assert back.platforms == spec.platforms
    assert back.schedule_at == spec.schedule_at
    assert back.extra == spec.extra


def test_post_overall_state_published(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram", "tiktok"])
    post = Post.new(spec)
    for a in post.attempts:
        a.state = AttemptState.PUBLISHED
        a.url = f"https://example/{a.platform}"
    assert post.overall_state() == PostState.PUBLISHED


def test_post_overall_state_failed(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    post.attempts[0].attempts = 3
    post.attempts[0].state = AttemptState.FAILED
    assert post.overall_state() == PostState.FAILED


def test_post_overall_state_scheduled(media_fixture_path):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"], schedule_at=future)
    post = Post.new(spec)
    assert post.overall_state() == PostState.SCHEDULED


def test_post_overall_state_queued(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    assert post.overall_state() == PostState.QUEUED


def test_post_round_trip(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram", "tiktok"])
    post = Post.new(spec)
    post.attempts[0].state = AttemptState.PUBLISHED
    post.attempts[0].url = "https://ig.example/post/1"
    post.attempts[0].published_at = now_iso()
    d = post.to_dict()
    back = Post.from_dict(d)
    assert back.id == post.id
    assert len(back.attempts) == 2
    assert back.attempts[0].state == AttemptState.PUBLISHED
    assert back.attempts[0].url == "https://ig.example/post/1"
