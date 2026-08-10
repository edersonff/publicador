from publicador.publishers import (
    FakePublisher,
    ShellPublisher,
    _fetch_cookies_netscape,
    _classify_stderr,
    build_default_registry,
    CookiesMissing,
    DependencyMissing,
)
from publicador.models import Spec, PublishResult
import os
import subprocess
import pytest
from pathlib import Path


def test_fake_publisher_success(media_fixture_path):
    pub = FakePublisher("instagram")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is True
    assert r.platform == "instagram"
    assert r.url is not None
    assert "instagram" in r.url


def test_fake_publisher_failure(media_fixture_path):
    pub = FakePublisher("instagram", succeed=False)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is False
    assert r.error is not None


def test_fake_publisher_auth_failure(media_fixture_path):
    pub = FakePublisher("instagram", fail_auth=True)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is False
    assert r.is_auth_failure is True


def test_fake_publisher_rate_limited(media_fixture_path):
    pub = FakePublisher("instagram", fail_rate=True)
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is False
    assert r.is_rate_limited is True


def test_classify_stderr_plain_failure():
    is_rate, is_auth = _classify_stderr("uploader exited 1")
    assert is_rate is False
    assert is_auth is False


def test_classify_stderr_auth():
    is_rate, is_auth = _classify_stderr("ERROR: 401 unauthorized")
    assert is_auth is True


def test_classify_stderr_rate():
    is_rate, is_auth = _classify_stderr("HTTP 429 too many requests")
    assert is_rate is True


def test_classify_stderr_cookie():
    is_rate, is_auth = _classify_stderr("session expired, cookie invalid")
    assert is_auth is True


def test_build_default_registry_has_three_platforms():
    reg = build_default_registry()
    assert set(reg) == {"instagram", "tiktok", "youtube"}
    for name, pub in reg.items():
        assert isinstance(pub, ShellPublisher)
        assert pub.name == name


def test_fetch_cookies_missing_cm(monkeypatch):
    monkeypatch.setattr("publicador.publishers.shutil.which", lambda name: None)
    with pytest.raises(DependencyMissing) as ei:
        _fetch_cookies_netscape("instagram.com")
    assert "cm binary not found" in str(ei.value)
    assert "edersonff/cookie-manager" in str(ei.value)


def test_fetch_cookies_cm_failure(monkeypatch):
    def fake_which(name):
        return "/usr/local/bin/cm" if name == "cm" else None

    def fake_run(cmd, **kwargs):
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no cookies found for instagram.com")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    with pytest.raises(CookiesMissing) as ei:
        _fetch_cookies_netscape("instagram.com")
    msg = str(ei.value)
    assert "cm could not read cookies" in msg
    assert "cm get instagram.com --no-cache" in msg


def test_fetch_cookies_empty(monkeypatch):
    def fake_which(name):
        return "/usr/local/bin/cm" if name == "cm" else None

    def fake_run(cmd, **kwargs):
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, 0, stdout="   \n  ", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    with pytest.raises(CookiesMissing) as ei:
        _fetch_cookies_netscape("instagram.com")
    assert "no cookies returned" in str(ei.value)


def test_fetch_cookies_success(monkeypatch):
    def fake_which(name):
        return "/usr/local/bin/cm" if name == "cm" else None

    def fake_run(cmd, **kwargs):
        if cmd[1] == "get":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tFALSE\t...\tdatr\tabc\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    body = _fetch_cookies_netscape("instagram.com")
    assert "Netscape" in body
    assert "datr" in body


def test_shell_publisher_uploader_missing(monkeypatch, media_fixture_path):
    def fake_which(name):
        return "/usr/local/bin/cm" if name == "cm" else None

    def fake_run(cmd, **kwargs):
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, 0, stdout="# Netscape\n.instagram.com\tdatr\tx\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    pub = ShellPublisher("instagram", "instagram.com", "ig-uploader")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is False
    assert "uploader command not found" in r.error
    assert "PUBLICADOR_INSTAGRAM_UPLOADER" in r.error


def test_shell_publisher_success(monkeypatch, media_fixture_path, tmp_path):
    uploader_path = tmp_path / "ig-uploader"
    uploader_path.write_text("#!/bin/sh\necho 'https://instagram.com/p/abc123'\nexit 0\n")
    uploader_path.chmod(0o755)

    def fake_which(name):
        if name == "cm":
            return "/usr/local/bin/cm"
        if name == "ig-uploader":
            return str(uploader_path)
        return None

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, 0, stdout="# Netscape\n.instagram.com\tdatr\tx\n", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="https://instagram.com/p/abc123\n", stderr=""
        )

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    pub = ShellPublisher("instagram", "instagram.com", "ig-uploader")
    spec = Spec(caption="hello", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is True
    assert r.url == "https://instagram.com/p/abc123"
    uploader_call = [c for c in calls if c[0] == str(uploader_path)][0]
    assert "--media" in uploader_call
    assert "--caption" in uploader_call
    assert "--cookies" in uploader_call
    media_idx = uploader_call.index("--media") + 1
    caption_idx = uploader_call.index("--caption") + 1
    cookies_idx = uploader_call.index("--cookies") + 1
    assert uploader_call[media_idx] == spec.media
    assert uploader_call[caption_idx] == "hello"
    cookies_file = uploader_call[cookies_idx]
    assert not os.path.exists(cookies_file), "cookies temp file should be cleaned up"


def test_shell_publisher_uploader_failure(monkeypatch, media_fixture_path, tmp_path):
    uploader_path = tmp_path / "ig-uploader"
    uploader_path.write_text("#!/bin/sh\necho 'auth expired' >&2\nexit 1\n")
    uploader_path.chmod(0o755)

    def fake_which(name):
        if name == "cm":
            return "/usr/local/bin/cm"
        if name == "ig-uploader":
            return str(uploader_path)
        return None

    def fake_run(cmd, **kwargs):
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, 0, stdout="# Netscape\n.instagram.com\tdatr\tx\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="session expired, cookie invalid")

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    pub = ShellPublisher("instagram", "instagram.com", "ig-uploader")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is False
    assert r.is_auth_failure is True
    assert "expired" in r.error


def test_shell_publisher_env_override(monkeypatch, media_fixture_path, tmp_path):
    custom_path = tmp_path / "custom-ig"
    custom_path.write_text("#!/bin/sh\necho 'https://custom.example/p/1'\nexit 0\n")
    custom_path.chmod(0o755)

    def fake_which(name):
        if name == "cm":
            return "/usr/local/bin/cm"
        if name == "custom-ig":
            return str(custom_path)
        return None

    def fake_run(cmd, **kwargs):
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, 0, stdout="# Netscape\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="https://custom.example/p/1\n", stderr="")

    monkeypatch.setattr("publicador.publishers.shutil.which", fake_which)
    monkeypatch.setattr("publicador.publishers.subprocess.run", fake_run)
    monkeypatch.setenv("PUBLICADOR_INSTAGRAM_UPLOADER", "custom-ig")
    pub = ShellPublisher("instagram", "instagram.com", "ig-uploader")
    spec = Spec(caption="x", media=media_fixture_path, platforms=["instagram"])
    r = pub.publish(spec)
    assert r.ok is True
    assert r.url == "https://custom.example/p/1"
