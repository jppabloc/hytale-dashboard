"""Hytale Server Dashboard – FastAPI Backend."""

import os
import json
import secrets
import asyncio
import subprocess
import shutil
import contextlib
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

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
CF_API_KEY = os.environ.get("CF_API_KEY", "")  # CurseForge API Key

SERVICE_NAME = "hytale.service"
BACKUP_DIR = Path("/opt/hytale-server/backups")
SERVER_DIR = Path("/opt/hytale-server")
LOG_LINES = 150

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
WORLD_CONFIG_FILE = SERVER_DIR / "universe" / "worlds" / "default" / "config.json"
SERVER_CONFIG_FILE = SERVER_DIR / "config.json"
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

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
    output, rc = run_cmd(
        ["journalctl", "-u", "hytale", "--no-pager", "-o", "short-iso"],
        timeout=15
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
    command = f"{'op' if enable else 'deop'} {name}"
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


@app.get("/api/status")
async def api_status(user: str = Depends(verify_credentials)):
    check_auto_update()
    check_hourly_updates()
    return JSONResponse({
        "service": get_service_status(),
        "backups": get_backups(),
        "disk": get_disk_usage(),
        "version": get_version_info(),
        "allow_control": ALLOW_CONTROL,
    })


@app.get("/api/logs")
async def api_logs(user: str = Depends(verify_credentials)):
    return JSONResponse({"lines": get_logs()})


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
    """Read current backup frequency from hytale.service (or override)."""
    # Check override first
    try:
        if HYTALE_OVERRIDE_FILE.exists():
            content = HYTALE_OVERRIDE_FILE.read_text()
            for line in content.splitlines():
                # Skip empty ExecStart= (systemd clear directive)
                stripped = line.strip()
                if stripped == "ExecStart=" or not stripped.startswith("ExecStart="):
                    continue
                if "--backup-frequency" in line:
                    parts = line.split("--backup-frequency")
                    if len(parts) > 1:
                        val = parts[1].strip().split()[0]
                        return int(val)
                # ExecStart with content but no --backup flag means backup is off
                return 0
    except (PermissionError, ValueError):
        pass

    # Fall back to main service file
    try:
        content = HYTALE_SERVICE_FILE.read_text()
        for line in content.splitlines():
            if "--backup-frequency" in line:
                parts = line.split("--backup-frequency")
                if len(parts) > 1:
                    val = parts[1].strip().split()[0]
                    return int(val)
    except (PermissionError, ValueError):
        pass

    return 0


def build_exec_start(frequency: int) -> str:
    """Build ExecStart line for hytale.service with given backup frequency."""
    base = "/usr/bin/java -Xms2G -Xmx4G -jar Server/HytaleServer.jar --assets Assets.zip --bind 0.0.0.0:5520"
    if frequency > 0:
        return f"{base} --backup --backup-frequency {frequency} --backup-dir backups"
    return base


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

    # Build override content
    override_content = f"""[Service]
ExecStart=
ExecStart={build_exec_start(freq)}
"""

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
@app.get("/api/players")
async def api_players(user: str = Depends(verify_credentials)):
    """Parse journalctl for player join/leave events."""
    players, error = get_player_entries()
    if error:
        return JSONResponse({"players": [], "error": error})
    return JSONResponse({"players": players, "ops": get_ops_list()})


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


@app.get("/api/console/output")
async def api_console_output(user: str = Depends(verify_credentials), since: str = ""):
    """Return recent log lines from journalctl."""
    cmd = ["journalctl", "-u", "hytale", "-n50", "--no-pager"]
    if since:
        cmd.extend(["--since", since])
    output, rc = run_cmd(cmd, timeout=10)
    lines = output.splitlines() if rc == 0 else [f"[Fehler: {output}]"]
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
        import ssl

        # Skip SSL verification for self-signed cert
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = f"https://127.0.0.1:{port}/Nitrado/Query"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        def fetch():
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                return json.loads(resp.read().decode())

        data = await asyncio.to_thread(fetch)
        return JSONResponse({"available": True, "data": data})
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

    if not CF_API_KEY:
        raise HTTPException(status_code=500, detail="CurseForge API Key nicht konfiguriert (CF_API_KEY)")

    url = f"{CF_API_BASE}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "x-api-key": CF_API_KEY,
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
    if not CF_API_KEY:
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
                "x-api-key": CF_API_KEY,
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                target_path.write_bytes(resp.read())

        await asyncio.to_thread(download)

        return {"ok": True, "file": file_name, "path": str(target_path)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Installation fehlgeschlagen: {e}")
