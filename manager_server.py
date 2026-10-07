# Title: Miruro Series Manager Server
# Creator: Vernox
# Description: FastAPI server to manage tracked series and run queued downloader jobs with live SSE progress, bulk actions, airing calendar, history stats and AniList search.

import argparse
import asyncio
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Windows consoles default to cp1252; avoid crashing on the unicode glyphs we print.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import db  # noqa: E402

db.init_db()

WEB_DIR = BASE_DIR / "webui"
DOWNLOADER = BASE_DIR / "downloader.py"
LIBRARY_DIR = BASE_DIR / "Downloads"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Only allow inputs that look like IDs / UUIDs / miruro URLs to reach the subprocess.
SAFE_QUERY_RE = re.compile(r"^[A-Za-z0-9_\-:/.?=&%#]{1,300}$")
SAFE_EPS_RE = re.compile(r"^[0-9,\- ]{1,100}$")

PROGRESS_PREFIX = "@@PROGRESS "
PROGRESS_BAR_RE = re.compile(r"^(?:\[.*?\]\s*)?Downloading\s+\[(.*?)\]:")
EP_START_RE = re.compile(r"Processing EP ([0-9.]+) \((\w+)\)")
EP_DONE_RE = re.compile(r"Downloaded EP ([0-9.]+) successfully")
TITLE_RE = re.compile(r"\u25b6\]?\s+(.+?)\s+\(ID: (\d+)\)")

API_TOKEN = os.environ.get("MIRURO_UI_TOKEN", "")
DEFAULT_MAX_JOBS = max(1, int(os.environ.get("MIRURO_MAX_JOBS", "1") or 1))


# ---------------------------------------------------------------------------
# Event bus (thread-safe pub/sub for SSE)
# ---------------------------------------------------------------------------
class EventBus:
    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, data: dict):
        payload = {"event": event, "data": data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # slow consumer: drop rather than block the worker


bus = EventBus()


# ---------------------------------------------------------------------------
# Job runner: queue + subprocess wrapper with streamed output
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, label: str, args: list[str], anime_id: Optional[int] = None):
        self.id = uuid.uuid4().hex[:8]
        self.label = label
        self.args = args
        self.anime_id = anime_id
        self.status = "queued"  # queued | running | done | failed | cancelled
        self.queued_at = time.time()
        self.started: float = self.queued_at  # sort key; reset when the job really starts
        self.ended: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.lines: deque = deque(maxlen=1500)
        self.proc: Optional[subprocess.Popen] = None
        self.position = 0                     # 1-based place in the queue (0 = not queued)
        self.progress: Optional[dict] = None  # {done, total, bps, bytes} of the current episode
        self.current: Optional[dict] = None   # {episode, category, title, anime_id}
        self.eps_done = 0

    def summary(self) -> dict:
        return {
            "id": self.id, "label": self.label, "status": self.status,
            "anime_id": self.anime_id, "started": self.started, "queued_at": self.queued_at,
            "ended": self.ended, "exit_code": self.exit_code, "args": self.args,
            "position": self.position, "progress": self.progress,
            "current": self.current, "eps_done": self.eps_done,
        }


class JobManager:
    def __init__(self, max_concurrent: int = 1):
        self.jobs: dict[str, Job] = {}
        self.max_concurrent = max_concurrent
        self.paused = False
        self._lock = threading.RLock()

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _classify(text: str) -> str:
        if "\u2718" in text or "Traceback" in text or "Error" in text:
            return "error"
        if "\u26a0" in text:
            return "warn"
        if "\u2714" in text:
            return "ok"
        return "info"

    def snapshot(self) -> list[Job]:
        with self._lock:
            return list(self.jobs.values())

    def queue_info(self) -> dict:
        js = self.snapshot()
        return {"max_concurrent": self.max_concurrent, "paused": self.paused,
                "queued": sum(1 for j in js if j.status == "queued"),
                "running": sum(1 for j in js if j.status == "running")}

    def _publish_queue(self):
        bus.publish("queue", self.queue_info())

    def _renumber(self):
        pos = 0
        for j in sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: j.queued_at):
            pos += 1
            if j.position != pos:
                j.position = pos
                bus.publish("job", j.summary())

    # -- output pump ------------------------------------------------------
    def _handle_line(self, job: Job, text: str):
        if text.startswith(PROGRESS_PREFIX):
            try:
                p = json.loads(text[len(PROGRESS_PREFIX):])
            except ValueError:
                return
            job.progress = p
            bus.publish("progress", {"job_id": job.id, **p, "current": job.current})
            return

        m = TITLE_RE.search(text)
        if m:
            job.current = {"title": m.group(1), "anime_id": int(m.group(2)), "episode": None, "category": None}
            bus.publish("job", job.summary())
        m = EP_START_RE.search(text)
        if m:
            cur = dict(job.current or {})
            cur.update({"episode": float(m.group(1)), "category": m.group(2)})
            job.current = cur
            job.progress = None
            bus.publish("job", job.summary())
        if EP_DONE_RE.search(text):
            job.eps_done += 1
            job.progress = None
            bus.publish("job", job.summary())

        entry = {"t": time.time(), "text": text, "level": self._classify(text)}
        # Collapse sequential tqdm-style progress lines (CLI fallback) in history
        m = PROGRESS_BAR_RE.match(text)
        if m and job.lines and PROGRESS_BAR_RE.match(job.lines[-1]["text"]) \
                and f"[{m.group(1)}]:" in job.lines[-1]["text"]:
            job.lines[-1] = entry
        else:
            job.lines.append(entry)
        bus.publish("log", {"job_id": job.id, **entry})

    def _pump(self, job: Job):
        try:
            assert job.proc and job.proc.stdout
            for raw in job.proc.stdout:
                text = ANSI_RE.sub("", raw.rstrip("\r\n"))
                if not text.strip():
                    continue
                self._handle_line(job, text)
        except Exception as e:  # never let the pump thread die silently
            bus.publish("log", {"job_id": job.id, "t": time.time(),
                                "text": f"[server] log pump error: {e}", "level": "error"})
        finally:
            code = job.proc.wait() if job.proc else -1
            job.exit_code = code
            job.ended = time.time()
            job.progress = None
            if job.status != "cancelled":
                job.status = "done" if code == 0 else "failed"
            bus.publish("job", job.summary())
            bus.publish("series_changed", {"reason": f"job:{job.label}"})
            self._dispatch()

    # -- scheduling -------------------------------------------------------
    def _launch(self, job: Job):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", MIRURO_JSON_PROGRESS="1")
        try:
            job.proc = subprocess.Popen(
                [sys.executable, "-u", str(DOWNLOADER), *job.args],
                cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env,
            )
        except OSError as e:
            job.status = "failed"
            job.ended = time.time()
            job.lines.append({"t": time.time(), "text": f"[server] failed to launch downloader: {e}", "level": "error"})
            bus.publish("job", job.summary())
            return
        job.status = "running"
        job.position = 0
        job.started = time.time()
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        bus.publish("job", job.summary())

    def _dispatch(self):
        """Start queued jobs (FIFO) while there is free capacity and the queue isn't paused."""
        with self._lock:
            if not self.paused:
                while True:
                    running = sum(1 for j in self.jobs.values() if j.status == "running")
                    if running >= self.max_concurrent:
                        break
                    nxt = min((j for j in self.jobs.values() if j.status == "queued"),
                              key=lambda j: j.queued_at, default=None)
                    if not nxt:
                        break
                    self._launch(nxt)
            self._renumber()
        self._publish_queue()

    def start(self, label: str, args: list[str], anime_id: Optional[int] = None) -> Job:
        with self._lock:
            for j in self.jobs.values():
                if j.status in ("queued", "running") and j.args == args:
                    raise HTTPException(409, f"An identical job is already {j.status} ({j.id}).")
            job = Job(label, args, anime_id)
            self.jobs[job.id] = job
            # Trim finished history
            finished = [j for j in self.jobs.values() if j.status in ("done", "failed", "cancelled")]
            for old in sorted(finished, key=lambda j: j.queued_at)[:-30]:
                self.jobs.pop(old.id, None)
        bus.publish("job", job.summary())
        self._dispatch()
        return job

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                raise HTTPException(404, "Job not found")
            if job.status == "queued":
                job.status = "cancelled"
                job.ended = time.time()
                job.position = 0
                bus.publish("job", job.summary())
            elif job.status == "running" and job.proc:
                job.status = "cancelled"
                job.proc.terminate()
        self._dispatch()
        return job

    def clear_queue(self) -> int:
        n = 0
        with self._lock:
            for j in self.jobs.values():
                if j.status == "queued":
                    j.status = "cancelled"
                    j.ended = time.time()
                    j.position = 0
                    bus.publish("job", j.summary())
                    n += 1
        self._dispatch()
        return n

    def configure(self, max_concurrent: Optional[int], paused: Optional[bool]):
        with self._lock:
            if max_concurrent is not None:
                self.max_concurrent = max_concurrent
            if paused is not None:
                self.paused = paused
        self._dispatch()


jobs = JobManager(DEFAULT_MAX_JOBS)


# ---------------------------------------------------------------------------
# Auth (optional shared token)
# ---------------------------------------------------------------------------
def require_auth(request: Request):
    if not API_TOKEN:
        return
    supplied = request.headers.get("x-token") or request.query_params.get("token", "")
    if not secrets.compare_digest(supplied, API_TOKEN):
        raise HTTPException(401, "Invalid or missing token")


app = FastAPI(title="Miruro Series Manager", dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class AddSeries(BaseModel):
    query: str = Field(..., description="AniList ID, Miruro UUID, or miruro URL")
    priority: Optional[int] = Field(None, ge=-1000, le=1000)


class PatchSeries(BaseModel):
    priority: Optional[int] = Field(None, ge=-1000, le=1000)
    is_tracked: Optional[bool] = None


class DownloadedBody(BaseModel):
    count: Optional[int] = Field(None, ge=0, le=5000, description="Mark episodes 1..count as downloaded")
    episodes: Optional[list[float]] = Field(None, description="Explicit list of downloaded episodes")


class DownloadBody(BaseModel):
    episodes: Optional[str] = None
    category: Literal["sub", "dub", "raw"] = "dub"
    force: bool = False
    dry_run: bool = False


class BulkBody(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=500)
    action: Literal["priority_set", "priority_add", "track", "untrack", "download", "refresh", "delete"]
    value: Optional[int] = Field(None, ge=-1000, le=1000)
    category: Literal["sub", "dub", "raw"] = "dub"


class QueueConfig(BaseModel):
    max_concurrent: Optional[int] = Field(None, ge=1, le=8)
    paused: Optional[bool] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _series_view(s: dict, failed: dict[int, int]) -> dict:
    aid = s["anime_id"]
    hist = db.get_tracking_history(aid)
    dl = sorted(hist.get("downloaded_eps", []))
    return {
        "anime_id": aid,
        "title": s.get("title"),
        "uuid": s.get("uuid"),
        "url": s.get("url"),
        "poster": s.get("poster_url"),
        "cover": s.get("cover_url"),
        "status": s.get("status"),
        "total_episodes": s.get("total_episodes"),
        "genres": s.get("genres") or [],
        "synopsis": s.get("synopsis"),
        "priority": s.get("priority") or 0,
        "is_tracked": bool(s.get("is_tracked")),
        "downloaded_count": len(dl),
        "downloaded_eps": dl,
        "last_episode_aired": hist.get("last_episode_aired") or 0,
        "last_checked_at": hist.get("last_checked_at"),
        "failed_count": failed.get(aid, 0),
        "next_airing_ep": s.get("next_airing_ep"),
        "next_airing_at": s.get("next_airing_at"),
    }


def _require_series(anime_id: int) -> dict:
    s = db.get_series(anime_id)
    if not s:
        raise HTTPException(404, f"Series {anime_id} not found")
    return s


def _failed_counts() -> dict[int, int]:
    out: dict[int, int] = {}
    for f in db.get_failed_downloads():
        out[f["anime_id"]] = out.get(f["anime_id"], 0) + 1
    return out


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _download_args(anime_id: int, body: DownloadBody) -> list[str]:
    args = ["--link", str(anime_id), "--category", body.category]
    if body.episodes:
        if not SAFE_EPS_RE.match(body.episodes):
            raise HTTPException(400, "Invalid episodes expression")
        args += ["--episodes", body.episodes.replace(" ", "")]
    if body.force:
        args.append("--force")
    if body.dry_run:
        args.append("--dry-run")
    return args


def _refresh_args(s: dict) -> list[str]:
    target = s.get("uuid") if s.get("uuid") and len(str(s["uuid"])) > 20 else str(s["anime_id"])
    return ["--add", str(target)]


# ---------------------------------------------------------------------------
# Routes: series
# ---------------------------------------------------------------------------
@app.get("/api/series")
def list_series(scope: str = "tracked"):
    if scope not in ("tracked", "untracked", "all"):
        raise HTTPException(400, "scope must be tracked|untracked|all")
    rows = db.get_all_series()
    if scope == "tracked":
        rows = [r for r in rows if r.get("is_tracked")]
    elif scope == "untracked":
        rows = [r for r in rows if not r.get("is_tracked")]
    failed = _failed_counts()
    return [_series_view(r, failed) for r in rows]


@app.get("/api/series/{anime_id}")
def get_series(anime_id: int):
    return _series_view(_require_series(anime_id), _failed_counts())


@app.post("/api/series")
def add_series(body: AddSeries):
    q = body.query.strip()
    if not SAFE_QUERY_RE.match(q):
        raise HTTPException(400, "Query contains invalid characters")
    args = ["--add", q]
    if body.priority is not None:
        args += ["--priority", str(body.priority)]
    job = jobs.start(f"add {q[:40]}", args)
    return job.summary()


@app.patch("/api/series/{anime_id}")
def patch_series(anime_id: int, body: PatchSeries):
    s = _require_series(anime_id)
    if body.priority is not None:
        db.set_priority(anime_id, body.priority)
    if body.is_tracked is not None:
        db.set_tracking(anime_id, body.is_tracked,
                        body.priority if body.priority is not None else (s.get("priority") or 0))
    bus.publish("series_changed", {"anime_id": anime_id, "reason": "patch"})
    return _series_view(db.get_series(anime_id), _failed_counts())


@app.delete("/api/series/{anime_id}")
def remove_series(anime_id: int, purge: bool = False):
    s = _require_series(anime_id)
    if purge:
        db.delete_series(anime_id)
    else:
        db.set_tracking(anime_id, False, s.get("priority") or 0)
    bus.publish("series_changed", {"anime_id": anime_id, "reason": "purge" if purge else "untrack"})
    return {"ok": True, "purged": purge}


@app.put("/api/series/{anime_id}/downloaded")
def set_downloaded(anime_id: int, body: DownloadedBody):
    _require_series(anime_id)
    if body.episodes is not None:
        eps = sorted(set(float(e) for e in body.episodes))
    elif body.count is not None:
        eps = [float(i) for i in range(1, body.count + 1)]
    else:
        raise HTTPException(400, "Provide 'count' or 'episodes'")
    db.update_tracking_history(anime_id, downloaded_eps=eps, mark_checked=False)
    bus.publish("series_changed", {"anime_id": anime_id, "reason": "downloaded"})
    return _series_view(db.get_series(anime_id), _failed_counts())


@app.post("/api/series/{anime_id}/download")
def download_series(anime_id: int, body: DownloadBody):
    s = _require_series(anime_id)
    args = _download_args(anime_id, body)
    return jobs.start(f"download {s.get('title', anime_id)}"[:60], args, anime_id).summary()


@app.post("/api/series/{anime_id}/refresh")
def refresh_series(anime_id: int):
    """Re-fetch metadata (title/poster/status) by re-running --add on the stored UUID/ID."""
    s = _require_series(anime_id)
    return jobs.start(f"refresh {s.get('title', anime_id)}"[:60], _refresh_args(s), anime_id).summary()


@app.post("/api/bulk")
def bulk(body: BulkBody):
    """Apply one action to many series. Per-item failures never abort the batch."""
    ok, skipped, errors = 0, 0, []
    for aid in dict.fromkeys(body.ids):  # de-dupe, keep order
        s = db.get_series(aid)
        if not s:
            skipped += 1
            continue
        try:
            a = body.action
            if a in ("priority_set", "priority_add"):
                if body.value is None:
                    raise HTTPException(400, "'value' is required for priority actions")
                cur = s.get("priority") or 0
                new = body.value if a == "priority_set" else max(-1000, min(1000, cur + body.value))
                db.set_priority(aid, new)
            elif a == "track":
                db.set_tracking(aid, True, s.get("priority") or 0)
            elif a == "untrack":
                db.set_tracking(aid, False, s.get("priority") or 0)
            elif a == "delete":
                db.delete_series(aid)
            elif a == "download":
                jobs.start(f"download {s.get('title', aid)}"[:60],
                           ["--link", str(aid), "--category", body.category], aid)
            elif a == "refresh":
                jobs.start(f"refresh {s.get('title', aid)}"[:60], _refresh_args(s), aid)
            ok += 1
        except HTTPException as e:
            if e.status_code == 409:
                skipped += 1  # already queued/running
            else:
                errors.append({"anime_id": aid, "error": e.detail})
        except Exception as e:  # noqa: BLE001 - report and continue
            errors.append({"anime_id": aid, "error": str(e)})
    bus.publish("series_changed", {"reason": f"bulk:{body.action}"})
    return {"ok": ok, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Routes: global operations, failures, stats
# ---------------------------------------------------------------------------
@app.post("/api/auto")
def run_auto(dry_run: bool = False, upgrade_dubs: bool = False):
    args = ["--auto"]
    if dry_run:
        args.append("--dry-run")
    if upgrade_dubs:
        args.append("--upgrade-dubs")
    return jobs.start("auto-scan" + (" (dry)" if dry_run else ""), args).summary()


@app.post("/api/retry-failed")
def retry_failed():
    return jobs.start("retry-failed", ["--retry-failed"]).summary()


@app.get("/api/failed")
def list_failed():
    return db.get_failed_downloads()


@app.delete("/api/failed")
def clear_failed():
    db.clear_all_failed_downloads()
    bus.publish("series_changed", {"reason": "failed-cleared"})
    return {"ok": True}


@app.get("/api/stats")
def stats():
    rows = db.get_all_series()
    tracked = [r for r in rows if r.get("is_tracked")]
    failed = db.get_failed_downloads()
    total_dl = sum(len(db.get_tracking_history(r["anime_id"]).get("downloaded_eps", [])) for r in tracked)
    qi = jobs.queue_info()
    return {
        "total_series": len(rows),
        "tracked": len(tracked),
        "episodes_downloaded": total_dl,
        "failed": len(failed),
        "running_jobs": qi["running"],
        "queued_jobs": qi["queued"],
    }


# ---------------------------------------------------------------------------
# Routes: queue
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def get_queue():
    return jobs.queue_info()


@app.put("/api/queue")
def configure_queue(body: QueueConfig):
    jobs.configure(body.max_concurrent, body.paused)
    return jobs.queue_info()


@app.delete("/api/queue")
def clear_queue():
    return {"cancelled": jobs.clear_queue()}


# ---------------------------------------------------------------------------
# Routes: airing calendar
# ---------------------------------------------------------------------------
@app.get("/api/calendar")
def calendar(days: int = 28):
    """Upcoming episodes for tracked series. Week 1 comes from Miruro's next_airing;
    later weeks are projected (+7d per episode) and flagged `projected`."""
    days = max(1, min(days, 90))
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    items, unscheduled = [], 0
    for s in db.get_tracked_series():
        base = _parse_iso(s.get("next_airing_at"))
        ep0 = s.get("next_airing_ep")
        if not base or ep0 is None:
            if (s.get("status") or "").upper() == "RELEASING":
                unscheduled += 1
            continue
        downloaded = set(db.get_tracking_history(s["anime_id"]).get("downloaded_eps", []))
        total = s.get("total_episodes") or None
        k = 0
        while True:
            at = base + timedelta(days=7 * k)
            ep = float(ep0) + k
            if at > horizon or (total and ep > total) or k > 60:
                break
            if at >= now - timedelta(hours=12):  # keep very recent releases visible
                items.append({
                    "anime_id": s["anime_id"], "title": s.get("title"), "poster": s.get("poster_url"),
                    "episode": ep, "air_at": at.isoformat(), "projected": k > 0,
                    "downloaded": ep in downloaded, "priority": s.get("priority") or 0,
                })
            k += 1
    items.sort(key=lambda i: i["air_at"])
    return {"now": now.isoformat(), "items": items, "unscheduled": unscheduled}


@app.post("/api/calendar/refresh")
def refresh_calendar():
    return jobs.start("refresh airing schedule", ["--refresh-airing"]).summary()


# ---------------------------------------------------------------------------
# Routes: history & detailed stats
# ---------------------------------------------------------------------------
_lib_cache: dict = {"t": 0.0, "bytes": 0}


def _library_bytes() -> int:
    """Total size of the Downloads folder (cached 60s; a recursive walk can be slow)."""
    if time.time() - _lib_cache["t"] < 60:
        return _lib_cache["bytes"]
    total = 0
    stack = [str(LIBRARY_DIR)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    _lib_cache.update(t=time.time(), bytes=total)
    return total


@app.get("/api/history")
def history(limit: int = 100, offset: int = 0):
    return db.get_download_history(limit, offset)


@app.get("/api/stats/detail")
async def stats_detail(days: int = 30):
    data = await asyncio.to_thread(db.get_download_stats, days)
    try:
        du = shutil.disk_usage(LIBRARY_DIR if LIBRARY_DIR.exists() else BASE_DIR)
        data["disk"] = {"total": du.total, "used": du.used, "free": du.free,
                        "library_bytes": await asyncio.to_thread(_library_bytes)}
    except OSError:
        data["disk"] = None
    return data


# ---------------------------------------------------------------------------
# Routes: AniList search (no Cloudflare involved, so it is safe to run in-process)
# ---------------------------------------------------------------------------
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 12) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id format status episodes seasonYear averageScore
      title { romaji english }
      coverImage { large }
      nextAiringEpisode { episode airingAt }
    }
  }
}
"""
_search_cache: dict[str, tuple[float, list]] = {}


def _anilist_search(q: str) -> list[dict]:
    hit = _search_cache.get(q.lower())
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    req = urllib.request.Request(
        ANILIST_URL,
        data=json.dumps({"query": ANILIST_QUERY, "variables": {"search": q}}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "miruro-manager/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise HTTPException(429, "AniList rate limit hit - wait a few seconds and retry.")
        raise HTTPException(502, f"AniList error: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise HTTPException(502, f"Could not reach AniList: {e}")
    out = []
    for m in (payload.get("data") or {}).get("Page", {}).get("media") or []:
        t = m.get("title") or {}
        nxt = m.get("nextAiringEpisode") or {}
        out.append({
            "anilist_id": m["id"],
            "title": t.get("english") or t.get("romaji"),
            "romaji": t.get("romaji"),
            "poster": (m.get("coverImage") or {}).get("large"),
            "format": m.get("format"), "status": m.get("status"), "episodes": m.get("episodes"),
            "year": m.get("seasonYear"), "score": m.get("averageScore"),
            "next_episode": nxt.get("episode"), "next_airing_at": nxt.get("airingAt"),
        })
    _search_cache[q.lower()] = (time.time(), out)
    if len(_search_cache) > 200:
        _search_cache.pop(next(iter(_search_cache)))
    return out


@app.get("/api/search")
async def search(q: str):
    q = q.strip()
    if len(q) < 2 or len(q) > 100:
        raise HTTPException(400, "Query must be 2-100 characters")
    results = await asyncio.to_thread(_anilist_search, q)
    out = []
    for r in results:
        s = db.get_series(r["anilist_id"])
        out.append({**r, "in_db": bool(s), "tracked": bool(s and s.get("is_tracked"))})
    return out


# ---------------------------------------------------------------------------
# Routes: jobs + SSE
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
def list_jobs():
    return [j.summary() for j in sorted(jobs.snapshot(), key=lambda j: -j.queued_at)]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    j = jobs.jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return {**j.summary(), "lines": list(j.lines)}


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    return jobs.cancel(job_id).summary()


@app.get("/api/events")
async def events():
    q = bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield f"event: hello\ndata: {json.dumps({'t': time.time()})}\n\n"
            while True:
                try:
                    item = await asyncio.to_thread(q.get, True, 15)
                    yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    return JSONResponse({"detail": f"Internal error: {exc}"}, status_code=500)


def main():
    ap = argparse.ArgumentParser(description="Miruro Series Manager web UI")
    ap.add_argument("--host", default="0.0.0.0", help="Bind address (default: all interfaces, reachable on your LAN; use 127.0.0.1 for local only)")
    ap.add_argument("--port", type=int, default=5300)
    ap.add_argument("--max-jobs", type=int, default=None, help="Max downloader jobs running at once (default 1, or MIRURO_MAX_JOBS)")
    args = ap.parse_args()
    if args.max_jobs:
        jobs.max_concurrent = max(1, min(args.max_jobs, 8))
    if args.host not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
        print("\u26a0 Binding to a non-loopback address WITHOUT MIRURO_UI_TOKEN set. "
              "Anyone on your network can control this server. Set MIRURO_UI_TOKEN to require a token.")
    print(f"\u25c6 Miruro Manager listening on {args.host}:{args.port}  (max parallel jobs: {jobs.max_concurrent})")
    if args.host in ("0.0.0.0", "::"):
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
                lan_ip = s.getsockname()[0]
        except OSError:
            lan_ip = "<this-machine-ip>"
        print(f"  \u2192 From this machine:   http://127.0.0.1:{args.port}")
        print(f"  \u2192 From other devices:  http://{lan_ip}:{args.port}   (NOT http://0.0.0.0:{args.port})")
    else:
        print(f"  \u2192 http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
