"""Config loader for blink-usb-bridge.

The whole project is config-driven from a single config.yaml. This module
loads, validates, and presents that config as a typed dataclass tree.

Precedence (lowest to highest):
  1. config.example.yaml defaults (as documented constants here)
  2. config.yaml at the project root
  3. environment variables (BUB_* prefixed; useful for systemd unit overrides)

The CLI entry points all start with config.load() and pass the result down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class SmbDestination:
    enabled: bool
    server: str
    share: str
    credentials_file: Path
    mount_point: Path
    smb_version: str
    layout: str  # "per_camera" | "flat"


@dataclass(frozen=True)
class RcloneDestination:
    enabled: bool
    remote: str
    config_file: Path
    layout: str  # "flat" | "per_camera"
    retention_days: int
    use_trash: bool
    prune_marker: str


@dataclass(frozen=True)
class Validation:
    min_bytes: int
    settle_seconds: float
    ffprobe_timeout_seconds: int


@dataclass(frozen=True)
class Wipe:
    """Nightly cleanup behavior.

    retention_days = 0  → delete everything (original behavior, lowest disk use)
    retention_days = N  → keep the last N days of clips on the backing image
                          (still requires periodic cleanup or the image fills up)
    """
    retention_days: int


@dataclass(frozen=True)
class Web:
    """Local web UI for browsing clips on the Pi itself."""
    enabled: bool
    listen_host: str       # e.g. "0.0.0.0" for LAN-wide, "127.0.0.1" for local-only
    listen_port: int
    mount_point: Path      # independent loop-mount, separate from sync's mount


@dataclass(frozen=True)
class Config:
    pi_user: str
    project_dir: Path
    backing_image_path: Path
    backing_image_size: str
    mount_point: Path
    timezone: str
    poll_interval_seconds: int
    nightly_wipe_time: str
    smb: SmbDestination
    rclone: RcloneDestination
    validation: Validation
    wipe: Wipe
    web: Web

    @property
    def state_file(self) -> Path:
        return self.project_dir / "sync_state.json"

    @property
    def log_file(self) -> Path:
        return self.project_dir / "sync.log"


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


def _path(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"expected path, got {type(value).__name__}: {value!r}")
    return Path(value).expanduser()


def _require(d: dict, key: str, where: str) -> object:
    if key not in d:
        raise ValueError(f"missing required config key: {where}.{key}")
    return d[key]


def load(path: Optional[Path] = None) -> Config:
    """Load and validate config.yaml. Raises ValueError on missing/bad keys."""
    if path is None:
        path = Path(os.environ.get("BUB_CONFIG", "config.yaml"))
    path = path.expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path}. "
            f"Copy config.example.yaml to config.yaml and edit it."
        )

    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    smb_raw = (raw.get("destinations") or {}).get("smb") or {}
    smb = SmbDestination(
        enabled=_coerce_bool(smb_raw.get("enabled", False)),
        server=str(smb_raw.get("server", "")),
        share=str(smb_raw.get("share", "")),
        credentials_file=_path(smb_raw.get("credentials_file", "/etc/samba/credentials/blink")),
        mount_point=_path(smb_raw.get("mount_point", "/mnt/blink-share")),
        smb_version=str(smb_raw.get("smb_version", "3.0")),
        layout=str(smb_raw.get("layout", "per_camera")),
    )
    if smb.enabled:
        if not smb.server:
            raise ValueError("destinations.smb.enabled is true but server is empty")
        if not smb.share:
            raise ValueError("destinations.smb.enabled is true but share is empty")
        if smb.layout not in ("per_camera", "flat"):
            raise ValueError(f"destinations.smb.layout must be per_camera or flat, got {smb.layout!r}")

    rc_raw = (raw.get("destinations") or {}).get("rclone") or {}
    rclone = RcloneDestination(
        enabled=_coerce_bool(rc_raw.get("enabled", False)),
        remote=str(rc_raw.get("remote", "")),
        config_file=_path(rc_raw.get("config_file", "~/.config/rclone/rclone.conf")),
        layout=str(rc_raw.get("layout", "flat")),
        retention_days=int(rc_raw.get("retention_days", 7)),
        use_trash=_coerce_bool(rc_raw.get("use_trash", True)),
        prune_marker=str(rc_raw.get("prune_marker", "__blinkpi__")),
    )
    if rclone.enabled:
        if not rclone.remote.endswith(":") and ":" not in rclone.remote:
            raise ValueError(
                f"destinations.rclone.remote must be like 'name:' or 'name:subfolder', got {rclone.remote!r}"
            )
        if rclone.layout not in ("per_camera", "flat"):
            raise ValueError(f"destinations.rclone.layout must be per_camera or flat, got {rclone.layout!r}")

    val_raw = raw.get("validation") or {}
    validation = Validation(
        min_bytes=int(val_raw.get("min_bytes", 65536)),
        settle_seconds=float(val_raw.get("settle_seconds", 0.5)),
        ffprobe_timeout_seconds=int(val_raw.get("ffprobe_timeout_seconds", 10)),
    )

    wipe_raw = raw.get("wipe") or {}
    wipe = Wipe(
        retention_days=int(wipe_raw.get("retention_days", 0)),
    )
    if wipe.retention_days < 0:
        raise ValueError(f"wipe.retention_days must be >= 0, got {wipe.retention_days}")

    web_raw = raw.get("web") or {}
    web = Web(
        enabled=_coerce_bool(web_raw.get("enabled", False)),
        listen_host=str(web_raw.get("listen_host", "0.0.0.0")),
        listen_port=int(web_raw.get("listen_port", 8080)),
        mount_point=_path(web_raw.get("mount_point", "/home/pi/blink-usb-bridge/web_mount")),
    )

    return Config(
        pi_user=str(_require(raw, "pi_user", "")),
        project_dir=_path(_require(raw, "project_dir", "")),
        backing_image_path=_path(_require(raw, "backing_image_path", "")),
        backing_image_size=str(raw.get("backing_image_size", "4G")),
        mount_point=_path(_require(raw, "mount_point", "")),
        timezone=str(raw.get("timezone", "UTC")),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 30)),
        nightly_wipe_time=str(raw.get("nightly_wipe_time", "03:00")),
        smb=smb,
        rclone=rclone,
        validation=validation,
        wipe=wipe,
        web=web,
    )
