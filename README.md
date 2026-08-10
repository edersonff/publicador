# publicador

Drop a post spec, publicador queues, retries, schedules, and dispatches it to every platform.

## Input

A spec.json path as the first argument. The spec has:

```json
{
  "caption": "ship it",
  "media": "reel.mp4",
  "platforms": ["instagram", "tiktok", "youtube"],
  "schedule_at": "2026-08-10T18:00:00+00:00"
}
```

`caption` is the post text. `media` is a path to a file (relative paths resolve from the spec file's folder). `platforms` is the list of targets. `schedule_at` is optional; if present, the post waits until then.

## Output

Prose: one line per action. `--json` returns a machine envelope on every command.

```
$ publicador publish spec.json
queued abc12345 for 3 platform(s)
  instagram: published at https://instagram.com/p/abc
  tiktok: published at https://tiktok.com/@you/video/123
  youtube: failed  attempt 1/3  retry at 2026-08-10T12:01:00+00:00
    uploader exited 1
```

## Run it

```
publicador publish spec.json              enqueue and attempt every platform now
publicador publish spec.json --json       machine-readable envelope
publicador schedule spec.json --at "2026-08-10 18:00"   enqueue for later
publicador status                         show the queue, grouped by state
publicador retry abc12345                 reset failures and try again
publicador retry abc12345 --platform instagram   retry one platform
publicador drain                          publish every due post (run from cron)
publicador drain --watch --interval 60    loop forever, publishing when due
publicador --version
```

Every command takes `--json`.

## Platforms and publishers

Each platform is a publisher. A publisher fetches cookies via `cm` (from edersonff/cookie-manager), writes them to a Netscape file, and calls an uploader command that does the real upload.

Three platforms ship today, each with a default uploader command:

| platform | cm domain  | default uploader | env override                    |
|----------|------------|------------------|---------------------------------|
| instagram| instagram.com | `ig-uploader`  | `PUBLICADOR_INSTAGRAM_UPLOADER` |
| tiktok   | tiktok.com  | `tt-uploader`   | `PUBLICADOR_TIKTOK_UPLOADER`    |
| youtube  | youtube.com | `yt-uploader`   | `PUBLICADOR_YOUTUBE_UPLOADER`   |

The uploader contract (what your `<platform>-uploader` binary must do):

- receive: `--media <path> --caption <text> --cookies <netscape-file>`
- on success: print the published URL as the first line of stdout, exit 0
- on failure: non-zero exit, stderr explains what broke
- stderr containing `auth`, `401`, `cookie`, `session`, `login`, `unauthorized` → classified as auth failure
- stderr containing `rate limit`, `429`, `too many requests` → classified as rate limited

If the uploader is missing, publicador names it and the env var to set. Uploader modules (`ig-uploader`, `tt-uploader`, `yt-uploader`) are separate sheol modules; publicador is the orchestrator, not the uploader.

## Retry

Each platform attempt: max 3 tries, exponential backoff. Base 60s, doubling, capped at 600s. Failures classify as `failed`, `auth_failed`, or `rate_limited`; all three retry until the cap. A published platform is never retried. `publicador retry <id>` resets failures to zero and tries immediately.

## Queue

`~/.local/share/publicador/queue.json` (override with `PUBLICADOR_HOME` or `XDG_DATA_HOME`). Atomic writes (tmp + rename + fsync). If the file corrupts, publicador quarantines it to `queue.json.corrupt-<timestamp>` and starts fresh, telling you where the old one went.

## Defaults

- max attempts: 3
- backoff base: 60s, max: 600s
- caption limits: instagram 2200, tiktok 2200, youtube 5000 (checked before any upload is attempted)
- drain watch interval: 60s
- post id: 8-char base36, typeable

No config file. Every setting above is a constant in `publicador/scheduler.py` or `publicador/models.py`. The uploader command per platform is the one thing overridable, and only via env var.

## What breaks

Measured from the actual binary, not guessed. Exit codes: 0 ok, 1 broken, 2 nothing to do.

- **cm not installed** → every platform fails with `cm binary not found on PATH; install edersonff/cookie-manager (needed for instagram.com)`. The post is queued with state `failed`, retry scheduled. Exit 0 (the publish command itself ran; the platforms failed).
- **cm returns no cookies** → `no cookies returned for instagram.com; sign in to the site in your browser, then: cm get instagram.com --no-cache`. Same retry path.
- **uploader command missing** → `uploader command not found for instagram: 'ig-uploader' is not on PATH. Set PUBLICADOR_INSTAGRAM_UPLOADER=<path> or install a instagram uploader.` Exit 0, post queued as failed.
- **uploader exits non-zero** → last line of stderr captured, classified by markers above. Exit 0, retry scheduled.
- **media file missing** → `media file not found: /path/to/reel.mp4`. Exit 1, nothing queued.
- **media is a folder** → `media path is a folder, not a file: /path/to/dir`. Exit 1.
- **caption too long** → `caption is 2201 chars, instagram limit is 2200`. Exit 1, per-platform breakdown if multiple exceed.
- **empty platforms list** → `platforms list is missing or empty`. Exit 1.
- **bad schedule_at format** → `schedule_at is not a valid ISO 8601 timestamp: not a date`. Exit 1.
- **schedule in the past** → `schedule time 2020-01-01T00:00:00+00:00 is in the past; drop the --at flag to publish now`. Exit 1.
- **unknown platform** → `unknown platform 'mastodon'; known: instagram, tiktok, youtube`. The post is queued; that platform's attempt fails immediately with this message. Others proceed.
- **queue corrupted** → `queue was corrupt and has been quarantined: <reason> (moved to <path>). re-run the command; the queue is now fresh.` Exit 1 on the command that hit it; subsequent commands see an empty queue.
- **retry on missing post** → `no post with id nope in the queue`. Exit 1.
- **status on empty queue** → `queue is empty`. Exit 2.
- **drain with nothing due** → `nothing to drain; queue has no due posts`. Exit 2.
- **publish crashes mid-upload** → the attempt stays in `publishing` state. `drain` will not retry it (publishing means "in progress"). Run `publicador retry <id>` to reset it. A future version will detect stale publishing attempts automatically.
- **uploader runs longer than 600s** → publicador kills it with `uploader timed out after 600s for <platform>`. For very large uploads (long YouTube videos on slow links), raise the uploader's own timeout if it has one; publicador's cap covers IG (90s max), TT (10 min max), and most YT uploads.

## Needs

`edersonff/cookie-manager` on the shelf. publicador calls `cm get <domain> --as=yt-dlp` for each platform's cookies.

## Install

```
pip install -e .
```

Or, from the shelf:

```
sheol pull edersonff/publicador
pip install -e ./edersonff/publicador
```
