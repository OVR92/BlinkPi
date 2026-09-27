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
class Email:
    enabled: bool
    host: str
    port: int
    security: str          # "starttls" | "ssl" | "none"
    username: str
    from_addr: str
    to: tuple[str, ...]
    on_new_clip: str       # "off" | "summary" (thumbnail + AI text) | "clip" (the mp4 attached = backup)
    max_attachment_mb: float  # clips larger than this fall back to a summary email


@dataclass(frozen=True)
class Webhook:
    enabled: bool
    url: str
    on_new_clip: bool


@dataclass(frozen=True)
class Notify:
    email: Email
    webhook: Webhook


@dataclass(frozen=True)
class Ai:
    """Describe new clips with a vision model (frames extracted by ffmpeg)."""
    enabled: bool
    base_url: str          # OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1
    model: str
    frames: int            # evenly spaced frames per clip (if frame_interval_seconds == 0)
    frame_interval_seconds: float
    max_per_hour: int      # 0 = unlimited
    prompt: str            # "" = built-in prompt; {camera} {when} {frames} are substituted
    cameras: tuple[str, ...]  # empty = all cameras
    in_notifications: bool # include the description in new-clip email/webhook


@dataclass(frozen=True)
class Watchdog:
    enabled: bool
    max_silence_hours: float
    remind_every_hours: float
    quiet_start: str       # "HH:MM" or "" - no silence alerts inside this window
    quiet_end: str
    alert_on_sync_failure: bool
    failures_before_alert: int


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
    notify: Notify
    ai: Ai
    watchdog: Watchdog
    secrets: dict = field(default_factory=dict)  # from secrets.yaml (smtp_password, ai_api_key)

    @property
    def state_file(self) -> Path:
        return self.project_dir / "sync_state.json"

    @property
    def secrets_file(self) -> Path:
        return self.project_dir / "secrets.yaml"

    @property
    def analysis_dir(self) -> Path:
        return self.project_dir / "analysis"

    @property
    def health_state_file(self) -> Path:
        return self.project_dir / "health_state.json"

    @property
    def log_file(self) -> Path:
        return self.project_dir / "sync.log"

    @property
    def thumbnail_dir(self) -> Path:
        return self.project_dir / "thumbnails"


def _email_mode(value: object) -> str:
    """on_new_clip: off | summary | clip. Booleans are accepted for compatibility."""
    if isinstance(value, bool):
        return "summary" if value else "off"
    v = str(value or "off").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return "summary"
    if v in ("0", "false", "no", ""):
        return "off"
    if v not in ("off", "summary", "clip"):
        raise ValueError(f"notify.email.on_new_clip must be off, summary or clip, got {value!r}")
    return v


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

    n_raw = raw.get("notify") or {}
    em_raw = n_raw.get("email") or {}
    to_raw = em_raw.get("to") or []
    if isinstance(to_raw, str):
        to_raw = [t for t in to_raw.replace(";", ",").split(",")]
    email = Email(
        enabled=_coerce_bool(em_raw.get("enabled", False)),
        host=str(em_raw.get("host", "")),
        port=int(em_raw.get("port", 587) or 587),
        security=str(em_raw.get("security", "starttls")).lower(),
        username=str(em_raw.get("username", "")),
        from_addr=str(em_raw.get("from", "")),
        to=tuple(str(t).strip() for t in to_raw if str(t).strip()),
        on_new_clip=_email_mode(em_raw.get("on_new_clip", "off")),
        max_attachment_mb=float(em_raw.get("max_attachment_mb", 20) or 20),
    )
    if email.enabled:
        if not email.host:
            raise ValueError("notify.email.enabled is true but host is empty")
        if not email.to:
            raise ValueError("notify.email.enabled is true but no recipients in 'to'")
        if email.security not in ("starttls", "ssl", "none"):
            raise ValueError("notify.email.security must be starttls, ssl or none")
    wh_raw = n_raw.get("webhook") or {}
    webhook = Webhook(
        enabled=_coerce_bool(wh_raw.get("enabled", False)),
        url=str(wh_raw.get("url", "")),
        on_new_clip=_coerce_bool(wh_raw.get("on_new_clip", True)),
    )
    if webhook.enabled and not webhook.url.lower().startswith(("http://", "https://")):
        raise ValueError("notify.webhook.enabled is true but url is not an http(s) URL")
    notify = Notify(email=email, webhook=webhook)

    ai_raw = raw.get("ai") or {}
    cams_raw = ai_raw.get("cameras") or []
    if isinstance(cams_raw, str):
        cams_raw = cams_raw.split(",")
    ai = Ai(
        enabled=_coerce_bool(ai_raw.get("enabled", False)),
        base_url=str(ai_raw.get("base_url", "") or ""),
        model=str(ai_raw.get("model", "") or ""),
        frames=int(ai_raw.get("frames", 4) or 4),
        frame_interval_seconds=float(ai_raw.get("frame_interval_seconds", 0) or 0),
        max_per_hour=int(ai_raw.get("max_per_hour", 20) or 0),
        prompt=str(ai_raw.get("prompt", "") or ""),
        cameras=tuple(str(x).strip() for x in cams_raw if str(x).strip()),
        in_notifications=_coerce_bool(ai_raw.get("in_notifications", True)),
    )
    if ai.enabled:
        if not ai.base_url.lower().startswith(("http://", "https://")):
            raise ValueError("ai.base_url must be an http(s) URL (an OpenAI-compatible endpoint)")
        if not ai.model:
            raise ValueError("ai.model is required when ai.enabled is true")

    wd_raw = raw.get("watchdog") or {}
    watchdog = Watchdog(
        enabled=_coerce_bool(wd_raw.get("enabled", False)),
        max_silence_hours=float(wd_raw.get("max_silence_hours", 24) or 0),
        remind_every_hours=float(wd_raw.get("remind_every_hours", 24) or 24),
        quiet_start=str(wd_raw.get("quiet_start", "") or ""),
        quiet_end=str(wd_raw.get("quiet_end", "") or ""),
        alert_on_sync_failure=_coerce_bool(wd_raw.get("alert_on_sync_failure", True)),
        failures_before_alert=int(wd_raw.get("failures_before_alert", 5) or 5),
    )

    secrets: dict = {}
    secrets_path = _path(_require(raw, "project_dir", "")) / "secrets.yaml"
    if secrets_path.exists():
        try:
            with secrets_path.open() as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                secrets = {str(k): str(v) for k, v in loaded.items() if v is not None}
        except (OSError, yaml.YAMLError):
            pass

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
        notify=notify,
        ai=ai,
        watchdog=watchdog,
        secrets=secrets,
    )
