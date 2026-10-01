# ==============================================================================
# Title: Miruro Downloader Configuration Module
# Creator: Vernox
# Description: Centralized configuration for all tunable parameters across the
#              downloader suite. Override via --config flag or environment vars.
# ==============================================================================

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Base Paths
# ---------------------------------------------------------------------------
CLI_DIR = Path(__file__).resolve().parent
DB_PATH = CLI_DIR / "miruro.db"

# ---------------------------------------------------------------------------
# Default Configuration
# ---------------------------------------------------------------------------
_DEFAULTS = {
    # ── API Server ────────────────────────────────────────────────────────
    "api_port": 5600,
    "api_timeout": 10,          # seconds per outbound API request
    "api_retries": 3,           # max retries for api_get()

    # ── Network & Connectivity ────────────────────────────────────────────
    "dns_check_hosts": [("google.com", 80), ("github.com", 80)],
    "dns_check_timeout": 2.5,   # seconds for is_online() socket ping
    "net_recovery_interval": 5.0,  # seconds between connectivity polls

    # ── HLS Download Engine ───────────────────────────────────────────────
    "max_retries": 10,          # per-chunk retry limit
    "chunk_timeout": 15,        # seconds per chunk GET
    "probe_timeout": 15,        # seconds for server probe requests
    "download_workers": 8,      # parallel chunk download threads
    "future_timeout": 60,       # seconds before as_completed watchdog fires
    "fail_threshold": 5,        # abort episode after N permanent chunk failures

    # ── Server Health Cache ───────────────────────────────────────────────
    "health_ttl": 300,          # seconds to blacklist a dead server (5 min)

    # ── Concurrency Limits ────────────────────────────────────────────────
    "probe_workers": 10,        # parallel series probing threads (--auto)
    "stream_discovery_workers": 6,  # parallel provider query threads
    "server_race_workers": 6,   # parallel server probe threads
    "rehash_workers": 10,       # parallel workers for --rehash
    "max_outbound_connections": 30,  # global semaphore cap

    # ── Rate Limiting ─────────────────────────────────────────────────────
    "max_429_retries": 8,       # per-chunk 429 retry limit

    # ── Storage & Naming ──────────────────────────────────────────────────
    "download_dir": "Downloads",
    "season_format": "Season {n:02d}",
    "default_category": "dub",
    "default_provider": "kiwi",
    "default_quality": 1080,
    "default_priority": 0,      # priority assigned to newly added series

    # ── TLS Impersonation ─────────────────────────────────────────────────
    "tls_impersonate": "chrome110",
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),

    # ── Debug Logging ─────────────────────────────────────────────────────
    "debug": False,             # verbose HLS chunk/retry/racing logs
}


class _Config:
    """
    Attribute-access wrapper around the configuration dictionary.
    Supports loading overrides from a YAML file.
    """

    def __init__(self):
        self._data = dict(_DEFAULTS)

    def __getattr__(self, name):
        if name.startswith("_"):
            return super().__getattribute__(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(f"Config has no setting '{name}'")

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._data[name] = value

    def load_yaml(self, path: str | Path):
        """Load overrides from a YAML config file. Requires PyYAML."""
        try:
            import yaml
        except ImportError:
            raise ImportError(
                "PyYAML is required for --config. Install it: pip install pyyaml"
            )
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            overrides = yaml.safe_load(f) or {}
        for key, val in overrides.items():
            if key in self._data:
                self._data[key] = val

    def load_env(self):
        """Load overrides from environment variables prefixed with MIRURO_."""
        for key in self._data:
            env_key = f"MIRURO_{key.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                # Skip complex types that can't be safely parsed from env vars
                default_type = type(self._data[key])
                if default_type in (list, tuple, dict):
                    continue
                try:
                    if default_type == bool:
                        self._data[key] = env_val.lower() in ("1", "true", "yes")
                    elif default_type == int:
                        self._data[key] = int(env_val)
                    elif default_type == float:
                        self._data[key] = float(env_val)
                    else:
                        self._data[key] = env_val
                except (ValueError, TypeError):
                    pass

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self._data['api_port']}"

    def summary(self) -> str:
        """Return a human-readable summary of current configuration."""
        lines = ["Current Configuration:"]
        for k, v in sorted(self._data.items()):
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
cfg = _Config()
cfg.load_env()
