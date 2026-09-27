"""Create and test rclone remotes from the setup page, without `rclone config`.

rclone's interactive `config` flow doesn't map onto a web form, and its
`--non-interactive` mode still asks follow-up questions for some backends.
So we write rclone.conf directly: it's a plain INI file where each section
is a remote and `type = <backend>` names the backend. Passwords are stored
obscured (via `rclone obscure`), exactly as `rclone config` would do.

OAuth backends (Google Drive, Dropbox, ...) need a browser on a machine
with a screen. The user runs `rclone authorize "<backend>"` on their
laptop and pastes the token JSON here - that's the documented headless
flow, just with a text box instead of a terminal prompt.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# What the form offers. `fields` are written to the config section as-is;
# `secret` fields are obscured first. `oauth` backends take a pasted token.
PROVIDERS: dict[str, dict] = {
    "drive": {
        "label": "Google Drive", "oauth": True,
        "fields": [
            {"key": "client_id", "label": "Client ID (optional)", "optional": True},
            {"key": "client_secret", "label": "Client secret (optional)", "optional": True, "secret": False},
        ],
        "fixed": {"scope": "drive"},
        "help": "Run  rclone authorize \"drive\"  on a computer with a browser, sign in, "
                "then paste the token JSON it prints.",
    },
    "dropbox": {
        "label": "Dropbox", "oauth": True, "fields": [],
        "help": "Run  rclone authorize \"dropbox\"  on a computer with a browser and paste the token.",
    },
    "box": {
        "label": "Box", "oauth": True, "fields": [],
        "help": "Run  rclone authorize \"box\"  on a computer with a browser and paste the token.",
    },
    "pcloud": {
        "label": "pCloud", "oauth": True, "fields": [],
        "help": "Run  rclone authorize \"pcloud\"  on a computer with a browser and paste the token.",
    },
    "onedrive": {
        "label": "Microsoft OneDrive", "oauth": True,
        "fields": [
            {"key": "drive_id", "label": "Drive ID"},
            {"key": "drive_type", "label": "Drive type (personal / business / documentLibrary)"},
        ],
        "help": "Run  rclone authorize \"onedrive\"  on a computer with a browser and paste the token. "
                "OneDrive also needs your drive_id and drive_type; the easiest way to get them is to "
                "run  rclone config  once on that computer and copy them from its rclone.conf.",
    },
    "s3": {
        "label": "S3 compatible (AWS, Wasabi, MinIO, ...)", "oauth": False,
        "fields": [
            {"key": "provider", "label": "Provider (AWS, Wasabi, Minio, Other, ...)"},
            {"key": "access_key_id", "label": "Access key ID"},
            {"key": "secret_access_key", "label": "Secret access key", "secret": False, "password": True},
            {"key": "region", "label": "Region (e.g. us-east-1)", "optional": True},
            {"key": "endpoint", "label": "Endpoint URL (non-AWS providers)", "optional": True},
        ],
        "help": "Put the bucket name in the remote path, e.g. remote name 'wasabi' and path 'wasabi:my-bucket/blink'.",
    },
    "b2": {
        "label": "Backblaze B2", "oauth": False,
        "fields": [
            {"key": "account", "label": "Application key ID"},
            {"key": "key", "label": "Application key", "password": True},
        ],
        "help": "Bucket goes in the remote path, e.g. 'b2:my-bucket/blink'.",
    },
    "sftp": {
        "label": "SFTP", "oauth": False,
        "fields": [
            {"key": "host", "label": "Host"},
            {"key": "user", "label": "Username"},
            {"key": "pass", "label": "Password", "secret": True, "password": True},
            {"key": "port", "label": "Port", "optional": True, "default": "22"},
        ],
        "help": "",
    },
    "webdav": {
        "label": "WebDAV / Nextcloud / ownCloud", "oauth": False,
        "fields": [
            {"key": "url", "label": "URL (e.g. https://cloud.example.com/remote.php/dav/files/USER/)"},
            {"key": "vendor", "label": "Vendor (nextcloud, owncloud, other)", "default": "other"},
            {"key": "user", "label": "Username"},
            {"key": "pass", "label": "Password", "secret": True, "password": True},
        ],
        "help": "",
    },
    "ftp": {
        "label": "FTP", "oauth": False,
        "fields": [
            {"key": "host", "label": "Host"},
            {"key": "user", "label": "Username"},
            {"key": "pass", "label": "Password", "secret": True, "password": True},
            {"key": "port", "label": "Port", "optional": True, "default": "21"},
            {"key": "explicit_tls", "label": "Explicit TLS (true/false)", "optional": True, "default": "false"},
        ],
        "help": "",
    },
    "raw": {
        "label": "Advanced: paste an rclone.conf section", "oauth": False, "fields": [],
        "help": "Paste the [section] from an rclone.conf that you configured elsewhere. "
                "The section name becomes the remote name.",
    },
}

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,62}$")


def available() -> bool:
    return shutil.which("rclone") is not None


def version() -> str:
    if not available():
        return "not installed"
    try:
        r = subprocess.run(["rclone", "version"], capture_output=True, text=True, timeout=10)
        return (r.stdout.splitlines() or ["?"])[0].strip()
    except subprocess.TimeoutExpired:
        return "?"


def _parser() -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(interpolation=None, delimiters=("=",))
    cp.optionxform = str  # rclone keys are case-sensitive
    return cp


def _read(conf: Path) -> configparser.RawConfigParser:
    cp = _parser()
    if conf.exists():
        cp.read(conf, encoding="utf-8")
    return cp


def _write(conf: Path, cp: configparser.RawConfigParser, owner: Optional[str]) -> None:
    conf.parent.mkdir(parents=True, exist_ok=True)
    tmp = conf.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=True)
    os.chmod(tmp, 0o600)
    tmp.replace(conf)
    _chown_tree(conf.parent, owner)
    _chown_tree(conf, owner)


def _chown_tree(path: Path, owner: Optional[str]) -> None:
    if not owner or os.name != "posix":
        return
    try:
        import pwd
        pw = pwd.getpwnam(owner)
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (ImportError, KeyError, PermissionError) as e:
        log.warning("could not chown %s to %s: %s", path, owner, e)


def list_remotes(conf: Path) -> list[dict]:
    cp = _read(conf)
    out = []
    for name in cp.sections():
        typ = cp.get(name, "type", fallback="?")
        out.append({
            "name": name, "type": typ,
            "label": PROVIDERS.get(typ, {}).get("label", typ),
        })
    return out


def obscure(secret: str) -> str:
    r = subprocess.run(["rclone", "obscure", secret], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"rclone obscure failed: {r.stderr.strip()}")
    return r.stdout.strip()


def extract_token(text: str) -> str:
    """Pull the token JSON out of whatever `rclone authorize` printed."""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object found in the pasted token")
    raw = m.group(0)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"token is not valid JSON: {e}") from e
    if "access_token" not in obj and "token" not in obj:
        raise ValueError("token JSON has no access_token")
    return json.dumps(obj, separators=(",", ":"))


def save_remote(conf: Path, name: str, typ: str, options: dict, token: str = "",
                owner: Optional[str] = None) -> dict:
    """Write one remote section. Returns {name, type}. Raises ValueError."""
    if typ == "raw":
        return _save_raw(conf, options.get("raw", ""), owner)
    if typ not in PROVIDERS:
        raise ValueError(f"unknown provider {typ!r}")
    if not _NAME_RE.match(name or ""):
        raise ValueError("remote name may contain letters, digits, '_', '-', '.', and spaces")
    if not available():
        raise ValueError("rclone is not installed on the Pi")

    spec = PROVIDERS[typ]
    cp = _read(conf)
    if cp.has_section(name):
        cp.remove_section(name)
    cp.add_section(name)
    cp.set(name, "type", typ)
    for k, v in spec.get("fixed", {}).items():
        cp.set(name, k, v)

    for f in spec["fields"]:
        val = str(options.get(f["key"], "") or "").strip()
        if not val and f.get("default"):
            val = f["default"]
        if not val:
            if f.get("optional"):
                continue
            raise ValueError(f"{f['label']} is required")
        if f.get("secret"):
            val = obscure(val)
        cp.set(name, f["key"], val)

    if spec.get("oauth"):
        cp.set(name, "token", extract_token(token))

    _write(conf, cp, owner)
    log.info("rclone remote %r (%s) saved to %s", name, typ, conf)
    return {"name": name, "type": typ}


def _save_raw(conf: Path, section_text: str, owner: Optional[str]) -> dict:
    incoming = _parser()
    try:
        incoming.read_string(section_text or "")
    except configparser.Error as e:
        raise ValueError(f"could not parse section: {e}") from e
    if len(incoming.sections()) != 1:
        raise ValueError("paste exactly one [section]")
    name = incoming.sections()[0]
    if not incoming.has_option(name, "type"):
        raise ValueError("section has no 'type = ...' line")
    cp = _read(conf)
    if cp.has_section(name):
        cp.remove_section(name)
    cp.add_section(name)
    for k, v in incoming.items(name):
        cp.set(name, k, v)
    _write(conf, cp, owner)
    return {"name": name, "type": incoming.get(name, "type")}


def delete_remote(conf: Path, name: str, owner: Optional[str] = None) -> bool:
    cp = _read(conf)
    if not cp.has_section(name):
        return False
    cp.remove_section(name)
    _write(conf, cp, owner)
    return True


def test_remote(conf: Path, remote: str, timeout: int = 60) -> tuple[bool, str]:
    """Try to list the remote's root (or the configured subfolder)."""
    if not available():
        return False, "rclone is not installed"
    remote = remote.strip()
    if ":" not in remote:
        remote += ":"
    cmd = [
        "rclone", "lsd", "--config", str(conf), "--max-depth", "1",
        "--contimeout", "15s", "--timeout", "30s",
        "--low-level-retries", "1", "--retries", "1", remote,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "rclone failed")[-2000:]
    listing = r.stdout.strip()
    return True, listing[:2000] if listing else "connected (remote is empty)"
