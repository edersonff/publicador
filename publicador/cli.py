from __future__ import annotations

import json as json_mod
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import typer

from .models import (
    Spec,
    Post,
    PostState,
    AttemptState,
    EXIT_OK,
    EXIT_BROKEN,
    EXIT_NOTHING_TO_DO,
    now_iso,
    parse_iso,
)
from .queue import QueueStore, QueueCorrupt, default_home
from .publishers import (
    build_default_registry,
    FakePublisher,
    Publisher,
)
from .scheduler import (
    drain_due,
    attempt_platform,
    reset_for_retry,
    is_post_due,
    MAX_ATTEMPTS,
)


app = typer.Typer(
    name="publicador",
    help="Drop a post spec, publicador queues, retries, schedules, dispatches it to every platform.",
    add_completion=False,
)


def _registry(use_fake: bool) -> dict[str, Publisher]:
    if use_fake:
        return {
            "instagram": FakePublisher("instagram"),
            "tiktok": FakePublisher("tiktok"),
            "youtube": FakePublisher("youtube"),
        }
    return build_default_registry()


def _emit_json(obj: dict) -> None:
    typer.echo(json_mod.dumps(obj, ensure_ascii=False, indent=2))


def _emit_prose(line: str) -> None:
    typer.echo(line)


def _validate_spec_or_exit(spec: Spec, json_out: bool) -> None:
    problems = spec.validate()
    for platform in spec.platforms:
        too_long = spec.caption_too_long_for(platform)
        if too_long is not None:
            problems.append(
                f"caption is {len(spec.caption)} chars, {platform} limit is {too_long}"
            )
    if problems:
        if json_out:
            _emit_json({"state": "problem", "problems": problems})
        else:
            _emit_prose(f"{len(problems)} problem(s) with the spec:")
            for p in problems:
                _emit_prose(f"  - {p}")
        raise typer.Exit(code=EXIT_BROKEN)


def _schedule_in_past_or_exit(spec: Spec, json_out: bool) -> None:
    if spec.schedule_at is None:
        return
    try:
        when = parse_iso(spec.schedule_at)
    except ValueError:
        if json_out:
            _emit_json({"state": "problem", "problems": [f"schedule_at is not a valid ISO timestamp: {spec.schedule_at}"]})
        else:
            _emit_prose(f"schedule_at is not a valid ISO timestamp: {spec.schedule_at}")
        raise typer.Exit(code=EXIT_BROKEN)
    if when <= datetime.now(timezone.utc):
        if json_out:
            _emit_json({"state": "problem", "problems": [f"schedule time {spec.schedule_at} is in the past; drop the --at flag to publish now"]})
        else:
            _emit_prose(f"schedule time {spec.schedule_at} is in the past; drop the --at flag to publish now")
        raise typer.Exit(code=EXIT_BROKEN)


def _post_summary(post: Post) -> dict:
    return {
        "id": post.id,
        "state": post.overall_state().value,
        "created_at": post.created_at,
        "schedule_at": post.schedule_at,
        "platforms": [
            {
                "platform": a.platform,
                "state": a.state.value,
                "attempts": a.attempts,
                "max_attempts": MAX_ATTEMPTS,
                "last_error": a.last_error,
                "url": a.url,
                "next_retry_at": a.next_retry_at,
                "published_at": a.published_at,
            }
            for a in post.attempts
        ],
    }


@app.command()
def publish(
    spec_path: str = typer.Argument(..., help="path to a spec.json file"),
    json_out: bool = typer.Option(False, "--json", help="machine-readable json envelope"),
    fake: bool = typer.Option(False, "--fake", help="use in-process fake publishers (no real upload); for testing"),
) -> None:
    """Enqueue a spec and attempt every platform right now."""
    try:
        spec = Spec.from_json_file(spec_path)
    except (OSError, json_mod.JSONDecodeError, KeyError) as e:
        msg = f"could not read spec at {spec_path}: {e}"
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    _validate_spec_or_exit(spec, json_out)
    spec.schedule_at = None
    store = QueueStore()
    try:
        post = Post.new(spec)
        store.add(post)
    except QueueCorrupt as e:
        msg = f"queue was corrupt and has been quarantined: {e.reason}"
        if e.quarantined_to:
            msg += f" (moved to {e.quarantined_to})"
        msg += ". re-run the command; the queue is now fresh."
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    reg = _registry(fake)
    now = datetime.now(timezone.utc)
    results = []
    for platform in spec.platforms:
        r = attempt_platform(post, platform, reg, now)
        if r is not None:
            results.append({
                "platform": r.platform,
                "ok": r.ok,
                "url": r.url,
                "error": r.error,
                "is_auth_failure": r.is_auth_failure,
                "is_rate_limited": r.is_rate_limited,
            })
    store.replace(post)
    summary = _post_summary(post)
    if json_out:
        _emit_json({"state": "ok", "post": summary, "results": results})
    else:
        _emit_prose(f"queued {post.id} for {len(spec.platforms)} platform(s)")
        for a in post.attempts:
            if a.state == AttemptState.PUBLISHED and a.url:
                _emit_prose(f"  {a.platform}: published at {a.url}")
            elif a.state == AttemptState.PUBLISHED:
                _emit_prose(f"  {a.platform}: published")
            else:
                retry_note = ""
                if a.next_retry_at:
                    retry_note = f" (retry {a.attempts}/{MAX_ATTEMPTS} at {a.next_retry_at})"
                _emit_prose(f"  {a.platform}: {a.state.value}{retry_note}")
                if a.last_error:
                    _emit_prose(f"    {a.last_error}")
    raise typer.Exit(code=EXIT_OK)


@app.command()
def schedule(
    spec_path: str = typer.Argument(..., help="path to a spec.json file"),
    at: str = typer.Option(..., "--at", help='ISO 8601 timestamp, e.g. "2026-08-10 18:00" or "2026-08-10T18:00:00+00:00"'),
    json_out: bool = typer.Option(False, "--json", help="machine-readable json envelope"),
    fake: bool = typer.Option(False, "--fake", help="use in-process fake publishers; for testing"),
) -> None:
    """Enqueue a spec for later. publicador drain publishes due posts."""
    try:
        spec = Spec.from_json_file(spec_path)
    except (OSError, json_mod.JSONDecodeError, KeyError) as e:
        msg = f"could not read spec at {spec_path}: {e}"
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    normalized = _normalize_iso(at)
    if normalized is None:
        msg = f"--at is not a valid ISO 8601 timestamp: {at}"
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    spec.schedule_at = normalized
    _validate_spec_or_exit(spec, json_out)
    _schedule_in_past_or_exit(spec, json_out)
    store = QueueStore()
    try:
        post = Post.new(spec)
        store.add(post)
    except QueueCorrupt as e:
        msg = f"queue was corrupt and has been quarantined: {e.reason}"
        if e.quarantined_to:
            msg += f" (moved to {e.quarantined_to})"
        msg += ". re-run the command; the queue is now fresh."
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    summary = _post_summary(post)
    if json_out:
        _emit_json({"state": "ok", "post": summary})
    else:
        _emit_prose(f"scheduled {post.id} for {spec.schedule_at} across {len(spec.platforms)} platform(s)")
        _emit_prose(f"  run 'publicador drain' (or from cron) to publish when the time comes")
    raise typer.Exit(code=EXIT_OK)


@app.command()
def status(
    json_out: bool = typer.Option(False, "--json", help="machine-readable json envelope"),
    home: Optional[str] = typer.Option(None, "--home", help="override the publicador data dir"),
) -> None:
    """Show the queue and recent results."""
    store = QueueStore(home=default_home(home) if home else None)
    try:
        posts = store.load()
    except QueueCorrupt as e:
        msg = f"queue was corrupt and has been quarantined: {e.reason}"
        if e.quarantined_to:
            msg += f" (moved to {e.quarantined_to})"
        msg += ". the queue is now fresh."
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    if not posts:
        if json_out:
            _emit_json({"state": "empty", "posts": [], "queue_path": str(store.path)})
        else:
            _emit_prose("queue is empty")
        raise typer.Exit(code=EXIT_NOTHING_TO_DO)
    summaries = [_post_summary(p) for p in posts]
    if json_out:
        _emit_json({"state": "ok", "posts": summaries, "queue_path": str(store.path)})
    else:
        order = {
            PostState.PUBLISHING: 0,
            PostState.FAILED: 1,
            PostState.QUEUED: 2,
            PostState.SCHEDULED: 3,
            PostState.PUBLISHED: 4,
        }
        sorted_posts = sorted(posts, key=lambda p: order.get(p.overall_state(), 9))
        for p in sorted_posts:
            s = p.overall_state().value
            sched = f" (scheduled for {p.schedule_at})" if p.schedule_at and s == "scheduled" else ""
            _emit_prose(f"{p.id}  {s}{sched}")
            for a in p.attempts:
                line = f"  {a.platform}: {a.state.value}"
                if a.url:
                    line += f"  {a.url}"
                if a.attempts > 0 and a.state != AttemptState.PUBLISHED:
                    line += f"  attempt {a.attempts}/{MAX_ATTEMPTS}"
                if a.next_retry_at:
                    line += f"  retry at {a.next_retry_at}"
                _emit_prose(line)
                if a.last_error and a.state != AttemptState.PUBLISHED:
                    _emit_prose(f"    {a.last_error}")
    raise typer.Exit(code=EXIT_OK)


@app.command()
def retry(
    post_id: str = typer.Argument(..., help="id of the post to retry"),
    platform: Optional[str] = typer.Option(None, "--platform", help="retry only one platform"),
    json_out: bool = typer.Option(False, "--json", help="machine-readable json envelope"),
    fake: bool = typer.Option(False, "--fake", help="use in-process fake publishers; for testing"),
) -> None:
    """Reset failed attempts on a post and try again right now."""
    store = QueueStore()
    try:
        post = store.get(post_id)
    except QueueCorrupt as e:
        msg = f"queue was corrupt and has been quarantined: {e.reason}"
        if e.quarantined_to:
            msg += f" (moved to {e.quarantined_to})"
        msg += ". re-run the command; the queue is now fresh."
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    if post is None:
        msg = f"no post with id {post_id} in the queue"
        if json_out:
            _emit_json({"state": "problem", "problems": [msg]})
        else:
            _emit_prose(msg)
        raise typer.Exit(code=EXIT_BROKEN)
    targets = [platform] if platform else [a.platform for a in post.attempts]
    reg = _registry(fake)
    now = datetime.now(timezone.utc)
    results = []
    for p in targets:
        attempt = post.attempt_for(p)
        if attempt is None:
            results.append({"platform": p, "ok": False, "error": f"post {post_id} has no platform {p}"})
            continue
        if attempt.state == AttemptState.PUBLISHED:
            results.append({"platform": p, "ok": True, "url": attempt.url, "error": None, "skipped": True})
            continue
        reset_for_retry(post, p)
        r = attempt_platform(post, p, reg, now)
        if r is not None:
            results.append({
                "platform": r.platform,
                "ok": r.ok,
                "url": r.url,
                "error": r.error,
                "is_auth_failure": r.is_auth_failure,
                "is_rate_limited": r.is_rate_limited,
            })
    store.replace(post)
    summary = _post_summary(post)
    if json_out:
        _emit_json({"state": "ok", "post": summary, "results": results})
    else:
        _emit_prose(f"retried {post.id}")
        for a in post.attempts:
            if a.state == AttemptState.PUBLISHED and a.url:
                _emit_prose(f"  {a.platform}: published at {a.url}")
            elif a.state == AttemptState.PUBLISHED:
                _emit_prose(f"  {a.platform}: published")
            else:
                retry_note = ""
                if a.next_retry_at:
                    retry_note = f" (retry {a.attempts}/{MAX_ATTEMPTS} at {a.next_retry_at})"
                _emit_prose(f"  {a.platform}: {a.state.value}{retry_note}")
                if a.last_error:
                    _emit_prose(f"    {a.last_error}")
    raise typer.Exit(code=EXIT_OK)


@app.command()
def drain(
    watch: bool = typer.Option(False, "--watch", help="loop forever, publishing due posts every interval"),
    interval: int = typer.Option(60, "--interval", help="seconds between drain passes when --watch"),
    json_out: bool = typer.Option(False, "--json", help="machine-readable json envelope"),
    fake: bool = typer.Option(False, "--fake", help="use in-process fake publishers; for testing"),
    once: bool = typer.Option(False, "--once", help="run a single drain pass and exit (default)"),
) -> None:
    """Publish every due post in the queue. Run from cron or with --watch."""
    reg = _registry(fake)
    store = QueueStore()

    def _one_pass() -> list[Post]:
        try:
            touched = drain_due(store, reg)
        except QueueCorrupt as e:
            msg = f"queue was corrupt and has been quarantined: {e.reason}"
            if e.quarantined_to:
                msg += f" (moved to {e.quarantined_to})"
            msg += ". re-run the command; the queue is now fresh."
            if json_out:
                _emit_json({"state": "problem", "problems": [msg]})
            else:
                _emit_prose(msg)
            raise typer.Exit(code=EXIT_BROKEN)
        return touched

    if not watch:
        touched = _one_pass()
        if not touched:
            if json_out:
                _emit_json({"state": "empty", "touched": []})
            else:
                _emit_prose("nothing to drain; queue has no due posts")
            raise typer.Exit(code=EXIT_NOTHING_TO_DO)
        summaries = [_post_summary(p) for p in touched]
        if json_out:
            _emit_json({"state": "ok", "touched": summaries})
        else:
            _emit_prose(f"drained {len(touched)} post(s)")
            for p in touched:
                _emit_prose(f"  {p.id}  {p.overall_state().value}")
                for a in p.attempts:
                    if a.state == AttemptState.PUBLISHED and a.url:
                        _emit_prose(f"    {a.platform}: {a.url}")
                    elif a.last_error:
                        _emit_prose(f"    {a.platform}: {a.state.value} - {a.last_error}")
        raise typer.Exit(code=EXIT_OK)

    _emit_prose(f"watching queue at {store.path} every {interval}s (ctrl-c to stop)")
    try:
        while True:
            touched = _one_pass()
            if touched and json_out:
                _emit_json({"state": "ok", "touched": [_post_summary(p) for p in touched]})
            elif touched:
                _emit_prose(f"{now_iso()}  drained {len(touched)} post(s)")
            time.sleep(interval)
    except KeyboardInterrupt:
        _emit_prose("stopped")
        raise typer.Exit(code=EXIT_OK)


def _normalize_iso(s: str) -> Optional[str]:
    candidates = [s, s.replace(" ", "T")]
    for c in candidates:
        try:
            dt = parse_iso(c)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="print version and exit"),
) -> None:
    if version:
        _emit_prose("publicador 0.1.0")
        raise typer.Exit(code=EXIT_OK)
    if ctx.invoked_subcommand is None:
        _emit_prose("publicador: drop a post spec, it publishes everywhere. --help for commands.")
        raise typer.Exit(code=EXIT_OK)


if __name__ == "__main__":
    app()
