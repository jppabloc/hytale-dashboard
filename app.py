"""Hytale Server Dashboard – FastAPI Backend."""

import os
import json
import secrets
import asyncio
import subprocess
import shutil
import contextlib
import re
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------
DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS = os.environ.get("DASH_PASS", "changeme")
ALLOW_CONTROL = os.environ.get("ALLOW_CONTROL", "false").lower() == "true"
# CurseForge API Key (from env, can be overridden via config file)
_CF_API_KEY_ENV = os.environ.get("CF_API_KEY", "")

SERVICE_NAME = "hytale.service"
BACKUP_DIR = Path("/opt/hytale-server/backups")
SERVER_DIR = Path("/opt/hytale-server")
LOG_LINES = 150
DB_PATH = Path(__file__).parent / "data" / "dashboard.db"

UPDATE_SCRIPT = "/usr/local/sbin/hytale-update.sh"
VERSION_FILE = SERVER_DIR / "last_version.txt"
LATEST_VERSION_FILE = SERVER_DIR / ".latest_version"
UPDATE_AFTER_BACKUP_FLAG = SERVER_DIR / ".update_after_backup"
UPDATE_CHECK_INTERVAL = int(os.environ.get("UPDATE_CHECK_INTERVAL", "3600"))
UPDATE_NOTICE_MINUTES = int(os.environ.get("UPDATE_NOTICE_MINUTES", "15"))
UPDATE_POSTPONE_COMMAND = os.environ.get("UPDATE_POSTPONE_COMMAND", "/postponeupdate")
UPDATE_CHECK_FILE = SERVER_DIR / ".last_version_check"
UPDATE_SCHEDULE_FILE = SERVER_DIR / ".update_schedule"
UPDATE_COMMAND_CURSOR_FILE = SERVER_DIR / ".update_command_cursor"
UPDATE_CHECK_LOCK = SERVER_DIR / ".update_check_lock"
UPDATE_NOTICE_PREFIX = "[Dashboard]"
CONSOLE_PIPE = SERVER_DIR / ".console_pipe"
MODS_DIR = SERVER_DIR / "mods"
# Universe path changed in Hytale Server 2026.01 to Server/universe/
# Check new location first, fall back to old location for backwards compatibility
_NEW_WORLD_CONFIG = SERVER_DIR / "Server" / "universe" / "worlds" / "default" / "config.json"
_OLD_WORLD_CONFIG = SERVER_DIR / "universe" / "worlds" / "default" / "config.json"
WORLD_CONFIG_FILE = _NEW_WORLD_CONFIG if _NEW_WORLD_CONFIG.exists() else _OLD_WORLD_CONFIG
SERVER_CONFIG_FILE = SERVER_DIR / "config.json"
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

# ---------------------------------------------------------------------------
# Runtime Configuration (persisted settings)
# ---------------------------------------------------------------------------
DASHBOARD_CONFIG_FILE = SERVER_DIR / ".dashboard_config.json"
_config_cache = None
from threading import Lock
_config_lock = Lock()


def load_config() -> dict:
    """Load runtime configuration from file."""
    global _config_cache

    default_config = {
        "cf_api_key": _CF_API_KEY_ENV,
    }

    with _config_lock:
        if _config_cache is not None:
            merged = {**default_config, **_config_cache}
            return merged

        if DASHBOARD_CONFIG_FILE.exists():
            try:
                with open(DASHBOARD_CONFIG_FILE, "r") as f:
                    _config_cache = json.load(f)
                merged = {**default_config, **_config_cache}
                return merged
            except (json.JSONDecodeError, PermissionError, OSError):
                pass

        _config_cache = default_config
        return default_config


def save_config(config: dict) -> bool:
    """Save runtime configuration to file."""
    global _config_cache

    with _config_lock:
        try:
            DASHBOARD_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DASHBOARD_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            _config_cache = config
            return True
        except (PermissionError, OSError) as e:
            print(f"[Dashboard] Failed to save config: {e}")
            return False


def get_cf_api_key() -> str:
    """Get CurseForge API key (from config or env)."""
    config = load_config()
    return config.get("cf_api_key", "")

# Security: Maximum command length to prevent buffer overflow attempts
MAX_COMMAND_LENGTH = 500

# Security: Blocked console commands that could affect host system or bypass controls
BLOCKED_CONSOLE_COMMANDS = {
    "op", "deop", "update", "restart", "ban", "unban", "stop", "whitelist",
    "reload", "plugins", "plugin", "execute", "eval"
}

# Security: Shell metacharacters that could enable command injection
# Note: Blocking backslash, newlines, carriage returns, and semicolons/pipes/etc.
# Quotes and tabs are blocked as they could be used in injection attempts and
# are not needed for legitimate Hytale console commands
SHELL_METACHARACTERS = set(';&|`$()<>\\\n\r')

# Security: Dangerous command patterns that could harm the host system
# Using word boundaries (\b) to avoid false positives on substrings
DANGEROUS_PATTERNS = [
    re.compile(r'\.\./'),  # Path traversal attempts
    re.compile(r'/etc/'),  # System configuration access
    re.compile(r'/proc/'), # Process information access
    re.compile(r'/sys/'),  # System information access
    re.compile(r'/dev/'),  # Device access
    re.compile(r'/root/'), # Root directory access
    re.compile(r'/opt/(?!hytale-server)'),  # Access to /opt/ except hytale-server subdirectories
    re.compile(r'/tmp/'),  # Temp directory access
    re.compile(r'/var/'),  # Var directory access (logs, etc)
    re.compile(r'\bsudo\b'),   # Privilege escalation
    re.compile(r'\bsu\b'),     # User switching
    re.compile(r'\bchmod\b'),  # Permission changes
    re.compile(r'\bchown\b'),  # Ownership changes
    re.compile(r'\brm\b'),     # File deletion
    re.compile(r'\bmv\b'),     # File moving
    re.compile(r'\bcp\b'),     # File copying
    re.compile(r'\bdd\b'),     # Disk operations
    re.compile(r'\bmkfs\b'),   # Filesystem creation
    re.compile(r'\bmount\b'),  # Filesystem mounting
    re.compile(r'\bumount\b'), # Filesystem unmounting
    re.compile(r'\bkill\b'),   # Process termination
    re.compile(r'\bpkill\b'),  # Process termination
    re.compile(r'\breboot\b'), # System reboot
    re.compile(r'\bshutdown\b'), # System shutdown
    re.compile(r'\bpoweroff\b'), # System poweroff
    re.compile(r'\bhalt\b'),   # System halt
    re.compile(r'\binit\b'),   # Init system control
    re.compile(r'\bsystemctl\b'), # Systemd control
    re.compile(r'\bservice\b'), # Service control
]

# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Hytale Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

security = HTTPBasic()


@app.on_event("startup")
async def startup_event():
    """Pre-warm caches in background on startup."""
    async def warm_caches():
        await asyncio.sleep(1)  # Let server start first
        # Import here to avoid circular reference
        global _players_cache, _perf_cache
        try:
            loop = asyncio.get_event_loop()
            # Warm player cache in background
            _players_cache["data"] = await loop.run_in_executor(None, _get_players_data)
            _players_cache["ts"] = time.time()
            # Warm performance cache
            _perf_cache["data"] = await loop.run_in_executor(None, _get_perf_data)
            _perf_cache["ts"] = time.time()
        except Exception:
            pass  # Ignore errors during warmup
    asyncio.create_task(warm_caches())


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, DASH_USER)
    correct_pass = secrets.compare_digest(credentials.password, DASH_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Thread pool for non-blocking subprocess calls
_executor = ThreadPoolExecutor(max_workers=4)


def run_cmd(cmd: list[str], timeout: int = 10) -> tuple[str, int]:
    """Run a subprocess and return (stdout+stderr, returncode)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return output.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "Command timed out", 1
    except FileNotFoundError:
        return f"Command not found: {cmd[0]}", 1
    except Exception as e:
        return str(e), 1


async def run_cmd_async(cmd: list[str], timeout: int = 10) -> tuple[str, int]:
    """Async version of run_cmd using thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, run_cmd, cmd, timeout)


def human_size(size_bytes: float) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def get_service_status() -> dict:
    """Query systemd for hytale.service status."""
    props = ["ActiveState", "SubState", "MainPID", "ActiveEnterTimestamp"]
    cmd = ["systemctl", "show", SERVICE_NAME, "--property=" + ",".join(props)]
    output, rc = run_cmd(cmd)
    if rc != 0:
        return {"error": output}

    data = {}
    for line in output.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            data[key.strip()] = val.strip()

    data["StartTime"] = data.get("ActiveEnterTimestamp", "n/a") or "n/a"
    return data


def get_logs() -> list[str]:
    """Fetch journal logs for hytale unit."""
    cmd = ["journalctl", "-u", "hytale", f"-n{LOG_LINES}", "--no-pager"]
    output, rc = run_cmd(cmd, timeout=15)
    if rc != 0:
        return [f"[Error fetching logs: {output}]"]
    return output.splitlines()


def get_db_connection():
    """Get a SQLite connection with proper settings."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get_performance_from_db() -> dict:
    """Get latest performance metrics from SQLite database."""
    result = {
        "tps": None,
        "cpu_percent": None,
        "ram_mb": None,
        "ram_percent": None,
        "view_radius": None,
        "mode": "sqlite"
    }

    conn = get_db_connection()
    if not conn:
        # Fallback to log parsing if DB not available
        return get_tps_from_logs_fallback()

    try:
        c = conn.cursor()
        c.execute("""
            SELECT tps, cpu_percent, ram_mb, ram_percent, view_radius
            FROM performance
            ORDER BY timestamp DESC
            LIMIT 1
        """)
        row = c.fetchone()
        if row:
            result["tps"] = row["tps"]
            result["cpu_percent"] = row["cpu_percent"]
            result["ram_mb"] = row["ram_mb"]
            result["ram_percent"] = row["ram_percent"]
            result["view_radius"] = row["view_radius"]
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        conn.close()

    return result


def get_players_from_db() -> dict:
    """Get player list from SQLite database."""
    conn = get_db_connection()
    if not conn:
        # Fallback to log parsing if DB not available
        return get_players_from_logs_fallback()

    try:
        c = conn.cursor()
        c.execute("""
            SELECT uuid, name, online, last_login, last_logout, world
            FROM players
            ORDER BY last_login DESC
        """)
        rows = c.fetchall()
        players = []
        for row in rows:
            players.append({
                "uuid": row["uuid"],
                "name": row["name"],
                "online": bool(row["online"]),
                "last_login": row["last_login"],
                "last_logout": row["last_logout"],
                "world": row["world"],
                "position": None
            })
        return {"players": players, "ops": get_ops_list()}
    except Exception as e:
        print(f"DB error: {e}")
        return {"players": [], "error": str(e)}
    finally:
        conn.close()


def get_performance_history(hours: int = 1) -> list:
    """Get performance history for graphs."""
    conn = get_db_connection()
    if not conn:
        return []

    try:
        c = conn.cursor()
        c.execute("""
            SELECT timestamp, tps, cpu_percent, ram_mb, players_online
            FROM performance
            WHERE timestamp > datetime('now', ? || ' hours')
            ORDER BY timestamp ASC
        """, (f"-{hours}",))
        return [dict(row) for row in c.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def get_view_radius_from_logs() -> int | None:
    """Get view radius from logs (quick check)."""
    cmd = ["journalctl", "-u", "hytale", "-n100", "--no-pager", "-q"]
    output, rc = run_cmd(cmd, timeout=5)
    if rc != 0:
        return None

    vr_re = re.compile(r"(?:Initial view radius is|View radius.*?to) (\d+)")
    for line in reversed(output.splitlines()):
        match = vr_re.search(line)
        if match:
            return int(match.group(1))
    return None


def get_tps_from_logs_fallback() -> dict:
    """Fallback: Parse TPS from logs if DB not available."""
    cmd = ["journalctl", "-u", "hytale", "-n500", "--no-pager", "-q"]
    output, rc = run_cmd(cmd, timeout=10)
    if rc != 0:
        return {"tps": None, "view_radius": None}

    tps = None
    view_radius = None
    tps_re = re.compile(r"Setting TPS of world \w+ to (\d+)")
    vr_re = re.compile(r"(?:Initial view radius is|View radius.*?to) (\d+)")

    for line in reversed(output.splitlines()):
        if tps is None:
            match = tps_re.search(line)
            if match:
                tps = int(match.group(1))
        if view_radius is None:
            match = vr_re.search(line)
            if match:
                view_radius = int(match.group(1))
        if tps is not None and view_radius is not None:
            break

    return {"tps": tps, "view_radius": view_radius}


def get_players_from_logs_fallback() -> dict:
    """Fallback: Get players from logs if DB not available."""
    output, rc = run_cmd(
        ["journalctl", "-u", "hytale", "--no-pager", "-o", "short-iso", "--since", "3 days ago"],
        timeout=30
    )
    if rc != 0:
        return {"players": [], "error": output}
    return {"players": parse_players(output), "ops": get_ops_list()}


def get_resource_usage() -> dict:
    """Get CPU and RAM usage for the server process (Docker or native)."""
    result = {"cpu_percent": None, "ram_mb": None, "ram_percent": None, "mode": "unknown"}

    # Check for Docker container first
    docker_container = os.getenv("HYTALE_DOCKER_CONTAINER", "")
    if docker_container:
        # Docker mode: use docker stats
        cmd = ["docker", "stats", "--no-stream", "--format",
               "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}", docker_container]
        output, rc = run_cmd(cmd, timeout=5)
        if rc == 0 and output.strip():
            try:
                parts = output.strip().split("\t")
                cpu_str = parts[0].replace("%", "").strip()
                mem_usage = parts[1].split("/")[0].strip()  # e.g., "1.5GiB"
                mem_pct = parts[2].replace("%", "").strip()

                result["cpu_percent"] = float(cpu_str)
                result["ram_percent"] = float(mem_pct)
                result["mode"] = "docker"

                # Parse memory (e.g., "1.5GiB" or "512MiB")
                if "GiB" in mem_usage:
                    result["ram_mb"] = float(mem_usage.replace("GiB", "")) * 1024
                elif "MiB" in mem_usage:
                    result["ram_mb"] = float(mem_usage.replace("MiB", ""))
                elif "GB" in mem_usage:
                    result["ram_mb"] = float(mem_usage.replace("GB", "")) * 1024
                elif "MB" in mem_usage:
                    result["ram_mb"] = float(mem_usage.replace("MB", ""))
            except (ValueError, IndexError):
                pass
        return result

    # Native mode: find Java process via systemd
    # First get wrapper PID from systemd
    cmd = ["systemctl", "show", SERVICE_NAME, "--property=MainPID", "--value"]
    output, rc = run_cmd(cmd, timeout=3)
    if rc != 0 or not output.strip():
        return result

    wrapper_pid = output.strip()
    if wrapper_pid == "0":
        return result

    # Find Java child process (HytaleServer.jar)
    cmd = ["pgrep", "-P", wrapper_pid, "java"]
    output, rc = run_cmd(cmd, timeout=3)
    java_pid = output.strip().split()[0] if rc == 0 and output.strip() else None

    # Fallback: search for HytaleServer.jar directly
    if not java_pid:
        cmd = ["pgrep", "-f", "HytaleServer.jar"]
        output, rc = run_cmd(cmd, timeout=3)
        java_pid = output.strip().split()[0] if rc == 0 and output.strip() else None

    if not java_pid:
        return result

    # Get CPU%, MEM%, RSS from ps
    cmd = ["ps", "-p", java_pid, "-o", "%cpu,%mem,rss", "--no-headers"]
    output, rc = run_cmd(cmd, timeout=3)
    if rc == 0 and output.strip():
        try:
            parts = output.strip().split()
            result["cpu_percent"] = float(parts[0])
            result["ram_percent"] = float(parts[1])
            result["ram_mb"] = int(parts[2]) / 1024  # RSS is in KB
            result["mode"] = "native"
        except (ValueError, IndexError):
            pass

    return result


def get_backups() -> dict:
    """List backup files sorted by mtime desc."""
    try:
        if not BACKUP_DIR.exists():
            return {"error": f"Backup-Verzeichnis nicht gefunden: {BACKUP_DIR}", "files": [], "count": 0, "last_backup": "n/a"}

        backup_files = list(BACKUP_DIR.glob("hytale_*.tar.gz")) + list(BACKUP_DIR.glob("*.zip"))
        files = sorted(
            backup_files,
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except PermissionError:
        return {"error": "Keine Berechtigung auf Backup-Verzeichnis", "files": [], "count": 0, "last_backup": "n/a"}

    result = []
    for f in files:
        st = f.stat()
        result.append({
            "name": f.name,
            "size": human_size(st.st_size),
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                      .strftime("%Y-%m-%d %H:%M:%S UTC"),
        })

    last_backup = result[0]["mtime"] if result else "n/a"
    return {"files": result, "count": len(result), "last_backup": last_backup}


def get_disk_usage() -> dict:
    """Disk usage for /opt/hytale-server."""
    try:
        if not SERVER_DIR.exists():
            return {"error": f"Pfad nicht gefunden: {SERVER_DIR}"}
        usage = shutil.disk_usage(str(SERVER_DIR))
        return {
            "total": human_size(usage.total),
            "used": human_size(usage.used),
            "free": human_size(usage.free),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
    except Exception as e:
        return {"error": str(e)}


def get_version_info() -> dict:
    """Read current and latest version from state files."""
    current = "unknown"
    latest = "unknown"
    try:
        if VERSION_FILE.exists():
            current = VERSION_FILE.read_text().strip()
    except (PermissionError, OSError):
        pass
    try:
        if LATEST_VERSION_FILE.exists():
            latest = LATEST_VERSION_FILE.read_text().strip()
    except (PermissionError, OSError):
        pass

    update_available = (
        latest != "unknown"
        and current != latest
    )
    update_after_backup = UPDATE_AFTER_BACKUP_FLAG.exists()

    return {
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "update_after_backup": update_after_backup,
    }


def get_backup_count() -> int:
    """Return the current number of backup files."""
    try:
        if not BACKUP_DIR.exists():
            return 0
        files = list(BACKUP_DIR.glob("hytale_*.tar.gz")) + list(BACKUP_DIR.glob("*.zip"))
        return len(files)
    except (PermissionError, OSError):
        return 0


def check_auto_update() -> None:
    """If update-after-backup flag is set and a new backup appeared, trigger update."""
    if not UPDATE_AFTER_BACKUP_FLAG.exists():
        return
    if not ALLOW_CONTROL:
        return
    try:
        stored_count = int(UPDATE_AFTER_BACKUP_FLAG.read_text().strip())
    except (ValueError, OSError):
        return
    current_count = get_backup_count()
    if current_count > stored_count:
        # New backup detected, trigger update
        run_cmd(["sudo", UPDATE_SCRIPT, "update"], timeout=300)
        # Flag is removed by the update script


def read_timestamp(path: Path) -> datetime | None:
    try:
        if not path.exists():
            return None
        value = path.read_text().strip()
        if not value:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except ValueError:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
    except (ValueError, OSError):
        return None


def write_timestamp(path: Path, value: datetime) -> None:
    try:
        path.write_text(str(int(value.timestamp())))
    except OSError:
        pass


def parse_players(output: str) -> list[dict]:
    players = {}
    join_re = re.compile(
        r"(\S+T\S+).*Adding player '([^']+)' to world '([^']+)' at location .+\(([a-f0-9-]+)\)"
    )
    leave_re = re.compile(
        r"(\S+T\S+).*Removing player '([^']+?)(?:\s*\([^)]+\))?'.*\(([a-f0-9-]+)\)\s*$"
    )
    for line in output.splitlines():
        m = join_re.search(line)
        if m:
            ts, name, world, uuid = m.group(1), m.group(2), m.group(3), m.group(4)
            players[uuid] = {
                "name": name, "uuid": uuid,
                "online": True, "last_login": ts,
                "last_logout": None, "world": world, "position": None,
            }
            continue
        m = leave_re.search(line)
        if m:
            ts, name, uuid = m.group(1), m.group(2), m.group(3)
            if uuid in players:
                players[uuid]["online"] = False
                players[uuid]["last_logout"] = ts
    return list(players.values())


def get_player_entries() -> tuple[list[dict], str | None]:
    # Use time-based filter (last 3 days) for player history - balance of coverage vs speed
    output, rc = run_cmd(
        ["journalctl", "-u", "hytale", "--no-pager", "-o", "short-iso", "--since", "3 days ago"],
        timeout=30
    )
    if rc != 0:
        return [], output
    return parse_players(output), None


def get_online_players() -> list[str] | None:
    players, error = get_player_entries()
    if error:
        return None
    return [p["name"] for p in players if p.get("online")]


def send_console_command(command: str, ignore_errors: bool = False) -> None:
    """
    Send a command to the Hytale server console via FIFO pipe.
    
    Security: This function assumes the command has already been validated
    by should_allow_console_command(). Additional defense-in-depth checks
    are performed here.
    """
    if not CONSOLE_PIPE.exists():
        if ignore_errors:
            return
        raise RuntimeError("Konsolen-Pipe nicht gefunden. Server laeuft nicht mit Wrapper.")
    
    # Defense in depth: Ensure no null bytes in command
    if '\x00' in command:
        if not ignore_errors:
            raise RuntimeError("Invalid command: contains null bytes")
        return
    
    # Defense in depth: Limit command length
    if len(command) > MAX_COMMAND_LENGTH:
        if not ignore_errors:
            raise RuntimeError(f"Command too long (max {MAX_COMMAND_LENGTH} characters)")
        return
    
    try:
        fd = os.open(str(CONSOLE_PIPE), os.O_WRONLY | os.O_NONBLOCK)
        # Only write the command itself, newline is added here
        # Using strict encoding to reject invalid UTF-8 rather than silently dropping characters
        os.write(fd, (command + "\n").encode('utf-8', errors='strict'))
        os.close(fd)
    except UnicodeEncodeError as exc:
        if ignore_errors:
            return
        raise RuntimeError(f"Invalid command encoding: {exc}") from exc
    except OSError as exc:
        if ignore_errors:
            return
        raise RuntimeError(f"Fehler beim Senden: {exc}") from exc


def send_update_notice() -> None:
    msg = (
        f"{UPDATE_NOTICE_PREFIX} Update startet in {UPDATE_NOTICE_MINUTES} Minuten. "
        f"Nutze {UPDATE_POSTPONE_COMMAND} um {UPDATE_NOTICE_MINUTES} Minuten zu verschieben."
    )
    send_console_command(f"say {msg}", ignore_errors=True)


def load_update_schedule() -> datetime | None:
    return read_timestamp(UPDATE_SCHEDULE_FILE)


def save_update_schedule(scheduled_at: datetime) -> None:
    write_timestamp(UPDATE_SCHEDULE_FILE, scheduled_at)


def clear_update_schedule() -> None:
    with contextlib.suppress(OSError):
        UPDATE_SCHEDULE_FILE.unlink()


def should_run_version_check(now: datetime) -> bool:
    if UPDATE_CHECK_INTERVAL <= 0:
        return False
    lock_time = read_timestamp(UPDATE_CHECK_LOCK)
    if lock_time and now - lock_time < timedelta(seconds=30):
        return False
    last_check = read_timestamp(UPDATE_CHECK_FILE)
    if not last_check:
        return True
    return now - last_check >= timedelta(seconds=UPDATE_CHECK_INTERVAL)


def check_for_updates() -> dict | None:
    output, rc = run_cmd(["sudo", UPDATE_SCRIPT, "check"], timeout=300)
    if rc != 0:
        return None
    try:
        return json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return None


def has_update_available() -> bool:
    info = get_version_info()
    if info.get("update_available"):
        return True
    return False


def apply_postpone_if_requested() -> bool:
    if not UPDATE_COMMAND_CURSOR_FILE.exists():
        last_cursor = datetime.min.replace(tzinfo=timezone.utc)
    else:
        last_cursor = read_timestamp(UPDATE_COMMAND_CURSOR_FILE) or datetime.min.replace(tzinfo=timezone.utc)
    since_arg = f"@{int(last_cursor.timestamp())}"
    output, rc = run_cmd(
        ["journalctl", "-u", "hytale", "--no-pager", "-o", "short-iso", f"--since={since_arg}"],
        timeout=10
    )
    if rc != 0:
        return False
    postpone_used = apply_postpone_chat_commands(output)
    write_timestamp(UPDATE_COMMAND_CURSOR_FILE, datetime.now(timezone.utc))
    return postpone_used


def schedule_or_run_update() -> None:
    if not ALLOW_CONTROL:
        return
    now = datetime.now(timezone.utc)
    schedule = load_update_schedule()
    if schedule:
        apply_postpone_if_requested()
        schedule = load_update_schedule()
        if schedule and now >= schedule:
            run_cmd(["sudo", UPDATE_SCRIPT, "update"], timeout=600)
            clear_update_schedule()
        return
    if not has_update_available():
        return
    online_players = get_online_players()
    if online_players is None:
        return
    if not online_players:
        run_cmd(["sudo", UPDATE_SCRIPT, "update"], timeout=600)
        return
    scheduled_at = now + timedelta(minutes=UPDATE_NOTICE_MINUTES)
    if load_update_schedule():
        return
    save_update_schedule(scheduled_at)
    send_update_notice()


def check_hourly_updates() -> None:
    if not ALLOW_CONTROL:
        return
    now = datetime.now(timezone.utc)
    if not should_run_version_check(now):
        schedule_or_run_update()
        return
    write_timestamp(UPDATE_CHECK_LOCK, now)
    result = check_for_updates()
    write_timestamp(UPDATE_CHECK_FILE, now)
    with contextlib.suppress(OSError):
        UPDATE_CHECK_LOCK.unlink()
    if result and result.get("update_available"):
        schedule_or_run_update()


def should_allow_console_command(command: str) -> tuple[bool, str]:
    """
    Validate console command for security.
    Returns (is_allowed, error_message).
    
    Security checks:
    1. Command length limits
    2. Shell metacharacter detection
    3. Blocked command list
    4. Dangerous pattern detection (path traversal, system commands, etc.)
    """
    # Check command length
    if len(command) > MAX_COMMAND_LENGTH:
        return False, f"Command too long (max {MAX_COMMAND_LENGTH} characters)"
    
    # Check for empty command
    if not command or not command.strip():
        return False, "Empty command"
    
    command_stripped = command.strip()
    
    # Check for shell metacharacters that could enable command injection
    if any(char in SHELL_METACHARACTERS for char in command):
        return False, "Command contains forbidden characters (shell metacharacters)"
    
    # Check for null bytes
    if '\x00' in command:
        return False, "Command contains null bytes"
    
    # Get first word (command name)
    parts = command_stripped.split()
    if not parts:
        return False, "Invalid command format"
    
    head = parts[0].lower()
    
    # Check blocked commands list
    if head in BLOCKED_CONSOLE_COMMANDS:
        return False, f"Command '{head}' is blocked. Use dashboard features instead"
    
    # Check for dangerous patterns in entire command
    command_lower = command_stripped.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command_lower):
            return False, f"Command contains forbidden pattern: {pattern.pattern}"
    
    # Command passes all security checks
    return True, ""


def get_ops_list() -> list[str]:
    ops_file = SERVER_DIR / "ops.json"
    if not ops_file.exists():
        return []
    try:
        data = json.loads(ops_file.read_text())
        if isinstance(data, list):
            return [str(entry) for entry in data]
    except (json.JSONDecodeError, OSError):
        return []
    return []


def set_operator(name: str, enable: bool) -> None:
    if not PLAYER_NAME_RE.match(name):
        raise RuntimeError("Ungueltiger Spielername.")
    # Hytale uses /op add <name> and /op remove <name> (not just op <name>)
    command = f"/op {'add' if enable else 'remove'} {name}"
    send_console_command(command)


def parse_chat_commands(output: str) -> list[dict]:
    entries = []
    chat_re = re.compile(r"(\S+T\S+).*<([^>]+)> (.+)")
    for line in output.splitlines():
        match = chat_re.search(line)
        if not match:
            continue
        ts, player, message = match.group(1), match.group(2), match.group(3).strip()
        entries.append({"time": ts, "player": player, "message": message})
    return entries


def apply_postpone_chat_commands(output: str) -> bool:
    scheduled = load_update_schedule()
    if not scheduled:
        return False
    entries = parse_chat_commands(output)
    for entry in entries:
        if entry["message"].startswith(UPDATE_POSTPONE_COMMAND):
            new_schedule = scheduled + timedelta(minutes=UPDATE_NOTICE_MINUTES)
            save_update_schedule(new_schedule)
            send_console_command(
                f"say {UPDATE_NOTICE_PREFIX} Update verschoben bis {new_schedule.strftime('%H:%M UTC')}.",
                ignore_errors=True
            )
            return True
    return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(verify_credentials)):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "allow_control": ALLOW_CONTROL,
        "user": user,
        "backup_dir": str(BACKUP_DIR),
        "service": SERVICE_NAME,
    })


@app.get("/manage", response_class=HTMLResponse)
async def manage(request: Request, user: str = Depends(verify_credentials)):
    return templates.TemplateResponse("manage.html", {
        "request": request,
        "allow_control": ALLOW_CONTROL,
        "user": user,
        "server_dir": str(SERVER_DIR),
        "service": SERVICE_NAME,
    })


def _get_status_data() -> dict:
    """Sync function to gather all status data."""
    check_auto_update()
    check_hourly_updates()
    return {
        "service": get_service_status(),
        "backups": get_backups(),
        "disk": get_disk_usage(),
        "version": get_version_info(),
        "allow_control": ALLOW_CONTROL,
    }


@app.get("/api/status")
async def api_status(user: str = Depends(verify_credentials)):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, _get_status_data)
    return JSONResponse(data)


# Cache for performance data (updated every 5 seconds max)
_perf_cache: dict = {"data": None, "ts": 0}


def _get_perf_data() -> dict:
    """Sync function to gather performance data from SQLite or fallback."""
    return get_performance_from_db()


@app.get("/api/performance")
async def api_performance(user: str = Depends(verify_credentials)):
    """Lightweight endpoint for performance data from SQLite."""
    # SQLite reads are fast, minimal caching needed
    now = time.time()
    if _perf_cache["data"] is None or now - _perf_cache["ts"] > 2:
        loop = asyncio.get_event_loop()
        _perf_cache["data"] = await loop.run_in_executor(_executor, _get_perf_data)
        _perf_cache["ts"] = now
    return JSONResponse(_perf_cache["data"])


@app.get("/api/performance/history")
async def api_performance_history(user: str = Depends(verify_credentials), hours: int = 1):
    """Get performance history for graphs."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, get_performance_history, hours)
    return JSONResponse({"history": data})


def get_metrics_data() -> str:
    """Generate Prometheus-compatible metrics."""
    lines = []

    # Performance metrics
    lines.append("# HELP hytale_tps Current server TPS (ticks per second)")
    lines.append("# TYPE hytale_tps gauge")
    lines.append("# HELP hytale_cpu_percent Server CPU usage percentage")
    lines.append("# TYPE hytale_cpu_percent gauge")
    lines.append("# HELP hytale_ram_mb Server RAM usage in MB")
    lines.append("# TYPE hytale_ram_mb gauge")
    lines.append("# HELP hytale_ram_percent Server RAM usage percentage")
    lines.append("# TYPE hytale_ram_percent gauge")
    lines.append("# HELP hytale_view_radius Current view radius")
    lines.append("# TYPE hytale_view_radius gauge")

    # Player metrics
    lines.append("# HELP hytale_players_online Number of online players")
    lines.append("# TYPE hytale_players_online gauge")
    lines.append("# HELP hytale_players_total Total known players")
    lines.append("# TYPE hytale_players_total gauge")

    # Server status
    lines.append("# HELP hytale_server_up Server running status (1=up, 0=down)")
    lines.append("# TYPE hytale_server_up gauge")

    # Disk metrics
    lines.append("# HELP hytale_disk_total_bytes Total disk space in bytes")
    lines.append("# TYPE hytale_disk_total_bytes gauge")
    lines.append("# HELP hytale_disk_used_bytes Used disk space in bytes")
    lines.append("# TYPE hytale_disk_used_bytes gauge")
    lines.append("# HELP hytale_disk_free_bytes Free disk space in bytes")
    lines.append("# TYPE hytale_disk_free_bytes gauge")
    lines.append("# HELP hytale_disk_used_percent Disk usage percentage")
    lines.append("# TYPE hytale_disk_used_percent gauge")

    # Backup metrics
    lines.append("# HELP hytale_backups_count Total number of backups")
    lines.append("# TYPE hytale_backups_count gauge")
    lines.append("# HELP hytale_backups_size_bytes Total size of all backups in bytes")
    lines.append("# TYPE hytale_backups_size_bytes gauge")
    lines.append("# HELP hytale_backup_last_timestamp Unix timestamp of last backup")
    lines.append("# TYPE hytale_backup_last_timestamp gauge")

    # Mod metrics
    lines.append("# HELP hytale_mods_count Number of installed mods")
    lines.append("# TYPE hytale_mods_count gauge")
    lines.append("# HELP hytale_mods_enabled Number of enabled mods")
    lines.append("# TYPE hytale_mods_enabled gauge")

    # Get performance data
    perf = get_performance_from_db()
    if perf.get("tps") is not None:
        lines.append(f'hytale_tps {perf["tps"]}')
    if perf.get("cpu_percent") is not None:
        lines.append(f'hytale_cpu_percent {perf["cpu_percent"]}')
    if perf.get("ram_mb") is not None:
        lines.append(f'hytale_ram_mb {perf["ram_mb"]:.2f}')
    if perf.get("ram_percent") is not None:
        lines.append(f'hytale_ram_percent {perf["ram_percent"]}')
    if perf.get("view_radius") is not None:
        lines.append(f'hytale_view_radius {perf["view_radius"]}')

    # Get player data
    players_data = get_players_from_db()
    players = players_data.get("players", [])
    online_count = sum(1 for p in players if p.get("online"))
    lines.append(f'hytale_players_online {online_count}')
    lines.append(f'hytale_players_total {len(players)}')

    # Server status
    status = get_service_status()
    server_up = 1 if status.get("ActiveState") == "active" else 0
    lines.append(f'hytale_server_up {server_up}')

    # Disk usage
    try:
        disk = shutil.disk_usage(SERVER_DIR)
        lines.append(f'hytale_disk_total_bytes {disk.total}')
        lines.append(f'hytale_disk_used_bytes {disk.used}')
        lines.append(f'hytale_disk_free_bytes {disk.free}')
        lines.append(f'hytale_disk_used_percent {(disk.used / disk.total) * 100:.1f}')
    except Exception:
        pass

    # Backup stats
    try:
        backups = get_backups()
        lines.append(f'hytale_backups_count {backups.get("count", 0)}')
        total_size = sum(f.get("size_bytes", 0) for f in backups.get("files", []))
        lines.append(f'hytale_backups_size_bytes {total_size}')
        # Parse last backup timestamp
        last_backup = backups.get("last_backup", "")
        if last_backup and last_backup != "n/a":
            try:
                from datetime import datetime
                dt = datetime.strptime(last_backup, "%Y-%m-%d %H:%M:%S UTC")
                lines.append(f'hytale_backup_last_timestamp {int(dt.timestamp())}')
            except Exception:
                pass
    except Exception:
        pass

    # Mod stats
    try:
        if MODS_DIR.exists():
            mods = [d for d in MODS_DIR.iterdir() if d.is_dir()]
            enabled = sum(1 for d in mods if not d.name.endswith(".disabled"))
            lines.append(f'hytale_mods_count {len(mods)}')
            lines.append(f'hytale_mods_enabled {enabled}')
    except Exception:
        pass

    return "\n".join(lines) + "\n"


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint (no auth for scraping)."""
    from fastapi.responses import PlainTextResponse
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, get_metrics_data)
    return PlainTextResponse(data, media_type="text/plain; charset=utf-8")


@app.get("/api/metrics")
async def api_metrics(user: str = Depends(verify_credentials)):
    """Prometheus metrics with authentication."""
    from fastapi.responses import PlainTextResponse
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, get_metrics_data)
    return PlainTextResponse(data, media_type="text/plain; charset=utf-8")


@app.get("/api/logs")
async def api_logs(user: str = Depends(verify_credentials)):
    loop = asyncio.get_event_loop()
    lines = await loop.run_in_executor(_executor, get_logs)
    return JSONResponse({"lines": lines})


# ---------------------------------------------------------------------------
# Control Endpoints (only when ALLOW_CONTROL=true)
# ---------------------------------------------------------------------------
@app.post("/api/server/{action}")
async def api_server_action(action: str, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert. ALLOW_CONTROL=true setzen.")

    allowed = {
        "start": ["sudo", "/bin/systemctl", "start", SERVICE_NAME],
        "stop": ["sudo", "/bin/systemctl", "stop", SERVICE_NAME],
        "restart": ["sudo", "/bin/systemctl", "restart", SERVICE_NAME],
    }
    if action not in allowed:
        raise HTTPException(status_code=400, detail=f"Unbekannte Aktion: {action}")

    output, rc = run_cmd(allowed[action], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output)
    return {"ok": True, "action": action}


@app.post("/api/backup/run")
async def api_backup_run(user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert. ALLOW_CONTROL=true setzen.")

    output, rc = run_cmd(["sudo", "/usr/local/sbin/hytale-backup.sh"], timeout=120)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output)
    return {"ok": True, "output": output}


# ---------------------------------------------------------------------------
# Configuration Endpoints
# ---------------------------------------------------------------------------
HYTALE_SERVICE_FILE = Path("/etc/systemd/system/hytale.service")
HYTALE_OVERRIDE_DIR = Path("/etc/systemd/system/hytale.service.d")
HYTALE_OVERRIDE_FILE = HYTALE_OVERRIDE_DIR / "override.conf"
ALLOWED_FREQUENCIES = [0, 30, 60, 120, 360]  # 0 = deaktiviert


def get_backup_frequency() -> int:
    """Read current backup frequency from override.conf Environment variable."""
    # Check override first for HYTALE_BACKUP_FREQUENCY environment variable
    try:
        if HYTALE_OVERRIDE_FILE.exists():
            content = HYTALE_OVERRIDE_FILE.read_text()
            for line in content.splitlines():
                stripped = line.strip()
                if "HYTALE_BACKUP_FREQUENCY" in stripped:
                    # Parse Environment="HYTALE_BACKUP_FREQUENCY=30"
                    import re
                    match = re.search(r'HYTALE_BACKUP_FREQUENCY[="](\d+)', stripped)
                    if match:
                        return int(match.group(1))
    except (PermissionError, ValueError):
        pass

    # Default value (30 minutes) if no override exists
    return 30


def build_override_content(frequency: int) -> str:
    """Build override.conf content with backup frequency environment variable."""
    return f"""[Service]
Environment="HYTALE_BACKUP_FREQUENCY={frequency}"
"""


@app.get("/api/config")
async def api_config(user: str = Depends(verify_credentials)):
    return JSONResponse({
        "backup_frequency": get_backup_frequency(),
        "allowed_frequencies": ALLOWED_FREQUENCIES,
    })


@app.post("/api/config/backup-frequency")
async def api_set_backup_frequency(request: Request, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")

    body = await request.json()
    freq = body.get("frequency")
    if freq is None or freq not in ALLOWED_FREQUENCIES:
        raise HTTPException(status_code=400, detail=f"Ungueltige Frequenz. Erlaubt: {ALLOWED_FREQUENCIES}")

    # Build override content (only sets Environment, not ExecStart)
    override_content = build_override_content(freq)

    # Create override directory
    output, rc = run_cmd(["sudo", "/bin/mkdir", "-p", str(HYTALE_OVERRIDE_DIR)])
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Fehler beim Erstellen des Override-Verzeichnisses: {output}")

    # Write override file via sudo tee
    try:
        proc = subprocess.run(
            ["sudo", "/usr/bin/tee", str(HYTALE_OVERRIDE_FILE)],
            input=override_content, capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Fehler beim Schreiben der Override-Datei: {proc.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Reload systemd and restart hytale
    output, rc = run_cmd(["sudo", "/bin/systemctl", "daemon-reload"], timeout=10)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"daemon-reload fehlgeschlagen: {output}")

    output, rc = run_cmd(["sudo", "/bin/systemctl", "restart", SERVICE_NAME], timeout=60)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"Server-Neustart fehlgeschlagen: {output}")

    return {"ok": True, "frequency": freq}


# ---------------------------------------------------------------------------
# Version / Update Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/version")
async def api_version(user: str = Depends(verify_credentials)):
    return JSONResponse(get_version_info())


@app.get("/api/update/log")
async def api_update_log(user: str = Depends(verify_credentials)):
    """Return current content of the downloader log and process status."""
    log_file = SERVER_DIR / ".downloader" / "download.log"
    log_content = ""
    try:
        if log_file.exists():
            log_content = log_file.read_text()
    except (PermissionError, OSError):
        pass

    # Check if update/downloader process is currently running
    running = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", "hytale-update.sh|hytale-downloader"],
            capture_output=True, text=True, timeout=5
        )
        running = result.returncode == 0
    except Exception:
        pass

    return JSONResponse({
        "log": log_content,
        "running": running,
    })


@app.post("/api/version/check")
async def api_version_check(user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert. ALLOW_CONTROL=true setzen.")

    output, rc = await asyncio.to_thread(run_cmd, ["sudo", UPDATE_SCRIPT, "check"], 300)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output)

    try:
        result = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=500, detail=f"Unerwartete Ausgabe: {output}")

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return JSONResponse(result)


@app.post("/api/update/run")
async def api_update_run(user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert. ALLOW_CONTROL=true setzen.")

    output, rc = await asyncio.to_thread(run_cmd, ["sudo", UPDATE_SCRIPT, "update"], 600)
    if rc != 0:
        raise HTTPException(status_code=500, detail=output)

    try:
        result = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=500, detail=f"Unerwartete Ausgabe: {output}")

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return JSONResponse(result)


@app.post("/api/update/auto")
async def api_update_auto(user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert. ALLOW_CONTROL=true setzen.")

    if UPDATE_AFTER_BACKUP_FLAG.exists():
        # Toggle off
        UPDATE_AFTER_BACKUP_FLAG.unlink(missing_ok=True)
        return {"ok": True, "update_after_backup": False}
    else:
        # Toggle on: store current backup count
        count = get_backup_count()
        UPDATE_AFTER_BACKUP_FLAG.write_text(str(count))
        return {"ok": True, "update_after_backup": True}


# ---------------------------------------------------------------------------
# Management Endpoints
# ---------------------------------------------------------------------------

# Cache for player data
_players_cache: dict = {"data": None, "ts": 0}


def _get_players_data() -> dict:
    """Sync function to get players data from SQLite."""
    return get_players_from_db()


@app.get("/api/players")
async def api_players(user: str = Depends(verify_credentials)):
    """Get player list from SQLite database."""
    now = time.time()
    # SQLite reads are fast, 5s cache is sufficient
    if _players_cache["data"] is None or now - _players_cache["ts"] > 5:
        loop = asyncio.get_event_loop()
        _players_cache["data"] = await loop.run_in_executor(_executor, _get_players_data)
        _players_cache["ts"] = now
    return JSONResponse(_players_cache["data"])


@app.post("/api/players/op")
async def api_player_op(request: Request, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")
    body = await request.json()
    name = body.get("name", "").strip()
    enable = bool(body.get("enable", True))
    if not name:
        raise HTTPException(status_code=400, detail="Kein Spieler angegeben.")
    try:
        set_operator(name, enable)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "name": name, "enabled": enable}


@app.post("/api/console/send")
async def api_console_send(request: Request, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")

    body = await request.json()
    command = body.get("command", "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="Kein Befehl angegeben.")
    
    # Security validation
    is_allowed, error_msg = should_allow_console_command(command)
    if not is_allowed:
        raise HTTPException(status_code=400, detail=error_msg)

    if not CONSOLE_PIPE.exists():
        raise HTTPException(status_code=500, detail="Konsolen-Pipe nicht gefunden. Server laeuft nicht mit Wrapper.")

    try:
        send_console_command(command)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"ok": True, "command": command}


def _get_console_output(since: str = "") -> list:
    """Sync function to get console output."""
    cmd = ["journalctl", "-u", "hytale", "-n50", "--no-pager"]
    if since:
        cmd.extend(["--since", since])
    output, rc = run_cmd(cmd, timeout=10)
    return output.splitlines() if rc == 0 else [f"[Fehler: {output}]"]


@app.get("/api/console/output")
async def api_console_output(user: str = Depends(verify_credentials), since: str = ""):
    """Return recent log lines from journalctl."""
    loop = asyncio.get_event_loop()
    lines = await loop.run_in_executor(_executor, _get_console_output, since)
    return JSONResponse({"lines": lines})


@app.get("/api/config/server")
async def api_config_server_get(user: str = Depends(verify_credentials)):
    try:
        content = SERVER_CONFIG_FILE.read_text()
        return JSONResponse({"content": content})
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/server")
async def api_config_server_set(request: Request, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")
    body = await request.json()
    content = body.get("content", "")
    try:
        json.loads(content)  # validate JSON
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Ungueltiges JSON: {e}")
    try:
        SERVER_CONFIG_FILE.write_text(content)
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.get("/api/config/world")
async def api_config_world_get(user: str = Depends(verify_credentials)):
    try:
        content = WORLD_CONFIG_FILE.read_text()
        return JSONResponse({"content": content})
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/world")
async def api_config_world_set(request: Request, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")
    body = await request.json()
    content = body.get("content", "")
    try:
        json.loads(content)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Ungueltiges JSON: {e}")
    try:
        WORLD_CONFIG_FILE.write_text(content)
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.get("/api/backups/list")
async def api_backups_list(user: str = Depends(verify_credentials)):
    result = []
    # Regular backups
    try:
        if BACKUP_DIR.exists():
            for f in sorted(BACKUP_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file() and (f.suffix in (".gz", ".zip")):
                    st = f.stat()
                    result.append({
                        "name": f.name, "size": human_size(st.st_size),
                        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "type": "backup", "path": str(f),
                    })
    except (PermissionError, OSError):
        pass
    # Update backups
    try:
        for d in sorted(SERVER_DIR.glob(".update_backup_*"), reverse=True):
            if d.is_dir():
                st = d.stat()
                result.append({
                    "name": d.name, "size": "-",
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "type": "update-backup", "path": str(d),
                })
    except (PermissionError, OSError):
        pass
    return JSONResponse({"backups": result})


@app.delete("/api/backups/{filename:path}")
async def api_backup_delete(filename: str, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")
    # Check in backup dir
    target = BACKUP_DIR / filename
    if not target.exists():
        target = SERVER_DIR / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden.")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.get("/api/mods")
async def api_mods(user: str = Depends(verify_credentials)):
    mods = []
    try:
        if MODS_DIR.exists():
            for d in sorted(MODS_DIR.iterdir()):
                if d.is_dir():
                    enabled = not d.name.endswith(".disabled")
                    display_name = d.name.removesuffix(".disabled")
                    has_manifest = (d / "manifest.json").exists()
                    total_size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    mods.append({
                        "name": display_name, "dir_name": d.name,
                        "enabled": enabled, "has_manifest": has_manifest,
                        "size": human_size(total_size),
                    })
    except (PermissionError, OSError):
        pass
    return JSONResponse({"mods": mods})


@app.post("/api/mods/{name}/toggle")
async def api_mod_toggle(name: str, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")
    enabled_path = MODS_DIR / name
    disabled_path = MODS_DIR / f"{name}.disabled"
    try:
        if enabled_path.exists() and enabled_path.is_dir():
            enabled_path.rename(disabled_path)
            return {"ok": True, "enabled": False}
        elif disabled_path.exists() and disabled_path.is_dir():
            disabled_path.rename(enabled_path)
            return {"ok": True, "enabled": True}
        else:
            raise HTTPException(status_code=404, detail="Mod nicht gefunden.")
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/mods/{name}")
async def api_mod_delete(name: str, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")
    target = MODS_DIR / name
    if not target.exists():
        target = MODS_DIR / f"{name}.disabled"
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Mod nicht gefunden.")
    try:
        shutil.rmtree(target)
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.post("/api/mods/upload")
async def api_mod_upload(request: Request, user: str = Depends(verify_credentials)):
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")

    from fastapi import UploadFile, File
    import tempfile
    import zipfile

    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="Keine Datei hochgeladen.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Leere Datei.")

    filename = file.filename or "mod"
    is_jar = filename.lower().endswith(".jar")

    if is_jar:
        # JAR file: create directory with mod name and put JAR inside
        mod_name = Path(filename).stem
        mod_dir = MODS_DIR / mod_name
        mod_dir.mkdir(parents=True, exist_ok=True)
        jar_path = mod_dir / filename
        jar_path.write_bytes(content)
        return {"ok": True, "mod_name": mod_name}

    # ZIP file handling
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            # Determine mod name from zip content
            names = zf.namelist()
            top_dirs = set()
            for n in names:
                parts = n.split("/")
                if len(parts) > 1 and parts[0]:
                    top_dirs.add(parts[0])

            if len(top_dirs) == 1:
                mod_name = top_dirs.pop()
                extract_to = MODS_DIR
            else:
                mod_name = Path(filename).stem
                extract_to = MODS_DIR / mod_name
                extract_to.mkdir(parents=True, exist_ok=True)

            zf.extractall(str(extract_to))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Ungueltige ZIP-Datei.")
    finally:
        os.unlink(tmp_path)

    return {"ok": True, "mod_name": mod_name}


# ---------------------------------------------------------------------------
# Plugin Store
# ---------------------------------------------------------------------------
PLUGIN_STORE = [
    {
        "id": "nitrado-webserver",
        "name": "Nitrado:WebServer",
        "description": "Base plugin for web applications and APIs. Required by Query and PrometheusExporter.",
        "version": "1.0.0",
        "author": "Nitrado",
        "url": "https://github.com/nitrado/hytale-plugin-webserver/releases/download/v1.0.0/nitrado-webserver-1.0.0.jar",
        "dir_name": "Nitrado_WebServer",
        "config_port": 5523,
    },
    {
        "id": "nitrado-query",
        "name": "Nitrado:Query",
        "description": "Exposes server status (player counts, TPS, etc.) via HTTP API.",
        "version": "1.0.1",
        "author": "Nitrado",
        "url": "https://github.com/nitrado/hytale-plugin-query/releases/download/v1.0.1/nitrado-query-1.0.1.jar",
        "dir_name": "Nitrado_Query",
        "depends": ["nitrado-webserver"],
    },
    {
        "id": "nitrado-performance-saver",
        "name": "Nitrado:PerformanceSaver",
        "description": "Dynamically limits view distance based on resource usage.",
        "version": "1.1.0",
        "author": "Nitrado",
        "url": "https://github.com/nitrado/hytale-plugin-performance-saver/releases/download/v1.1.0/nitrado-performance-saver-1.1.0.jar",
        "dir_name": "Nitrado_PerformanceSaver",
    },
    {
        "id": "apexhosting-prometheus",
        "name": "ApexHosting:PrometheusExporter",
        "description": "Exposes detailed server and JVM metrics for Prometheus monitoring.",
        "version": "1.0.0",
        "author": "ApexHosting",
        "url": "https://github.com/apexhosting/hytale-plugin-prometheus/releases/download/v1.0.0/apexhosting-prometheusexporter-1.0.0.jar",
        "dir_name": "ApexHosting_PrometheusExporter",
        "depends": ["nitrado-webserver"],
    },
]


@app.get("/api/plugins")
async def api_plugins(user: str = Depends(verify_credentials)):
    """List available plugins from the store with install status."""
    result = []
    for plugin in PLUGIN_STORE:
        installed = False
        enabled = False
        # Check for JAR file in mods/ root (new method)
        jar_name = plugin["url"].split("/")[-1]
        jar_base = jar_name.replace(".jar", "")
        jars = list(MODS_DIR.glob(f"{jar_base.split('-')[0]}*.jar"))
        disabled_jars = list(MODS_DIR.glob(f"{jar_base.split('-')[0]}*.jar.disabled"))
        # Also check for old-style directory installation (backwards compat)
        dir_name = plugin["dir_name"]
        dir_exists = (MODS_DIR / dir_name).exists()
        dir_disabled = (MODS_DIR / f"{dir_name}.disabled").exists()

        if jars or dir_exists:
            installed = True
            enabled = True
        elif disabled_jars or dir_disabled:
            installed = True
            enabled = False
        result.append({
            **plugin,
            "installed": installed,
            "enabled": enabled,
        })
    return JSONResponse({"plugins": result})


@app.post("/api/plugins/{plugin_id}/install")
async def api_plugin_install(plugin_id: str, user: str = Depends(verify_credentials)):
    """Download and install a plugin from the store."""
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")

    plugin = next((p for p in PLUGIN_STORE if p["id"] == plugin_id), None)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin nicht gefunden.")

    # Check dependencies by looking for JAR files
    depends = plugin.get("depends", [])
    for dep_id in depends:
        dep = next((p for p in PLUGIN_STORE if p["id"] == dep_id), None)
        if dep:
            dep_jar_pattern = dep["url"].split("/")[-1].replace(".jar", "*.jar")
            dep_jars = list(MODS_DIR.glob(dep_jar_pattern.split("-")[0] + "-*.jar"))
            if not dep_jars:
                raise HTTPException(
                    status_code=400,
                    detail=f"Abhaengigkeit fehlt: {dep['name']}. Bitte zuerst installieren."
                )

    import urllib.request

    jar_name = plugin["url"].split("/")[-1]
    jar_path = MODS_DIR / jar_name
    disabled_jar_path = MODS_DIR / f"{jar_name}.disabled"

    # Check if already installed (JAR in root or in subdirectory for backwards compat)
    if jar_path.exists() or disabled_jar_path.exists():
        raise HTTPException(status_code=400, detail="Plugin bereits installiert.")
    dir_name = plugin["dir_name"]
    if (MODS_DIR / dir_name).exists() or (MODS_DIR / f"{dir_name}.disabled").exists():
        raise HTTPException(status_code=400, detail="Plugin bereits installiert.")

    try:
        # Download JAR directly to mods/ root (not in subdirectory)
        def download():
            urllib.request.urlretrieve(plugin["url"], str(jar_path))

        await asyncio.to_thread(download)

        # Create config directory if plugin has config_port setting
        if "config_port" in plugin:
            config_dir = MODS_DIR / dir_name
            config_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if jar_path.exists():
            jar_path.unlink()
        raise HTTPException(status_code=500, detail=f"Download fehlgeschlagen: {e}")

    return {"ok": True, "plugin": plugin["name"]}


@app.get("/api/server/query")
async def api_server_query(user: str = Depends(verify_credentials)):
    """Get server status from Nitrado Query API (if installed)."""
    # Check for plugin JAR files (they can be either in root or subdirectories)
    query_jar = list(MODS_DIR.glob("nitrado-query*.jar"))
    webserver_jar = list(MODS_DIR.glob("nitrado-webserver*.jar"))

    if not query_jar or not webserver_jar:
        return JSONResponse({"available": False, "reason": "Nitrado:Query oder Nitrado:WebServer nicht installiert."})

    # Read WebServer config to get port (config is in Nitrado_WebServer folder)
    config_path = MODS_DIR / "Nitrado_WebServer" / "config.json"
    port = 5523  # default: game port (5520) + 3
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            port = cfg.get("port", port)
        except Exception:
            pass

    try:
        import urllib.request
        import urllib.error
        import ssl

        # Skip SSL verification for self-signed cert
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = f"https://127.0.0.1:{port}/Nitrado/Query"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        def fetch():
            # Use a custom redirect handler to detect auth redirects
            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    if "/login" in newurl:
                        raise urllib.error.HTTPError(req.full_url, 401, "WebServer requires login", headers, fp)
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            opener = urllib.request.build_opener(NoRedirectHandler, urllib.request.HTTPSHandler(context=ctx))
            with opener.open(req, timeout=5) as resp:
                return json.loads(resp.read().decode())

        data = await asyncio.to_thread(fetch)
        return JSONResponse({"available": True, "data": data})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return JSONResponse({"available": False, "reason": "WebServer Login erforderlich. Erstelle ein Spieler-Passwort im Spiel mit /webserver password <passwort>"})
        return JSONResponse({"available": False, "reason": str(e)})
    except urllib.error.URLError as e:
        if "Connection refused" in str(e):
            return JSONResponse({"available": False, "reason": "WebServer nicht erreichbar (Server läuft?)"})
        return JSONResponse({"available": False, "reason": str(e)})
    except Exception as e:
        return JSONResponse({"available": False, "reason": str(e)})


# ---------------------------------------------------------------------------
# CurseForge Integration
# ---------------------------------------------------------------------------
CF_API_BASE = "https://api.curseforge.com/v1"
CF_HYTALE_GAME_ID = None  # Will be discovered dynamically


async def cf_request(endpoint: str, params: dict = None) -> dict:
    """Make a request to the CurseForge API."""
    import urllib.request
    import urllib.parse

    if not get_cf_api_key():
        raise HTTPException(status_code=500, detail="CurseForge API Key nicht konfiguriert (CF_API_KEY)")

    url = f"{CF_API_BASE}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "x-api-key": get_cf_api_key(),
    })

    def fetch():
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    return await asyncio.to_thread(fetch)


async def get_hytale_game_id() -> int:
    """Get or discover the Hytale game ID from CurseForge."""
    global CF_HYTALE_GAME_ID
    if CF_HYTALE_GAME_ID:
        return CF_HYTALE_GAME_ID

    # Fetch all games and find Hytale
    data = await cf_request("/games")
    for game in data.get("data", []):
        if game.get("slug") == "hytale" or game.get("name", "").lower() == "hytale":
            CF_HYTALE_GAME_ID = game["id"]
            return CF_HYTALE_GAME_ID

    raise HTTPException(status_code=500, detail="Hytale nicht in CurseForge gefunden")


@app.get("/api/curseforge/status")
async def api_cf_status(user: str = Depends(verify_credentials)):
    """Check if CurseForge integration is configured and working."""
    if not get_cf_api_key():
        return JSONResponse({"available": False, "reason": "API Key nicht konfiguriert"})

    try:
        game_id = await get_hytale_game_id()
        return JSONResponse({"available": True, "game_id": game_id})
    except Exception as e:
        return JSONResponse({"available": False, "reason": str(e)})


@app.get("/api/curseforge/search")
async def api_cf_search(
    q: str = "",
    category: str = "",
    page: int = 0,
    user: str = Depends(verify_credentials)
):
    """Search for Hytale mods on CurseForge."""
    try:
        game_id = await get_hytale_game_id()
        params = {
            "gameId": game_id,
            "sortField": 2,  # Popularity
            "sortOrder": "desc",
            "pageSize": 20,
            "index": page * 20,
        }
        if q:
            params["searchFilter"] = q
        if category:
            params["classId"] = category

        data = await cf_request("/mods/search", params)
        mods = []
        for mod in data.get("data", []):
            mods.append({
                "id": mod["id"],
                "name": mod["name"],
                "slug": mod["slug"],
                "summary": mod.get("summary", ""),
                "author": mod.get("authors", [{}])[0].get("name", "Unknown"),
                "downloads": mod.get("downloadCount", 0),
                "icon": mod.get("logo", {}).get("thumbnailUrl", ""),
                "updated": mod.get("dateModified", ""),
            })
        return JSONResponse({
            "mods": mods,
            "total": data.get("pagination", {}).get("totalCount", 0),
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/curseforge/mod/{mod_id}")
async def api_cf_mod(mod_id: int, user: str = Depends(verify_credentials)):
    """Get details and files for a specific mod."""
    try:
        # Get mod info
        mod_data = await cf_request(f"/mods/{mod_id}")
        mod = mod_data.get("data", {})

        # Get files
        files_data = await cf_request(f"/mods/{mod_id}/files", {"pageSize": 50})
        files = []
        for f in files_data.get("data", []):
            files.append({
                "id": f["id"],
                "name": f["fileName"],
                "version": f.get("displayName", f["fileName"]),
                "size": f.get("fileLength", 0),
                "date": f.get("fileDate", ""),
                "download_url": f.get("downloadUrl", ""),
                "game_versions": f.get("gameVersions", []),
            })

        return JSONResponse({
            "id": mod["id"],
            "name": mod["name"],
            "summary": mod.get("summary", ""),
            "description": mod.get("description", ""),  # HTML
            "author": mod.get("authors", [{}])[0].get("name", "Unknown"),
            "downloads": mod.get("downloadCount", 0),
            "icon": mod.get("logo", {}).get("thumbnailUrl", ""),
            "files": files,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/curseforge/install/{mod_id}/{file_id}")
async def api_cf_install(mod_id: int, file_id: int, user: str = Depends(verify_credentials)):
    """Download and install a mod from CurseForge."""
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")

    import urllib.request

    try:
        # Get file info
        file_data = await cf_request(f"/mods/{mod_id}/files/{file_id}")
        file_info = file_data.get("data", {})
        file_name = file_info.get("fileName", f"mod_{mod_id}_{file_id}.jar")
        download_url = file_info.get("downloadUrl")

        if not download_url:
            # Some mods require fetching download URL separately
            url_data = await cf_request(f"/mods/{mod_id}/files/{file_id}/download-url")
            download_url = url_data.get("data")

        if not download_url:
            raise HTTPException(status_code=400, detail="Download-URL nicht verfuegbar")

        # Download file
        target_path = MODS_DIR / file_name

        def download():
            req = urllib.request.Request(download_url, headers={
                "x-api-key": get_cf_api_key(),
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                target_path.write_bytes(resp.read())

        await asyncio.to_thread(download)

        return {"ok": True, "file": file_name, "path": str(target_path)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Installation fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# Settings API (Runtime Configuration)
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """Get current runtime settings."""
    config = load_config()
    return {
        "cf_api_key": "***" if config.get("cf_api_key") else "",  # Mask the key
        "cf_api_key_set": bool(config.get("cf_api_key")),
    }


@app.post("/api/settings")
async def update_settings(request: Request, _: HTTPBasicCredentials = Depends(verify_credentials)):
    """Update runtime settings."""
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="Control-Aktionen deaktiviert.")

    body = await request.json()
    config = load_config()

    # Update only provided values (don't overwrite with masked value)
    if "cf_api_key" in body and body["cf_api_key"] != "***":
        config["cf_api_key"] = body["cf_api_key"]

    if save_config(config):
        return {"ok": True, "message": "Einstellungen gespeichert / Settings saved"}
    else:
        raise HTTPException(status_code=500, detail="Speichern fehlgeschlagen / Save failed")


@app.get("/api/settings/cf-status")
async def check_cf_status(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """Check if CurseForge API key is valid."""
    api_key = get_cf_api_key()

    if not api_key:
        return {
            "valid": False,
            "message": "Kein API-Key konfiguriert / No API key configured"
        }

    # Test the API key
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.curseforge.com/v1/games",
            headers={"Accept": "application/json", "x-api-key": api_key}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return {"valid": True, "message": "API-Key gueltig / API key valid"}
    except Exception as e:
        return {
            "valid": False,
            "message": f"API-Key ungueltig / API key invalid: {str(e)}"
        }

    return {"valid": False, "message": "Unbekannter Fehler / Unknown error"}
