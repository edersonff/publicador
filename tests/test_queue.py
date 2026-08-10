from publicador.queue import QueueStore, QueueCorrupt, default_home, queue_path
from publicador.models import Spec, Post, AttemptState
import json
import os
import pytest
from pathlib import Path


def _spec(media_fixture_path):
    return Spec(caption="x", media=media_fixture_path, platforms=["instagram"])


def test_default_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLICADOR_HOME", str(tmp_path / "custom"))
    assert default_home() == tmp_path / "custom"


def test_default_home_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLICADOR_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_home() == tmp_path / "xdg" / "publicador"


def test_default_home_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLICADOR_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_home() == tmp_path / ".local" / "share" / "publicador"


def test_queue_empty_when_missing(tmp_home):
    store = QueueStore(home=tmp_home)
    assert store.load() == []


def test_queue_add_and_load(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    post = Post.new(_spec(media_fixture_path))
    store.add(post)
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].id == post.id


def test_queue_replace(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    post = Post.new(_spec(media_fixture_path))
    store.add(post)
    post.attempts[0].state = AttemptState.PUBLISHED
    assert store.replace(post) is True
    loaded = store.load()
    assert loaded[0].attempts[0].state == AttemptState.PUBLISHED


def test_queue_replace_missing(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    post = Post.new(_spec(media_fixture_path))
    assert store.replace(post) is False


def test_queue_get(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    post = Post.new(_spec(media_fixture_path))
    store.add(post)
    got = store.get(post.id)
    assert got is not None
    assert got.id == post.id
    assert store.get("nonexistent") is None


def test_queue_remove(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    post = Post.new(_spec(media_fixture_path))
    store.add(post)
    assert store.remove(post.id) is True
    assert store.load() == []
    assert store.remove(post.id) is False


def test_queue_atomic_write_no_partial(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    post = Post.new(_spec(media_fixture_path))
    store.add(post)
    queue_file = store.path
    contents = queue_file.read_text(encoding="utf-8")
    assert json.loads(contents)
    temp_files = list(tmp_home.glob(".queue.*.tmp"))
    assert temp_files == []


def test_queue_corrupt_quarantines(tmp_home):
    store = QueueStore(home=tmp_home)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(QueueCorrupt) as ei:
        store.load()
    assert ei.value.quarantined_to is not None
    assert ei.value.quarantined_to.exists()
    assert not store.path.exists()
    assert store.load() == []


def test_queue_corrupt_not_a_list(tmp_home):
    store = QueueStore(home=tmp_home)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(QueueCorrupt):
        store.load()


def test_queue_corrupt_post_field(tmp_home):
    store = QueueStore(home=tmp_home)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps([{"id": "abc", "spec": {"caption": "x"}, "created_at": "now", "attempts": []}]),
        encoding="utf-8",
    )
    with pytest.raises(QueueCorrupt):
        store.load()


def test_queue_save_preserves_multiple(tmp_home, media_fixture_path):
    store = QueueStore(home=tmp_home)
    p1 = Post.new(_spec(media_fixture_path))
    p2 = Post.new(_spec(media_fixture_path))
    store.add(p1)
    store.add(p2)
    loaded = store.load()
    assert len(loaded) == 2
    ids = {p.id for p in loaded}
    assert ids == {p1.id, p2.id}
