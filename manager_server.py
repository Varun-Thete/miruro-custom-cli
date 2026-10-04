# Title: Miruro Series Manager Server
# Creator: Vernox
# Description: FastAPI web server to manage tracked series (priority, add/remove, episode counts) and run downloader jobs with live SSE log streaming.

import argparse
import asyncio
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

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
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Only allow inputs that look like IDs / UUIDs / miruro URLs to reach the subprocess.
SAFE_QUERY_RE = re.compile(r"^[A-Za-z0-9_\-:/.?=&%#]{1,300}$")
SAFE_EPS_RE = re.compile(r"^[0-9,\- ]{1,100}$")

API_TOKEN = os.environ.get("MIRURO_UI_TOKEN", "")


# ──────────────────────────────────────────────────────────────────────────────
# Event bus (thread-safe pub/sub for SSE)
# ──────────────────────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────────────────────
# Job runner: wraps downloader.py as subprocesses with streamed output
# ──────────────────────────────────────────────────────────────────────────────
class Job:
    def __init__(self, label: str, args: list[str], anime_id: Optional[int] = None):
        self.id = uuid.uuid4().hex[:8]
        self.label = label
        self.args = args
        self.anime_id = anime_id
        self.status = "running"
        self.started = time.time()
        self.ended: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.lines: deque = deque(maxlen=1500)
        self.proc: Optional[subprocess.Popen] = None

    def summary(self) -> dict:
        return {
            "id": self.id, "label": self.label, "status": self.status,
            "anime_id": self.anime_id, "started": self.started,
            "ended": self.ended, "exit_code": self.exit_code,
            "args": self.args,
        }


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _classify(self, text: str) -> str:
        if "✘" in text or "Traceback" in text or "Error" in text:
            return "error"
        if "⚠" in text:
            return "warn"
        if "✔" in text:
            return "ok"
        return "info"

    def _pump(self, job: Job):
        try:
            assert job.proc and job.proc.stdout
            for raw in job.proc.stdout:
                text = ANSI_RE.sub("", raw.rstrip("\r\n"))
                if not text.strip():
                    continue
                entry = {"t": time.time(), "text": text, "level": self._classify(text)}
                job.lines.append(entry)
                bus.publish("log", {"job_id": job.id, **entry})
        except Exception as e:  # never let the pump thread die silently
            bus.publish("log", {"job_id": job.id, "t": time.time(),
                                "text": f"[server] log pump error: {e}", "level": "error"})
        finally:
            code = job.proc.wait() if job.proc else -1
            job.exit_code = code
            job.ended = time.time()
            if job.status != "cancelled":
                job.status = "done" if code == 0 else "failed"
            bus.publish("job", job.summary())
            bus.publish("series_changed", {"reason": f"job:{job.label}"})

    def start(self, label: str, args: list[str], anime_id: Optional[int] = None) -> Job:
        with self._lock:
            for j in self.jobs.values():
                if j.status == "running" and j.args == args:
                    raise HTTPException(409, f"An identical job is already running ({j.id}).")
            job = Job(label, args, anime_id)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            try:
                job.proc = subprocess.Popen(
                    [sys.executable, "-u", str(DOWNLOADER), *args],
                    cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=env,
                )
            except OSError as e:
                raise HTTPException(500, f"Failed to launch downloader: {e}")
            self.jobs[job.id] = job
            # Trim finished history
            finished = [j for j in self.jobs.values() if j.status != "running"]
            for old in sorted(finished, key=lambda j: j.started)[:-30]:
                self.jobs.pop(old.id, None)
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        bus.publish("job", job.summary())
        return job

    def cancel(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.status == "running" and job.proc:
            job.status = "cancelled"
            job.proc.terminate()
        return job


jobs = JobManager()


# ──────────────────────────────────────────────────────────────────────────────
# Auth (optional shared token)
# ──────────────────────────────────────────────────────────────────────────────
def require_auth(request: Request):
    if not API_TOKEN:
        return
    supplied = request.headers.get("x-token") or request.query_params.get("token", "")
    if not secrets.compare_digest(supplied, API_TOKEN):
        raise HTTPException(401, "Invalid or missing token")


app = FastAPI(title="Miruro Series Manager", dependencies=[Depends(require_auth)])


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────
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
    category: str = "dub"
    force: bool = False
    dry_run: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────────────────────
# Routes: series
# ──────────────────────────────────────────────────────────────────────────────
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
    args = ["--link", str(anime_id), "--category", body.category]
    if body.episodes:
        if not SAFE_EPS_RE.match(body.episodes):
            raise HTTPException(400, "Invalid episodes expression")
        args += ["--episodes", body.episodes.replace(" ", "")]
    if body.force:
        args.append("--force")
    if body.dry_run:
        args.append("--dry-run")
    return jobs.start(f"download {s.get('title', anime_id)}"[:60], args, anime_id).summary()


@app.post("/api/series/{anime_id}/refresh")
def refresh_series(anime_id: int):
    """Re-fetch metadata (title/poster/status) by re-running --add on the stored UUID/ID."""
    s = _require_series(anime_id)
    target = s.get("uuid") if s.get("uuid") and len(str(s["uuid"])) > 20 else str(anime_id)
    return jobs.start(f"refresh {s.get('title', anime_id)}"[:60], ["--add", str(target)], anime_id).summary()


# ──────────────────────────────────────────────────────────────────────────────
# Routes: global operations, failures, stats
# ──────────────────────────────────────────────────────────────────────────────
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
    return {
        "total_series": len(rows),
        "tracked": len(tracked),
        "episodes_downloaded": total_dl,
        "failed": len(failed),
        "running_jobs": sum(1 for j in jobs.jobs.values() if j.status == "running"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Routes: jobs + SSE
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/jobs")
def list_jobs():
    return [j.summary() for j in sorted(jobs.jobs.values(), key=lambda j: -j.started)]


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


# ──────────────────────────────────────────────────────────────────────────────
# Static UI
# ──────────────────────────────────────────────────────────────────────────────
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
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
        print("⚠ Binding to a non-loopback address WITHOUT MIRURO_UI_TOKEN set. "
              "Anyone on your network can control this server. Set MIRURO_UI_TOKEN to require a token.")
    print(f"◆ Miruro Manager listening on {args.host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
                lan_ip = s.getsockname()[0]
        except OSError:
            lan_ip = "<this-machine-ip>"
        print(f"  → From this machine:   http://127.0.0.1:{args.port}")
        print(f"  → From other devices:  http://{lan_ip}:{args.port}   (NOT http://0.0.0.0:{args.port})")
    else:
        print(f"  → http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
