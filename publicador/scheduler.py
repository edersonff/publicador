from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable

from .models import (
    Post,
    PlatformAttempt,
    AttemptState,
    PublishResult,
    PostState,
    now_iso,
    parse_iso,
)
from .publishers import Publisher
from .queue import QueueStore


MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 600


def backoff_seconds(failed_attempts: int) -> int:
    if failed_attempts <= 0:
        return BACKOFF_BASE_SECONDS
    delay = BACKOFF_BASE_SECONDS * (2 ** (failed_attempts - 1))
    return min(delay, BACKOFF_MAX_SECONDS)


def should_retry(attempt: PlatformAttempt) -> bool:
    if attempt.attempts >= MAX_ATTEMPTS:
        return False
    return attempt.state in (
        AttemptState.FAILED,
        AttemptState.RATE_LIMITED,
        AttemptState.AUTH_FAILED,
    )


def is_attempt_due(attempt: PlatformAttempt, now: datetime) -> bool:
    if attempt.state == AttemptState.PENDING:
        return True
    if not should_retry(attempt):
        return False
    if not attempt.next_retry_at:
        return True
    try:
        nxt = parse_iso(attempt.next_retry_at)
    except ValueError:
        return True
    return now >= nxt


def is_post_due(post: Post, now: datetime) -> bool:
    if not post.schedule_at:
        return True
    try:
        when = parse_iso(post.schedule_at)
    except ValueError:
        return True
    return now >= when


PublisherRegistry = dict[str, Publisher]


def _apply_result(attempt: PlatformAttempt, result: PublishResult, now: datetime) -> None:
    attempt.attempts += 1
    attempt.last_error = result.error if not result.ok else None
    if result.ok:
        attempt.state = AttemptState.PUBLISHED
        attempt.url = result.url
        attempt.next_retry_at = None
        attempt.published_at = now_iso()
        return
    if result.is_auth_failure:
        attempt.state = AttemptState.AUTH_FAILED
    elif result.is_rate_limited:
        attempt.state = AttemptState.RATE_LIMITED
    else:
        attempt.state = AttemptState.FAILED
    if should_retry(attempt):
        delay = backoff_seconds(attempt.attempts)
        attempt.next_retry_at = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
    else:
        attempt.next_retry_at = None


def attempt_platform(
    post: Post,
    platform: str,
    registry: PublisherRegistry,
    now: Optional[datetime] = None,
) -> Optional[PublishResult]:
    if now is None:
        now = datetime.now(timezone.utc)
    attempt = post.attempt_for(platform)
    if attempt is None:
        return None
    publisher = registry.get(platform)
    if publisher is None:
        result = PublishResult(
            platform=platform,
            ok=False,
            error=(
                f"unknown platform '{platform}'; "
                f"known: {', '.join(sorted(registry))}"
            ),
        )
    else:
        attempt.state = AttemptState.PUBLISHING
        try:
            result = publisher.publish(post.spec)
        except Exception as e:
            result = PublishResult(
                platform=platform,
                ok=False,
                error=f"publisher raised {type(e).__name__}: {e}",
            )
    _apply_result(attempt, result, now)
    return result


def drain_due(
    store: QueueStore,
    registry: PublisherRegistry,
    now: Optional[datetime] = None,
) -> list[Post]:
    if now is None:
        now = datetime.now(timezone.utc)
    posts = store.load()
    touched: list[Post] = []
    for post in posts:
        if not is_post_due(post, now):
            continue
        any_attempted = False
        for attempt in post.attempts:
            if not is_attempt_due(attempt, now):
                continue
            any_attempted = True
            attempt_platform(post, attempt.platform, registry, now)
        if any_attempted:
            touched.append(post)
    if touched:
        store.save(posts)
    return touched


def reset_for_retry(post: Post, platform: str) -> bool:
    attempt = post.attempt_for(platform)
    if attempt is None:
        return False
    attempt.state = AttemptState.PENDING
    attempt.next_retry_at = None
    attempt.last_error = None
    attempt.attempts = 0
    return True
