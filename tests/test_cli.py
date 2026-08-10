from typer.testing import CliRunner
from publicador.cli import app
from publicador.queue import QueueStore
from publicador.models import AttemptState
import json
import os
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta


runner = CliRunner()


def test_publish_success_json(tmp_home, media_fixture_path, sample_spec_path):
    result = runner.invoke(app, ["publish", sample_spec_path, "--json", "--fake"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["state"] == "ok"
    post = data["post"]
    assert len(post["platforms"]) == 3
    for p in post["platforms"]:
        assert p["state"] == "published"
        assert p["url"] is not None


def test_publish_success_prose(tmp_home, media_fixture_path, sample_spec_path):
    result = runner.invoke(app, ["publish", sample_spec_path, "--fake"])
    assert result.exit_code == 0, result.stdout
    assert "queued" in result.stdout
    assert "instagram" in result.stdout
    assert "tiktok" in result.stdout
    assert "youtube" in result.stdout


def test_publish_missing_spec_file(tmp_home):
    result = runner.invoke(app, ["publish", "/nonexistent/spec.json", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["state"] == "problem"
    assert "could not read spec" in data["problems"][0]


def test_publish_missing_media(tmp_home, tmp_path):
    spec_path = tmp_path / "bad_spec.json"
    spec_path.write_text(json.dumps({
        "caption": "x",
        "media": "/nonexistent/video.mp4",
        "platforms": ["instagram"],
    }))
    result = runner.invoke(app, ["publish", str(spec_path), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["state"] == "problem"
    assert any("media file not found" in p for p in data["problems"])


def test_publish_caption_too_long(tmp_home, tmp_path, media_fixture_path):
    long_caption = "x" * 2201
    spec_path = tmp_path / "long_spec.json"
    spec_path.write_text(json.dumps({
        "caption": long_caption,
        "media": media_fixture_path,
        "platforms": ["instagram"],
    }))
    result = runner.invoke(app, ["publish", str(spec_path), "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert any("instagram limit is 2200" in p for p in data["problems"])


def test_publish_unknown_platform(tmp_home, tmp_path, media_fixture_path):
    spec_path = tmp_path / "weird_spec.json"
    spec_path.write_text(json.dumps({
        "caption": "x",
        "media": media_fixture_path,
        "platforms": ["mastodon"],
    }))
    result = runner.invoke(app, ["publish", str(spec_path), "--json", "--fake"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    res = data["results"][0]
    assert res["ok"] is False
    assert "unknown platform 'mastodon'" in res["error"]


def test_publish_persists_to_queue(tmp_home, media_fixture_path, sample_spec_path):
    result = runner.invoke(app, ["publish", sample_spec_path, "--fake", "--json"])
    assert result.exit_code == 0
    store = QueueStore(home=tmp_home)
    posts = store.load()
    assert len(posts) == 1
    assert posts[0].attempts[0].state == AttemptState.PUBLISHED


def test_status_empty_json(tmp_home):
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["state"] == "empty"
    assert data["posts"] == []


def test_status_empty_prose(tmp_home):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "queue is empty" in result.stdout


def test_status_shows_post(tmp_home, media_fixture_path, sample_spec_path):
    runner.invoke(app, ["publish", sample_spec_path, "--fake"])
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["state"] == "ok"
    assert len(data["posts"]) == 1
    post = data["posts"][0]
    assert post["state"] == "published"
    assert len(post["platforms"]) == 3


def test_schedule_future(tmp_home, media_fixture_path, scheduled_spec_path):
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    result = runner.invoke(app, ["schedule", scheduled_spec_path, "--at", future, "--json"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["state"] == "ok"
    assert data["post"]["schedule_at"] is not None


def test_schedule_past_rejected(tmp_home, media_fixture_path, scheduled_spec_path):
    result = runner.invoke(app, ["schedule", scheduled_spec_path, "--at", "2020-01-01T00:00:00+00:00", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["state"] == "problem"
    assert "in the past" in data["problems"][0]


def test_schedule_bad_format(tmp_home, media_fixture_path, scheduled_spec_path):
    result = runner.invoke(app, ["schedule", scheduled_spec_path, "--at", "not a date", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert "not a valid ISO 8601" in data["problems"][0]


def test_drain_empty_queue(tmp_home):
    result = runner.invoke(app, ["drain", "--json", "--fake"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["state"] == "empty"


def test_drain_publishes_scheduled(tmp_home, media_fixture_path, sample_spec_path):
    future = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    runner.invoke(app, ["schedule", sample_spec_path, "--at", future, "--fake", "--json"])
    import time
    time.sleep(1.2)
    result = runner.invoke(app, ["drain", "--json", "--fake"])
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["state"] == "ok"
    assert len(data["touched"]) == 1
    for p in data["touched"][0]["platforms"]:
        assert p["state"] == "published"


def test_retry_post(tmp_home, tmp_path, media_fixture_path):
    fail_spec = tmp_path / "fail_spec.json"
    fail_spec.write_text(json.dumps({
        "caption": "x",
        "media": media_fixture_path,
        "platforms": ["instagram"],
    }))
    result = runner.invoke(app, ["publish", str(fail_spec), "--json", "--fake"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    post_id = data["post"]["id"]
    result2 = runner.invoke(app, ["retry", post_id, "--json", "--fake"])
    assert result2.exit_code == 0, result2.stdout


def test_retry_missing_post(tmp_home):
    result = runner.invoke(app, ["retry", "nonexistent", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert "no post with id" in data["problems"][0]


def test_retry_one_platform(tmp_home, tmp_path, media_fixture_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "caption": "x",
        "media": media_fixture_path,
        "platforms": ["instagram", "tiktok"],
    }))
    runner.invoke(app, ["publish", str(spec_path), "--fake", "--json"])
    store = QueueStore(home=tmp_home)
    post = store.load()[0]
    post.attempts[0].state = AttemptState.FAILED
    post.attempts[0].attempts = 1
    store.replace(post)
    result = runner.invoke(app, ["retry", post.id, "--platform", "instagram", "--json", "--fake"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    ig = [p for p in data["post"]["platforms"] if p["platform"] == "instagram"][0]
    assert ig["state"] == "published"


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "publicador 0.1.0" in result.stdout


def test_publish_quarantines_corrupt_queue(tmp_home, media_fixture_path, sample_spec_path):
    store = QueueStore(home=tmp_home)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{broken", encoding="utf-8")
    result = runner.invoke(app, ["publish", sample_spec_path, "--fake", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["state"] == "problem"
    assert "quarantined" in data["problems"][0]
    corrupt_files = list(tmp_home.glob("queue.json.corrupt-*"))
    assert len(corrupt_files) == 1
