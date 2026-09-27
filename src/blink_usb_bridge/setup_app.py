"""Setup & maintenance web page (bub-setup).

Runs as root on port 80 and serves two jobs:

  1. First boot: if the Pi has no network for ~60 s it raises a WPA2
     hotspot (default SSID "BlinkPi-Setup"). Joining it and opening any
     web page lands on this app (captive portal), where the user picks
     their Wi-Fi, configures storage destinations and rclone, and applies.
  2. Afterwards: reachable on the LAN at http://<hostname>.local/ for
     status, logs, config changes, updates and reboots.

Everything that needs root (nmcli, systemctl, writing /etc, running the
installer) happens here; the sync/web services stay unprivileged.

Set an admin password on the Settings tab. Until one is set the page is
open to anyone on the LAN (same trust model as the clip browser).
"""


import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import ai_vision, notify, sm2
from . import config as cfg
from . import netmgr as nm
from . import rclone_setup as rc

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
INDEX_HTML = _HERE / "setup_index.html"

UNITS = [
    "blink-gadget.service", "blink-sync.timer", "blink-sync.service",
    "blink-wipe.timer", "bub-prune.timer", "bub-web.service", "blink-setup.service",
]

CAPTIVE_PROBES = {
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
    "/connecttest.txt", "/ncsi.txt", "/redirect", "/canonical.html", "/success.txt",
    "/check_network_status.txt", "/mobile/status.php",
}

_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


# ────────────────────────────── paths ──────────────────────────────

def project_dir() -> Path:
    env = os.environ.get("BUB_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    # Editable install: src/blink_usb_bridge/setup_app.py -> repo root.
    candidate = _HERE.parents[1]
    if (candidate / "scripts" / "install.sh").exists():
        return candidate.resolve()
    return Path.cwd().resolve()


def config_path() -> Path:
    return Path(os.environ.get("BUB_CONFIG", str(project_dir() / "config.yaml")))


def state_path() -> Path:
    return project_dir() / "setup_state.json"


def _default_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(project_dir().stat().st_uid).pw_name
    except (ImportError, KeyError, OSError):
        return "pi"


# ────────────────────────────── state ──────────────────────────────

_state_lock = threading.Lock()


def load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(patch: dict) -> dict:
    with _state_lock:
        st = load_state()
        st.update(patch)
        p = state_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(p)
        return st


def _hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def _check_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    candidate = _hash_password(pw, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(candidate, dk_hex)


# ────────────────────────────── config I/O ──────────────────────────────

def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_raw_config() -> tuple[dict, bool]:
    """(config dict, exists). Falls back to config.example.yaml + derived paths."""
    pd = project_dir()
    example = pd / "config.example.yaml"
    base: dict = {}
    if example.exists():
        base = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    cp = config_path()
    exists = cp.exists()
    if exists:
        try:
            base = _deep_merge(base, yaml.safe_load(cp.read_text(encoding="utf-8")) or {})
        except yaml.YAMLError as e:
            log.error("config.yaml unreadable: %s", e)
    else:
        user = _default_user()
        base.update({
            "pi_user": user,
            "project_dir": str(pd),
            "backing_image_path": str(pd / "usb_backing.img"),
            "mount_point": str(pd / "usb_mount"),
        })
        base.setdefault("destinations", {})
        base["destinations"].setdefault("smb", {})["enabled"] = False
        base["destinations"].setdefault("rclone", {})["enabled"] = False
        base["destinations"]["rclone"]["config_file"] = f"/home/{user}/.config/rclone/rclone.conf"
        base.setdefault("web", {})["mount_point"] = str(pd / "web_mount")
        base["web"]["enabled"] = True
        try:
            base["timezone"] = (Path("/etc/timezone").read_text().strip() or "UTC")
        except OSError:
            pass
    base.setdefault("setup", {})
    base["setup"] = {
        "hotspot_enabled": True, "hotspot_ssid": "BlinkPi-Setup",
        "hotspot_password": "blinkpi123", "listen_host": "0.0.0.0", "listen_port": 80,
        **(base.get("setup") or {}),
    }
    return base, exists


def _yaml_header() -> str:
    return (
        "# BlinkPi configuration - written by the setup page.\n"
        "# Field documentation lives in config.example.yaml. You can still edit\n"
        "# this file by hand; the setup page will pick the changes up.\n"
        f"# Last written: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
    )


def validate_config(raw: dict) -> list[str]:
    """Run the real loader against a temp copy. Returns a list of problems."""
    problems = []
    tmp = project_dir() / ".config.validate.tmp.yaml"
    try:
        tmp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        cfg.load(tmp)
    except (ValueError, FileNotFoundError) as e:
        problems.append(str(e))
    except Exception as e:  # noqa: BLE001 - surface anything to the UI
        problems.append(f"{type(e).__name__}: {e}")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    tz = str(raw.get("timezone", ""))
    from zoneinfo import ZoneInfo, available_timezones
    if available_timezones():  # empty only when the host has no tz database
        try:
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            problems.append(f"unknown timezone {tz!r}")
    wt = str(raw.get("nightly_wipe_time", ""))
    if not re.match(r"^\d{2}:\d{2}$", wt):
        problems.append("nightly_wipe_time must be HH:MM")
    return problems


def write_config(raw: dict) -> None:
    cp = config_path()
    text = _yaml_header() + yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cp)
    _chown(cp, raw.get("pi_user"))


def _chown(path: Path, user: Optional[str]) -> None:
    if not user or os.name != "posix":
        return
    try:
        import pwd
        pw = pwd.getpwnam(str(user))
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (ImportError, KeyError, PermissionError, OSError) as e:
        log.warning("chown %s: %s", path, e)


def write_smb_credentials(path: Path, username: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"username={username}\npassword={password}\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def smb_username(path: Path) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("username="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


SECRET_KEYS = ("smtp_password", "ai_api_key")


def secrets_path() -> Path:
    return project_dir() / "secrets.yaml"


def load_secrets() -> dict:
    p = secrets_path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        return {str(k): str(v) for k, v in (data or {}).items() if v is not None}
    except (OSError, yaml.YAMLError):
        return {}


def save_secrets(patch: dict, owner: Optional[str]) -> dict:
    """Merge non-empty values; the literal "__clear__" removes a key."""
    cur = load_secrets()
    for k, v in patch.items():
        if k not in SECRET_KEYS:
            continue
        v = str(v or "")
        if v == "__clear__":
            cur.pop(k, None)
        elif v:
            cur[k] = v
    p = secrets_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text("# BlinkPi secrets - written by the setup page. Keep mode 0600.\n"
                   + yaml.safe_dump(cur, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)
    _chown(p, owner)
    return cur


# ────────────────────────────── system helpers ──────────────────────────────

def _run(cmd: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def unit_states() -> dict[str, dict]:
    out = {}
    for u in UNITS:
        active = _run(["systemctl", "is-active", u], timeout=10).stdout.strip() or "unknown"
        enabled = _run(["systemctl", "is-enabled", u], timeout=10).stdout.strip() or "unknown"
        out[u] = {"active": active, "enabled": enabled}
    return out


def image_status(path: Path) -> dict:
    st = {"exists": path.exists(), "formatted": False, "size_bytes": 0, "allocated_bytes": 0}
    if not st["exists"]:
        return st
    try:
        s = path.stat()
        st["size_bytes"] = s.st_size
        st["allocated_bytes"] = getattr(s, "st_blocks", 0) * 512
        with path.open("rb") as f:
            f.seek(446 + 8)
            start = struct.unpack("<I", f.read(4))[0]
            if start > 0:
                f.seek(start * 512 + 3)
                st["formatted"] = f.read(8) == b"EXFAT   "
    except (OSError, struct.error):
        pass
    return st


def last_log_line(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            chunk = b""
            while pos > 0 and chunk.count(b"\n") < 3:
                step = min(4096, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + chunk
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        return lines[-1].decode("utf-8", errors="replace") if lines else ""
    except OSError:
        return ""


def version_info() -> dict:
    ver = "?"
    try:
        from importlib.metadata import version as _v
        ver = _v("blink-usb-bridge")
    except Exception:  # noqa: BLE001
        pass
    git = _run(["git", "-C", str(project_dir()), "rev-parse", "--short", "HEAD"], timeout=10)
    return {"version": ver, "git": git.stdout.strip() if git.returncode == 0 else ""}


def hostname() -> str:
    try:
        return Path("/etc/hostname").read_text().strip()
    except OSError:
        import socket
        return socket.gethostname()


def set_hostname(name: str) -> None:
    if shutil.which("raspi-config"):
        r = _run(["raspi-config", "nonint", "do_hostname", name], timeout=30)
        if r.returncode == 0:
            return
    _run(["hostnamectl", "set-hostname", name], timeout=30)
    try:
        hosts = Path("/etc/hosts")
        lines = hosts.read_text().splitlines()
        lines = [l for l in lines if not l.startswith("127.0.1.1")]
        lines.append(f"127.0.1.1\t{name}")
        hosts.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


# ────────────────────────────── jobs ──────────────────────────────

class Job:
    def __init__(self, name: str):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.status = "running"
        self.lines: list[str] = []
        self.started = time.time()
        self.finished: Optional[float] = None
        self.result: dict = {}
        self._lock = threading.Lock()

    def log(self, line: str) -> None:
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S")
            for ln in str(line).rstrip().splitlines() or [""]:
                self.lines.append(f"{ts}  {ln}")
                if len(self.lines) > 2000:
                    del self.lines[:500]

    def stream(self, cmd: list[str], env: Optional[dict] = None, timeout: int = 1800) -> int:
        """Run a command, streaming its output into the job log."""
        self.log("$ " + " ".join(cmd))
        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={**os.environ, **(env or {})}, cwd=str(project_dir()),
            )
        except FileNotFoundError as e:
            self.log(f"error: {e}")
            return 127
        deadline = time.time() + timeout
        assert p.stdout is not None
        for line in p.stdout:
            self.log(line)
            if time.time() > deadline:
                p.kill()
                self.log("error: timed out")
                return 124
        return p.wait()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "name": self.name, "status": self.status,
                "log": list(self.lines), "result": self.result,
                "started": self.started, "finished": self.finished,
            }


class Jobs:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, name: str, fn: Callable[[Job], None]) -> Job:
        job = Job(name)
        with self._lock:
            self._jobs[job.id] = job
            # Keep memory bounded.
            for jid in list(self._jobs)[:-50]:
                del self._jobs[jid]

        def runner():
            try:
                fn(job)
                if job.status == "running":
                    job.status = "done"
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", name)
                job.log(f"error: {type(e).__name__}: {e}")
                job.status = "failed"
            finally:
                job.finished = time.time()

        threading.Thread(target=runner, name=f"job-{name}", daemon=True).start()
        return job

    def get(self, jid: str) -> Optional[Job]:
        return self._jobs.get(jid)

    def running(self, name: str) -> bool:
        return any(j.name == name and j.status == "running" for j in self._jobs.values())


# ────────────────────────────── network watchdog ──────────────────────────────

class NetWatch(threading.Thread):
    """Raise the hotspot when the Pi has no network; retry the saved Wi-Fi periodically."""

    def __init__(self, get_setup_cfg: Callable[[], dict], grace: int = 60,
                 interval: int = 10, retry_every: int = 300):
        super().__init__(name="netwatch", daemon=True)
        self.get_setup_cfg = get_setup_cfg
        self.grace, self.interval, self.retry_every = grace, interval, retry_every
        self.pause = threading.Event()     # set while a Wi-Fi connect job runs
        self.down_since: Optional[float] = None
        self.last_retry = 0.0
        self.last_error = ""
        self.enabled = nm.available() and os.environ.get("BUB_NO_HOTSPOT") != "1"

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("netwatch tick failed")
            time.sleep(self.interval)

    def tick(self) -> None:
        setup = self.get_setup_cfg()
        if not self.enabled or not setup.get("hotspot_enabled", True) or self.pause.is_set():
            return
        if not nm.wifi_device():
            return  # no Wi-Fi adapter: nothing to raise a hotspot on
        if nm.hotspot_active():
            if nm.saved_wifi() and time.time() - self.last_retry > self.retry_every:
                self.last_retry = time.time()
                self.try_saved_wifi(setup)
            return
        if nm.has_uplink():
            self.down_since = None
            return
        if self.down_since is None:
            self.down_since = time.time()
            return
        if time.time() - self.down_since >= self.grace:
            ok, msg = nm.hotspot_up(setup.get("hotspot_ssid", "BlinkPi-Setup"),
                                    setup.get("hotspot_password", ""))
            if not ok:
                log.warning("hotspot failed: %s", msg)
            self.down_since = time.time()  # back off before retrying

    def try_saved_wifi(self, setup: dict) -> bool:
        """Drop the hotspot, try the saved profile, restore the hotspot on failure."""
        log.info("netwatch: retrying saved Wi-Fi")
        nm.hotspot_down()
        ok, msg = nm.connect_wifi(45)
        if ok:
            self.last_error = ""
            return True
        self.last_error = msg
        log.info("netwatch: saved Wi-Fi failed (%s); hotspot back up", msg)
        nm.hotspot_up(setup.get("hotspot_ssid", "BlinkPi-Setup"), setup.get("hotspot_password", ""))
        return False


# ────────────────────────────── app ──────────────────────────────

def make_app():
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials

    app = FastAPI(title="BlinkPi setup", version=version_info()["version"], docs_url=None, redoc_url=None)
    jobs = Jobs()
    basic = HTTPBasic(auto_error=False)

    def setup_cfg() -> dict:
        raw, _ = load_raw_config()
        return raw.get("setup") or {}

    watch = NetWatch(setup_cfg)
    watch.start()

    # ---- auth ----
    def require_auth(request: Request, creds: Optional[HTTPBasicCredentials] = Depends(basic)):
        stored = load_state().get("admin_password")
        if not stored:
            return
        if creds and _check_password(creds.password, stored):
            return
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": 'Basic realm="BlinkPi"'})

    auth = [Depends(require_auth)]

    # ---- captive portal probes (no auth) ----
    @app.get("/generate_204")
    @app.get("/gen_204")
    @app.get("/hotspot-detect.html")
    @app.get("/library/test/success.html")
    @app.get("/connecttest.txt")
    @app.get("/ncsi.txt")
    @app.get("/redirect")
    @app.get("/canonical.html")
    @app.get("/success.txt")
    @app.get("/check_network_status.txt")
    @app.get("/mobile/status.php")
    def captive_probe(request: Request):
        if nm.hotspot_active():
            return RedirectResponse(f"http://{nm.HOTSPOT_IP}/", status_code=302)
        raise HTTPException(404)

    # ---- page ----
    @app.get("/", response_class=HTMLResponse, dependencies=auth)
    def index():
        if not INDEX_HTML.exists():
            raise HTTPException(500, "setup_index.html missing from package")
        return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))

    # ---- status ----
    @app.get("/api/status", dependencies=auth)
    def api_status():
        raw, exists = load_raw_config()
        units = unit_states()
        image = image_status(Path(str(raw.get("backing_image_path", ""))))
        installed = units["blink-gadget.service"]["enabled"] not in ("unknown", "not-found", "")
        pd = Path(str(raw.get("project_dir", project_dir())))
        try:
            du = shutil.disk_usage(pd if pd.exists() else project_dir())
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            disk = {}
        return JSONResponse({
            "hostname": hostname(),
            "config_exists": exists,
            "installed": installed,
            "version": version_info(),
            "time": datetime.now().isoformat(timespec="seconds"),
            "network": {
                "available": nm.available(),
                "connectivity": nm.connectivity(),
                "hotspot": nm.hotspot_active(),
                "hotspot_ssid": (raw.get("setup") or {}).get("hotspot_ssid"),
                "hotspot_ip": nm.HOTSPOT_IP,
                "ips": nm.ip_addresses(),
                "devices": nm.devices(),
                "ssid": nm.current_ssid(),
                "saved": nm.saved_wifi(),
                "last_error": watch.last_error or load_state().get("last_wifi_error", ""),
            },
            "services": units,
            "image": image,
            "disk": disk,
            "last_sync": last_log_line(pd / "sync.log"),
            "rclone": {"available": rc.available(), "version": rc.version()},
            "auth": {"password_set": bool(load_state().get("admin_password"))},
            "jobs": {"apply": jobs.running("apply"), "update": jobs.running("update"),
                     "wifi": jobs.running("wifi")},
        })

    # ---- config ----
    @app.get("/api/config", dependencies=auth)
    def api_config():
        raw, exists = load_raw_config()
        smb = (raw.get("destinations") or {}).get("smb") or {}
        creds = Path(str(smb.get("credentials_file", "/etc/samba/credentials/blink")))
        rconf = Path(str(((raw.get("destinations") or {}).get("rclone") or {}).get("config_file", "")))
        from zoneinfo import available_timezones
        return JSONResponse({
            "config": raw,
            "exists": exists,
            "smb_username": smb_username(creds),
            "smb_password_set": creds.exists(),
            "rclone_remotes": rc.list_remotes(rconf) if str(rconf) else [],
            "timezones": sorted(available_timezones()) or [str(raw.get("timezone", "UTC"))],
            "project_dir": str(project_dir()),
            "secrets_set": {k: bool(load_secrets().get(k)) for k in SECRET_KEYS},
            "ai_presets": ai_vision.PRESETS,
            "default_prompt": ai_vision.DEFAULT_PROMPT,
        })

    @app.post("/api/config", dependencies=auth)
    async def api_config_save(request: Request):
        body = await request.json()
        raw_current, _ = load_raw_config()
        incoming = body.get("config") or {}
        raw = _deep_merge(raw_current, incoming)
        # Derived paths are not user-editable; keep them consistent.
        raw["project_dir"] = str(project_dir())
        raw["backing_image_path"] = str(project_dir() / "usb_backing.img")
        raw["mount_point"] = str(project_dir() / "usb_mount")
        raw.setdefault("web", {})["mount_point"] = str(project_dir() / "web_mount")
        raw["nightly_wipe_time"] = str(raw.get("nightly_wipe_time", "03:00"))
        raw["poll_interval_seconds"] = int(raw.get("poll_interval_seconds", 30) or 30)

        problems = validate_config(raw)
        new_host = str(body.get("hostname") or "").strip().lower()
        if new_host and not _HOSTNAME_RE.match(new_host):
            problems.append("hostname may contain lowercase letters, digits and hyphens")
        smb = raw["destinations"]["smb"]
        smb_user = str(body.get("smb_username") or "").strip()
        smb_pass = str(body.get("smb_password") or "")
        creds = Path(str(smb.get("credentials_file", "/etc/samba/credentials/blink")))
        if smb.get("enabled") and not creds.exists() and not (smb_user and smb_pass):
            problems.append("SMB is enabled but no username/password was given")
        if problems:
            return JSONResponse({"ok": False, "problems": problems}, status_code=400)

        def apply(job: Job):
            if smb_user and smb_pass:
                write_smb_credentials(creds, smb_user, smb_pass)
                job.log(f"wrote SMB credentials to {creds}")
            write_config(raw)
            job.log(f"wrote {config_path()}")
            tz = str(raw.get("timezone", "UTC"))
            if shutil.which("timedatectl"):
                _run(["timedatectl", "set-timezone", tz], timeout=30)
                job.log(f"timezone set to {tz}")
            if new_host and new_host != hostname():
                set_hostname(new_host)
                job.log(f"hostname set to {new_host} (reachable as {new_host}.local after reboot)")
            installer = project_dir() / "scripts" / "install.sh"
            if not installer.exists():
                job.log("install.sh not found; config written only")
                return
            rc_ = job.stream(["bash", str(installer)], env={"BUB_SKIP_VENV": "1", "BUB_FROM_SETUP": "1"}, timeout=900)
            if rc_ != 0:
                job.status = "failed"
                job.log(f"install.sh exited {rc_}")
                return
            if (raw.get("web") or {}).get("enabled"):
                job.stream(["systemctl", "restart", "bub-web.service"])
            else:
                job.stream(["systemctl", "disable", "--now", "bub-web.service"])
            job.log("applied. If this was the first setup, plug the Pi into the Sync Module and "
                    "format the drive from the Blink app (Sync Module > Local Storage).")

        job = jobs.start("apply", apply)
        return JSONResponse({"ok": True, "job": job.id})

    @app.post("/api/smb/test", dependencies=auth)
    async def api_smb_test(request: Request):
        body = await request.json()
        server = str(body.get("server") or "").strip()
        share = str(body.get("share") or "").strip()
        vers = str(body.get("smb_version") or "3.0").strip()
        user = str(body.get("username") or "").strip()
        pw = str(body.get("password") or "")
        raw, _ = load_raw_config()
        creds = Path(str(raw["destinations"]["smb"].get("credentials_file", "/etc/samba/credentials/blink")))
        if not (server and share):
            return JSONResponse({"ok": False, "message": "server and share are required"}, status_code=400)

        def test(job: Job):
            if os.geteuid() != 0:
                job.log("not running as root; cannot mount for a test")
                job.status = "failed"
                return
            cred_file = creds
            if user and pw:
                cred_file = Path("/run/blinkpi-smbtest.cred")
                write_smb_credentials(cred_file, user, pw)
            elif not creds.exists():
                job.log("no saved credentials and none given")
                job.status = "failed"
                return
            mnt = Path("/run/blinkpi-smbtest")
            mnt.mkdir(exist_ok=True)
            opts = f"credentials={cred_file},vers={vers},ro"
            r = job.stream(["mount", "-t", "cifs", f"//{server}/{share}", str(mnt), "-o", opts], timeout=60)
            if r == 0:
                try:
                    names = sorted(p.name for p in mnt.iterdir())[:20]
                    job.log("mounted OK; top-level entries: " + (", ".join(names) or "(empty)"))
                except OSError as e:
                    job.log(f"mounted but listing failed: {e}")
                finally:
                    _run(["umount", str(mnt)], timeout=30)
            else:
                job.status = "failed"
                job.log("mount failed - check server, share name, credentials and SMB version. "
                        "`dmesg | tail` on the Pi has the kernel's reason.")
            if cred_file != creds:
                try:
                    cred_file.unlink()
                except OSError:
                    pass

        job = jobs.start("smbtest", test)
        return JSONResponse({"ok": True, "job": job.id})

    # ---- Wi-Fi ----
    @app.get("/api/wifi/scan", dependencies=auth)
    def api_wifi_scan(rescan: int = 0):
        return JSONResponse({"networks": nm.scan(rescan=bool(rescan)), "hotspot": nm.hotspot_active()})

    @app.post("/api/wifi/connect", dependencies=auth)
    async def api_wifi_connect(request: Request):
        body = await request.json()
        ssid = str(body.get("ssid") or "").strip()
        password = str(body.get("password") or "")
        country = str(body.get("country") or "").strip()
        hidden = bool(body.get("hidden"))
        if not ssid:
            return JSONResponse({"ok": False, "message": "SSID is required"}, status_code=400)
        if jobs.running("wifi"):
            return JSONResponse({"ok": False, "message": "a Wi-Fi change is already in progress"}, status_code=409)
        setup = setup_cfg()

        def connect(job: Job):
            watch.pause.set()
            try:
                if country:
                    ok, msg = nm.set_country(country)
                    job.log(f"wifi country {country}: {msg}")
                ok, msg = nm.save_wifi(ssid, password, hidden)
                if not ok:
                    job.status = "failed"
                    job.log(f"could not save profile: {msg}")
                    return
                was_hotspot = nm.hotspot_active()
                if was_hotspot:
                    job.log("taking the setup hotspot down - your phone/laptop will lose this "
                            "connection. If the hotspot does not come back within a minute, "
                            f"the Pi joined '{ssid}'. Rejoin your own network and open "
                            f"http://{hostname()}.local/")
                    time.sleep(2)
                    nm.hotspot_down()
                job.log(f"connecting to '{ssid}'...")
                ok, msg = nm.connect_wifi(45)
                if ok:
                    ips = ", ".join(f"{a['device']} {a['address']}" for a in nm.ip_addresses())
                    job.log(f"connected. addresses: {ips}")
                    save_state({"last_wifi_error": ""})
                    watch.last_error = ""
                    watch.down_since = None
                else:
                    job.status = "failed"
                    job.log(f"connection failed: {msg}")
                    save_state({"last_wifi_error": msg})
                    watch.last_error = msg
                    if was_hotspot:
                        job.log("restoring the setup hotspot")
                        nm.hotspot_up(setup.get("hotspot_ssid", "BlinkPi-Setup"),
                                      setup.get("hotspot_password", ""))
            finally:
                watch.pause.clear()

        job = jobs.start("wifi", connect)
        return JSONResponse({"ok": True, "job": job.id})

    @app.post("/api/wifi/forget", dependencies=auth)
    def api_wifi_forget():
        nm.forget_wifi()
        return JSONResponse({"ok": True})

    @app.post("/api/hotspot", dependencies=auth)
    async def api_hotspot(request: Request):
        body = await request.json()
        setup = setup_cfg()
        if body.get("up"):
            ok, msg = nm.hotspot_up(setup.get("hotspot_ssid", "BlinkPi-Setup"), setup.get("hotspot_password", ""))
        else:
            nm.hotspot_down()
            ok, msg = True, "down"
        return JSONResponse({"ok": ok, "message": msg})

    # ---- rclone ----
    @app.get("/api/rclone/providers", dependencies=auth)
    def api_rclone_providers():
        return JSONResponse({"providers": [{"id": k, **v} for k, v in rc.PROVIDERS.items()],
                             "available": rc.available(), "version": rc.version()})

    def _rclone_conf(raw: dict) -> Path:
        return Path(str(raw["destinations"]["rclone"].get("config_file")))

    @app.get("/api/rclone/remotes", dependencies=auth)
    def api_rclone_remotes():
        raw, _ = load_raw_config()
        return JSONResponse({"remotes": rc.list_remotes(_rclone_conf(raw)), "config_file": str(_rclone_conf(raw))})

    @app.post("/api/rclone/remote", dependencies=auth)
    async def api_rclone_save(request: Request):
        body = await request.json()
        raw, _ = load_raw_config()
        try:
            res = rc.save_remote(
                _rclone_conf(raw), str(body.get("name") or "").strip(), str(body.get("type") or ""),
                body.get("options") or {}, str(body.get("token") or ""), owner=str(raw.get("pi_user") or ""),
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=400)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=500)
        return JSONResponse({"ok": True, **res})

    @app.delete("/api/rclone/remote/{name}", dependencies=auth)
    def api_rclone_delete(name: str):
        raw, _ = load_raw_config()
        return JSONResponse({"ok": rc.delete_remote(_rclone_conf(raw), name, owner=str(raw.get("pi_user") or ""))})

    @app.post("/api/rclone/test", dependencies=auth)
    async def api_rclone_test(request: Request):
        body = await request.json()
        raw, _ = load_raw_config()
        remote = str(body.get("remote") or "")
        conf = _rclone_conf(raw)

        def test(job: Job):
            job.log(f"rclone lsd {remote}")
            ok, msg = rc.test_remote(conf, remote)
            job.log(msg)
            if not ok:
                job.status = "failed"

        job = jobs.start("rclonetest", test)
        return JSONResponse({"ok": True, "job": job.id})

    # ---- secrets / notifications / AI ----
    @app.post("/api/secrets", dependencies=auth)
    async def api_secrets(request: Request):
        body = await request.json()
        raw, _ = load_raw_config()
        cur = save_secrets(body or {}, str(raw.get("pi_user") or ""))
        return JSONResponse({"ok": True, "secrets_set": {k: bool(cur.get(k)) for k in SECRET_KEYS}})

    @app.post("/api/notify/test", dependencies=auth)
    async def api_notify_test(request: Request):
        body = await request.json()
        channel = str(body.get("channel") or "email")
        hostn = hostname()
        if channel == "email":
            e = body.get("email") or {}
            password = str(e.get("password") or "") or load_secrets().get("smtp_password", "")
            to = str(e.get("to") or "").replace(";", ",").split(",")
            ok, msg = notify.send_email(
                host=str(e.get("host") or ""), port=int(e.get("port") or 0), security=str(e.get("security") or "starttls"),
                username=str(e.get("username") or ""), password=password, from_addr=str(e.get("from") or ""),
                to=to, subject=f"BlinkPi test from {hostn}",
                body="This is a test message from your BlinkPi setup page. Email notifications are working.",
            )
        elif channel == "webhook":
            ok, msg = notify.post_webhook(str((body.get("webhook") or {}).get("url") or ""), {
                "event": "test", "subject": f"BlinkPi test from {hostn}", "body": "Webhook notifications are working.",
                "host": hostn, "time": datetime.now().isoformat(timespec="seconds"),
                "camera": "test-camera", "filename": "2026-01-01_12-00-00_test-camera.mp4",
                "description": "Example payload; real events carry the clip's details.", "alert": False, "tags": [],
            })
        else:
            return JSONResponse({"ok": False, "message": "unknown channel"}, status_code=400)
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 502)

    @app.post("/api/ai/models", dependencies=auth)
    async def api_ai_models(request: Request):
        body = await request.json()
        key = str(body.get("api_key") or "") or load_secrets().get("ai_api_key", "")
        models, msg = ai_vision.list_models(str(body.get("base_url") or ""), key)
        return JSONResponse({"ok": msg == "ok" or bool(models), "models": models, "message": msg})

    @app.post("/api/ai/test", dependencies=auth)
    async def api_ai_test(request: Request):
        """Describe the newest clip on the image with the given (or saved) settings."""
        body = await request.json()
        api_key = str(body.get("api_key") or "")
        base_url = str(body.get("base_url") or "")
        model = str(body.get("model") or "")
        frames = int(body.get("frames") or 0)
        interval = float(body.get("frame_interval_seconds") or 0)
        prompt = str(body.get("prompt") or "")
        if not config_path().exists():
            return JSONResponse({"ok": False, "message": "press Apply once first so config.yaml exists"}, status_code=400)

        def test(job: Job):
            import dataclasses
            from zoneinfo import ZoneInfo
            c = cfg.load(config_path())
            secrets = dict(c.secrets)
            if api_key:
                secrets["ai_api_key"] = api_key
            ai = dataclasses.replace(
                c.ai, enabled=True, base_url=base_url or c.ai.base_url,
                model=model or c.ai.model, prompt=prompt or c.ai.prompt,
                frames=frames or c.ai.frames, frame_interval_seconds=interval if interval else c.ai.frame_interval_seconds,
            )
            c = dataclasses.replace(c, ai=ai, secrets=secrets)
            if not c.backing_image_path.exists():
                job.log("no backing image yet")
                job.status = "failed"
                return
            if os.geteuid() != 0:
                job.log("not running as root; cannot mount the image")
                job.status = "failed"
                return
            mnt = c.mount_point.with_name(c.mount_point.name + "_setup")
            mnt.mkdir(parents=True, exist_ok=True)
            _run(["umount", str(mnt)], timeout=20)
            r = _run(["mount", "-o", f"ro,loop,offset={sm2.partition_offset(c.backing_image_path)}",
                      str(c.backing_image_path), str(mnt)], timeout=30)
            if r.returncode != 0:
                job.log(f"mount failed: {r.stderr.strip()} (has the Sync Module formatted the drive yet?)")
                job.status = "failed"
                return
            try:
                newest = None
                for p_ in (mnt / sm2.CLIPS_ROOT).rglob("*.mp4"):
                    rel = str(p_.relative_to(mnt))
                    if sm2.is_skippable(rel):
                        continue
                    parsed = sm2.parse(rel)
                    if parsed and (newest is None or parsed.timestamp_utc > newest[1].timestamp_utc):
                        newest = (p_, parsed)
                if not newest:
                    job.log("no clips on the image yet")
                    job.status = "failed"
                    return
                clip, parsed = newest
                rel = str(clip.relative_to(mnt))
                job.log(f"analysing {rel} with {c.ai.model} at {c.ai.base_url}")
                dur = 0.0
                pr = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", str(clip)], timeout=30)
                try:
                    dur = float(pr.stdout.strip())
                except ValueError:
                    dur = 5.0
                local_ts = parsed.timestamp_utc.astimezone(ZoneInfo(c.timezone))
                res, msg = ai_vision.analyze_clip(c, clip, rel, parsed.camera, dur, local_ts, force=True)
                if res is None:
                    job.log(f"failed: {msg}")
                    job.status = "failed"
                    return
                job.log(f"frames: {res.get('frames')}  alert: {res.get('alert')}  tags: {', '.join(res.get('tags') or [])}")
                job.log(res.get("description", ""))
                u = res.get("usage") or {}
                if u.get("input_tokens"):
                    job.log(f"tokens: {u.get('input_tokens')} in / {u.get('output_tokens')} out")
            finally:
                _run(["umount", str(mnt)], timeout=20)

        job = jobs.start("aitest", test)
        return JSONResponse({"ok": True, "job": job.id})

    # ---- jobs ----
    @app.get("/api/jobs/{jid}", dependencies=auth)
    def api_job(jid: str):
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404, "no such job")
        return JSONResponse(job.to_dict())

    # ---- maintenance ----
    @app.get("/api/logs", dependencies=auth)
    def api_logs(unit: str = "blink-sync.service", lines: int = 200):
        lines = max(10, min(lines, 2000))
        if unit == "sync.log":
            raw, _ = load_raw_config()
            p = Path(str(raw.get("project_dir", project_dir()))) / "sync.log"
            try:
                text = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
            except OSError as e:
                text = f"(no sync.log: {e})"
            return PlainTextResponse(text)
        if unit not in UNITS:
            raise HTTPException(400, "unknown unit")
        r = _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=30)
        return PlainTextResponse(r.stdout or r.stderr)

    @app.post("/api/actions/{action}", dependencies=auth)
    def api_action(action: str):
        if action == "run-sync":
            job = jobs.start("sync", lambda j: j.stream(["systemctl", "start", "blink-sync.service"], timeout=600))
        elif action == "run-wipe":
            job = jobs.start("wipe", lambda j: j.stream(["systemctl", "start", "blink-wipe.service"], timeout=900))
        elif action == "restart-gadget":
            job = jobs.start("gadget", lambda j: j.stream(["systemctl", "restart", "blink-gadget.service"], timeout=60))
        elif action == "update":
            if jobs.running("update"):
                raise HTTPException(409, "update already running")

            def update(j: Job):
                pd = str(project_dir())
                raw, _ = load_raw_config()
                # git refuses to run as root in a directory owned by another
                # user, and pip should not litter the venv with root-owned
                # files, so run both as the project owner.
                as_user = ["sudo", "-u", str(raw.get("pi_user") or _default_user())] if os.geteuid() == 0 else []
                if not (project_dir() / ".git").exists():
                    j.log("this install is not a git clone; update from GitHub is not available")
                    j.status = "failed"
                    return
                if j.stream([*as_user, "git", "-C", pd, "pull", "--ff-only"], timeout=300) != 0:
                    j.status = "failed"
                    return
                pip = project_dir() / ".venv" / "bin" / "pip"
                if j.stream([*as_user, str(pip), "install", "-e", f"{pd}[web]"], timeout=900) != 0:
                    j.status = "failed"
                    return
                if j.stream(["bash", f"{pd}/scripts/install.sh"], env={"BUB_SKIP_VENV": "1", "BUB_FROM_SETUP": "1"}, timeout=900) != 0:
                    j.status = "failed"
                    return
                j.log("update applied; restarting services in 3 s")
                _run(["systemd-run", "--on-active=3", "--quiet", "systemctl", "restart",
                      "bub-web.service", "blink-setup.service"], timeout=30)

            job = jobs.start("update", update)
        elif action == "reboot":
            _run(["systemd-run", "--on-active=2", "--quiet", "systemctl", "reboot"], timeout=30)
            return JSONResponse({"ok": True, "message": "rebooting"})
        else:
            raise HTTPException(400, "unknown action")
        return JSONResponse({"ok": True, "job": job.id})

    @app.post("/api/password", dependencies=auth)
    async def api_password(request: Request):
        body = await request.json()
        pw = str(body.get("password") or "")
        if pw == "":
            save_state({"admin_password": ""})
            return JSONResponse({"ok": True, "password_set": False})
        if len(pw) < 6:
            return JSONResponse({"ok": False, "message": "password must be at least 6 characters"}, status_code=400)
        save_state({"admin_password": _hash_password(pw)})
        return JSONResponse({"ok": True, "password_set": True})

    # ---- captive portal catch-all (must be last) ----
    @app.get("/{path:path}")
    def catch_all(path: str, request: Request):
        if path.startswith("api/"):
            raise HTTPException(404)
        if nm.hotspot_active():
            return RedirectResponse(f"http://{nm.HOTSPOT_IP}/", status_code=302)
        raise HTTPException(404)

    return app


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BUB_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn not installed. Reinstall with `pip install -e '.[web]'`.")
        return 2
    setup, _ = load_raw_config()
    s = setup.get("setup") or {}
    host = os.environ.get("BUB_SETUP_HOST", str(s.get("listen_host", "0.0.0.0")))
    port = int(os.environ.get("BUB_SETUP_PORT", s.get("listen_port", 80)))
    if os.name == "posix" and os.geteuid() != 0:
        log.warning("not running as root: Wi-Fi, installer and service controls will not work")
    app = make_app()
    log.info("setup page on http://%s:%d (project %s)", host, port, project_dir())
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("BUB_LOG_LEVEL", "info").lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
