<div align="center">

# 🎬 Miruro Custom CLI

### An autonomous anime downloader for Miruro and its providers

Automatic tracking · Cloudflare bypass · multi-provider HLS racing · local library management

<br>

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-local%20DB-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)

<br>

[Features](#-features) · [Installation](#-installation) · [Quick Start](#-quick-start) · [CLI Reference](#-cli-reference) · [Web Manager](#-web-manager) · [Advanced](#-advanced-features)

</div>

---

## ✨ Features

| | Feature | Description |
|---|---|---|
| 🤖 | **Automated Tracking** | Add your favorite series with `--auto` and the CLI periodically probes and downloads newly aired episodes. |
| 🛡️ | **Stealth Cloudflare Bypass** | Spins up headless browser sessions to solve and cache Cloudflare challenges when the API blocks requests. |
| ⚡ | **HLS Chunk Racing** | Finds streams across multiple CDN providers (Animepahe, Anikoto, Icarus, …) and parallelizes downloads. |
| 🎯 | **Strict Quality Filtering** | Parses raw HLS metadata to fall back safely, or lock strictly to a target resolution such as 1080p. |
| 🔊 | **Sub/Dub Hierarchy** | Prefers dubs, falls back to subs if a dub hasn't aired, and can upgrade subs to dubs later with `--upgrade-dubs`. |
| 🗄️ | **Local SQLite Database** | Persists download history, tracks watched episodes, and prevents duplicate processing. |
| 🖥️ | **Web Manager** | Dashboard with library grid, queue, airing calendar, stats, and a live console. |

---

## 🚀 Installation

### Prerequisites

- **Python 3.10+**
- **`ffmpeg`** installed and on your system `PATH` (required to stitch HLS streams together)
- *(Optional, recommended)* **Google Chrome or Chromium**, used by `curl_cffi` for TLS fingerprint spoofing and Cloudflare bypass

### Setup

```bash
# 1. Clone the repository
git clone https://github.com/Varun-Thete/miruro-custom-cli.git
cd miruro-custom-cli

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## ⚡ Quick Start

### 1️⃣ Add a series to tracking

Use an AniList ID, a raw Miruro UUID, or a direct Miruro watch/anime URL.

```bash
# By AniList ID
python downloader.py --add 163142

# By direct Miruro URL
python downloader.py --add "https://www.miruro.bz/watch/yu-vVgTCWNdDu1JNx2FRonr3tqK71f0z/black-clover-season-2?ep=1"

# With a priority (higher priority is downloaded first during --auto)
python downloader.py --add 163142 --priority 100
```

> [!NOTE]
> The CLI queries the Miruro API to map URLs and UUIDs to their AniList IDs automatically.

### 2️⃣ Auto-download new episodes

Probe every tracked series, fetch schedules, and download missing episodes to your `Downloads` folder.

```bash
python downloader.py --auto
```

### 3️⃣ Download a single series

Target one specific series for a manual one-off download, without running a full database auto-scan. Use this when you only want to process a single show.

```bash
python downloader.py -l 163142
```

**Variations**

```bash
# By Miruro URL instead of an AniList ID
python downloader.py -l "https://www.miruro.bz/watch/yu-vVgTCWNdDu1JNx2FRonr3tqK71f0z/black-clover-season-2?ep=1"

# Specific episode, or a range
python downloader.py -l 163142 -e 1
python downloader.py -l 163142 -e 3-5

# Redownload an episode that's already marked as downloaded
python downloader.py -l 163142 -e 1 --force

# Choose audio category (default is dub, falling back to sub)
python downloader.py -l 163142 -c sub

# Lock to a resolution
python downloader.py -l 163142 -q 1080

# Use one provider instead of racing all of them
python downloader.py -l 163142 -p yuki

# Combine options: episodes 3-5, sub, 1080p
python downloader.py -l 163142 -e 3-5 -c sub -q 1080

# Subtitles only (skip the video)
python downloader.py -l 163142 --subtitles-only

# Preview what would be downloaded without writing files
python downloader.py -l 163142 --dry-run
```

### 4️⃣ Target a specific quality

```bash
python downloader.py --auto -q 1080
```

### 5️⃣ Manage your library

```bash
python downloader.py --list-tracked          # List tracked series
python downloader.py --remove 163142         # Untrack a series
python downloader.py --retry-failed          # Retry failed downloads
python downloader.py --rec 163142 5          # Mark 5 episodes as downloaded
python downloader.py --add 163142 --priority 50   # Update priority
```

---

## 📖 CLI Reference

Every argument available in `downloader.py`.

### 🗂️ Database & Tracking

| Argument | Description | Example |
|---|---|---|
| `--add <ID_OR_URL> [...]` | Add one or more series (AniList IDs, Miruro UUIDs, or `miruro.bz` watch links). | `--add 163142` |
| `--priority <INT>` | Priority score when adding/updating. Higher gets probed and downloaded first in `--auto`. | `--add 163142 --priority 100` |
| `--untrack` | Remove a series from tracking when combined with `--link`. | `-l 163142 --untrack` |
| `--remove <ID_OR_NAME>` | Alias to untrack. Accepts IDs or partial title matches. | `--remove "Frieren"` |
| `--list-tracked` | Print all tracked series and their IDs. | `--list-tracked` |
| `--rec <ID_OR_NAME> <COUNT>` | Manually mark `<COUNT>` episodes as downloaded so they are skipped. | `--rec "Frieren" 5` |

### ⬇️ Downloading

| Argument | Description | Example |
|---|---|---|
| `-l`, `--link <ID_OR_URL>` | Target one series for a manual one-off download. Omit it and use `--auto` to process everything. | `-l 163142` |
| `-a`, `--auto` | Probe all tracked series and download new episodes. | `--auto` |
| `-e`, `--episodes <RANGE>` | Target specific episodes (single numbers or ranges). | `-l 163142 -e 3-5` |
| `-f`, `--force` | Redownload even if already marked as downloaded. | `-l 163142 -e 1 --force` |
| `--retry-failed` | Retry episodes that failed (network drops, missing chunks, …). | `--retry-failed` |

### 🎛️ Filtering & Preferences

| Argument | Description | Example |
|---|---|---|
| `-c`, `--category <sub\|dub\|raw>` | Preferred audio category. Default is `dub`, falling back to `sub` if no dub has aired. | `--auto -c sub` |
| `-q`, `--quality <HEIGHT>` | Target resolution height, strictly enforced by evaluating raw `.m3u8` playlists. | `--auto -q 1080` |
| `-p`, `--provider <NAME>` | Target a specific CDN provider (e.g. `hop`, `yuki`, `alpha`). Default is `all`, which races available streams. | `-l 163142 -p yuki` |
| `--upgrade-dubs` | Find series where you downloaded the `sub` because the dub wasn't out, check if the `dub` exists now, and replace the file. | `--auto --upgrade-dubs` |
| `--subtitles-only` | Download only subtitle files (`.vtt`/`.ass`) and skip the video. | `-l 163142 --subtitles-only` |

### 🧰 Utilities & Debugging

| Argument | Description | Example |
|---|---|---|
| `--refresh-airing` | Fetch the latest airing schedule for tracked series and update the database. Populates the Web Manager calendar. | `--refresh-airing` |
| `--rehash` / `--rehash-all` | Re-download missing metadata images (posters/backgrounds) for one series (requires `-l`) or all series. | `--rehash-all` |
| `--force-images` | Combine with rehash flags to overwrite existing images instead of only grabbing missing ones. | `--rehash-all --force-images` |
| `--dry-run` | Show what *would* be downloaded without writing any files. | `--auto --dry-run` |
| `--debug` | Verbose output of HTTP requests and raw API responses. | `--debug` |
| `--test-subs <UUID> <EP_NUM>` | Developer flag: probe and fetch subtitle tracks for one episode without touching the video. | `--test-subs <UUID> 1` |

---

## 🖥️ Web Manager

A modern web UI for managing your tracked series:

- 📚 Library grid
- 📋 Queue system
- 📅 Airing calendar
- 📊 Download stats
- ✅ Bulk selection actions
- 📟 **Live console** streaming job output over Server-Sent Events

```bash
python manager_server.py
```

### Server options

| Option | Default | Description | Example |
|---|---|---|---|
| `--host <IP>` | `0.0.0.0` | Interface to bind to. `0.0.0.0` is reachable from your whole local network; use `127.0.0.1` to restrict it to this machine. | `python manager_server.py --host 127.0.0.1` |
| `--port <PORT>` | `5300` | Port for the web server. | `python manager_server.py --port 8080` |
| `--max-jobs <INT>` | `1` | Max simultaneous background downloader processes in the queue. Keeping it at 1 prevents bandwidth congestion. | `python manager_server.py --max-jobs 2` |
| `MIRURO_UI_TOKEN` *(env var)* | none | Secures the UI behind a token. Access it at `http://<server-ip>:5300/?token=secret`. | `MIRURO_UI_TOKEN=secret python manager_server.py` |

> [!WARNING]
> The server binds to `0.0.0.0` by default, so anyone on your network can reach it. Set `MIRURO_UI_TOKEN` or bind to `127.0.0.1` if that isn't what you want.

---

## 🛠️ Advanced Features

### Smart stream resolution filtering

Providers sometimes obscure video resolutions, labeling 720p streams the same as 1080p in their JSON endpoints. The CLI parses `.m3u8` master playlists, evaluates the real heights, and defaults to `0` (Unknown) for streams lacking metadata, so you never accidentally downgrade quality while racing servers.

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome! Feel free to open an issue or submit a pull request.

## 📝 License

This project is licensed under the **MIT License**.
