import subprocess

def has_audio_track(filepath):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            stderr=subprocess.STDOUT
        ).decode()
        return "audio" in out.lower()
    except Exception:
        return True

# ==============================================================================
# Title: Miruro Smart Auto-Downloader with Parallel Server Racing
# Creator: Vernox
# Description: CLI tool to auto-download anime episodes with parallel provider
#              probing, server racing, and health-cache-based failover.
# ==============================================================================

import sys

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

import argparse
import re
import concurrent.futures
from pathlib import Path

# Fix Windows console unicode issues
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import httpx

# Import local db module
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import hls_downloader
db.init_db()

from config import cfg
import api_manager

CLI_DIR = Path(__file__).resolve().parent
DL_DIR = CLI_DIR / "Downloads"

# Delegate API lifecycle to the dedicated module
check_api_running = api_manager.check_api_running
start_api_if_needed = api_manager.start_api_if_needed

# Helper for GET requests
def api_get(endpoint):
    url = f"{cfg.api_base}{endpoint}"
    for attempt in range(cfg.api_retries):
        try:
            # timeout to fail faster and trigger fallbacks immediately
            r = httpx.get(url, timeout=cfg.api_timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if not hls_downloader.is_online():
                hls_downloader.wait_for_internet()
                continue
            if attempt == 2:
                if cfg.debug:
                    print(f"  {C.RED}✘{C.RESET} API error at {endpoint}: {e}")
                return None
            time.sleep(2)

import string

def sanitize_filename(name):
    # Convert to Title Case (using capwords to avoid breaking apostrophes like I'm -> I'M)
    name = string.capwords(name)
    name = re.sub(r'[\\/*?:"<>|]', "", name).strip('. ')
    # Fallback for titles that are entirely special characters
    if not name:
        name = "Unknown Title"
    # Truncate to avoid Windows 260-char path limit
    if len(name) > 200:
        name = name[:200].rstrip()
    return name

# --- Season Detection Helpers ---
_ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10}

def extract_season_number(title: str) -> int:
    m = re.search(r'Season\s+(\d+)', title, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r'(\d+)(?:st|nd|rd|th)\s+Season', title, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r'\s(\d+)\s*$', title)
    if m and 2 <= int(m.group(1)) <= 20: return int(m.group(1))
    m = re.search(r'\s([IVXL]+)\s*$', title)
    if m and m.group(1) in _ROMAN: return _ROMAN[m.group(1)]
    return 1

def _has_part_or_cour(title: str) -> bool:
    return bool(re.search(r'Season\s+\d+\s+(?:Part|Cour)\s+\d+', title, re.IGNORECASE))

def extract_base_title(title: str) -> str:
    if _has_part_or_cour(title): return title
    base = re.sub(r'\s*Season\s+\d+\s*', ' ', title, flags=re.IGNORECASE).strip()
    if base != title: return base
    base = re.sub(r'\s*\d+(?:st|nd|rd|th)\s+Season\s*', ' ', title, flags=re.IGNORECASE).strip()
    if base != title: return base
    m = re.search(r'\s(\d+)\s*$', title)
    if m and 2 <= int(m.group(1)) <= 20: return title[:m.start()].strip()
    m = re.search(r'\s([IVXL]+)\s*$', title)
    if m and m.group(1) in _ROMAN: return title[:m.start()].strip()
    return title

# --------------------------------

def get_jellyfin_dir(title, anilist_id):
    # Determine base title for folder grouping
    base_title = extract_base_title(title)
    safe_base = sanitize_filename(base_title)
    
    # Check if a non-grouped folder already exists to avoid stranding old downloads
    legacy_safe = sanitize_filename(title)
    legacy_dir = DL_DIR / legacy_safe
    
    # If legacy folder exists and base folder does not, use legacy to prevent splitting
    if legacy_dir.exists() and legacy_safe != safe_base and not (DL_DIR / safe_base).exists():
        target_dir = legacy_dir
    else:
        target_dir = DL_DIR / safe_base
        
    anilist_dir = DL_DIR / f"{legacy_safe} [anilistid-{anilist_id}]"
    
    # Revert folder name if it was previously renamed to the anilist format
    if anilist_dir.exists() and not target_dir.exists():
        try:
            anilist_dir.rename(target_dir)
        except OSError:
            target_dir = anilist_dir # fallback if locked
            
    # Auto-migrate internal structure
    if target_dir.exists():
        season_num = extract_season_number(title)
        season_dir = target_dir / f"Season {season_num:02d}"
        # Move root videos to Season XX
        for f in target_dir.glob("*.mp4"):
            season_dir.mkdir(exist_ok=True)
            f.rename(season_dir / f.name)
        # Move cover.jpg to backdrop.jpg
        if (target_dir / "cover.jpg").exists():
            (target_dir / "cover.jpg").rename(target_dir / "backdrop.jpg")
        # Move thumbnails
        old_thumbs = target_dir / "Thumbnails"
        if old_thumbs.exists():
            season_dir.mkdir(exist_ok=True)
            for f in old_thumbs.glob("*.*"): 
                m = re.match(r'(S\d+E\d+)\.(.+)', f.name)
                if m:
                    new_name = f"{legacy_safe} - {m.group(1)}-thumb.{m.group(2)}"
                    f.rename(season_dir / new_name)
            try: old_thumbs.rmdir()
            except OSError: pass
            
    return target_dir

# ==========================================
# Core Operations
# ==========================================
def download_image(url, dest_path, force=False):
    pass

def fetch_and_add_series(anilist_id, force=False):
    # Query the Miruro v1 API directly — the old local uvicorn API is gone.
    # api_v1_request is not yet available here (defined later), so we do a
    # lightweight search by anilist_id_in which works without a cached UUID.
    info = api_v1_request("anime", {"anilist_id_in": str(anilist_id), "limit": 1})
    anime = None
    if info and "data" in info and info["data"]:
        anime = info["data"][0]

    if not anime:
        print(f"{C.RED}✘{C.RESET} Could not fetch metadata for ID {anilist_id} from Miruro API.")
        if not force:
            print(f"  {C.YELLOW}⚠{C.RESET} Run with --force to add it anyway with a placeholder title.")
            return None
        print(f"  {C.YELLOW}⚠{C.RESET} Force flag used. Adding as 'Unknown Title (ID {anilist_id})'.")
        anime = {}

    # Map v1 field names → our DB schema
    title_obj = anime.get("title") or {}
    title = title_obj.get("english") or title_obj.get("romaji") or f"Unknown Title (ID {anilist_id})"
    miruro_uuid = anime.get("id") or str(anilist_id)

    entry = {
        "anime_id": anilist_id,
        "title": title,
        "uuid": miruro_uuid,
        "url": f"https://www.miruro.bz/watch/{miruro_uuid}",
        "poster": anime.get("cover_url"),
        "cover": anime.get("background_url") or anime.get("banner_url"),
        "synopsis": anime.get("description"),
        "status": anime.get("status") or "Unknown",
        "total_episodes": anime.get("episode_count"),
        "genres": anime.get("genres", []),
        "is_tracked": 1,
        "priority": cfg.default_priority
    }
    db.upsert_series(entry)
    print(f"{C.GREEN}✔{C.RESET} Added to tracking: {title} (ID: {anilist_id})")

    series_dir = get_jellyfin_dir(title, anilist_id)
    series_dir.mkdir(parents=True, exist_ok=True)

    if entry["poster"]:
        download_image(entry["poster"], series_dir / "poster.jpg")
    if entry["cover"]:
        download_image(entry["cover"], series_dir / "backdrop.jpg")

    return title

def parse_episodes_arg(ep_str):
    if not ep_str or ep_str.lower() == "all":
        return None
    res = []
    for part in ep_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = map(int, part.split("-"))
            res.extend(range(start, end + 1))
        else:
            res.append(int(part))
    return sorted(list(set(res)))


# ==========================================
# Autonomous Cloudflare Solver + Pipe Engine
# ==========================================
import json as _json
import base64 as _base64
import zlib as _zlib
import threading as _threading
from curl_cffi import requests as _cf_requests

_GLOBAL_CF_COOKIE = None   # cached for the lifetime of this process
_CF_LOCK = _threading.Lock()
_PIPE_SEMAPHORE = _threading.Semaphore(8)

_PIPE_XOR_KEY = bytes.fromhex("71951034f8fbcf53d89db52ceb3dc22c")
_MIRURO_DOMAIN = "www.miruro.bz"


def _solve_cf_in_subprocess() -> str | None:
    """
    Spawn an isolated Python subprocess to run DrissionPage.
    Solves Linux signal/threading issues: Chrome requires a clean main-thread
    environment which is guaranteed in a fresh subprocess but not always available
    in the parent process after spawning the API manager.
    Returns the cf_clearance cookie string, or None on failure.
    """
    import subprocess, sys, textwrap

    # The solver script runs in its own clean process
    solver_script = textwrap.dedent("""
        import sys, time
        DOMAIN = "DOMAIN_PLACEHOLDER"
        
        def try_drission(headless):
            from DrissionPage import ChromiumPage, ChromiumOptions
            opt = ChromiumOptions()
            opt.headless(headless)
            opt.set_argument("--no-sandbox")
            opt.set_argument("--disable-dev-shm-usage")
            opt.set_argument("--disable-blink-features=AutomationControlled")
            opt.set_argument("--log-level=3")
            
            import platform
            if platform.system() == "Windows":
                ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            elif platform.system() == "Darwin":
                ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            else:
                ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            opt.set_argument(f"--user-agent={ua}")
            
            page = ChromiumPage(addr_or_opts=opt)
            try:
                page.get(f"https://{DOMAIN}/")
                
                # Wait up to 15 seconds for cf_clearance cookie
                deadline = time.monotonic() + 15
                c = None
                while time.monotonic() < deadline:
                    c = page.cookies().as_dict().get("cf_clearance")
                    if c:
                        break
                    time.sleep(0.5)

                if c:
                    # Use standard headers if we can't intercept a real one easily
                    headers = {
                        "User-Agent": page.user_agent,
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "sec-fetch-site": "same-origin",
                        "sec-fetch-mode": "cors",
                        "sec-fetch-dest": "empty",
                    }
                    import json
                    return json.dumps({
                        "cookie": c,
                        "headers": headers
                    })
                return None
            finally:
                try: page.quit()
                except: pass

        # Try headless first, then visible
        for headless in [True, False]:
            try:
                res = try_drission(headless)
                if res:
                    print(res, flush=True)
                    sys.exit(0)
            except Exception as e:
                sys.stderr.write(f"drission headless={headless} error: {e}\\n")

        # Fallback: undetected-chromedriver
        try:
            import undetected_chromedriver as uc
            for headless in [True, False]:
                try:
                    driver = uc.Chrome(headless=headless, use_subprocess=True)
                    try:
                        driver.get(f"https://{DOMAIN}")
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
                            if "cf_clearance" in cookies:
                                import json
                                c = cookies["cf_clearance"]
                                ua = driver.execute_script("return navigator.userAgent")
                                scu = driver.execute_script("return navigator.userAgentData ? navigator.userAgentData.brands.map(b => `\\\"${b.brand}\\\";v=\\\"${b.version}\\\"`).join(', ') : ''")
                                scum = driver.execute_script("return navigator.userAgentData ? (navigator.userAgentData.mobile ? '?1' : '?0') : '?0'")
                                scup = driver.execute_script("return navigator.userAgentData ? `\\\"${navigator.userAgentData.platform}\\\"` : '\\\"Windows\\\"'")
                                print(json.dumps({
                                    "cookie": c,
                                    "headers": {
                                        "User-Agent": ua,
                                        "sec-ch-ua": scu,
                                        "sec-ch-ua-mobile": scum,
                                        "sec-ch-ua-platform": scup
                                    }
                                }), flush=True)
                                sys.exit(0)
                            time.sleep(0.5)
                    finally:
                        try: driver.quit()
                        except: pass
                except Exception as e:
                    sys.stderr.write(f"uc headless={headless} error: {e}\\n")
        except ImportError:
            pass

        sys.exit(1)
    """).replace("DOMAIN_PLACEHOLDER", _MIRURO_DOMAIN)

    try:
        result = subprocess.run(
            [sys.executable, "-c", solver_script],
            capture_output=True,
            text=True,
            timeout=50,
        )
        if cfg.debug and result.stderr.strip():
            print(f"  [CF solver debug] {result.stderr.strip()}")
        cookie = result.stdout.strip()
        if cookie and len(cookie) > 10:
            return cookie
    except subprocess.TimeoutExpired:
        if cfg.debug:
            print(f"  {C.YELLOW}⚠{C.RESET} CF subprocess timed out.")
    except Exception as exc:
        if cfg.debug:
            print(f"  {C.YELLOW}⚠{C.RESET} CF subprocess error: {exc}")

    return None


def _get_cf_cookie() -> dict | None:
    """
    Obtain a cf_clearance cookie via an isolated subprocess.
    Cached globally so the browser is only launched once per session.
    Thread-safe: all threads wait on the lock while the first one solves.
    """
    global _GLOBAL_CF_COOKIE
    # Fast path — already resolved
    if _GLOBAL_CF_COOKIE is not None:
        return _GLOBAL_CF_COOKIE if _GLOBAL_CF_COOKIE else None

    with _CF_LOCK:
        # Double-check after acquiring lock
        if _GLOBAL_CF_COOKIE is not None:
            return _GLOBAL_CF_COOKIE if _GLOBAL_CF_COOKIE else None

        print(f"  {C.BLUE}◆{C.RESET} Launching stealth browser to solve Cloudflare...")
        out = _solve_cf_in_subprocess()
        if out:
            try:
                data = _json.loads(out)
                print(f"  {C.GREEN}✔{C.RESET} Cloudflare solved autonomously!")
                _GLOBAL_CF_COOKIE = data
                return data
            except Exception:
                pass

        print(f"  {C.YELLOW}⚠{C.RESET} Could not obtain CF clearance — proceeding without cookie.")
            
        _GLOBAL_CF_COOKIE = {}
        return None


def _b64u_enc(data) -> str:
    if isinstance(data, str): data = data.encode()
    return _base64.b64encode(data).decode().replace("+", "-").replace("/", "_").rstrip("=")

def _b64u_dec(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return _base64.b64decode(s)

def api_v1_request(path: str, query: dict = None, impersonate: str = "chrome124") -> dict | None:
    cf_data = _get_cf_cookie()
    url = f"https://{_MIRURO_DOMAIN}/api/v1/{path.lstrip('/')}"
    if query:
        import urllib.parse
        url += "?" + urllib.parse.urlencode(query)

    sess = _cf_requests.Session(impersonate=impersonate)
    
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": f"https://{_MIRURO_DOMAIN}/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    
    if cf_data:
        sess.cookies.set("cf_clearance", cf_data["cookie"], domain=_MIRURO_DOMAIN)
        if "headers" in cf_data:
            for k, v in cf_data["headers"].items():
                if not k.startswith(":"):
                    hdrs[k] = v
    
    with _PIPE_SEMAPHORE:
        for attempt in range(6):
            try:
                r = sess.get(url, headers=hdrs, timeout=15)
                
                if r.status_code in (429, 502, 503):
                    wait = 2 ** attempt
                    if wait > 10: wait = 10
                    if cfg.debug: print(f"  [v1] {r.status_code} on {path}, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                    
                if r.status_code != 200:
                    if cfg.debug: print(f"  [v1 debug] {r.status_code} on {path}")
                    return None
                    
                raw = bytearray(r.content)
                key = b"miruro/catalog"
                for i in range(len(raw)):
                    raw[i] ^= key[i % len(key)]
                
                import zlib
                out = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(bytes(raw))
                import json
                return json.loads(out.decode("utf-8", errors="ignore"))
                
            except Exception as e:
                if attempt < 2:
                    time.sleep(1)
                    continue
                return None
    return None

def fetch_miruro_metadata(anilist_id: int) -> dict | None:
    # We must fetch the API to get up-to-date dub_counts, but we can use the cached UUID 
    # to query the faster direct endpoint if we have it!
    series = db.get_series(anilist_id)
    cached_uuid = series["uuid"] if series and series.get("uuid") and not series["uuid"].isdigit() else None
    
    if cached_uuid:
        data = api_v1_request(f"anime/{cached_uuid}")
        if data and "id" in data:
            return data
            
    # Fallback to search endpoint if no cache or direct fetch failed
    data = api_v1_request("anime", {"anilist_id_in": str(anilist_id), "limit": 100})
    if data and "data" in data and len(data["data"]) > 0:
        anime_data = data["data"][0]
        # Cache it back to the database
        if series:
            try:
                with db.db() as conn:
                    conn.execute("UPDATE series SET uuid = ? WHERE anime_id = ?", (anime_data["id"], anilist_id))
            except Exception:
                pass
        return anime_data
    return None

def fetch_miruro_id(anilist_id: int) -> str | None:
    meta = fetch_miruro_metadata(anilist_id)
    return meta["id"] if meta else None


def fetch_v1_episodes(miruro_id: str) -> list | None:
    data = api_v1_request(f"anime/{miruro_id}/episodes", {"kind": "regular", "limit": 10000})
    if data and "data" in data:
        return data["data"]
    return None


# ==========================================
# Parallel Provider + Server Discovery
# ==========================================

_CDN_CF_COOKIES: dict = {}   # netloc → cf_clearance value
_CDN_CF_LOCK = _threading.Lock()


def _solve_cf_for_subtitle_cdn(url: str) -> str | None:
    """
    Spawn a DrissionPage subprocess to navigate to a subtitle CDN URL,
    automatically passing any CF challenge, then extract and return the
    cf_clearance cookie for that domain.  Cached globally per domain.
    """
    import subprocess, sys, textwrap
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc  # e.g. "0mf2u.eclipseharbor.world"
    # Use root domain for the cookie scope (works for all subdomains)
    parts = netloc.split(".")
    root_domain = ".".join(parts[-2:]) if len(parts) >= 2 else netloc

    solver = textwrap.dedent(f"""
        import sys, time, json
        target_url = {repr(url)}
        root_domain = {repr(root_domain)}

        HARD_BLOCK_SIGNALS = [
            "you have been blocked",
            "sorry, you have been blocked",
            "access denied",
        ]

        def is_hard_blocked(page):
            try:
                title = (page.title or "").lower()
                body  = (page.run_js("return document.body ? document.body.innerText : ''") or "").lower()
                for sig in HARD_BLOCK_SIGNALS:
                    if sig in title or sig in body:
                        return True
            except:
                pass
            return False

        def solve(headless):
            from DrissionPage import ChromiumPage, ChromiumOptions
            opt = ChromiumOptions()
            opt.headless(headless)
            opt.set_argument("--no-sandbox")
            opt.set_argument("--disable-dev-shm-usage")
            opt.set_argument("--disable-blink-features=AutomationControlled")
            opt.set_argument("--log-level=3")
            page = ChromiumPage(addr_or_opts=opt)
            try:
                page.get(target_url)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    # Bail immediately on hard block (WAF rule, not a solvable challenge)
                    if is_hard_blocked(page):
                        sys.stderr.write("hard_blocked\\n")
                        return
                    for ck in page.cookies():
                        if ck.get("name") == "cf_clearance":
                            domain = ck.get("domain", "")
                            if root_domain in domain or domain in root_domain:
                                print(json.dumps({{"cookie": ck["value"], "domain": domain}}), flush=True)
                                sys.exit(0)
                    time.sleep(0.5)
            finally:
                try: page.quit()
                except: pass

        for h in [True, False]:
            try:
                solve(h)
            except Exception as e:
                sys.stderr.write(f"err headless={{h}}: {{e}}\\n")
        sys.exit(1)
    """)

    try:
        import subprocess as _sp, sys as _sys
        res = _sp.run([_sys.executable, "-c", solver],
                      capture_output=True, text=True, timeout=60)
        for line in reversed(res.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                data = _json.loads(line)
                return data.get("cookie")
    except Exception:
        pass
    return None


def _download_subtitle(subs: list, dest_path: "Path") -> bool:
    """
    Try to download the first working English subtitle from the subs list.
    Fast path: curl_cffi with browser headers.
    Slow path: if a CDN returns 403, solve its CF challenge via DrissionPage,
    cache the cf_clearance cookie, and retry once.
    Returns True on success.
    """
    from curl_cffi import requests as _cfr
    from urllib.parse import urlparse

    import requests as _req

    for sub in subs:
        sub_url = sub.get("file", "")
        if not sub_url:
            continue
        lang = (sub.get("language") or "").lower()
        label_field = (sub.get("label") or "").lower()
        if lang not in ("en", "eng") and not label_field.startswith("eng"):
            continue

        sub_ext = sub.get("format") or sub_url.split(".")[-1]
        if len(sub_ext) > 4:
            sub_ext = "vtt"
        lang_tag = lang[:3] if lang else "en"
        out_path = dest_path.parent / f"{dest_path.stem}.{lang_tag}.{sub_ext}"

        if out_path.exists():
            print(f"    {C.GRAY}↷{C.RESET} Subtitle already on disk.")
            return True

        # Use the dynamic referer extracted directly from the API response
        referer = sub.get("_referer", f"https://{_MIRURO_DOMAIN}/")
        # Origin is typically just the Referer without the trailing slash
        origin = referer.rstrip("/")
        hdrs = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
                           "Gecko/20100101 Firefox/133.0"),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
            "Origin": origin,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "DNT": "1",
        }

        try:
            resp = _req.get(sub_url, headers=hdrs, timeout=20, allow_redirects=True)
            if resp.status_code == 200 and resp.content:
                out_path.write_bytes(resp.content)
                print(f"    {C.GREEN}✔{C.RESET} Downloaded subtitle → {out_path.name}")
                return True
            if resp.status_code == 403:
                print(f"    {C.YELLOW}⚠{C.RESET} CDN 403 (Referer: {referer}) ({sub_url[:55]}...)")
            else:
                print(f"    {C.YELLOW}⚠{C.RESET} CDN {resp.status_code} ({sub_url[:55]}...)")
        except Exception as _se:
            print(f"    {C.YELLOW}⚠{C.RESET} Fetch error ({sub_url[:55]}...): {_se}")

    return False

def discover_all_streams(miruro_id, ep_num, target_cat, target_providers=None, target_res=1080):
    """
    Query ALL providers for a specific episode via the v1 play endpoint.
    Returns a list of (url, headers, server_name, subtitles) from every provider.
    Automatically filters out lower resolutions to prevent racing them.
    Subtitles are always harvested from the 'ssub' track regardless of video category.
    """
    # Episode number must be an integer in the API path (not 1.0)
    ep_num_int = int(ep_num) if float(ep_num) == int(ep_num) else ep_num
    data = api_v1_request(f"anime/{miruro_id}/episodes/{ep_num_int}/play")
    if not data or "tracks" not in data:
        return []

    # ── Pass 1: harvest English subtitles from ALL ssub providers ──
    # Subtitle CDN URLs are independent of which video server wins the race,
    # so we collect English subs from EVERY ssub provider into a flat list.
    # The download loop tries each URL in order — if one 403s, it falls
    # through to the next provider's URL automatically.
    global_subs = []
    _seen_sub_urls = set()
    # Collect per-provider English subs in separate buckets first
    _sub_buckets: dict = {}   # provider_name → [sub_entry, ...]
    for track in data.get("tracks", []):
        if track.get("track") != "ssub":
            continue
        for prov in track.get("providers", []):
            prov_name = prov.get("provider", "")
            # Try to grab Referer from the first server entry in this provider
            dynamic_referer = f"https://{_MIRURO_DOMAIN}/"
            servers = prov.get("servers", [])
            if servers:
                hdrs = servers[0].get("headers") or {}
                if "Referer" in hdrs:
                    dynamic_referer = hdrs["Referer"]

            for sub in prov.get("subtitles", []):
                lang = (sub.get("language") or "").lower()
                label = (sub.get("label") or "").lower()
                url = sub.get("file", "")
                is_english = lang in ("en", "eng") or label.startswith("eng")
                if is_english and url and url not in _seen_sub_urls:
                    _seen_sub_urls.add(url)
                    tagged_sub = dict(sub)
                    tagged_sub["_referer"] = dynamic_referer
                    _sub_buckets.setdefault(prov_name, []).append(tagged_sub)

    # Provider priority: kickassanime CDN (krussdomi.com) hard-blocks many IPs,
    # so try anikoto and icarus first, kickassanime as last resort.
    _DEPRIORITIZED_PROVIDERS = {"kickassanime"}
    for pname, subs in _sub_buckets.items():
        if pname not in _DEPRIORITIZED_PROVIDERS:
            global_subs.extend(subs)
    for pname, subs in _sub_buckets.items():
        if pname in _DEPRIORITIZED_PROVIDERS:
            global_subs.extend(subs)

    # ── Pass 2: collect video streams for the requested category ──
    valid_tracks = [target_cat]
    if target_cat == "sub":
        valid_tracks = ["sub", "ssub"]

    best_streams_by_server = {}

    for track in data.get("tracks", []):
        if track.get("track") not in valid_tracks:
            continue

        for prov in track.get("providers", []):
            prov_name = prov.get("provider", "")
            if target_providers and prov_name not in target_providers:
                continue

            # Inject global_subs so every stream entry carries subtitles
            # regardless of which provider ends up winning the server race.
            subs = global_subs or prov.get("subtitles", [])

            for srv in prov.get("servers", []):
                srv_name = f"{prov_name}/{srv.get('server', 'Unknown')}"
                headers = srv.get("headers", {})

                best_url = None
                best_h = -1

                for stream in srv.get("streams", []):
                    if stream.get("format") == "hls" or "m3u8" in stream.get("url", ""):
                        h = 0  # Default to 0 (unknown) — don't discard unknown-res streams
                        res_obj = stream.get("resolution")
                        if isinstance(res_obj, dict) and res_obj.get("height"):
                            h = res_obj["height"]
                        else:
                            q_str = str(stream.get("quality", "")).lower()
                            if "p" in q_str:
                                try: h = int(q_str.replace("p", "").strip())
                                except: pass

                        if target_res > 0:
                            if (h == 0 or h <= target_res) and h >= best_h:
                                best_h = h
                                best_url = stream["url"]
                        else:
                            if h >= best_h:
                                best_h = h
                                best_url = stream["url"]

                if best_url:
                    best_streams_by_server[srv_name] = (best_url, headers, srv_name, subs)
                    print(f"    {C.BLUE}◆{C.RESET} Selected {best_h}p stream for {srv_name}")

    return list(best_streams_by_server.values())


def download_episode(anilist_id, title, ep_data, provider, category, target_res, subtitles_only=False):
    import time

    ep_num = ep_data["episode_number"]
    target_cat = ep_data.get("_target_category", category)
    miruro_id = ep_data.get("_miruro_id")
    
    if not miruro_id:
        return False
        
    print(f"  [▶] Processing EP {ep_num} ({target_cat}) —\"{ep_data.get('title', 'Unknown')}\"...")
    
    all_streams = discover_all_streams(miruro_id, ep_num, target_cat, target_res=target_res)
    
    if not all_streams:
        print(f"    {C.RED}?{C.RESET} No HLS streams found for EP {ep_num} ({target_cat}) from any provider.")
        if target_cat == "dub":
            safe_title = sanitize_filename(title)
            season_num = extract_season_number(title)
            season_dir = get_jellyfin_dir(title, anilist_id) / f"Season {season_num:02d}"
            sub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}.mp4"
            if sub_file.exists() and not subtitles_only:
                print(f"  {C.YELLOW}?{C.RESET} Sub version already exists on disk. Skipping redundant sub download.")
                return False
                
            print(f"  {C.BLUE}?{C.RESET} Falling back to sub...")
            ep_data_sub = ep_data.copy()
            ep_data_sub["_target_category"] = "sub"
            # Also update the original ep_data in place so the caller knows
            # the final downloaded category was "sub", not "dub". Without this,
            # the caller would see _target_category="dub" + success=True and
            # incorrectly delete the existing sub file it just downloaded.
            ep_data["_target_category"] = "sub"
            return download_episode(anilist_id, title, ep_data_sub, provider, category, target_res, subtitles_only)
        return False
        
    # --- Filter by target provider if requested ---
    if provider and provider != "all":
        provider_streams = [s for s in all_streams if s[2].startswith(provider)]
        if not provider_streams:
            print(f"    {C.YELLOW}?{C.RESET} No streams found for provider '{provider}'. Falling back to any provider...")
        else:
            all_streams = provider_streams

    import hls_downloader
    winner = hls_downloader.race_servers(all_streams, target_res)
    if not winner:
        if not hls_downloader.is_online():
            hls_downloader.wait_for_internet()
            return download_episode(anilist_id, title, ep_data, provider, category, target_res, subtitles_only)
            
        print(f"    {C.YELLOW}⚠{C.RESET} All servers failed probe for EP {ep_num} ({target_cat}).")
        if target_cat == "dub":
            safe_title = sanitize_filename(title)
            season_num = extract_season_number(title)
            season_dir = get_jellyfin_dir(title, anilist_id) / f"Season {season_num:02d}"
            sub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}.mp4"
            if sub_file.exists() and not subtitles_only:
                print(f"  {C.YELLOW}⚠{C.RESET} Sub version already exists on disk. Skipping redundant sub download.")
                return False
                
            print(f"  {C.BLUE}↩{C.RESET} Falling back to sub...")
            ep_data_sub = ep_data.copy()
            ep_data_sub["_target_category"] = "sub"
            # Mutate original so caller sees the final downloaded category
            ep_data["_target_category"] = "sub"
            return download_episode(anilist_id, title, ep_data_sub, provider, category, target_res, subtitles_only)
        return False

    safe_title = sanitize_filename(title)
    season_num = extract_season_number(title)
    series_dir = get_jellyfin_dir(title, anilist_id)
    season_dir = series_dir / f"Season {season_num:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)

    dub_suffix = " - Dub" if target_cat == "dub" else ""
    base_name = f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}{dub_suffix}"
    filename = f"{base_name}.mp4"
    save_path = season_dir / filename

    if subtitles_only:
        print(f"    [↓] Skipping video/thumbnail download. Fetching subtitles only...")
        subs = winner.get("subtitles") or []
        if subs:
            _download_subtitle(subs, save_path)
        else:
            print(f"    {C.YELLOW}⚠{C.RESET} No subtitles available from this provider.")
        return True

    if ep_data.get("thumbnail_url"):
        thumb_ext = ep_data["thumbnail_url"].split('.')[-1] if '.' in ep_data["thumbnail_url"] else "jpg"
        if len(thumb_ext) > 4:
            thumb_ext = "jpg"
        thumb_path = season_dir / f"{base_name}-thumb.{thumb_ext}"
        download_image(ep_data["thumbnail_url"], thumb_path)

    t0 = time.monotonic()
    success = hls_downloader.download_hls(
        playlist_url=winner["m3u8_url"],
        output_path=str(save_path),
        headers=winner["headers"],
        workers=cfg.download_workers,
        delay=0.0,
        target_res=target_res,
        _preprobed=winner,
    )
    t1 = time.monotonic()

    if success:
        if not has_audio_track(str(save_path)):
            print(f"    {C.RED}✘{C.RESET} File has NO AUDIO TRACK. Deleting and trying another server...")
            try:
                save_path.unlink(missing_ok=True)
            except Exception: pass
            success = False
        else:
            elapsed = t1 - t0
            time_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            print(f"    {C.GREEN}✔{C.RESET} Downloaded EP {ep_num} successfully in {time_str}.")

            subs = winner.get("subtitles") or []
            if subs:
                _download_subtitle(subs, save_path)
            return True

    print(f"    {C.RED}✘{C.RESET} Winner failed (no audio). Trying remaining servers...")
    remaining = [(u, h, s, subs) for u, h, s, subs in all_streams if u != winner["m3u8_url"]]

    for m3u8_url, req_headers, server_name, subs in remaining:
        print(f"    [↓] Retrying via {server_name} ...")
        t0_fb = time.monotonic()
        success = hls_downloader.download_hls(
            playlist_url=m3u8_url,
            output_path=str(save_path),
            headers=req_headers,
            workers=cfg.download_workers,
            delay=0.0,
            target_res=target_res,
        )
        t1_fb = time.monotonic()
        if success:
            if not has_audio_track(str(save_path)):
                print(f"    {C.RED}✘{C.RESET} File has NO AUDIO TRACK. Deleting and trying next...")
                try:
                    save_path.unlink(missing_ok=True)
                except Exception: pass
                success = False
            else:
                elapsed = t1_fb - t0_fb
                time_str = f"{elapsed:.1f}s" if elapsed < 60 else f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                print(f"    {C.GREEN}✔{C.RESET} Downloaded EP {ep_num} successfully in {time_str}.")

                # Use global_subs (same across all streams) for subtitle download
                global_subs = winner.get("subtitles") or subs
                if global_subs:
                    _download_subtitle(global_subs, save_path)
                return True
        if not success:
            print(f"    {C.RED}✘{C.RESET} {server_name} failed. Trying next...")

    print(f"    {C.RED}?{C.RESET} All servers exhausted for EP {ep_num}.")
    return False

def process_series(anilist_id, target_eps=None, provider="hop", category="dub", quality=1080, force=False, upgrade_dubs=False, subtitles_only=False):
    series = db.get_series(anilist_id)
    if not series:
        print(f"{C.BLUE}?{C.RESET} Fetching metadata for new series (ID: {anilist_id})...")
        title = fetch_and_add_series(anilist_id)
        if not title:
            print(f"{C.RED}?{C.RESET} Could not add series {anilist_id}")
            return
    else:
        title = series["title"]
        
    print(f"\n[▶] {title} (ID: {anilist_id})")
    
    meta = fetch_miruro_metadata(anilist_id)
    if not meta:
        print(f"  {C.RED}?{C.RESET} Could not map AniList ID to Miruro ID.")
        return
        
    miruro_id = meta["id"]
    dub_count = meta.get("episode_counts", {}).get("dub") or 0
    sub_count = meta.get("episode_counts", {}).get("sub") or 0
        
    eps = fetch_v1_episodes(miruro_id)
    if not eps:
        print(f"  {C.RED}?{C.RESET} No episodes found or API error.")
        return

    history = db.get_tracking_history(anilist_id)
    downloaded_eps = history.get("downloaded_eps", [])
    last_aired = history.get("last_episode_aired", 0)

    safe_title = sanitize_filename(title)
    season_num = extract_season_number(title)
    season_dir = get_jellyfin_dir(title, anilist_id) / f"Season {season_num:02d}"

    eps_to_dl = []
    seen_queued = set()
    for ep in eps:
        ep_num = ep["episode_number"]
        if ep_num in seen_queued:
            continue
        seen_queued.add(ep_num)
        
        if target_eps and ep_num not in target_eps:
            continue
            
        needs_download = False
        if not target_eps:
            # Mirror probe_worker_auto: for dub, allow if sub OR dub is out
            if category == "dub":
                available = max(dub_count, sub_count)
            else:
                available = sub_count
            if ep_num > available:
                continue

        if force or subtitles_only or ep_num not in downloaded_eps:
            needs_download = True
        elif upgrade_dubs and category == "dub" and ep_num <= dub_count:
            sub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}.mp4"
            dub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d} - Dub.mp4"
            if sub_file.exists() and not dub_file.exists():
                needs_download = True
                print(f"  {C.BLUE}↑{C.RESET} Dub Upgrade found for EP {ep_num}!")

        if needs_download:
            ep_copy = ep.copy()
            ep_copy["_miruro_id"] = miruro_id
            # If no dub track exists at all, go straight to sub
            if category == "dub" and dub_count == 0:
                ep_copy["_target_category"] = "sub"
            else:
                ep_copy["_target_category"] = category
            eps_to_dl.append(ep_copy)
        
    if not eps_to_dl:
        print(f"  {C.GREEN}?{C.RESET} Up to date! No new episodes.")
        return
        
    print(f"  {C.BLUE}?{C.RESET} Queued {len(eps_to_dl)} episodes for download.")
    
    for ep in eps_to_dl:
        ep_num = ep["episode_number"]
        success = download_episode(
            anilist_id, title, ep, provider, category, quality, subtitles_only
        )
            
        if success:
            ep_actual_cat = ep.get("_target_category", category)
            if upgrade_dubs and ep_actual_cat == "dub":
                sub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}.mp4"
                if sub_file.exists():
                    try:
                        sub_file.unlink()
                        sub_thumb_jpg = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}-thumb.jpg"
                        sub_thumb_png = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}-thumb.png"
                        if sub_thumb_jpg.exists(): sub_thumb_jpg.unlink()
                        if sub_thumb_png.exists(): sub_thumb_png.unlink()
                        print(f"  {C.GREEN}?{C.RESET} Deleted old subbed file for EP {ep_num}")
                    except Exception as e:
                        print(f"  {C.RED}?{C.RESET} Failed to delete old subbed file: {e}")

            if ep_num not in downloaded_eps:
                downloaded_eps.append(ep_num)
                downloaded_eps.sort()
            new_last = max(last_aired, ep_num)
            db.update_tracking_history(anilist_id, new_last, downloaded_eps)
            
            db.upsert_episode(anilist_id, {
                "number": ep_num,
                "session": str(ep_num),
                "snapshot_url": ep.get("thumbnail_url"),
                "title": ep.get("title"),
                "created_at": ep.get("aired_on")
            })

def probe_worker_auto(s, provider, category, quality, upgrade_dubs):
    anilist_id = s["anime_id"]
    title = s["title"]
    
    meta = fetch_miruro_metadata(anilist_id)
    if not meta:
        return {"anime_id": anilist_id, "title": title, "eps_to_dl": [], "error": "Could not map ID"}
        
    miruro_id = meta["id"]
    dub_count = meta.get("episode_counts", {}).get("dub") or 0
    sub_count = meta.get("episode_counts", {}).get("sub") or 0
        
    eps = fetch_v1_episodes(miruro_id)
    if not eps:
        return {"anime_id": anilist_id, "title": title, "eps_to_dl": [], "error": "No episodes found"}

    history = db.get_tracking_history(anilist_id)
    downloaded_eps = history.get("downloaded_eps", [])
    
    safe_title = sanitize_filename(title)
    season_num = extract_season_number(title)
    season_dir = get_jellyfin_dir(title, anilist_id) / f"Season {season_num:02d}"

    eps_to_dl = []
    upgrades = 0
    for ep in eps:
        ep_num = ep["episode_number"]
        
        # Gate on actual released counts from the metadata endpoint (/api/v1/anime/{uuid}).
        # When category is "dub", use max(dub_count, sub_count) so that episodes where
        # sub is out but dub hasn't aired yet still enter the queue and trigger sub-fallback.
        if category == "dub":
            available = max(dub_count, sub_count)
        else:
            available = sub_count
        if ep_num > available:
            continue
            
        needs_download = False
        if ep_num not in downloaded_eps:
            needs_download = True
        elif upgrade_dubs and category == "dub" and ep_num <= dub_count:
            # Only attempt a dub upgrade if the dub has actually been released (ep_num <= dub_count).
            # Without this guard, we'd try to upgrade every sub-only episode, fail to find a dub,
            # then find the sub already on disk and skip — wasted API calls for every episode.
            sub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}.mp4"
            dub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d} - Dub.mp4"
            if sub_file.exists() and not dub_file.exists():
                needs_download = True
                upgrades += 1
                
        if needs_download:
            ep_copy = ep.copy()
            ep_copy["_miruro_id"] = miruro_id
            # If we want dubs but no dub track exists yet for this show at all,
            # go straight to sub — skip the failed dub probe entirely.
            if category == "dub" and dub_count == 0:
                ep_copy["_target_category"] = "sub"
            else:
                ep_copy["_target_category"] = category
            eps_to_dl.append(ep_copy)
            
    # actual_released = what the metadata endpoint reports as available for this category
    if category == "dub":
        actual_released = max(dub_count, sub_count)
    else:
        actual_released = sub_count

    return {
        "anime_id": anilist_id, 
        "title": title, 
        "eps_to_dl": eps_to_dl, 
        "upgrades": upgrades,
        "provider": provider,
        "category": category,
        "quality": quality,
        "total_released": actual_released,
        "priority": s.get("priority", 0)
    }

# ==========================================
# Rehash Logic
# ==========================================

def rehash_series(s, force_img=False):
    anilist_id = s["anime_id"]
    title = s["title"]
    safe_title = sanitize_filename(title)
    series_dir = get_jellyfin_dir(title, anilist_id)
    season_num = extract_season_number(title)
    season_dir = series_dir / f"Season {season_num:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    
    if s.get("poster_url"):
        poster_path = series_dir / "poster.jpg"
        if force_img or not poster_path.exists():
            print(f"  {C.GREEN}✔{C.RESET} Redownloading poster for: {title}")
            download_image(s["poster_url"], poster_path, force=force_img)
            
    if s.get("cover_url"):
        cover_path = series_dir / "backdrop.jpg"
        if force_img or not cover_path.exists():
            print(f"  {C.GREEN}✔{C.RESET} Redownloading cover for: {title}")
            download_image(s["cover_url"], cover_path, force=force_img)
            
    miruro_id = fetch_miruro_id(anilist_id)
    if not miruro_id:
        return
        
    eps = fetch_v1_episodes(miruro_id)
    if not eps:
        return
        
    for ep in eps:
        if ep.get("thumbnail_url"):
            ep_num = ep["episode_number"]
            thumb_ext = ep["thumbnail_url"].split('.')[-1] if '.' in ep["thumbnail_url"] else "jpg"
            if len(thumb_ext) > 4: thumb_ext = "jpg"
            
            base_name = f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}"
            thumb_path = season_dir / f"{base_name}-thumb.{thumb_ext}"
            if force_img or not thumb_path.exists():
                print(f"  {C.GREEN}✔{C.RESET} Downloading missing thumbnail for {title} EP {ep_num}")
                download_image(ep["thumbnail_url"], thumb_path, force=force_img)

def do_global_rehash(force_img=False):
    series_list = db.get_tracked_series()
    print(f"\n{C.BLUE}◆{C.RESET} Rehashing {len(series_list)} tracked series...")
    for s in series_list:
        print(f"  [▶] {s['title']}")
        rehash_series(s, force_img)
        
    print(f"\n{C.GREEN}✔{C.RESET} Global rehash complete.")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Miruro HLS Direct Downloader (Cloudflare Auto-Bypass)")
    parser.add_argument("-l", "--link", help="AniList ID or Miruro URL")
    parser.add_argument("-a", "--auto", action="store_true", help="Probe all tracked series for new episodes and download them")
    parser.add_argument("-p", "--provider", default="all", help="Target provider (e.g., hop, yuki, alpha). Default: all")
    parser.add_argument("-c", "--category", default="dub", choices=["sub", "dub", "raw"], help="Target category. Default: dub (auto-falls back to sub)")
    parser.add_argument("-q", "--quality", default=1080, type=int, help="Target resolution height (e.g. 1080). Default: 1080")
    parser.add_argument("-e", "--episodes", help="Specific episodes (e.g. 1, 3-5). Default: all un-downloaded")
    parser.add_argument("-f", "--force", action="store_true", help="Force redownload even if marked downloaded in DB")
    parser.add_argument("--add", nargs='+', help="Add AniList ID(s) or Miruro UUID(s) to auto-tracking without downloading")
    parser.add_argument("--untrack", action="store_true", help="Remove the series from auto-tracking")
    parser.add_argument("--remove", nargs='+', help="Remove AniList ID or search query from tracking database (alias to untrack but works on titles/IDs)")
    parser.add_argument("--rec", nargs=2, metavar=("ID_OR_NAME", "COUNT"), help="Record count episodes as already downloaded")
    parser.add_argument("--list-tracked", action="store_true", help="List all currently tracked series")
    parser.add_argument("--rehash", action="store_true", help="Re-download missing thumbnails/posters for a specific series")
    parser.add_argument("--rehash-all", action="store_true", help="Re-download missing thumbnails/posters for all tracked series")
    parser.add_argument("--force-images", action="store_true", help="Overwrite existing images during rehash")
    parser.add_argument("--retry-failed", action="store_true", help="Retry all previously failed episode downloads")
    parser.add_argument("--upgrade-dubs", action="store_true", help="Check for dubs of previously downloaded sub episodes and replace them")
    parser.add_argument("--subtitles-only", action="store_true", help="Only download subtitle files (VTT/ASS), skip video streams")
    parser.add_argument("--test-subs", nargs=2, metavar=("MIRURO_UUID", "EP_NUM"), help="Debug: probe and download subtitle for one episode without downloading video. e.g. --test-subs EHT-j9hg7K6M__5XDixVgMh9rKe6Nwcz 1")
    parser.add_argument("--dry-run", action="store_true", help="Probe and show what would be downloaded without actually downloading")
    parser.add_argument("--debug", action="store_true", help="Enable verbose tracing for HTTP requests")
    args = parser.parse_args()

    VERSION = "1.3.6"
    print(f"{C.BLUE}◆{C.RESET} {C.BOLD}Miruro CLI{C.RESET} {C.GRAY}v{VERSION}{C.RESET}")

    if args.debug:
        cfg.debug = True
        print(f"  {C.YELLOW}[*]{C.RESET} Verbose debug logging enabled.")

    if args.test_subs:
        raw_id, ep_num_str = args.test_subs
        # Episode number must be an integer in the API path (not 1.0)
        ep_num = int(float(ep_num_str))

        # Auto-resolve: if it looks like an AniList ID (pure digits), look up the Miruro UUID
        if raw_id.isdigit():
            print(f"  {C.BLUE}↻{C.RESET} Resolving AniList ID {raw_id} → Miruro UUID...")
            _get_cf_cookie()
            meta = fetch_miruro_metadata(int(raw_id))
            if not meta:
                print(f"  {C.RED}✘{C.RESET} Could not resolve AniList ID {raw_id}.")
                return
            miruro_uuid = meta["id"]
            print(f"  {C.GREEN}✔{C.RESET} Resolved: {miruro_uuid}")
        else:
            miruro_uuid = raw_id
            _get_cf_cookie()

        play_path = f"anime/{miruro_uuid}/episodes/{ep_num}/play"
        print(f"\n{C.BLUE}◆{C.RESET} Probing: /{play_path}")
        data = api_v1_request(play_path)
        if not data or "tracks" not in data:
            print(f"  {C.RED}✘{C.RESET} No play data returned. Try --debug for details.")
            return
        # List all sub entries found
        found = 0
        all_eng_subs = []
        for track in data.get("tracks", []):
            if track.get("track") != "ssub":
                continue
            for prov in track.get("providers", []):
                prov_name = prov.get("provider", "unknown")
                
                # Try to grab Referer from the first server entry in this provider
                dynamic_referer = f"https://{_MIRURO_DOMAIN}/"
                servers = prov.get("servers", [])
                if servers:
                    hdrs = servers[0].get("headers") or {}
                    if "Referer" in hdrs:
                        dynamic_referer = hdrs["Referer"]

                for sub in prov.get("subtitles", []):
                    lang = (sub.get("language") or sub.get("label", "?")).lower()[:3]
                    url = sub.get("file", "")
                    fmt = sub.get("format", "?")
                    is_eng = lang in ("en", "eng") or (sub.get("label") or "").lower().startswith("eng")
                    marker = f"{C.GREEN}●{C.RESET}" if is_eng else f"{C.GRAY}○{C.RESET}"
                    print(f"  {marker} [{prov_name}] {lang}.{fmt}  {url[:75]}")
                    found += 1
                    if is_eng:
                        tagged = dict(sub)
                        tagged["_prov"] = prov_name
                        tagged["_referer"] = dynamic_referer
                        all_eng_subs.append(tagged)
        if not found:
            print(f"  {C.YELLOW}⚠{C.RESET} No subtitle tracks found in ssub.")
            return
        if not all_eng_subs:
            print(f"  {C.YELLOW}⚠{C.RESET} No English subtitles found.")
            return

        # Output dir: script root / temp_files/
        import requests as _req
        out_dir = Path(__file__).parent / "temp_files"
        out_dir.mkdir(exist_ok=True)

        print(f"\n{C.BLUE}◆{C.RESET} Testing all {len(all_eng_subs)} English subtitle source(s)...")
        print(f"  Output dir: {out_dir}\n")

        results = []
        for i, sub in enumerate(all_eng_subs, 1):
            sub_url = sub.get("file", "")
            prov_name = sub.get("_prov", "?")
            fmt = sub.get("format", "vtt")
            if len(fmt) > 4: fmt = "vtt"
            out_path = out_dir / f"test_ep{ep_num}_src{i}_{prov_name}.{fmt}"

            referer = sub.get("_referer", f"https://{_MIRURO_DOMAIN}/")
            origin = referer.rstrip("/")
            hdrs = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
                               "Gecko/20100101 Firefox/133.0"),
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": referer,
                "Origin": origin,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "DNT": "1",
            }
            print(f"  [{i}/{len(all_eng_subs)}] {prov_name}  {sub_url[:65]}...")
            try:
                resp = _req.get(sub_url, headers=hdrs, timeout=20, allow_redirects=True)
                if resp.status_code == 200 and resp.content:
                    out_path.write_bytes(resp.content)
                    kb = len(resp.content) // 1024
                    results.append((True, prov_name, sub_url, f"{kb} KB → {out_path.name}"))
                    print(f"    {C.GREEN}✔{C.RESET} {resp.status_code} — {kb} KB saved → {out_path.name}")
                else:
                    msg = f"HTTP {resp.status_code}"
                    if resp.status_code == 403:
                        msg += f" (Referer: {referer})"
                    results.append((False, prov_name, sub_url, msg))
                    print(f"    {C.RED}✘{C.RESET} {msg}")
            except Exception as e:
                results.append((False, prov_name, sub_url, str(e)))
                print(f"    {C.RED}✘{C.RESET} Error: {e}")

        # Summary
        passed = [r for r in results if r[0]]
        failed = [r for r in results if not r[0]]
        print(f"\n{C.BLUE}━━ Subtitle Test Results ━━{C.RESET}")
        print(f"  {C.GREEN}✔ Passed: {len(passed)}{C.RESET}  {C.RED}✘ Failed: {len(failed)}{C.RESET}\n")
        for ok, prov, url, msg in results:
            sym = f"{C.GREEN}✔{C.RESET}" if ok else f"{C.RED}✘{C.RESET}"
            print(f"  {sym} [{prov}] {msg}")
        return

    if args.list_tracked:
        tracked = db.get_tracked_series()
        print(f"\n{C.BLUE}◆{C.RESET} Tracked Series ({len(tracked)}):")
        for s in tracked:
            print(f"  - {s['anime_id']}: {s['title']}")
        print()
        return

    def resolve_input_to_anilist_id(query: str) -> int | None:
        if not query:
            return None
        # 1. Pure AniList ID
        if query.isdigit():
            return int(query)
            
        import re
        
        # 2. Extract AniList ID from info URL (e.g. miruro.bz/info/12345)
        m_info = re.search(r'info/(\d+)', query)
        if m_info:
            return int(m_info.group(1))
            
        # 3. Extract Miruro UUID from URL (e.g. watch/UUID/..., anime/UUID/...)
        miruro_uuid = query
        m_watch = re.search(r'(?:watch|anime)/([^/?]+)', query)
        if m_watch:
            miruro_uuid = m_watch.group(1)
            
        # 4. Resolve UUID via API
        if len(miruro_uuid) > 20 and not miruro_uuid.isdigit():
            print(f"  {C.YELLOW}⟳{C.RESET} Resolving Miruro UUID to AniList ID...")
            data = api_v1_request(f"anime/{miruro_uuid}")
            if data and "external_ids" in data and "anilist" in data["external_ids"]:
                anilist_id = int(data["external_ids"]["anilist"][0])
                print(f"  {C.GREEN}✔{C.RESET} Resolved to AniList ID: {anilist_id}")
                return anilist_id
            else:
                print(f"  {C.RED}✘{C.RESET} Failed to resolve AniList ID for {miruro_uuid}.")
                return None
                
        return None

    if args.add:
        for query in args.add:
            try:
                anilist_id = resolve_input_to_anilist_id(query)
                if anilist_id:
                    fetch_and_add_series(anilist_id, force=True)
                else:
                    print(f"  {C.RED}✘{C.RESET} Invalid ID or URL format: {query}")
            except Exception as e:
                print(f"  {C.RED}✘{C.RESET} Failed to add {query}: {e}")
        return
    if args.remove:
        query = " ".join(args.remove)
        if query.isdigit():
            anilist_id = int(query)
            series = db.get_series(anilist_id)
            if series:
                with db.db() as conn:
                    conn.execute("UPDATE series SET is_tracked = 0 WHERE anime_id = ?", (anilist_id,))
                print(f"{C.GREEN}✔{C.RESET} Untracked '{series.get('title')}' (ID: {anilist_id}).")
            else:
                print(f"{C.RED}✘{C.RESET} ID {anilist_id} not found in database.")
        else:
            tracked = db.get_tracked_series()
            matches = [s for s in tracked if query.lower() in str(s.get("title", "")).lower()]
            if not matches:
                print(f"{C.RED}✘{C.RESET} No tracked series found matching '{query}'.")
            else:
                best = matches[0]
                with db.db() as conn:
                    conn.execute("UPDATE series SET is_tracked = 0 WHERE anime_id = ?", (best["anime_id"],))
                print(f"{C.GREEN}✔{C.RESET} Untracked '{best.get('title')}' (ID: {best['anime_id']}).")
        return

    if args.rec:
        query, count_val = args.rec
        try:
            count_val = int(count_val)
        except ValueError:
            print(f"{C.RED}✘{C.RESET} Count must be an integer.")
            return
            
        tracked = db.get_tracked_series()
        if query.isdigit():
            matches = [s for s in tracked if str(s["anime_id"]) == query]
        else:
            matches = [s for s in tracked if query.lower() in str(s.get("title", "")).lower()]
            
        if not matches:
            print(f"{C.RED}✘{C.RESET} No tracked series found matching '{query}'.")
            return
            
        best = matches[0]
        anime_id = best["anime_id"]
        
        new_dl_eps = [float(i) for i in range(1, count_val + 1)]
        db.update_tracking_history(anime_id=anime_id, downloaded_eps=new_dl_eps)
        print(f"{C.GREEN}✔{C.RESET} Updated downloaded record for '{best['title']}' to eps 1 through {count_val}")
        return

    if args.rehash_all:
        do_global_rehash(args.force_images)
        return

    manual_id = None
    if args.link:
        manual_id = resolve_input_to_anilist_id(args.link)
        if not manual_id:
            print(f"{C.RED}✘{C.RESET} Could not parse AniList ID from link or URL.")
            sys.exit(1)
                
        
    if args.untrack and manual_id:
        with db.db() as conn:
            conn.execute("UPDATE series SET is_tracked = 0 WHERE anime_id = ?", (manual_id,))
        print(f"  {C.GREEN}✔{C.RESET} Untracked series {manual_id}")
        return
        
    if args.rehash and manual_id:
        s = db.get_series(manual_id)
        if s:
            print(f"\n{C.BLUE}◆{C.RESET} Rehashing: {s['title']}")
            rehash_series(s, args.force_images)
            print(f"  {C.GREEN}✔{C.RESET} Rehash complete.")
        else:
            print(f"{C.RED}✘{C.RESET} Series {manual_id} not in DB.")
        return

    if args.retry_failed:
        failed = db.get_all_failed_downloads()
        if not failed:
            print(f"  {C.GREEN}✔{C.RESET} No failed downloads to retry.")
            return
            
        print(f"\n{C.BLUE}◆{C.RESET} Retrying {len(failed)} failed episodes...")
        succeeded = 0
        retried = 0
        for f in failed:
            anime_id = f["anime_id"]
            ep_num = f["episode"]
            cat = f.get("category", "dub")
            
            series = db.get_series(anime_id)
            if not series:
                continue
            title = series["title"]
            
            miruro_id = fetch_miruro_id(anime_id)
            if not miruro_id:
                continue
                
            eps = fetch_v1_episodes(miruro_id)
            if not eps:
                continue
                
            ep_data = next((e for e in eps if e["episode_number"] == ep_num), None)
            if not ep_data:
                continue
                
            retried += 1
            print(f"\n{C.MAGENTA}{C.BOLD}▶ Retrying: {title}{C.RESET} {C.GRAY}EP {int(ep_num)} ({cat}){C.RESET}")
            
            ep_copy = ep_data.copy()
            ep_copy["_miruro_id"] = miruro_id
            ep_copy["_target_category"] = cat
            
            success = download_episode(anime_id, title, ep_copy, args.provider, cat, args.quality, subtitles_only=args.subtitles_only)
            
            if success:
                db.clear_failed_download(anime_id, ep_num, cat)
                succeeded += 1
                hist = db.get_tracking_history(anime_id)
                downloaded_eps = hist.get("downloaded_eps", [])
                if ep_num not in downloaded_eps:
                    downloaded_eps.append(ep_num)
                    downloaded_eps.sort()
                new_last = max(hist.get("last_episode_aired", 0), ep_num)
                db.update_tracking_history(anime_id, new_last, downloaded_eps)
                
        print(f"\n{C.GREEN}✔{C.RESET} Retry session complete. Succeeded: {succeeded}/{retried}")
        return

    if args.auto:
        tracked = db.get_tracked_series()
        if not tracked:
            print(f"{C.YELLOW}⚠{C.RESET} No tracked series found. Use --add <id> first.")
            return

        hls_downloader.wait_for_internet()
            
        import concurrent.futures
        print(f"\n{C.BLUE}◆{C.RESET} Probing {len(tracked)} series in parallel...")
        
        _get_cf_cookie()
        
        import time

        t0 = time.monotonic()
        tasks = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.probe_workers) as executor:
            future_to_s = {
                executor.submit(probe_worker_auto, s, args.provider, args.category, args.quality, args.upgrade_dubs): s 
                for s in tracked
            }
            for future in concurrent.futures.as_completed(future_to_s):
                try:
                    res = future.result()
                    if res.get("error"):
                        print(f"{C.RED}✘{C.RESET} {res.get('title', 'Unknown')}: {res['error']}")
                        continue
                        
                    tasks.append(res)
                    
                    new_c = len(res['eps_to_dl'])
                    if new_c > 0:
                        upg_msg = f"  {C.CYAN}+{res['upgrades']} dub upgrades{C.RESET}" if res.get('upgrades') else ""
                        print(f" {C.GREEN}✔{C.RESET} {C.WHITE}{res['title']}{C.RESET}\n"
                              f"    {C.GRAY}Released: {res['total_released']}  •  {C.YELLOW}+{new_c} new{C.RESET}{upg_msg}")
                    else:
                        print(f" {C.GREEN}✔{C.RESET} {C.WHITE}{res['title']}{C.RESET}\n    {C.GRAY}Released: {res['total_released']}  •  up to date{C.RESET}")
                        
                except Exception as e:
                    print(f"{C.RED}✘{C.RESET} Probe failed: {e}")
                    
        elapsed = time.monotonic() - t0
        tasks = [t for t in tasks if t['eps_to_dl']]
        
        tasks.sort(key=lambda x: (x.get("priority", 0), x["anime_id"]), reverse=True)
        
        total_eps = sum(len(t['eps_to_dl']) for t in tasks)
        print(f"\n{C.BLUE}◆{C.RESET} Probe done • {C.GREEN}{len(tasks)} series queued{C.RESET} • {C.YELLOW}{total_eps} episodes total{C.RESET} • elapsed: {elapsed:.1f}s\n")
        
        if not tasks:
            return
        
        print(f"{C.BLUE}◆{C.RESET} Download queue (priority order):")
        for i, t in enumerate(tasks, 1):
            pri_str = f"{C.YELLOW}[P{t.get('priority', 0)}]{C.RESET} " if t.get('priority', 0) > 0 else ""
            ep_count = len(t['eps_to_dl'])
            print(f"  {C.GRAY}{i:2d}.{C.RESET} {pri_str}{t['title']} {C.GRAY}({ep_count} eps){C.RESET}")
        print()
            
        if args.dry_run:
            print(f"\n{C.YELLOW}⚠{C.RESET} Dry run mode — no episodes will be downloaded.")
            return

        total_downloaded = 0
        total_failed = 0
        total_skipped = 0
        for idx, t in enumerate(tasks, 1):
            pri_tag = f" {C.YELLOW}[P{t.get('priority', 0)}]{C.RESET}" if t.get('priority', 0) > 0 else ""
            print(f"\n{C.MAGENTA}{C.BOLD}▶  {t['title']}{C.RESET} {C.GRAY}(ID: {t['anime_id']}){C.RESET}{pri_tag}")
            for ep in t['eps_to_dl']:
                ep_num = ep.get("episode_number", ep.get("number"))
                success = download_episode(
                    t['anime_id'], t['title'], ep, t['provider'], t['category'], t['quality'], subtitles_only=args.subtitles_only
                )
                if success:
                    total_downloaded += 1
                    db.clear_failed_download(t['anime_id'], ep_num, ep.get("_target_category", t['category']))
                    ep_actual_cat = ep.get("_target_category", t['category'])
                    if args.upgrade_dubs and ep_actual_cat == "dub":
                        safe_title = sanitize_filename(t['title'])
                        season_num = extract_season_number(t["title"])
                        season_dir = get_jellyfin_dir(t["title"], t["anime_id"]) / f"Season {season_num:02d}"
                        sub_file = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}.mp4"
                        if sub_file.exists():
                            try:
                                sub_file.unlink()
                                sub_thumb_jpg = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}-thumb.jpg"
                                sub_thumb_png = season_dir / f"{safe_title} - S{season_num:02d}E{int(ep_num):02d}-thumb.png"
                                if sub_thumb_jpg.exists(): sub_thumb_jpg.unlink()
                                if sub_thumb_png.exists(): sub_thumb_png.unlink()
                                print(f"    {C.GREEN}✔{C.RESET} Deleted old subbed file for EP {ep_num}")
                            except Exception:
                                pass
                                
                    hist = db.get_tracking_history(t['anime_id'])
                    downloaded_eps = hist.get("downloaded_eps", [])
                    if ep_num not in downloaded_eps:
                        downloaded_eps.append(ep_num)
                        downloaded_eps.sort()
                    new_last = max(hist.get("last_episode_aired", 0), ep_num)
                    db.update_tracking_history(t['anime_id'], new_last, downloaded_eps)
                    db.upsert_episode(t['anime_id'], {
                        "number": ep_num,
                        "session": str(ep_num),
                        "snapshot_url": ep.get("thumbnail_url"),
                        "title": ep.get("title"),
                        "created_at": ep.get("aired_on")
                    })
                else:
                    # Distinguish true failures from "sub already on disk" skips
                    # by checking if the sub file exists on disk
                    safe_title_t = sanitize_filename(t['title'])
                    season_num_t = extract_season_number(t['title'])
                    season_dir_t = get_jellyfin_dir(t['title'], t['anime_id']) / f"Season {season_num_t:02d}"
                    sub_file_t = season_dir_t / f"{safe_title_t} - S{season_num_t:02d}E{int(ep_num):02d}.mp4"
                    dub_file_t = season_dir_t / f"{safe_title_t} - S{season_num_t:02d}E{int(ep_num):02d} - Dub.mp4"
                    if sub_file_t.exists() and not dub_file_t.exists():
                        total_skipped += 1
                    else:
                        total_failed += 1
                        db.record_failed_download(
                            t['anime_id'], ep_num,
                            category=ep.get("_target_category", t['category']),
                            error="All servers failed probe or no streams found"
                        )

        elapsed_total = time.monotonic() - t0
        mins = int(elapsed_total // 60)
        secs = int(elapsed_total % 60)
        print(f"\n{'═' * 50}")
        print(f"  {C.BOLD}Download Session Complete{C.RESET}")
        print(f"  {'─' * 46}")
        print(f"  {C.GREEN}✔{C.RESET} Downloaded:  {total_downloaded} episodes")
        if total_failed > 0:
            print(f"  {C.RED}✘{C.RESET} Failed:      {total_failed} episodes")
        if total_skipped > 0:
            print(f"  {C.YELLOW}⚠{C.RESET} Skipped:     {total_skipped} episodes")
        print(f"  {C.BLUE}◆{C.RESET} Time:        {mins}m {secs}s")
        print(f"{'═' * 50}")
            
    elif manual_id:
        target_eps = parse_episodes_arg(args.episodes)
        process_series(manual_id, target_eps, args.provider, args.category, args.quality, upgrade_dubs=args.upgrade_dubs, subtitles_only=args.subtitles_only)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
