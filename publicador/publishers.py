from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

from .models import Spec, PublishResult


CM_BINARY = "cm"


DEFAULT_UPLOADER_COMMANDS = {
    "instagram": "ig-uploader",
    "tiktok": "tt-uploader",
    "youtube": "yt-uploader",
}


CM_DOMAINS = {
    "instagram": "instagram.com",
    "tiktok": "tiktok.com",
    "youtube": "youtube.com",
}


RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "too many requests")
AUTH_FAILURE_MARKERS = ("auth", "401", "cookie", "session", "login", "unauthorized")


class CookiesMissing(Exception):
    pass


class DependencyMissing(Exception):
    pass


class UploaderMissing(Exception):
    pass


class Publisher(Protocol):
    name: str

    def publish(self, spec: Spec) -> PublishResult:
        ...


def _fetch_cookies_netscape(domain: str) -> str:
    cm_path = shutil.which(CM_BINARY)
    if cm_path is None:
        raise DependencyMissing(
            f"cm binary not found on PATH; install edersonff/cookie-manager "
            f"(needed for {domain})"
        )
    try:
        proc = subprocess.run(
            [cm_path, "get", domain, "--as=yt-dlp"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise CookiesMissing(
            f"cm timed out reading cookies for {domain}: {e}"
        ) from e
    if proc.returncode != 0:
        raise CookiesMissing(
            f"cm could not read cookies for {domain}: "
            f"{proc.stderr.strip() or 'no detail from cm'}. "
            f"Try: cm get {domain} --no-cache"
        )
    body = proc.stdout.strip()
    if not body:
        raise CookiesMissing(
            f"no cookies returned for {domain}; "
            f"sign in to the site in your browser, then: cm get {domain} --no-cache"
        )
    return body


def _classify_stderr(stderr: str) -> tuple[bool, bool]:
    low = stderr.lower()
    is_rate = any(m in low for m in RATE_LIMIT_MARKERS)
    is_auth = any(m in low for m in AUTH_FAILURE_MARKERS)
    return is_rate, is_auth


@dataclass
class ShellPublisher:
    name: str
    cm_domain: str
    uploader_cmd: str

    def _resolve_uploader(self) -> Optional[str]:
        env_key = f"PUBLICADOR_{self.name.upper()}_UPLOADER"
        cmd = os.environ.get(env_key, self.uploader_cmd)
        return shutil.which(cmd)

    def publish(self, spec: Spec) -> PublishResult:
        try:
            cookies = _fetch_cookies_netscape(self.cm_domain)
        except (CookiesMissing, DependencyMissing) as e:
            return PublishResult(
                platform=self.name, ok=False, error=str(e), is_auth_failure=isinstance(e, CookiesMissing)
            )

        uploader_path = self._resolve_uploader()
        if uploader_path is None:
            env_key = f"PUBLICADOR_{self.name.upper()}_UPLOADER"
            return PublishResult(
                platform=self.name,
                ok=False,
                error=(
                    f"uploader command not found for {self.name}: "
                    f"'{os.environ.get(env_key, self.uploader_cmd)}' is not on PATH. "
                    f"Set {env_key}=<path> or install a {self.name} uploader."
                ),
            )

        cookies_fd, cookies_path = tempfile.mkstemp(
            prefix=f"publicador-{self.name}-", suffix=".txt"
        )
        try:
            with os.fdopen(cookies_fd, "w", encoding="utf-8") as f:
                f.write(cookies)
            try:
                proc = subprocess.run(
                    [
                        uploader_path,
                        "--media", spec.media,
                        "--caption", spec.caption,
                        "--cookies", cookies_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                return PublishResult(
                    platform=self.name,
                    ok=False,
                    error=f"uploader timed out after 600s for {self.name}",
                )
            if proc.returncode == 0:
                url = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else None
                return PublishResult(platform=self.name, ok=True, url=url)
            is_rate, is_auth = _classify_stderr(proc.stderr or "")
            err_excerpt = (proc.stderr or "").strip().splitlines()
            err_msg = err_excerpt[-1] if err_excerpt else f"uploader exited {proc.returncode}"
            return PublishResult(
                platform=self.name,
                ok=False,
                error=err_msg,
                is_rate_limited=is_rate,
                is_auth_failure=is_auth,
            )
        finally:
            try:
                os.unlink(cookies_path)
            except OSError:
                pass


@dataclass
class FakePublisher:
    name: str
    succeed: bool = True
    fail_auth: bool = False
    fail_rate: bool = False
    url_template: str = "https://{name}.example/post/{n}"

    def publish(self, spec: Spec) -> PublishResult:
        if self.fail_auth:
            return PublishResult(
                platform=self.name,
                ok=False,
                error="no cookies returned for fake.example",
                is_auth_failure=True,
            )
        if self.fail_rate:
            return PublishResult(
                platform=self.name,
                ok=False,
                error="429 too many requests",
                is_rate_limited=True,
            )
        if not self.succeed:
            return PublishResult(
                platform=self.name,
                ok=False,
                error="uploader exited 1",
            )
        url = self.url_template.format(name=self.name, n=os.getpid())
        return PublishResult(platform=self.name, ok=True, url=url)


def build_default_registry() -> dict[str, Publisher]:
    reg: dict[str, Publisher] = {}
    for platform, uploader in DEFAULT_UPLOADER_COMMANDS.items():
        reg[platform] = ShellPublisher(
            name=platform,
            cm_domain=CM_DOMAINS[platform],
            uploader_cmd=uploader,
        )
    return reg
