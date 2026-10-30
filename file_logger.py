# ==============================================================================
# Title: Detailed Diagnostic File Logger
# Creator: Vernox
# Description: Thread-safe diagnostic file logger for Miruro CLI that logs all
#              HTTP requests, Referer/Origin headers, playlists, and individual segment URLs to /logs.
# ==============================================================================

import atexit
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import threading
import urllib.parse

_LOG_FILE = None
_LOG_PATH: Path | None = None
_LOCK = threading.Lock()
_ENABLED = False
_ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip_ansi(text: str) -> str:
    """Remove terminal color codes from text for clean log output."""
    if not isinstance(text, str):
        text = str(text)
    return _ANSI_REGEX.sub('', text)


def init_logger(log_arg: bool | str = True, logs_dir: Path | None = None) -> Path | None:
    """
    Initialize the file logger in the specified logs directory.
    If log_arg is False or None, file logging is disabled.
    If log_arg is True, generates a timestamped log file in logs_dir.
    If log_arg is a string, uses it as the log filename or path.
    """
    global _LOG_FILE, _LOG_PATH, _ENABLED

    if not log_arg:
        _ENABLED = False
        return None

    with _LOCK:
        if _LOG_FILE is not None:
            return _LOG_PATH

        if logs_dir is None:
            logs_dir = Path(__file__).resolve().parent / "logs"

        logs_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(log_arg, str) and log_arg.strip() and log_arg is not True:
            custom_name = log_arg.strip()
            if not custom_name.lower().endswith((".log", ".txt")):
                custom_name += ".log"
            # Support both relative filenames and explicit paths
            custom_path = Path(custom_name)
            if custom_path.is_absolute() or len(custom_path.parts) > 1:
                target_path = custom_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                target_path = logs_dir / custom_name
        else:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_path = logs_dir / f"miruro_{ts_str}.log"

        try:
            _LOG_FILE = open(target_path, "a", encoding="utf-8", buffering=1)
            _LOG_PATH = target_path
            _ENABLED = True
        except Exception as e:
            sys.stderr.write(f"[WARN] Failed to open log file {target_path}: {e}\n")
            _ENABLED = False
            return None

        banner = [
            "=" * 90,
            f"Miruro CLI - Detailed Diagnostic Session Log",
            f"Started at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}",
            f"Log path   : {target_path}",
            f"Command    : {' '.join(sys.argv)}",
            f"Platform   : {sys.platform} | Python: {sys.version.split()[0]}",
            "=" * 90,
            "",
        ]
        _LOG_FILE.write("\n".join(banner) + "\n")
        _LOG_FILE.flush()

        atexit.register(close_logger)
        return _LOG_PATH


def is_enabled() -> bool:
    """Return True if diagnostic file logging is currently active."""
    return _ENABLED and _LOG_FILE is not None


def get_log_path() -> Path | None:
    """Return the active log file Path, or None if disabled."""
    return _LOG_PATH


def log(category: str, message: str, details: dict | list | str | None = None) -> None:
    """
    Thread-safe write of a categorized log entry to the log file.
    """
    if not _ENABLED or _LOG_FILE is None:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    cat_clean = _strip_ansi(category).upper()
    msg_clean = _strip_ansi(message)

    lines = [f"[{now_str}] [{cat_clean:<11}] {msg_clean}"]

    if details is not None:
        if isinstance(details, dict):
            for k, v in details.items():
                lines.append(f"    {k}: {v}")
        elif isinstance(details, list):
            for item in details:
                lines.append(f"    - {item}")
        else:
            det_clean = _strip_ansi(str(details))
            for subline in det_clean.splitlines():
                lines.append(f"    {subline}")

    entry = "\n".join(lines) + "\n"

    with _LOCK:
        try:
            if _LOG_FILE and not _LOG_FILE.closed:
                _LOG_FILE.write(entry)
                _LOG_FILE.flush()
        except Exception:
            pass


def log_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    status_code: int | None = None,
    duration_ms: float | None = None,
    byte_size: int | None = None,
    error: str | None = None,
    category: str = "HTTP",
) -> None:
    """Log details of an outbound HTTP request including Referer and Origin."""
    if not _ENABLED:
        return

    parts = [f"{method} {url}"]
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    if byte_size is not None:
        parts.append(f"Size: {byte_size} bytes")
    if duration_ms is not None:
        parts.append(f"Time: {duration_ms:.1f}ms")
    if error:
        parts.append(f"Error: {error}")

    details = {}
    if headers:
        # Prioritize key navigation headers for easy diagnostic reading
        for priority_key in ("Referer", "Origin", "User-Agent", "Host", "Cookie"):
            for hk, hv in headers.items():
                if hk.lower() == priority_key.lower():
                    # Mask cookie if very long
                    if priority_key == "Cookie" and len(str(hv)) > 80:
                        details[hk] = str(hv)[:80] + "... [truncated]"
                    else:
                        details[hk] = hv
        # Add remaining headers
        for hk, hv in headers.items():
            if hk.lower() not in ("referer", "origin", "user-agent", "host", "cookie"):
                details[hk] = hv

    log(category, " | ".join(parts), details=details if details else None)


def log_playlist(
    playlist_url: str,
    headers: dict | None = None,
    is_master: bool = False,
    resolution: int = 0,
    segment_urls: list[str] | None = None,
    key_info: dict | None = None,
) -> None:
    """Log HLS playlist resolution and list all discovered segment URLs."""
    if not _ENABLED:
        return

    ptype = "Master Playlist" if is_master else "Media Playlist"
    ref = ""
    if headers and "Referer" in headers:
        ref = f" | Referer: {headers['Referer']}"

    summary = f"{ptype} Resolved: {playlist_url}{ref}"
    if resolution:
        summary += f" | Target/Best Res: {resolution}p"

    details = {}
    if headers:
        details["Playlist Headers"] = headers
    if key_info:
        details["Decryption Key Info"] = {
            "method": key_info.get("method"),
            "has_key": bool(key_info.get("key")),
            "key_bytes": len(key_info.get("key", b"")),
            "iv": key_info.get("iv").hex() if key_info.get("iv") else "None",
        }
    if segment_urls is not None:
        details["Discovered Segments Count"] = len(segment_urls)

    log("PLAYLIST", summary, details=details if details else None)

    # Log each individual segment URL extracted from the playlist
    if segment_urls:
        log("PLAYLIST_SEGS", f"Discovered {len(segment_urls)} segment URLs in {playlist_url}:")
        with _LOCK:
            try:
                if _LOG_FILE and not _LOG_FILE.closed:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    for idx, seg_url in enumerate(segment_urls):
                        _LOG_FILE.write(f"[{now_str}] [SEG_LINK   ]   #{idx:04d} -> {seg_url}\n")
                    _LOG_FILE.flush()
            except Exception:
                pass


def log_segment(
    index: int,
    url: str,
    attempt: int = 1,
    status_code: int | None = None,
    size: int | None = None,
    duration_ms: float | None = None,
    referer: str | None = None,
    headers: dict | None = None,
    error: str | None = None,
    decrypted: bool = False,
) -> None:
    """
    Log an individual segment download attempt, including full URL, Referer,
    response status code, payload size, duration, and error details.
    """
    if not _ENABLED:
        return

    status_str = f"HTTP {status_code}" if status_code is not None else "STATUS:???"
    size_str = f"{size} B" if size is not None else "0 B"
    time_str = f"{duration_ms:.1f}ms" if duration_ms is not None else "N/A"
    aes_str = " | Decrypted: AES-128" if decrypted else ""

    summary = (
        f"Segment #{index:04d} (Attempt {attempt}) | {status_str} | "
        f"Size: {size_str} | Time: {time_str}{aes_str}"
    )

    details = {
        "Segment URL": url,
        "Referer": referer or (headers.get("Referer") if headers else "None"),
    }
    if error:
        details["Error"] = error
    if headers and "Origin" in headers:
        details["Origin"] = headers["Origin"]

    log("SEGMENT", summary, details=details)


def log_server_race(
    candidates: list,
    winner: dict | None = None,
    target_res: int = 0,
) -> None:
    """Log the outcome of parallel server racing."""
    if not _ENABLED:
        return

    cand_names = [c[2] for c in candidates]
    log("SERVER_RACE", f"Probing {len(candidates)} candidate stream(s) for target resolution {target_res}p:", cand_names)
    if winner:
        log("SERVER_RACE", f"Server Race WINNER: {winner.get('server_name')}", {
            "m3u8_url": winner.get("m3u8_url"),
            "headers": winner.get("headers"),
            "segments_count": len(winner.get("segments", [])),
            "probe_size": f"{winner.get('probe_size', 0)} bytes",
        })
    else:
        log("SERVER_RACE", "All candidate servers failed probe or were unreachable.")


def log_ffmpeg(
    cmd: list[str],
    returncode: int,
    duration_sec: float,
    output_path: str,
    error_output: str | None = None,
) -> None:
    """Log FFmpeg remuxing execution, duration, and exit status."""
    if not _ENABLED:
        return

    status = "SUCCESS" if returncode == 0 else f"FAILED (exit code {returncode})"
    details = {
        "Command": " ".join(cmd),
        "Output File": output_path,
        "Duration": f"{duration_sec:.2f}s",
    }
    if error_output:
        details["FFmpeg Error"] = error_output.strip()

    log("FFMPEG", f"Remux to MP4: {status}", details=details)


def close_logger() -> None:
    """Flush and safely close the diagnostic log file."""
    global _LOG_FILE, _ENABLED
    with _LOCK:
        if _LOG_FILE is not None:
            try:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                _LOG_FILE.write(f"\n[{now_str}] [SYSTEM     ] Diagnostic session closed.\n")
                _LOG_FILE.write("=" * 90 + "\n")
                _LOG_FILE.flush()
                _LOG_FILE.close()
            except Exception:
                pass
            finally:
                _LOG_FILE = None
                _ENABLED = False
