"""Local web UI for browsing clips on the Pi.

Single FastAPI app, single HTML page, no auth (designed for local network).
Independent loop-mount of the backing image — separate from the sync
service's mount, so they don't step on each other.

Architecture:
  GET  /              → index.html (single-page UI)
  GET  /api/clips     → JSON list of {name, camera, ts, size_bytes, url}
  GET  /clip/<rel>    → the clip bytes, with HTTP Range support for scrubbing
  GET  /api/status    → JSON with mount status, clip count, last sync run

The mount is refreshed lazily: every API call checks if the mount is
healthy, and if not, attempts to remount. This handles the gadget
service cycling during the nightly wipe gracefully.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import config as cfg
from . import sm2

log = logging.getLogger(__name__)

# Path to the bundled HTML asset, served at GET /.
_HERE = Path(__file__).parent
INDEX_HTML = _HERE / "web_index.html"

def _thumbnail_path(thumbnail_dir: Path, rel_path: str) -> Path:
    key = hashlib.sha256(rel_path.encode()).hexdigest()
    return thumbnail_dir / f"{key}.jpg"


def _generate_thumbnail(src: Path, dest: Path) -> bool:
    """Generate a thumbnail, trying t=1s then falling back to first frame."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for extra in (["-ss", "1"], []):
        try:
            r = subprocess.run(
                ["ffmpeg", *extra, "-i", str(src),
                 "-vframes", "1", "-vf", "scale=320:-1",
                 "-q:v", "5", "-f", "image2", str(dest), "-y"],
                capture_output=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            return False
        if r.returncode == 0 and dest.exists():
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Mount management
# ──────────────────────────────────────────────────────────────────────

def _is_mounted(mount_point: Path) -> bool:
    return subprocess.run(
        ["mountpoint", "-q", str(mount_point)],
    ).returncode == 0


def _mount_ro(image: Path, mount_point: Path) -> bool:
    """Loop-mount the backing image read-only at mount_point.

    Uses the existing NOPASSWD sudoers entry installed by scripts/install.sh.
    Same offset as the SM2 (16384 bytes for the partition table).
    """
    mount_point.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mount_point):
        return True
    cmd = [
        "sudo", "-n", "mount",
        "-o", f"ro,loop,offset={sm2.partition_offset(image)},noatime,nodev,nosuid,noexec",
        str(image), str(mount_point),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("web mount failed: %s", result.stderr.strip())
        return False
    return True


def _ensure_mounted(c: cfg.Config) -> bool:
    """Best-effort: mount if not already mounted. Cheap when already up."""
    if _is_mounted(c.web.mount_point):
        return True
    return _mount_ro(c.backing_image_path, c.web.mount_point)


# ──────────────────────────────────────────────────────────────────────
# Clip discovery
# ──────────────────────────────────────────────────────────────────────

def _list_clips(c: cfg.Config) -> list[dict]:
    """Return all valid clips on the backing image, newest first."""
    if not _ensure_mounted(c):
        return []

    tz = ZoneInfo(c.timezone)
    clips_dir = c.web.mount_point / sm2.CLIPS_ROOT
    if not clips_dir.exists():
        return []

    out: list[dict] = []
    for p in clips_dir.rglob("*.mp4"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(c.web.mount_point))
        if sm2.is_skippable(rel):
            continue
        parsed = sm2.parse(rel)
        if parsed is None:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < c.validation.min_bytes:
            continue

        local_ts = parsed.timestamp_utc.astimezone(tz)
        # The relative path is what the client uses to fetch the clip
        # via /clip/<rel>. We URL-quote it on the client side.
        out.append({
            "rel_path": rel,
            "filename": parsed.filename,
            "camera": parsed.camera,
            "timestamp": local_ts.isoformat(),
            "timestamp_unix": local_ts.timestamp(),
            "size_bytes": size,
        })

    out.sort(key=lambda c: c["timestamp_unix"], reverse=True)
    return out


# ──────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────

def make_app(c: cfg.Config):
    """Build the FastAPI app with the given config bound."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

    app = FastAPI(title="blink-usb-bridge", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def index():
        if not INDEX_HTML.exists():
            raise HTTPException(500, "web_index.html missing from package")
        return HTMLResponse(INDEX_HTML.read_text())

    @app.get("/api/clips")
    def api_clips():
        return JSONResponse({"clips": _list_clips(c)})

    @app.get("/api/status")
    def api_status():
        mounted = _is_mounted(c.web.mount_point)
        clip_count = len(_list_clips(c)) if mounted else 0

        # Read the sync log's last line to get the most recent run summary.
        last_sync = "no log yet"
        try:
            log_path = c.log_file
            if log_path.exists():
                with log_path.open("rb") as f:
                    f.seek(0, 2)  # end of file
                    pos = f.tell()
                    chunk = b""
                    while pos > 0 and chunk.count(b"\n") < 2:
                        step = min(4096, pos)
                        pos -= step
                        f.seek(pos)
                        chunk = f.read(step) + chunk
                    lines = [ln for ln in chunk.splitlines() if ln.strip()]
                    if lines:
                        last_sync = lines[-1].decode("utf-8", errors="replace")
        except OSError:
            pass

        return JSONResponse({
            "mounted": mounted,
            "clip_count": clip_count,
            "last_sync": last_sync,
            "version": "0.1.0",
        })

    @app.get("/clip/{rel_path:path}")
    def serve_clip(rel_path: str):
        if not _ensure_mounted(c):
            raise HTTPException(503, "backing image not mounted")
        if sm2.is_skippable(rel_path):
            raise HTTPException(404, "not found")
        target = (c.web.mount_point / rel_path).resolve()
        # Defense in depth: ensure target stays under mount_point.
        try:
            target.relative_to(c.web.mount_point.resolve())
        except ValueError:
            raise HTTPException(403, "path outside mount")
        if not target.is_file() or target.suffix.lower() != ".mp4":
            raise HTTPException(404, "not found")
        # FileResponse handles Range requests automatically — exactly what
        # the browser needs to scrub through video.
        return FileResponse(
            target,
            media_type="video/mp4",
            filename=target.name,
        )

    @app.get("/thumbnail/{rel_path:path}")
    def serve_thumbnail(rel_path: str):
        if not _ensure_mounted(c):
            raise HTTPException(503, "backing image not mounted")
        if sm2.is_skippable(rel_path):
            raise HTTPException(404, "not found")
        target = (c.web.mount_point / rel_path).resolve()
        try:
            target.relative_to(c.web.mount_point.resolve())
        except ValueError:
            raise HTTPException(403, "path outside mount")
        if not target.is_file() or target.suffix.lower() != ".mp4":
            raise HTTPException(404, "not found")

        thumb_path = _thumbnail_path(c.thumbnail_dir, rel_path)
        if not thumb_path.exists():
            if not _generate_thumbnail(target, thumb_path):
                raise HTTPException(404, "thumbnail generation failed")

        return FileResponse(
            thumb_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return app


# ──────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BUB_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        c = cfg.load()
    except (FileNotFoundError, ValueError) as e:
        log.error("config error: %s", e)
        return 2
    if not c.web.enabled:
        log.error("web.enabled is false in config; exiting")
        return 0

    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn not installed. Reinstall with `pip install -e '.[web]'`.")
        return 2

    app = make_app(c)
    log.info(
        "starting web UI on http://%s:%d (mount: %s)",
        c.web.listen_host, c.web.listen_port, c.web.mount_point,
    )
    uvicorn.run(app, host=c.web.listen_host, port=c.web.listen_port,
                log_level=os.environ.get("BUB_LOG_LEVEL", "info").lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
