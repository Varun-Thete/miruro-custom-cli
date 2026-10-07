# ==============================================================================
# Title: Production Masked HLS Downloader with Parallel Server Racing
# Creator: Vernox
# Description: Multithreaded HLS ripper with probe-based server selection,
#              AES-128 decryption, live tqdm progress, and fail-fast abort.
# ==============================================================================

import json
import os
import re
import sys

# Fix Windows console unicode issues
if sys.stdout and hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, 'reconfigure') and sys.stderr.encoding != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"

import time
import random
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from curl_cffi import requests
from config import cfg

# When launched by the web manager, emit structured progress instead of a tqdm bar.
_JSON_PROGRESS = os.environ.get("MIRURO_JSON_PROGRESS") == "1"
_progress_last = 0.0


def _emit_progress(done, total, stats, force=False):
    """Print a throttled `@@PROGRESS {json}` line (max ~4/s) for the manager server to parse."""
    global _progress_last
    now = time.monotonic()
    if not force and now - _progress_last < 0.25:
        return
    _progress_last = now
    try:
        elapsed = max(time.time() - stats.start_time, 1e-6)
        bps = stats.total_bytes / elapsed
        print("@@PROGRESS " + json.dumps({"done": done, "total": total,
              "bytes": stats.total_bytes, "bps": int(bps)}), flush=True)
    except Exception:
        pass
from pathlib import Path

_global_sem = threading.Semaphore(cfg.max_outbound_connections)

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

MAX_RETRIES = cfg.max_retries
CHUNK_TIMEOUT = cfg.chunk_timeout
PROBE_TIMEOUT = cfg.probe_timeout
pbar = None
VERBOSE = False  # Set to True for debug logs

def set_debug(enabled: bool = True):
    global VERBOSE
    VERBOSE = enabled
    cfg.debug = enabled

def is_debug() -> bool:
    return VERBOSE or getattr(cfg, "debug", False)

# ---------------------------------------------------------------------------
# Network Connectivity Watchdog & Auto-Pause
# ---------------------------------------------------------------------------
_net_lock = threading.Lock()
_net_offline_event = threading.Event()
_net_offline_event.set()  # Initial state: assumed online

def is_online(timeout: float = cfg.dns_check_timeout) -> bool:
    """Check connectivity via raw TCP socket ping to Cloudflare/Google DNS without DNS lookup."""
    import socket
    for host, port in cfg.dns_check_hosts:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.close()
            return True
        except Exception:
            continue
    return False

def wait_for_internet(interval: float = cfg.net_recovery_interval):
    """
    Thread-safe pause that blocks until the internet connection is restored.
    Coordinates across all worker threads so only one message is displayed.
    """
    if is_online():
        _net_offline_event.set()
        return

    _net_offline_event.clear()

    with _net_lock:
        if is_online():
            _net_offline_event.set()
            return

        global pbar
        log_msg(f"\n    {C.YELLOW}⚠{C.RESET} {C.BOLD}Internet connection lost! Pausing downloader...{C.RESET}")

        waited = 0.0
        while not is_online():
            time.sleep(interval)
            waited += interval
            print(f"\r    {C.GRAY}[*] Waiting for network to recover... ({waited:.0f}s elapsed){C.RESET}", end="", flush=True)

        print(f"\n    {C.GREEN}✔{C.RESET} {C.BOLD}Internet connection restored! Resuming operations...{C.RESET}\n")
        _net_offline_event.set()


# ---------------------------------------------------------------------------
# Server Health Cache  (shared across episodes in the same process)
# ---------------------------------------------------------------------------
_health_cache = {}          # key: url_prefix -> {"status": "alive"|"dead", "ts": float}
_health_lock = threading.Lock()
HEALTH_TTL = cfg.health_ttl           # blacklist dead servers for 5 minutes


def _cache_key(url: str) -> str:
    """Extract scheme + host as a cache key."""
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def mark_server(url: str, alive: bool):
    # Only blacklist server if internet is actually working!
    # If the machine is offline, the failure is NOT the server's fault.
    if not alive and not is_online():
        return
    key = _cache_key(url)
    with _health_lock:
        _health_cache[key] = {"status": "alive" if alive else "dead", "ts": time.time()}


def is_server_dead(url: str) -> bool:
    key = _cache_key(url)
    with _health_lock:
        entry = _health_cache.get(key)
        if not entry:
            return False
        if entry["status"] == "dead" and (time.time() - entry["ts"]) < HEALTH_TTL:
            return True
        return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log_msg(msg: str, debug: bool = False):
    """Print without corrupting the tqdm progress bar."""
    if debug and not is_debug():
        return
    global pbar
    if TQDM_AVAILABLE and pbar is not None:
        tqdm.write(msg)
    else:
        print(msg)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
class DownloadStats:
    """Thread-safe speed and byte counter."""
    def __init__(self):
        self._lock = threading.Lock()
        self.total_bytes = 0
        self.start_time = time.time()

    def add_bytes(self, n: int):
        with self._lock:
            self.total_bytes += n

    def get_speed_string(self) -> str:
        with self._lock:
            elapsed = time.time() - self.start_time
            if elapsed <= 0:
                return "0.00 MB/s"
            speed = self.total_bytes / elapsed
            mb = self.total_bytes / (1024 * 1024)
            if speed >= 1024 * 1024:
                return f"{speed / (1024*1024):.2f} MB/s [{mb:.1f} MB]"
            return f"{speed / 1024:.1f} KB/s [{mb:.1f} MB]"


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
def get_session(headers: dict | None = None, pool_size: int = 8) -> requests.Session:
    session = requests.Session(impersonate=cfg.tls_impersonate)
    defaults = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive",
        "User-Agent": cfg.user_agent,
    }
    if headers:
        if "Referer" in headers and "Origin" not in headers:
            headers["Origin"] = headers["Referer"].rstrip("/")
        defaults.update(headers)
    session.headers.update(defaults)
    return session


# ---------------------------------------------------------------------------
# Playlist parsing
# ---------------------------------------------------------------------------
def fetch_playlist_segments(
    playlist_url: str,
    session: requests.Session,
    target_res: int = 0,
) -> tuple[list[str], dict | None]:
    """Resolve master -> media playlist and return (segment_urls, key_info)."""
    res = session.get(playlist_url, timeout=PROBE_TIMEOUT)
    res.raise_for_status()

    lines = res.text.splitlines()
    base_url = playlist_url.rsplit("/", 1)[0]
    parsed_orig = urllib.parse.urlparse(playlist_url)

    # ---- Master playlist? -------------------------------------------------
    is_master = any("EXT-X-STREAM-INF" in l for l in lines)
    if is_master:
        best_res = 0
        best_url = ""
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and "RESOLUTION=" in line:
                h_str = line.split("RESOLUTION=")[1].split(",")[0]
                height = int(h_str.split("x")[1]) if "x" in h_str else int(h_str)
                pick = False
                if target_res > 0:
                    pick = height <= target_res and height > best_res
                else:
                    pick = height > best_res
                if pick and i + 1 < len(lines):
                    best_res = height
                    best_url = lines[i + 1].strip()
        if not best_url:
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                    best_url = lines[i + 1].strip()
                    break
        if best_url:
            if not best_url.startswith("http"):
                pb = urllib.parse.urlparse(best_url)
                if not pb.query and parsed_orig.query:
                    best_url = f"{best_url}?{parsed_orig.query}"
                best_url = urllib.parse.urljoin(playlist_url, best_url)
            log_msg(f"    {C.BLUE}◆{C.RESET} Master playlist detected. Selected resolution: {best_res}p", debug=True)
            return fetch_playlist_segments(best_url, session, target_res)

    # ---- Media playlist ---------------------------------------------------
    segments: list[str] = []
    key_info: dict | None = None

    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            mm = re.search(r"METHOD=([^,\s]+)", line)
            um = re.search(r'URI="([^"]+)"', line)
            im = re.search(r"IV=([^,\s]+)", line)
            if mm and um:
                ku = um.group(1)
                if not ku.startswith("http"):
                    ku = urllib.parse.urljoin(playlist_url, ku)
                log_msg(f"    {C.BLUE}◆{C.RESET} Fetching decryption key...", debug=True)
                kr = session.get(ku, timeout=PROBE_TIMEOUT)
                kr.raise_for_status()
                iv = None
                if im:
                    iv_s = im.group(1)
                    if iv_s.lower().startswith("0x"):
                        iv = bytes.fromhex(iv_s[2:])
                key_info = {"method": mm.group(1), "key": kr.content, "iv": iv}
                log_msg(f"    {C.GREEN}✔{C.RESET} Decryption key fetched (Method: {key_info['method']})", debug=True)

        elif line and not line.startswith("#"):
            pl = urllib.parse.urlparse(line)
            if not pl.query and parsed_orig.query:
                line = f"{line}?{parsed_orig.query}"
            if not line.startswith("http"):
                line = urllib.parse.urljoin(playlist_url, line)
            segments.append(line)

    return segments, key_info


# ---------------------------------------------------------------------------
# Probe: lightweight first-chunk test
# ---------------------------------------------------------------------------
def probe_server(
    m3u8_url: str,
    headers: dict | None,
    server_name: str,
    target_res: int,
    subtitles: list,
) -> dict | None:
    """Fetch playlist + first chunk from a server. Returns probe result or None."""
    if is_server_dead(m3u8_url):
        return None
    try:
        session = get_session(headers)
        segments, key_info = fetch_playlist_segments(m3u8_url, session, target_res)
        if not segments:
            mark_server(m3u8_url, False)
            return None

        # Download the very first chunk as a health check
        with _global_sem:
            r = session.get(segments[0], timeout=PROBE_TIMEOUT)
        if r.status_code != 200:
            mark_server(m3u8_url, False)
            return None

        probe_data = r.content

        # Decrypt probe chunk if AES-128
        if key_info and key_info.get("method") == "AES-128":
            try:
                from Crypto.Cipher import AES
                iv = key_info.get("iv") or (0).to_bytes(16, byteorder="big")
                probe_data = AES.new(key_info["key"], AES.MODE_CBC, iv=iv).decrypt(probe_data)
            except ImportError:
                pass  # Will be caught during full download

        # Strip fake image header before MPEG-TS sync byte
        sync = probe_data.find(b"\x47")
        if sync != -1:
            probe_data = probe_data[sync:]

        mark_server(m3u8_url, True)
        return {
            "m3u8_url": m3u8_url,
            "headers": headers,
            "server_name": server_name,
            "session": session,
            "segments": segments,
            "key_info": key_info,
            "probe_chunk": (0, probe_data),   # already decrypted + stripped
            "probe_size": len(r.content),
            "subtitles": subtitles,
        }
    except Exception as e:
        log_msg(f"    {C.GRAY}[DEBUG] Probe failed for {server_name}: {e}{C.RESET}", debug=True)
        mark_server(m3u8_url, False)
        return None


def race_servers(
    stream_list: list[tuple[str, dict, str, list]],
    target_res: int = 0,
) -> dict | None:
    """
    Probe ALL servers in parallel. Return the first one that responds with
    a valid playlist + first chunk.  Only blacklist servers that actually
    FAILED the probe (not ones that were just slower).
    """
    if not stream_list:
        return None

    # Ensure network connection before probing
    wait_for_internet()

    # Filter out known-dead servers
    candidates = [(u, h, s, subs) for u, h, s, subs in stream_list if not is_server_dead(u)]
    if not candidates:
        # TTL might have expired, try all
        candidates = stream_list

    log_msg(f"    {C.BLUE}◆{C.RESET} Racing {len(candidates)} server(s)...", debug=True)

    winner = None
    with ThreadPoolExecutor(max_workers=min(len(candidates), cfg.server_race_workers)) as pool:
        futures = {
            pool.submit(probe_server, url, hdrs, name, target_res, subs): name
            for url, hdrs, name, subs in candidates
        }
        for future in as_completed(futures):
            result = future.result()
            if result and winner is None:
                winner = result
                log_msg(f"    {C.GREEN}✔{C.RESET} Server WON: {result['server_name']} "
                        f"(probe chunk: {result['probe_size']} bytes)", debug=True)
                # Don't wait for remaining probes — but don't blacklist them either
                break

    if not winner:
        log_msg(f"    {C.YELLOW}\u26a0{C.RESET} All servers failed the probe.")
    return winner


# ---------------------------------------------------------------------------
# Chunk downloader with failure-threshold abort + thread-safe sessions
# ---------------------------------------------------------------------------
_FAIL_THRESHOLD = 5   # abort after this many permanent chunk failures

class _FailCounter:
    """Thread-safe counter for permanently failed chunks."""
    def __init__(self, threshold: int):
        self._lock = threading.Lock()
        self._count = 0
        self._threshold = threshold
        self.abort = threading.Event()
        self.first_error = None

    def record_failure(self, error_msg: str):
        with self._lock:
            self._count += 1
            if self.first_error is None:
                self.first_error = error_msg
            if self._count >= self._threshold:
                self.abort.set()


# Thread-local storage for curl_cffi sessions (NOT thread-safe per handle)
_thread_local = threading.local()

def _get_thread_session(headers: dict | None) -> requests.Session:
    """Return a per-thread curl_cffi session (created once per thread)."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = get_session(headers)
    return _thread_local.session


_rl_lock = threading.Lock()
_rl_until = 0.0

def download_segment_with_retry(
    index: int,
    url: str,
    headers: dict | None,
    stats: DownloadStats,
    key_info: dict | None,
    fail_counter: _FailCounter,
) -> tuple[int, bytes]:
    global _rl_until
    session = _get_thread_session(headers)
    backoff = 1.5
    attempt = 1
    max_429_retries = cfg.max_429_retries
    attempt_429 = 0

    while attempt <= MAX_RETRIES:
        if fail_counter.abort.is_set():
            raise RuntimeError(f"Aborted (first error: {fail_counter.first_error})")
        
        # Check if another thread detected an offline state and paused
        if not _net_offline_event.is_set():
            _net_offline_event.wait()

        # Global rate-limit pause check
        now = time.time()
        if now < _rl_until:
            time.sleep(_rl_until - now)

        try:
            with _global_sem:
                r = session.get(url, timeout=CHUNK_TIMEOUT)

            if r.status_code == 429:
                attempt_429 += 1
                if attempt_429 > max_429_retries:
                    raise RuntimeError("Too many 429 Too Many Requests")
                
                wait = backoff + random.uniform(1.0, 2.5)
                # Set global pause so other threads chill out too
                with _rl_lock:
                    if time.time() + wait > _rl_until:
                        _rl_until = time.time() + wait
                
                log_msg(f"    {C.RED}✘{C.RESET} 429 on chunk {index:04d}, global pause {wait:.1f}s "
                        f"(429-attempt {attempt_429}/{max_429_retries})", debug=True)
                time.sleep(wait)
                backoff *= 1.5
                continue  # Note: Does NOT increment standard `attempt`

            if r.status_code != 200:
                log_msg(f"    {C.RED}✘{C.RESET} HTTP {r.status_code} on chunk {index:04d}", debug=True)
                r.raise_for_status()

            data = r.content
            stats.add_bytes(len(data))

            # AES-128 decrypt
            if key_info and key_info.get("method") == "AES-128":
                try:
                    from Crypto.Cipher import AES
                except ImportError:
                    fail_counter.record_failure("pycryptodome missing! pip install pycryptodome")
                    fail_counter.abort.set()
                    raise RuntimeError("pycryptodome missing! pip install pycryptodome")
                iv = key_info.get("iv") or index.to_bytes(16, byteorder="big")
                data = AES.new(key_info["key"], AES.MODE_CBC, iv=iv).decrypt(data)

            # Strip fake image header before MPEG-TS sync byte
            sync = data.find(b"\x47")
            if sync != -1:
                data = data[sync:]

            if attempt > 1:
                log_msg(f"    {C.GREEN}✔{C.RESET} chunk {index:04d} recovered on attempt {attempt}", debug=True)

            return index, data

        except Exception as e:
            error_msg = str(e)
            if fail_counter.abort.is_set():
                raise  # Preserve the REAL error

            # Check if network connection died
            if not is_online():
                wait_for_internet()
                time.sleep(1.0)
                continue

            if not _net_offline_event.is_set():
                _net_offline_event.wait()
                time.sleep(1.0)
                continue

            log_msg(f"    {C.RED}✘{C.RESET} chunk {index:04d} attempt {attempt}/{MAX_RETRIES}: {error_msg}", debug=True)
            if attempt == MAX_RETRIES:
                fail_counter.record_failure(error_msg)
                raise RuntimeError(f"chunk {index:04d} failed: {error_msg}")
            
            log_msg(f"    {C.YELLOW}↻{C.RESET} retrying chunk {index:04d} in {backoff:.1f}s (next: attempt {attempt+1}/{MAX_RETRIES})...", debug=True)
            time.sleep(backoff)
            backoff *= 1.5
            attempt += 1

    fail_counter.record_failure(f"chunk {index:04d} exhausted retries")
    raise RuntimeError(f"chunk {index:04d} failed.")


# ---------------------------------------------------------------------------
# Main download entry point
# ---------------------------------------------------------------------------
def download_hls(
    playlist_url: str,
    output_path: str,
    headers: dict | None = None,
    workers: int = cfg.download_workers,
    delay: float = 0.0,
    target_res: int = 0,
    *,
    _preprobed: dict | None = None,
) -> bool:
    """
    Download an HLS stream to an MP4 file.

    If `_preprobed` is provided (from race_servers), skip the playlist fetch
    and reuse the already-fetched segments + first chunk.
    """
    global pbar
    temp_ts = f"{output_path}.temp.ts"
    wait_for_internet()

    if _preprobed:
        segment_urls = _preprobed["segments"]
        key_info = _preprobed["key_info"]
        dl_headers = _preprobed.get("headers") or headers
    else:
        session = get_session(headers)
        try:
            segment_urls, key_info = fetch_playlist_segments(
                playlist_url, session, target_res
            )
        except Exception as e:
            if not is_online():
                wait_for_internet()
                return download_hls(playlist_url, output_path, headers, workers, delay, target_res)
            log_msg(f"    {C.YELLOW}⚠{C.RESET} Failed to fetch playlist: {e}")
            return False
        dl_headers = headers

    # Derive Referer from first chunk URL's domain if not already set
    if segment_urls and dl_headers:
        chunk_host = urllib.parse.urlparse(segment_urls[0])
        chunk_origin = f"{chunk_host.scheme}://{chunk_host.netloc}"
        # If the chunk CDN is different from the Referer domain, update it
        if "Referer" in dl_headers:
            ref_host = urllib.parse.urlparse(dl_headers["Referer"]).netloc
            if ref_host != chunk_host.netloc:
                log_msg(f"    {C.BLUE}◆{C.RESET} Chunk CDN ({chunk_host.netloc}) differs from Referer ({ref_host})", debug=True)

    total = len(segment_urls)
    if total == 0:
        log_msg(f"    {C.YELLOW}⚠{C.RESET} Empty playlist.")
        return False

    chunks_dir = Path(f"{output_path}.chunks")
    chunks_dir.mkdir(exist_ok=True)
    downloaded: set[int] = set()
    stats = DownloadStats()

    # Reuse the probed first chunk if available (already decrypted + stripped)
    if _preprobed and _preprobed.get("probe_chunk"):
        idx, data = _preprobed["probe_chunk"]
        chunk_path = chunks_dir / f"{idx:06d}.ts"
        chunk_path.write_bytes(data)
        downloaded.add(idx)
        stats.add_bytes(len(data))

    remaining = [(i, url) for i, url in enumerate(segment_urls) if i not in downloaded]

    pbar = None
    if TQDM_AVAILABLE and not _JSON_PROGRESS:
        srv_name = ""
        if _preprobed and "server_name" in _preprobed:
            srv_name = f" [{_preprobed['server_name']}]"
            
        pbar = tqdm(
            total=total,
            initial=len(downloaded),
            desc=f"    [\u2193] Downloading{srv_name}",
            unit="seg",
            leave=False,
            bar_format="{desc}: {percentage:3.0f}%|{bar:20}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}"
        )
    else:
        print(f"    [\u2193] Downloading ({total} chunks)...")

    fail_counter = _FailCounter(threshold=_FAIL_THRESHOLD)

    # Reset thread-local sessions so workers get fresh sessions with correct
    # headers (Referer, Origin) for this specific provider/episode.
    # Creating a new threading.local() object forces all threads to re-initialize.
    global _thread_local
    _thread_local = threading.local()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    download_segment_with_retry,
                    idx, url, dl_headers, stats, key_info, fail_counter,
                ): idx
                for idx, url in remaining
            }
            # Iterate over completed chunk downloads as they finish
            for future in as_completed(futures):
                try:
                    idx, data = future.result()
                    chunk_path = chunks_dir / f"{idx:06d}.ts"
                    chunk_path.write_bytes(data)
                    del data
                    downloaded.add(idx)
                    if pbar:
                        pbar.set_postfix_str(f"Speed: {stats.get_speed_string()}")
                        pbar.update(1)
                    elif _JSON_PROGRESS:
                        _emit_progress(len(downloaded), total, stats)
                    else:
                        done = len(downloaded)
                        pct = (done / total) * 100
                        print(
                            f"\r    {C.BLUE}◆{C.RESET} [{done}/{total}] {pct:.1f}% | "
                            f"Speed: {stats.get_speed_string()}",
                            end="", flush=True,
                        )
                except Exception:
                    if fail_counter.abort.is_set():
                        # Cancel remaining futures
                        for f in futures:
                            f.cancel()
                        raise
                    # Individual chunk failed but threshold not reached — continue
                    continue
    except Exception as e:
        if pbar:
            pbar.close()
        pbar = None
        err = fail_counter.first_error or str(e)
        log_msg(f"\n    {C.RED}✘{C.RESET} Download aborted ({fail_counter._count} chunks failed): {err}")
        mark_server(playlist_url, False)
        # Clean up stranded chunks directory
        if chunks_dir.exists():
            import shutil
            shutil.rmtree(chunks_dir, ignore_errors=True)
        return False

    if pbar:
        pbar.close()
    pbar = None
    if _JSON_PROGRESS:
        _emit_progress(len(downloaded), total, stats, force=True)
    elif not TQDM_AVAILABLE:
        print()

    # ---- Assemble TS -------------------------------------------------------
    log_msg(f"    {C.BLUE}◆{C.RESET} Assembling TS stream...", debug=True)
    with open(temp_ts, "wb") as f:
        for i in range(total):
            if i in downloaded:
                chunk_path = chunks_dir / f"{i:06d}.ts"
                f.write(chunk_path.read_bytes())
                chunk_path.unlink()
            else:
                log_msg(f"    {C.RED}✘{C.RESET} Warning: missing chunk {i}")
    
    try:
        chunks_dir.rmdir()
    except OSError:
        pass

    # ---- Remux to MP4 ------------------------------------------------------
    log_msg(f"    {C.BLUE}◆{C.RESET} Remuxing to MP4...", debug=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", temp_ts, "-c", "copy", "-loglevel", "error",
             str(output_path)],
            check=True,
        )
        mark_server(playlist_url, True)
        return True
    except subprocess.CalledProcessError:
        log_msg(f"    {C.YELLOW}⚠{C.RESET} FFmpeg remux failed.")
        return False
    finally:
        if os.path.exists(temp_ts):
            os.remove(temp_ts)
