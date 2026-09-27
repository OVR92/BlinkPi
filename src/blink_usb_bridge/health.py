"""Watchdog: tell the owner when the system has gone quiet or broken.

Runs at the end of every sync pass (every 30 s), so it costs nothing
extra and sees exactly what sync sees. State lives in
<project_dir>/health_state.json so alerts fire once, get reminded on a
long interval, and produce a recovery message when things come back.

Conditions
----------
- silence:       no new clip for more than watchdog.max_silence_hours
                 (suppressed during quiet hours, e.g. overnight)
- sync_failure:  N consecutive sync passes failed (image missing, mount
                 failed, ...)
- gadget_down:   blink-gadget.service is not active - the SM2 sees no
                 USB drive. This is the failure mode that used to go
                 unnoticed until someone checked the Blink app.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

from . import config as cfg
from .notify import Notifier

log = logging.getLogger(__name__)

GADGET_SERVICE = "blink-gadget.service"


def _state_path(c: cfg.Config) -> Path:
    return c.health_state_file


def load_state(c: cfg.Config) -> dict:
    p = _state_path(c)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(c: cfg.Config, st: dict) -> None:
    p = _state_path(c)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("could not write health state: %s", e)


# ──────────────────────────── recording ────────────────────────────

def record_clip(c: cfg.Config, ts: float) -> None:
    """Note the newest clip time seen (SM2 file mtime or now)."""
    st = load_state(c)
    if ts > float(st.get("last_clip_at") or 0):
        st["last_clip_at"] = ts
        save_state(c, st)


def record_sync_result(c: cfg.Config, ok: bool, reason: str = "") -> None:
    st = load_state(c)
    if ok:
        st["sync_failures"] = 0
        st["last_sync_ok_at"] = time.time()
    else:
        st["sync_failures"] = int(st.get("sync_failures") or 0) + 1
        st["last_sync_error"] = reason
    save_state(c, st)


# ──────────────────────────── checks ────────────────────────────

def _in_quiet_hours(w: cfg.Watchdog, now: datetime) -> bool:
    if not (w.quiet_start and w.quiet_end):
        return False
    try:
        s = dtime.fromisoformat(w.quiet_start)
        e = dtime.fromisoformat(w.quiet_end)
    except ValueError:
        return False
    t = now.time()
    if s <= e:
        return s <= t < e
    return t >= s or t < e  # window crosses midnight


def gadget_active() -> Optional[bool]:
    """True/False, or None if systemctl is unavailable (dev machine)."""
    try:
        r = subprocess.run(["systemctl", "is-active", GADGET_SERVICE], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() == "active"


def _fmt_hours(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:.0f} h" if h >= 2 else f"{seconds / 60:.0f} min"


def check(c: cfg.Config, notifier: Optional[Notifier] = None, now: Optional[float] = None,
          gadget_state: Optional[bool] = None) -> list[str]:
    """Evaluate all conditions, send alerts/recoveries. Returns event names fired."""
    w = c.watchdog
    if not w.enabled:
        return []
    notifier = notifier or Notifier(c)
    now = now or time.time()
    now_dt = datetime.fromtimestamp(now)
    st = load_state(c)
    fired: list[str] = []
    host = c.project_dir.name

    def alert(kind: str, subject: str, body: str) -> None:
        notifier.send(f"BlinkPi: {subject}", body, event=f"watchdog_{kind}",
                      data={"kind": kind, "camera": None})
        fired.append(kind)

    # ---- silence ----
    last_clip = float(st.get("last_clip_at") or 0)
    threshold = float(w.max_silence_hours or 0) * 3600
    if last_clip and threshold > 0:
        silence = now - last_clip
        quiet = _in_quiet_hours(w, now_dt)
        if silence > threshold and not quiet:
            last_alert = float(st.get("silence_alerted_at") or 0)
            remind = float(w.remind_every_hours or 24) * 3600
            if not last_alert or now - last_alert >= remind:
                since = datetime.fromtimestamp(last_clip).strftime("%Y-%m-%d %H:%M")
                alert("silence", f"no clips for {_fmt_hours(silence)}",
                      f"The last motion clip was recorded at {since}.\n\n"
                      "If the cameras should have seen motion since then, check that the "
                      "Sync Module shows the USB drive as connected and that the cameras are armed.")
                st["silence_alerted_at"] = now
                st["silence_open"] = True
        elif st.get("silence_open") and silence <= threshold:
            alert("silence_recovered", "clips are being recorded again",
                  f"A new clip arrived at {datetime.fromtimestamp(last_clip).strftime('%Y-%m-%d %H:%M')}.")
            st["silence_open"] = False
            st["silence_alerted_at"] = 0

    # ---- sync failures ----
    if w.alert_on_sync_failure:
        failures = int(st.get("sync_failures") or 0)
        if failures >= max(1, int(w.failures_before_alert or 3)) and not st.get("sync_failure_open"):
            alert("sync_failure", f"sync has failed {failures} times in a row",
                  f"Last error: {st.get('last_sync_error', '?')}\n\n"
                  "Open the setup page's Maintenance tab and look at the blink-sync.service log.")
            st["sync_failure_open"] = True
        elif failures == 0 and st.get("sync_failure_open"):
            alert("sync_recovered", "sync is working again", "The last sync pass completed normally.")
            st["sync_failure_open"] = False

    # ---- gadget ----
    active = gadget_state if gadget_state is not None else gadget_active()
    if active is False and not st.get("gadget_open"):
        alert("gadget_down", "USB drive is offline",
              "blink-gadget.service is not running, so the Sync Module sees no USB drive and "
              "nothing is being recorded. Press \"Re-plug USB drive\" on the setup page's "
              "Maintenance tab, or reboot the Pi.")
        st["gadget_open"] = True
    elif active is True and st.get("gadget_open"):
        alert("gadget_recovered", "USB drive is back online", "blink-gadget.service is active again.")
        st["gadget_open"] = False

    st["last_check_at"] = now
    save_state(c, st)
    return fired
