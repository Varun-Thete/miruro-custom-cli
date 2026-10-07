# Title: AniVault SQLite Database Layer
# Creator: Vernox
# Description: Schema, CRUD, and migration utilities for the AniVault SQLite database.

import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent / "miruro.db"


# ── Connection ────────────────────────────────────────────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory  = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    anime_id       INTEGER PRIMARY KEY,
    uuid           TEXT,
    url            TEXT,
    title          TEXT NOT NULL,
    title_japanese TEXT,
    title_kanji    TEXT,
    synonyms       TEXT DEFAULT '[]',
    synopsis       TEXT,
    type           TEXT,
    total_episodes INTEGER,
    status         TEXT,
    duration       TEXT,
    aired_from     TEXT,
    aired_to       TEXT,
    season         TEXT,
    poster_url     TEXT,
    poster_local   TEXT,
    cover_url      TEXT,
    cover_local    TEXT,
    preview_url    TEXT,
    local_folder   TEXT,
    genres         TEXT DEFAULT '[]',
    themes         TEXT DEFAULT '[]',
    demographics   TEXT DEFAULT '[]',
    studios        TEXT DEFAULT '[]',
    ext_links      TEXT DEFAULT '{}',
    relations      TEXT DEFAULT '[]',
    date_added     TEXT,
    last_updated   TEXT,
    is_tracked     INTEGER DEFAULT 0,
    priority       INTEGER DEFAULT 0,
    meta_status    TEXT DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS episodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_id       INTEGER NOT NULL REFERENCES series(anime_id) ON DELETE CASCADE,
    number         REAL    NOT NULL,
    session        TEXT,
    snapshot_url   TEXT,
    snapshot_local TEXT,
    duration       TEXT,
    audio          TEXT DEFAULT 'jpn',
    title          TEXT,
    created_at     TEXT,
    UNIQUE(anime_id, number)
);

CREATE INDEX IF NOT EXISTS idx_ep_anime    ON episodes(anime_id);
CREATE INDEX IF NOT EXISTS idx_ep_created  ON episodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_s_status    ON series(status);
CREATE INDEX IF NOT EXISTS idx_s_season    ON series(season);
CREATE INDEX IF NOT EXISTS idx_s_folder    ON series(local_folder);

CREATE TABLE IF NOT EXISTS tracking_history (
    anime_id           INTEGER PRIMARY KEY REFERENCES series(anime_id) ON DELETE CASCADE,
    last_episode_aired REAL,
    downloaded_eps     TEXT DEFAULT '[]',
    last_checked_at    TEXT
);

CREATE TABLE IF NOT EXISTS watch_history (
    anime_id     INTEGER NOT NULL REFERENCES series(anime_id) ON DELETE CASCADE,
    episode      REAL NOT NULL,
    timestamp    REAL DEFAULT 0,
    duration     REAL DEFAULT 0,
    is_completed INTEGER DEFAULT 0,
    last_watched TEXT,
    UNIQUE(anime_id, episode)
);

CREATE TABLE IF NOT EXISTS failed_downloads (
    anime_id     INTEGER NOT NULL REFERENCES series(anime_id) ON DELETE CASCADE,
    episode      REAL NOT NULL,
    category     TEXT DEFAULT 'dub',
    error        TEXT,
    failed_at    TEXT,
    retry_count  INTEGER DEFAULT 0,
    UNIQUE(anime_id, episode, category)
);
"""

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_id     INTEGER NOT NULL,
    title        TEXT,
    episode      REAL NOT NULL,
    category     TEXT,
    server       TEXT,
    size_bytes   INTEGER DEFAULT 0,
    duration_s   REAL DEFAULT 0,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dh_completed ON download_history(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dh_anime     ON download_history(anime_id);
"""

def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(HISTORY_SCHEMA)
        # Attempt to add columns to existing DB silently without wiping data
        _migrations = [
            "ALTER TABLE series ADD COLUMN is_tracked INTEGER DEFAULT 0",
            "ALTER TABLE series ADD COLUMN priority INTEGER DEFAULT 0",
            "ALTER TABLE series ADD COLUMN meta_status TEXT DEFAULT 'none'",
            "ALTER TABLE series ADD COLUMN watchlist_status TEXT DEFAULT NULL",
            "ALTER TABLE series ADD COLUMN next_airing_ep REAL DEFAULT NULL",
            "ALTER TABLE series ADD COLUMN next_airing_at TEXT DEFAULT NULL",
        ]
        for sql in _migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # Column already exists


# ── JSON helpers ──────────────────────────────────────────────────────────────
def _jl(val):
    """Deserialise a JSON list column, always returning a list."""
    if isinstance(val, list):
        return val
    try:
        return json.loads(val or "[]")
    except Exception:
        return []

def _jd(val):
    """Deserialise a JSON dict column, always returning a dict."""
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val or "{}")
    except Exception:
        return {}

def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict with JSON fields decoded."""
    if row is None:
        return None
    d = dict(row)
    for key in ("synonyms", "genres", "themes", "demographics", "studios", "relations"):
        d[key] = _jl(d.get(key))
    d["ext_links"] = _jd(d.get("ext_links"))
    return d


# ── Upsert ────────────────────────────────────────────────────────────────────
def upsert_series(data: dict):
    """Insert or replace a full series record."""
    with db() as conn:
        conn.execute("""
            INSERT INTO series (
                anime_id, uuid, url, title, title_japanese, title_kanji,
                synonyms, synopsis, type, total_episodes, status, duration,
                aired_from, aired_to, season, poster_url, poster_local,
                cover_url, cover_local, preview_url, local_folder,
                genres, themes, demographics, studios, ext_links, relations,
                date_added, last_updated
            ) VALUES (
                :anime_id, :uuid, :url, :title, :title_japanese, :title_kanji,
                :synonyms, :synopsis, :type, :total_episodes, :status, :duration,
                :aired_from, :aired_to, :season, :poster_url, :poster_local,
                :cover_url, :cover_local, :preview_url, :local_folder,
                :genres, :themes, :demographics, :studios, :ext_links, :relations,
                :date_added, :last_updated
            )
            ON CONFLICT(anime_id) DO UPDATE SET
                uuid=excluded.uuid, url=excluded.url, title=excluded.title,
                title_japanese=excluded.title_japanese, title_kanji=excluded.title_kanji,
                synonyms=excluded.synonyms, synopsis=excluded.synopsis, type=excluded.type,
                total_episodes=excluded.total_episodes, status=excluded.status,
                duration=excluded.duration, aired_from=excluded.aired_from,
                aired_to=excluded.aired_to, season=excluded.season,
                poster_url=excluded.poster_url, poster_local=excluded.poster_local,
                cover_url=excluded.cover_url, cover_local=excluded.cover_local,
                preview_url=excluded.preview_url, local_folder=excluded.local_folder,
                genres=excluded.genres, themes=excluded.themes,
                demographics=excluded.demographics, studios=excluded.studios,
                ext_links=excluded.ext_links, relations=excluded.relations,
                last_updated=excluded.last_updated
        """, {
            "anime_id":       data.get("anime_id"),
            "uuid":           data.get("uuid"),
            "url":            data.get("url"),
            "title":          data.get("title", "Unknown"),
            "title_japanese": data.get("japanese_title") or data.get("title_japanese"),
            "title_kanji":    data.get("japanese_kanji") or data.get("title_kanji"),
            "synonyms":       json.dumps(_jl(data.get("synonyms")), ensure_ascii=False),
            "synopsis":       data.get("synopsis"),
            "type":           data.get("type"),
            "total_episodes": data.get("total_episodes"),
            "status":         data.get("status"),
            "duration":       data.get("duration"),
            "aired_from":     data.get("aired_from"),
            "aired_to":       data.get("aired_to"),
            "season":         data.get("season"),
            "poster_url":     data.get("poster"),           # JSON uses 'poster'
            "poster_local":   data.get("poster_local"),
            "cover_url":      data.get("cover"),            # JSON uses 'cover'
            "cover_local":    data.get("cover_local"),
            "preview_url":    data.get("preview_url"),
            "local_folder":   data.get("local_folder"),
            "genres":         json.dumps(_jl(data.get("genres")), ensure_ascii=False),
            "themes":         json.dumps(_jl(data.get("themes")), ensure_ascii=False),
            "demographics":   json.dumps(_jl(data.get("demographics")), ensure_ascii=False),
            "studios":        json.dumps(_jl(data.get("studios")), ensure_ascii=False),
            "ext_links":      json.dumps(_jd(data.get("external_links") or data.get("ext_links")), ensure_ascii=False),
            "relations":      json.dumps(_jl(data.get("relations")), ensure_ascii=False),
            "date_added":     data.get("date_added", datetime.now().strftime("%Y-%m-%d")),
            "last_updated":   data.get("last_updated", datetime.now().strftime("%Y-%m-%d")),
        })
        
        # Optionally set tracking immediately on upsert if flag present
        if "is_tracked" in data:
            conn.execute("UPDATE series SET is_tracked=?, priority=? WHERE anime_id=?", 
                         (int(data["is_tracked"]), int(data.get("priority", 0)), data["anime_id"]))


def upsert_episode(anime_id: int, ep: dict):
    with db() as conn:
        conn.execute("""
            INSERT INTO episodes (
                anime_id, number, session, snapshot_url, snapshot_local,
                duration, audio, title, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(anime_id, number) DO UPDATE SET
                session=excluded.session, snapshot_url=excluded.snapshot_url,
                snapshot_local=excluded.snapshot_local, duration=excluded.duration,
                audio=excluded.audio, title=excluded.title, created_at=excluded.created_at
        """, (
            anime_id,
            ep.get("number", ep.get("episode", 0)),
            ep.get("session"),
            ep.get("snapshot") or ep.get("snapshot_url"),
            ep.get("snapshot_local"),
            ep.get("duration"),
            ep.get("audio", "jpn"),
            ep.get("title"),
            ep.get("created_at"),
        ))


def set_poster_local(anime_id: int, rel_path: str):
    with db() as conn:
        conn.execute("UPDATE series SET poster_local=? WHERE anime_id=?", (rel_path, anime_id))

def set_cover_local(anime_id: int, rel_path: str):
    with db() as conn:
        conn.execute("UPDATE series SET cover_local=? WHERE anime_id=?", (rel_path, anime_id))

def set_local_folder(anime_id: int, folder: str):
    with db() as conn:
        conn.execute("UPDATE series SET local_folder=? WHERE anime_id=?", (folder, anime_id))

def set_snapshot_local(anime_id: int, ep_num: float, rel_path: str):
    with db() as conn:
        conn.execute(
            "UPDATE episodes SET snapshot_local=? WHERE anime_id=? AND number=?",
            (rel_path, anime_id, ep_num)
        )

def set_meta_status(anime_id: int, status: str):
    """Set metadata scraping status: 'none', 'partial', or 'complete'."""
    with db() as conn:
        conn.execute("UPDATE series SET meta_status=? WHERE anime_id=?", (status, anime_id))

def get_snap_stats(anime_id: int) -> dict:
    """Return snapshot download stats for a series."""
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM episodes WHERE anime_id=?", (anime_id,)).fetchone()[0]
        downloaded = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE anime_id=? AND snapshot_local IS NOT NULL AND snapshot_local != ''",
            (anime_id,)
        ).fetchone()[0]
        return {"total": total, "downloaded": downloaded}


# ── Reads ─────────────────────────────────────────────────────────────────────
def get_series(anime_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM series WHERE anime_id=?", (anime_id,)).fetchone()
        return _row_to_dict(row) if row else None

def delete_series(anime_id: int):
    """Deletes a series and all its associated episodes/history from the database."""
    with db() as conn:
        conn.execute("DELETE FROM series WHERE anime_id=?", (anime_id,))
        # Cascading deletes will handle episodes and tracking_history if schema is setup,
        # but just in case, we do it manually too.
        conn.execute("DELETE FROM episodes WHERE anime_id=?", (anime_id,))
        conn.execute("DELETE FROM tracking_history WHERE anime_id=?", (anime_id,))
        conn.execute("DELETE FROM watch_history WHERE anime_id=?", (anime_id,))
        conn.commit()

def get_series_by_uuid(uuid: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM series WHERE uuid=?", (uuid,)).fetchone()
        return _row_to_dict(row)

def get_series_by_folder(folder: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM series WHERE local_folder=?", (folder,)).fetchone()
        return _row_to_dict(row)

def get_all_series() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM series ORDER BY title COLLATE NOCASE").fetchall()
        return [_row_to_dict(r) for r in rows]

def get_episodes(anime_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes WHERE anime_id=? ORDER BY number", (anime_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_recent_episodes(limit: int = 60) -> list[dict]:
    """Return recent episodes (those with a created_at date), newest first."""
    with db() as conn:
        rows = conn.execute("""
            SELECT e.*, s.title AS series_title, s.anime_id AS s_anime_id,
                   s.poster_local, s.poster_url, s.local_folder
            FROM episodes e
            JOIN series s ON s.anime_id = e.anime_id
            WHERE e.created_at IS NOT NULL AND e.created_at != ''
            ORDER BY e.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

def search(query: str) -> list[dict]:
    q = f"%{query.lower()}%"
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM series
            WHERE lower(title) LIKE ?
               OR lower(title_japanese) LIKE ?
               OR lower(synonyms) LIKE ?
            ORDER BY title COLLATE NOCASE
            LIMIT 20
        """, (q, q, q)).fetchall()
        return [_row_to_dict(r) for r in rows]

def get_by_field_value(field: str, value: str) -> list[dict]:
    """
    Generic filter: series where a JSON-array text field contains `value`.
    field must be one of: genres, themes, demographics, studios
    """
    allowed = {"genres", "themes", "demographics", "studios", "status", "type", "season"}
    if field not in allowed:
        return []
    with db() as conn:
        if field in ("status", "type", "season"):
            rows = conn.execute(
                f"SELECT * FROM series WHERE {field}=? ORDER BY title COLLATE NOCASE", (value,)
            ).fetchall()
        else:
            rows = conn.execute(
                f'SELECT * FROM series WHERE json_valid({field}) AND EXISTS '
                f'(SELECT 1 FROM json_each({field}) WHERE value=?) '
                f'ORDER BY title COLLATE NOCASE',
                (value,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

def get_distinct_values(field: str) -> list[str]:
    """
    Return sorted unique values for a JSON array field (genres, themes, etc.)
    or plain text field (status, type, season).
    """
    allowed = {"status", "type", "season", "genres", "themes", "demographics", "studios", "relations"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' is not allowed for distinct value queries")
    plain_fields = {"status", "type", "season"}
    if field in plain_fields:
        with db() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT {field} FROM series WHERE {field} IS NOT NULL AND {field}!='' ORDER BY {field}"
            ).fetchall()
            return [r[0] for r in rows]
    # JSON array fields
    with db() as conn:
        rows = conn.execute(
            f"SELECT json_each.value FROM series, json_each(series.{field}) "
            f"WHERE json_valid(series.{field}) GROUP BY json_each.value ORDER BY json_each.value"
        ).fetchall()
        return [r[0] for r in rows]

def count_by_field_value(field: str) -> list[tuple]:
    """Return [(value, count)] sorted by count DESC — for browse pages."""
    allowed = {"status", "type", "season", "genres", "themes", "demographics", "studios", "relations"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' is not allowed for count queries")
    plain_fields = {"status", "type", "season"}
    if field in plain_fields:
        with db() as conn:
            rows = conn.execute(
                f"SELECT {field}, COUNT(*) as cnt FROM series "
                f"WHERE {field} IS NOT NULL AND {field}!='' "
                f"GROUP BY {field} ORDER BY cnt DESC"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
    with db() as conn:
        rows = conn.execute(
            f"SELECT json_each.value, COUNT(*) as cnt "
            f"FROM series, json_each(series.{field}) "
            f"WHERE json_valid(series.{field}) "
            f"GROUP BY json_each.value ORDER BY cnt DESC"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


# ── Migration from JSON ───────────────────────────────────────────────────────
def migrate_from_json(json_path: Path) -> int:
    """Import series-metadata.json into the SQLite DB. Returns count imported."""
    if not json_path.exists():
        return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    count = 0
    for key, entry in data.items():
        try:
            upsert_series(entry)
            for ep in entry.get("episodes", []):
                upsert_episode(entry["anime_id"], ep)
            count += 1
        except Exception as e:
            print(f"  [migrate] Failed for {key}: {e}")
            
    return count


# ── Tracking (CLI / Plugin) ───────────────────────────────────────────────────
def get_tracked_series() -> list[dict]:
    """Return all series currently marked for active tracking."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM series WHERE is_tracked=1 ORDER BY priority DESC, title COLLATE NOCASE").fetchall()
        return [_row_to_dict(r) for r in rows]

def set_tracking(anime_id: int, is_tracked: bool, priority: int = 0):
    """Enable or disable tracking for a specific series."""
    with db() as conn:
        conn.execute("UPDATE series SET is_tracked=?, priority=? WHERE anime_id=?", 
                     (1 if is_tracked else 0, priority, anime_id))

def set_priority(anime_id: int, priority: int):
    """Update priority for a specific series."""
    with db() as conn:
        conn.execute("UPDATE series SET priority=? WHERE anime_id=?", (priority, anime_id))



# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST_STATUSES = ["planning", "watching", "completed", "on_hold", "dropped", "favorite"]

def set_watchlist_status(anime_id: int, status: str | None):
    """Set or clear watchlist status. Pass None to remove from all lists."""
    if status and status not in WATCHLIST_STATUSES:
        raise ValueError(f"Invalid watchlist status: {status}")
    with db() as conn:
        conn.execute("UPDATE series SET watchlist_status=? WHERE anime_id=?", (status, anime_id))

def get_watchlist(status: str = None) -> list[dict]:
    """Return series in the watchlist, optionally filtered by status."""
    with db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM series WHERE watchlist_status=? ORDER BY title COLLATE NOCASE",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM series WHERE watchlist_status IS NOT NULL ORDER BY title COLLATE NOCASE"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

def get_all_watchlist_statuses() -> dict:
    """Return {status: count} for each watchlist status."""
    with db() as conn:
        rows = conn.execute(
            "SELECT watchlist_status, COUNT(*) as cnt FROM series "
            "WHERE watchlist_status IS NOT NULL GROUP BY watchlist_status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


# ── Download History Management ───────────────────────────────────────────────
def clear_download_history(anime_id: int):
    """Reset downloaded_eps to empty for a specific series."""
    with db() as conn:
        conn.execute(
            "UPDATE tracking_history SET downloaded_eps='[]' WHERE anime_id=?",
            (anime_id,)
        )

def clear_all_untracked_download_history():
    """Reset downloaded_eps to empty for all series NOT in the queue."""
    with db() as conn:
        conn.execute(
            "UPDATE tracking_history SET downloaded_eps='[]' "
            "WHERE anime_id IN (SELECT anime_id FROM series WHERE is_tracked=0)"
        )

def get_tracking_history(anime_id: int) -> dict:
    """Gets the tracking history object, automatically generating a blank one if missing."""
    with db() as conn:
        row = conn.execute("SELECT * FROM tracking_history WHERE anime_id=?", (anime_id,)).fetchone()
        if row:
            d = dict(row)
            d["downloaded_eps"] = _jl(d["downloaded_eps"])
            return d
        return {"anime_id": anime_id, "last_episode_aired": 0, "downloaded_eps": [], "last_checked_at": None}

def update_tracking_history(anime_id: int, last_episode_aired: float = None, 
                            downloaded_eps: list[float] = None, mark_checked: bool = True):
    """Update tracking metadata. Pass None to preserve existing values."""
    with db() as conn:
        # Get existing first
        row = conn.execute("SELECT * FROM tracking_history WHERE anime_id=?", (anime_id,)).fetchone()
        if not row:
            conn.execute("""
                INSERT INTO tracking_history (anime_id, last_episode_aired, downloaded_eps, last_checked_at)
                VALUES (?, ?, ?, ?)
            """, (anime_id, 
                  last_episode_aired or 0, 
                  json.dumps(downloaded_eps or []),
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S") if mark_checked else None))
        else:
            curr_last = row["last_episode_aired"]
            curr_dl = _jl(row["downloaded_eps"])
            
            new_last = last_episode_aired if last_episode_aired is not None else curr_last
            new_dl = downloaded_eps if downloaded_eps is not None else curr_dl
            new_checked = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if mark_checked else row["last_checked_at"]
            
            conn.execute("""
                UPDATE tracking_history SET 
                    last_episode_aired=?, downloaded_eps=?, last_checked_at=?
                WHERE anime_id=?
            """, (new_last, json.dumps(new_dl), new_checked, anime_id))

# ── Playback Watch History ───────────────────────────────────────────────────
def update_watch_progress(anime_id: int, episode: float, timestamp: float, duration: float, is_completed: bool):
    """Upsert playback history for a specific episode."""
    with db() as conn:
        conn.execute("""
            INSERT INTO watch_history (anime_id, episode, timestamp, duration, is_completed, last_watched)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(anime_id, episode) DO UPDATE SET 
                timestamp=excluded.timestamp,
                duration=excluded.duration,
                is_completed=excluded.is_completed,
                last_watched=excluded.last_watched
        """, (anime_id, episode, timestamp, duration, 1 if is_completed else 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

def delete_watch_history(anime_id: int):
    """Delete all watch history entries for a series."""
    with db() as conn:
        conn.execute("DELETE FROM watch_history WHERE anime_id=?", (anime_id,))

def get_watch_history(anime_id: int) -> dict:
    """Return a map of {episode: {timestamp, duration, is_completed, last_watched}} for an anime."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM watch_history WHERE anime_id=?", (anime_id,)).fetchall()
        return {r["episode"]: _row_to_dict(r) for r in rows}

def get_last_watched(anime_id: int) -> dict | None:
    """Get the most recently interacted with episode to allow 'Resume Playback' from the main screen."""
    with db() as conn:
        row = conn.execute("""
            SELECT * FROM watch_history 
            WHERE anime_id=? 
            ORDER BY last_watched DESC LIMIT 1
        """, (anime_id,)).fetchone()
        return _row_to_dict(row) if row else None

def get_continue_watching(limit: int = 15) -> list[dict]:
    """Get the most recently interacted with series across the whole library."""
    with db() as conn:
        rows = conn.execute("""
            SELECT s.*, w.episode as resume_ep, w.timestamp, w.duration as watch_duration, w.last_watched
            FROM watch_history w
            JOIN series s ON s.anime_id = w.anime_id
            WHERE w.is_completed = 0 AND w.timestamp > 0
            GROUP BY s.anime_id
            HAVING w.last_watched = MAX(w.last_watched)
            ORDER BY w.last_watched DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]


# ── Failed Downloads ──────────────────────────────────────────────────────────
def record_failed_download(anime_id: int, episode: float, category: str = "dub", error: str = ""):
    """Record or update a failed download attempt."""
    with db() as conn:
        conn.execute("""
            INSERT INTO failed_downloads (anime_id, episode, category, error, failed_at, retry_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(anime_id, episode, category) DO UPDATE SET
                error=excluded.error,
                failed_at=excluded.failed_at,
                retry_count=retry_count + 1
        """, (anime_id, episode, category, error, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def clear_failed_download(anime_id: int, episode: float, category: str = "dub"):
    """Remove a failed download record after successful retry."""
    with db() as conn:
        conn.execute(
            "DELETE FROM failed_downloads WHERE anime_id=? AND episode=? AND category=?",
            (anime_id, episode, category)
        )


def get_failed_downloads() -> list[dict]:
    """Get all failed downloads with series titles."""
    with db() as conn:
        rows = conn.execute("""
            SELECT f.*, s.title
            FROM failed_downloads f
            JOIN series s ON s.anime_id = f.anime_id
            ORDER BY f.failed_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def clear_all_failed_downloads():
    """Purge all failed download records."""
    with db() as conn:
        conn.execute("DELETE FROM failed_downloads")



# ── Airing calendar ───────────────────────────────────────────────────────────
def set_next_airing(anime_id: int, episode, air_at):
    """Store the next airing episode (air_at = ISO-8601 UTC string) for a series. None clears it."""
    with db() as conn:
        conn.execute("UPDATE series SET next_airing_ep=?, next_airing_at=? WHERE anime_id=?",
                     (episode, air_at, anime_id))


# ── Download history & stats ──────────────────────────────────────────────────
def record_download(anime_id: int, title: str, episode: float, category: str = "",
                    server: str = "", size_bytes: int = 0, duration_s: float = 0.0):
    """Append a completed download to the history log."""
    with db() as conn:
        conn.execute("""
            INSERT INTO download_history
                (anime_id, title, episode, category, server, size_bytes, duration_s, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (anime_id, title, episode, category, server, int(size_bytes or 0),
              float(duration_s or 0), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def get_download_history(limit: int = 100, offset: int = 0) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM download_history ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 1000)), max(0, offset))).fetchall()
        return [dict(r) for r in rows]


def get_download_stats(days: int = 30) -> dict:
    """Aggregate totals, per-day series, category split and top series."""
    days = max(1, min(days, 365))
    with db() as conn:
        tot = conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes,
                   COALESCE(SUM(duration_s),0) AS secs FROM download_history
        """).fetchone()
        per_day = conn.execute("""
            SELECT substr(completed_at,1,10) AS day, COUNT(*) AS episodes,
                   COALESCE(SUM(size_bytes),0) AS bytes
            FROM download_history
            WHERE completed_at >= datetime('now','localtime', ?)
            GROUP BY day ORDER BY day
        """, (f"-{days} days",)).fetchall()
        by_cat = conn.execute("""
            SELECT COALESCE(NULLIF(category,''),'?') AS category, COUNT(*) AS episodes
            FROM download_history GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()
        top = conn.execute("""
            SELECT anime_id, MAX(title) AS title, COUNT(*) AS episodes,
                   COALESCE(SUM(size_bytes),0) AS bytes
            FROM download_history GROUP BY anime_id ORDER BY episodes DESC LIMIT 8
        """).fetchall()
        week = conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes FROM download_history
            WHERE completed_at >= datetime('now','localtime','-7 days')
        """).fetchone()
    secs = tot["secs"] or 0
    return {
        "total_episodes": tot["n"], "total_bytes": tot["bytes"], "total_seconds": secs,
        "avg_speed_bps": (tot["bytes"] / secs) if secs > 0 else 0,
        "week_episodes": week["n"], "week_bytes": week["bytes"],
        "per_day": [dict(r) for r in per_day],
        "by_category": [dict(r) for r in by_cat],
        "top_series": [dict(r) for r in top],
    }