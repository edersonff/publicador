from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any
import json
import os
import secrets


EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_NOTHING_TO_DO = 2


class PostState(str, Enum):
    QUEUED = "queued"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    SCHEDULED = "scheduled"


class AttemptState(str, Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"


POST_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
POST_ID_LEN = 8


def new_post_id() -> str:
    return "".join(secrets.choice(POST_ID_ALPHABET) for _ in range(POST_ID_LEN))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


CAPTION_LIMITS = {
    "instagram": 2200,
    "tiktok": 2200,
    "youtube": 5000,
}


@dataclass
class Spec:
    caption: str
    media: str
    platforms: list[str]
    schedule_at: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Spec":
        return cls(
            caption=d["caption"],
            media=d["media"],
            platforms=list(d["platforms"]),
            schedule_at=d.get("schedule_at"),
            extra=dict(d.get("extra") or {}),
        )

    @classmethod
    def from_json_file(cls, path: str) -> "Spec":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        spec_dir = os.path.dirname(os.path.abspath(path))
        media = data.get("media", "")
        if media and not os.path.isabs(media):
            data["media"] = os.path.join(spec_dir, media)
        return cls.from_dict(data)

    def validate(self) -> list[str]:
        problems = []
        if not isinstance(self.caption, str) or not self.caption:
            problems.append("caption is missing or empty")
        if not isinstance(self.media, str) or not self.media:
            problems.append("media path is missing or empty")
        elif not os.path.exists(self.media):
            problems.append(f"media file not found: {self.media}")
        elif os.path.isdir(self.media):
            problems.append(f"media path is a folder, not a file: {self.media}")
        if not isinstance(self.platforms, list) or not self.platforms:
            problems.append("platforms list is missing or empty")
        else:
            for p in self.platforms:
                if not isinstance(p, str) or not p:
                    problems.append(f"platform entry is not a string: {p!r}")
        if self.schedule_at is not None:
            try:
                parse_iso(self.schedule_at)
            except ValueError:
                problems.append(
                    f"schedule_at is not a valid ISO 8601 timestamp: {self.schedule_at}"
                )
        return problems

    def caption_too_long_for(self, platform: str) -> Optional[int]:
        limit = CAPTION_LIMITS.get(platform)
        if limit is None:
            return None
        return limit if len(self.caption) > limit else None


@dataclass
class PlatformAttempt:
    platform: str
    state: AttemptState = AttemptState.PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    url: Optional[str] = None
    next_retry_at: Optional[str] = None
    published_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlatformAttempt":
        return cls(
            platform=d["platform"],
            state=AttemptState(d.get("state", AttemptState.PENDING.value)),
            attempts=int(d.get("attempts", 0)),
            last_error=d.get("last_error"),
            url=d.get("url"),
            next_retry_at=d.get("next_retry_at"),
            published_at=d.get("published_at"),
        )


@dataclass
class Post:
    id: str
    spec: Spec
    created_at: str
    attempts: list[PlatformAttempt]
    schedule_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spec": self.spec.to_dict(),
            "created_at": self.created_at,
            "schedule_at": self.schedule_at,
            "attempts": [a.to_dict() for a in self.attempts],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Post":
        return cls(
            id=d["id"],
            spec=Spec.from_dict(d["spec"]),
            created_at=d["created_at"],
            schedule_at=d.get("schedule_at"),
            attempts=[PlatformAttempt.from_dict(a) for a in d.get("attempts", [])],
        )

    @classmethod
    def new(cls, spec: Spec) -> "Post":
        return cls(
            id=new_post_id(),
            spec=spec,
            created_at=now_iso(),
            attempts=[PlatformAttempt(platform=p) for p in spec.platforms],
            schedule_at=spec.schedule_at,
        )

    def overall_state(self) -> PostState:
        if self.schedule_at and not self._is_due():
            return PostState.SCHEDULED
        states = [a.state for a in self.attempts]
        if any(s == AttemptState.PUBLISHING for s in states):
            return PostState.PUBLISHING
        if all(s == AttemptState.PUBLISHED for s in states):
            return PostState.PUBLISHED
        if all(s in (AttemptState.FAILED, AttemptState.AUTH_FAILED) for s in states):
            return PostState.FAILED
        if any(s == AttemptState.FAILED for s in states):
            return PostState.PUBLISHING
        return PostState.QUEUED

    def _is_due(self) -> bool:
        if not self.schedule_at:
            return True
        try:
            when = parse_iso(self.schedule_at)
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= when

    def attempt_for(self, platform: str) -> Optional[PlatformAttempt]:
        for a in self.attempts:
            if a.platform == platform:
                return a
        return None


@dataclass
class PublishResult:
    platform: str
    ok: bool
    url: Optional[str] = None
    error: Optional[str] = None
    is_auth_failure: bool = False
    is_rate_limited: bool = False
