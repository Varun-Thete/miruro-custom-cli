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

### 5. Web Manager (Dashboard)
A modern web UI to manage your tracked series: poster grid, priority stepper, add/untrack/delete, fix episode counts, trigger downloads / auto-scan, and a **live console** streaming job output over Server-Sent Events.
```bash
python manager_server.py                 # all interfaces: http://<server-ip>:5300
python manager_server.py --host 127.0.0.1  # local machine only
MIRURO_UI_TOKEN=secret python manager_server.py --host 0.0.0.0   # then open /?token=secret
```
*Listens on all interfaces by default so it is reachable on your local network (the startup banner prints the exact URL). Set `MIRURO_UI_TOKEN` to require a token, or use `--host 127.0.0.1` to restrict it to this machine.*

## 🛠 Advanced Features

### Smart Stream Resolution Filtering
Providers sometimes obscure video resolutions, labeling 720p streams identically to 1080p within their JSON endpoints. This CLI parses `.m3u8` master playlists, dynamically evaluates heights, and safely defaults to `0` (Unknown) for streams lacking metadata, ensuring you never accidentally downgrade quality while racing servers.

## 🤝 Contributing
Contributions, issues, and feature requests are welcome!

## 📝 License
This project is licensed under the MIT License.
