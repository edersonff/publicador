from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Post


PUBLICADOR_DIR_ENV = "PUBLICADOR_HOME"


def default_home() -> Path:
    env = os.environ.get(PUBLICADOR_DIR_ENV)
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "publicador"
    return Path.home() / ".local" / "share" / "publicador"


def queue_path(home: Optional[Path] = None) -> Path:
    return (home or default_home()) / "queue.json"


class QueueCorrupt(Exception):
    def __init__(self, path: Path, reason: str, quarantined_to: Optional[Path] = None):
        self.path = path
        self.reason = reason
        self.quarantined_to = quarantined_to
        super().__init__(f"queue at {path} is corrupt: {reason}")


class QueueStore:
    def __init__(self, home: Optional[Path] = None):
        self.home = home or default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "queue.json"

    def _atomic_write(self, data: list[dict]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".queue.", suffix=".tmp", dir=str(self.home)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> list[Post]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            quarantined = self._quarantine()
            raise QueueCorrupt(
                self.path, str(e), quarantined_to=quarantined
            ) from e
        if not isinstance(raw, list):
            quarantined = self._quarantine()
            raise QueueCorrupt(
                self.path,
                "top-level json is not a list",
                quarantined_to=quarantined,
            )
        posts: list[Post] = []
        for i, item in enumerate(raw):
            try:
                posts.append(Post.from_dict(item))
            except (KeyError, TypeError, ValueError) as e:
                quarantined = self._quarantine()
                raise QueueCorrupt(
                    self.path,
                    f"post at index {i} is malformed: {e}",
                    quarantined_to=quarantined,
                ) from e
        return posts

    def save(self, posts: list[Post]) -> None:
        self._atomic_write([p.to_dict() for p in posts])

    def add(self, post: Post) -> None:
        posts = self.load()
        posts.append(post)
        self.save(posts)

    def replace(self, post: Post) -> bool:
        posts = self.load()
        for i, p in enumerate(posts):
            if p.id == post.id:
                posts[i] = post
                self.save(posts)
                return True
        return False

    def get(self, post_id: str) -> Optional[Post]:
        for p in self.load():
            if p.id == post_id:
                return p
        return None

    def remove(self, post_id: str) -> bool:
        posts = self.load()
        kept = [p for p in posts if p.id != post_id]
        if len(kept) == len(posts):
            return False
        self.save(kept)
        return True

    def _quarantine(self) -> Optional[Path]:
        if not self.path.exists():
            return None
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.home / f"queue.json.corrupt-{ts}"
        try:
            os.replace(self.path, dest)
            return dest
        except OSError:
            return None
