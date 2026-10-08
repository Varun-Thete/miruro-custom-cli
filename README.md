# Miruro Custom CLI Downloader

An advanced, autonomous CLI tool for batch downloading anime streams from Miruro and its providers. Features automatic background tracking, Cloudflare bypass, multi-provider HLS chunk racing, database synchronization, and local media management.

## ✨ Features

- **Automated Tracking (`--auto`)**: Add your favorite series and the CLI will periodically probe and seamlessly download newly aired episodes as they become available.
- **Stealth Cloudflare Bypass**: Automatically spins up headless browser sessions to solve and cache Cloudflare challenges when the API blocks requests.
- **HLS Chunk Racing Engine**: Intelligently identifies available streams across multiple CDN providers (Animepahe, Anikoto, Icarus, etc.) and parallelizes downloads.
- **Strict Quality Filtering**: Built-in resolution logic that parses raw HLS metadata and safely falls back or strictly enforces target resolutions (e.g., locking to 1080p).
- **Sub/Dub Hierarchy**: Prefer Dubs? The CLI searches for Dubs first and gracefully falls back to Subs if a Dub hasn't aired yet. Upgrade logic (`--upgrade-dubs`) allows swapping Subs with Dubs as they release.
- **Local SQLite Database**: Persists your download history, tracks watched episodes, and prevents duplicate processing.

## 🚀 Installation

### Prerequisites
- Python 3.10+
- `ffmpeg` installed and added to your system `PATH` (crucial for stitching HLS streams together)
- (Optional but recommended) Google Chrome or Chromium (used by `curl_cffi` for advanced TLS fingerprint spoofing and Cloudflare bypass)

### Setup
1. Clone this repository:
   ```bash
   git clone https://github.com/Varun-Thete/miruro-custom-cli.git
   cd miruro-custom-cli
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## 📚 Usage

### 1. Add Series to Tracking
Use the AniList ID, a raw Miruro UUID, or a direct Miruro watch/anime URL to add a series to your local database.
```bash
# By AniList ID
python downloader.py --add 163142

# By direct Miruro URL
python downloader.py --add "https://www.miruro.bz/watch/yu-vVgTCWNdDu1JNx2FRonr3tqK71f0z/black-clover-season-2?ep=1"

# With a specific priority (higher priority series are downloaded first during --auto)
python downloader.py --add 163142 --priority 100
```
*Note: The CLI proactively queries the Miruro API to map URLs and UUIDs seamlessly to their AniList IDs.*

### 2. Auto-Download New Episodes
Probe all tracked series in your database, fetch schedules, and download any missing episodes natively to your `Downloads` folder:
```bash
python downloader.py --auto
```

### 3. Target Specific Qualities
Lock downloads to 1080p specifically:
```bash
python downloader.py --auto -q 1080
```

### 4. Advanced Management
- **List tracked series:**
  ```bash
  python downloader.py --list-tracked
  ```
- **Untrack/Remove a series:**
  ```bash
  python downloader.py --remove 163142
  ```
- **Retry failed downloads:**
  ```bash
  python downloader.py --retry-failed
  ```
- **Mark an episode as downloaded manually:**
  ```bash
  python downloader.py --rec 163142 5
  ```
- **Update priority for an existing series:**
  ```bash
  python downloader.py --add 163142 --priority 50
  ```

### Command Line Arguments Reference

Below is a detailed list of every argument and feature available in `downloader.py`:

#### Database & Tracking
- **`--add <ID_OR_URL> [ID_OR_URL...]`**
  Adds one or more series to your tracking database. You can pass pure AniList IDs, Miruro UUIDs, or full `miruro.bz` watch links.
  *Usage:* `python downloader.py --add 163142`
- **`--priority <INT>`**
  Assigns a priority score to a series when adding or updating. Higher numbers get probed and downloaded first during `--auto`.
  *Usage:* `python downloader.py --add 163142 --priority 100`
- **`--untrack`**
  Removes a series from tracking when combined with `--link`.
  *Usage:* `python downloader.py -l 163142 --untrack`
- **`--remove <ID_OR_NAME>`**
  Alias to untrack, but accepts partial title matches or IDs directly.
  *Usage:* `python downloader.py --remove "Frieren"`
- **`--list-tracked`**
  Prints a list of all currently tracked series and their IDs.
  *Usage:* `python downloader.py --list-tracked`
- **`--rec <ID_OR_NAME> <COUNT>`**
  Manually mark `<COUNT>` episodes as downloaded in the database to prevent the script from downloading them.
  *Usage:* `python downloader.py --rec "Frieren" 5`

#### Downloading Operations
- **`-l, --link <ID_OR_URL>`**
  Target a specific series for a manual one-off download. If omitted, you must use `--auto` to process everything.
  *Usage:* `python downloader.py -l 163142`
- **`-a, --auto`**
  Probe all tracked series in your database for new episodes and download them automatically.
  *Usage:* `python downloader.py --auto`
- **`-e, --episodes <RANGE>`**
  Target specific episodes instead of all un-downloaded ones. Supports single numbers and ranges.
  *Usage:* `python downloader.py -l 163142 -e 1` or `... -e 3-5`
- **`-f, --force`**
  Force redownload an episode even if it is already marked as downloaded in the local database.
  *Usage:* `python downloader.py -l 163142 -e 1 --force`
- **`--retry-failed`**
  Re-attempts to download any episodes that previously failed (due to network drops, missing chunks, etc.).
  *Usage:* `python downloader.py --retry-failed`

#### Filtering & Preferences
- **`-c, --category <sub|dub|raw>`**
  Specify the preferred audio category. Default is `dub` (which safely falls back to `sub` if a dub isn't aired yet).
  *Usage:* `python downloader.py --auto -c sub`
- **`-q, --quality <HEIGHT>`**
  Target resolution height. The CLI evaluates raw `.m3u8` playlists to strictly enforce this.
  *Usage:* `python downloader.py --auto -q 1080`
- **`-p, --provider <NAME>`**
  Target a specific CDN provider (e.g., `hop`, `yuki`, `alpha`). Default is `all`, which races available streams.
  *Usage:* `python downloader.py -l 163142 -p yuki`
- **`--upgrade-dubs`**
  Scan your library for series where you previously downloaded the `sub` (because the dub wasn't out), check if the `dub` is now available, and replace the file.
  *Usage:* `python downloader.py --auto --upgrade-dubs`
- **`--subtitles-only`**
  Download only the subtitle files (`.vtt`/`.ass`) and skip the heavy video streams. Great for extracting subs.
  *Usage:* `python downloader.py -l 163142 --subtitles-only`

#### Utilities & Debugging
- **`--refresh-airing`**
  Fetch the latest airing schedule (next episode release date/time) for all tracked series and update the local database. Used to populate the Web Manager Calendar.
  *Usage:* `python downloader.py --refresh-airing`
- **`--rehash` / `--rehash-all`**
  Re-download missing metadata images (posters/backgrounds) for a specific series (requires `-l`) or all series.
  *Usage:* `python downloader.py --rehash-all`
- **`--force-images`**
  Combine with rehash flags to overwrite existing posters/backgrounds instead of only grabbing missing ones.
- **`--dry-run`**
  Probe the API and database to show exactly what *would* be downloaded, without actually writing any files.
  *Usage:* `python downloader.py --auto --dry-run`
- **`--debug`**
  Enable verbose printing of HTTP requests and raw API responses for troubleshooting.
- **`--test-subs <UUID> <EP_NUM>`**
  Developer flag to probe and fetch subtitle tracks for a single episode without touching the video.

### 5. Web Manager (Dashboard)
A modern web UI to manage your tracked series: Library grid, Queue system, Airing Calendar, Download Stats, bulk selection actions, and a **live console** streaming job output over Server-Sent Events.

**Server Arguments & Environment Variables:**
- **`--host <IP>`**
  The interface to bind the server to. Default is `0.0.0.0` (accessible from any device on your local network). Use `127.0.0.1` to restrict access strictly to the local machine.
  *Usage:* `python manager_server.py --host 127.0.0.1`
- **`--port <PORT>`**
  The port to run the web server on. Default is `5300`.
  *Usage:* `python manager_server.py --port 8080`
- **`--max-jobs <INT>`**
  The maximum number of background downloader processes allowed to run simultaneously in the queue. Default is `1` (prevents bandwidth congestion).
  *Usage:* `python manager_server.py --max-jobs 2`
- **`MIRURO_UI_TOKEN` (Environment Variable)**
  Since the server binds to the local network (`0.0.0.0`) by default, anyone on your network can access it. Set this variable to secure the UI behind a token. You will then access it via `http://<server-ip>:5300/?token=secret`.
  *Usage:* `MIRURO_UI_TOKEN=secret python manager_server.py`

## 🛠 Advanced Features

### Smart Stream Resolution Filtering
Providers sometimes obscure video resolutions, labeling 720p streams identically to 1080p within their JSON endpoints. This CLI parses `.m3u8` master playlists, dynamically evaluates heights, and safely defaults to `0` (Unknown) for streams lacking metadata, ensuring you never accidentally downgrade quality while racing servers.

## 🤝 Contributing
Contributions, issues, and feature requests are welcome!

## 📝 License
This project is licensed under the MIT License.
