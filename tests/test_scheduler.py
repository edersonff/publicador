from publicador.scheduler import (
    backoff_seconds,
    should_retry,
    is_attempt_due,
    is_post_due,
    attempt_platform,
    drain_due,
    reset_for_retry,
    MAX_ATTEMPTS,
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
)
from publicador.publishers import FakePublisher
from publicador.queue import QueueStore
from publicador.models import (
    Spec,
    Post,
    PlatformAttempt,
    AttemptState,
)
from datetime import datetime, timezone, timedelta
import pytest


def test_backoff_sequence():
    assert backoff_seconds(0) == BACKOFF_BASE_SECONDS
    assert backoff_seconds(1) == BACKOFF_BASE_SECONDS
    assert backoff_seconds(2) == BACKOFF_BASE_SECONDS * 2
    assert backoff_seconds(3) == BACKOFF_BASE_SECONDS * 4
    assert backoff_seconds(10) <= BACKOFF_MAX_SECONDS


def test_should_retry_under_max():
    a = PlatformAttempt(platform="instagram", attempts=1, state=AttemptState.FAILED)
    assert should_retry(a) is True


def test_should_retry_at_max():
    a = PlatformAttempt(platform="instagram", attempts=MAX_ATTEMPTS, state=AttemptState.FAILED)
    assert should_retry(a) is False


def test_should_retry_published():
    a = PlatformAttempt(platform="instagram", attempts=1, state=AttemptState.PUBLISHED)
    assert should_retry(a) is False


def test_should_retry_pending():
    a = PlatformAttempt(platform="instagram", attempts=0, state=AttemptState.PENDING)
    assert should_retry(a) is False


def test_is_attempt_due_pending():
    a = PlatformAttempt(platform="instagram", state=AttemptState.PENDING)
    assert is_attempt_due(a, datetime.now(timezone.utc)) is True


def test_is_attempt_due_failed_no_retry_time():
    a = PlatformAttempt(platform="instagram", attempts=1, state=AttemptState.FAILED)
    assert is_attempt_due(a, datetime.now(timezone.utc)) is True


def test_is_attempt_due_failed_future_retry():
    future = (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat(timespec="seconds")
    a = PlatformAttempt(platform="instagram", attempts=1, state=AttemptState.FAILED, next_retry_at=future)
    assert is_attempt_due(a, datetime.now(timezone.utc)) is False


def test_is_attempt_due_failed_past_retry():
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
    a = PlatformAttempt(platform="instagram", attempts=1, state=AttemptState.FAILED, next_retry_at=past)
    assert is_attempt_due(a, datetime.now(timezone.utc)) is True


def test_is_attempt_due_failed_max_attempts():
    a = PlatformAttempt(platform="instagram", attempts=MAX_ATTEMPTS, state=AttemptState.FAILED)
    assert is_attempt_due(a, datetime.now(timezone.utc)) is False


def test_is_post_due_no_schedule(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    assert is_post_due(post, datetime.now(timezone.utc)) is True


def test_is_post_due_future_schedule(media_fixture_path):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"], schedule_at=future)
    post = Post.new(spec)
    assert is_post_due(post, datetime.now(timezone.utc)) is False


def test_is_post_due_past_schedule(media_fixture_path):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"], schedule_at=past)
    post = Post.new(spec)
    assert is_post_due(post, datetime.now(timezone.utc)) is True


def test_attempt_platform_success_updates_state(tmp_home, media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    reg = {"instagram": FakePublisher("instagram")}
    now = datetime.now(timezone.utc)
    r = attempt_platform(post, "instagram", reg, now)
    assert r.ok is True
    attempt = post.attempt_for("instagram")
    assert attempt.state == AttemptState.PUBLISHED
    assert attempt.attempts == 1
    assert attempt.url is not None
    assert attempt.published_at is not None
    assert attempt.next_retry_at is None


def test_attempt_platform_failure_schedules_retry(tmp_home, media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    reg = {"instagram": FakePublisher("instagram", succeed=False)}
    now = datetime.now(timezone.utc)
    r = attempt_platform(post, "instagram", reg, now)
    assert r.ok is False
    attempt = post.attempt_for("instagram")
    assert attempt.state == AttemptState.FAILED
    assert attempt.attempts == 1
    assert attempt.next_retry_at is not None


def test_attempt_platform_auth_failure_classified(tmp_home, media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    reg = {"instagram": FakePublisher("instagram", fail_auth=True)}
    now = datetime.now(timezone.utc)
    r = attempt_platform(post, "instagram", reg, now)
    attempt = post.attempt_for("instagram")
    assert attempt.state == AttemptState.AUTH_FAILED
    assert attempt.attempts == 1


def test_attempt_platform_rate_limited_classified(tmp_home, media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    reg = {"instagram": FakePublisher("instagram", fail_rate=True)}
    now = datetime.now(timezone.utc)
    attempt_platform(post, "instagram", reg, now)
    attempt = post.attempt_for("instagram")
    assert attempt.state == AttemptState.RATE_LIMITED


def test_attempt_platform_unknown_platform(tmp_home, media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["mastodon"])
    post = Post.new(spec)
    reg = {"instagram": FakePublisher("instagram")}
    r = attempt_platform(post, "mastodon", reg, datetime.now(timezone.utc))
    assert r.ok is False
    assert "unknown platform 'mastodon'" in r.error


def test_drain_due_attempts_due_posts(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram", "tiktok"])
    post = Post.new(spec)
    store.add(post)
    reg = {
        "instagram": FakePublisher("instagram"),
        "tiktok": FakePublisher("tiktok"),
    }
    touched = drain_due(store, reg)
    assert len(touched) == 1
    loaded = store.load()
    for a in loaded[0].attempts:
        assert a.state == AttemptState.PUBLISHED


def test_drain_due_skips_future_scheduled(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"], schedule_at=future)
    post = Post.new(spec)
    store.add(post)
    reg = {"instagram": FakePublisher("instagram")}
    touched = drain_due(store, reg)
    assert touched == []
    loaded = store.load()
    assert loaded[0].attempts[0].state == AttemptState.PENDING


def test_drain_due_skips_future_retry(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(timespec="seconds")
    post.attempts[0].state = AttemptState.FAILED
    post.attempts[0].attempts = 1
    post.attempts[0].next_retry_at = future
    store.add(post)
    reg = {"instagram": FakePublisher("instagram")}
    touched = drain_due(store, reg)
    assert touched == []


def test_drain_due_max_attempts_no_retry(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    post.attempts[0].state = AttemptState.FAILED
    post.attempts[0].attempts = MAX_ATTEMPTS
    store.add(post)
    reg = {"instagram": FakePublisher("instagram")}
    touched = drain_due(store, reg)
    assert touched == []


def test_drain_due_retry_path_after_backoff_window(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    post.attempts[0].state = AttemptState.FAILED
    post.attempts[0].attempts = 1
    post.attempts[0].next_retry_at = past
    store.add(post)
    reg = {"instagram": FakePublisher("instagram")}
    touched = drain_due(store, reg)
    assert len(touched) == 1
    attempt = touched[0].attempt_for("instagram")
    assert attempt.state == AttemptState.PUBLISHED
    assert attempt.attempts == 2


def test_reset_for_retry(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    post.attempts[0].state = AttemptState.FAILED
    post.attempts[0].attempts = 2
    post.attempts[0].next_retry_at = "2030-01-01T00:00:00+00:00"
    post.attempts[0].last_error = "boom"
    assert reset_for_retry(post, "instagram") is True
    a = post.attempt_for("instagram")
    assert a.state == AttemptState.PENDING
    assert a.next_retry_at is None
    assert a.last_error is None
    assert a.attempts == 0


def test_reset_for_retry_gives_fresh_budget(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    post.attempts[0].state = AttemptState.FAILED
    post.attempts[0].attempts = MAX_ATTEMPTS
    assert is_attempt_due(post.attempts[0], datetime.now(timezone.utc)) is False
    reset_for_retry(post, "instagram")
    attempt = post.attempt_for("instagram")
    assert attempt.attempts == 0
    assert is_attempt_due(attempt, datetime.now(timezone.utc)) is True


def test_attempt_platform_isolates_publisher_exception(tmp_home, media_fixture_path):
    class RaisingPublisher:
        name = "instagram"
        def publish(self, spec):
            raise RuntimeError("plugin exploded")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    reg = {"instagram": RaisingPublisher()}
    r = attempt_platform(post, "instagram", reg, datetime.now(timezone.utc))
    assert r.ok is False
    assert "plugin exploded" in r.error
    assert "RuntimeError" in r.error
    attempt = post.attempt_for("instagram")
    assert attempt.state == AttemptState.FAILED
    assert attempt.attempts == 1


def test_reset_for_retry_missing_platform(media_fixture_path):
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    post = Post.new(spec)
    assert reset_for_retry(post, "mastodon") is False
