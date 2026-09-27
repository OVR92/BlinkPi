"""Thin wrapper around NetworkManager's nmcli for the setup page.

Raspberry Pi OS Bookworm ships NetworkManager, which gives us Wi-Fi
scanning, a WPA2 hotspot with built-in DHCP/DNS (dnsmasq in "shared"
mode), and persistent connection profiles - all from one CLI. Nothing
here is BlinkPi-specific except the profile names.

All functions are best-effort: on a machine without nmcli (dev laptop,
tests) they return empty/False rather than raising.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

HOTSPOT_CON = "blinkpi-setup"   # NM profile name for the fallback AP
WIFI_CON = "blinkpi-wifi"       # NM profile name for the user's home Wi-Fi
HOTSPOT_IP = "10.42.0.1"        # NM's default shared-mode gateway; pinned below


def available() -> bool:
    return shutil.which("nmcli") is not None


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    if not available():
        return subprocess.CompletedProcess(args, 127, "", "nmcli not installed")
    try:
        return subprocess.run(
            ["nmcli", *args], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "nmcli timed out")


def _split_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped colons."""
    out, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


# ──────────────────────────── status ────────────────────────────

def devices() -> list[dict]:
    r = _run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        dev, typ, state, con = parts[:4]
        if typ in ("loopback", "bridge", "tun", "dummy") or dev.startswith("p2p-"):
            continue
        out.append({"device": dev, "type": typ, "state": state, "connection": con})
    return out


def wifi_device() -> Optional[str]:
    for d in devices():
        if d["type"] == "wifi":
            return d["device"]
    return None


def connectivity() -> str:
    """none | portal | limited | full | unknown."""
    r = _run(["-t", "networking", "connectivity"], timeout=10)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def ip_addresses() -> list[dict]:
    """IPv4 addresses per interface via `ip -j`, excluding loopback."""
    if not shutil.which("ip"):
        return []
    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    out = []
    for iface in data:
        name = iface.get("ifname", "")
        if name == "lo":
            continue
        for a in iface.get("addr_info", []):
            if a.get("family") == "inet":
                out.append({"device": name, "address": a.get("local")})
    return out


def hotspot_active() -> bool:
    r = _run(["-t", "-f", "NAME", "connection", "show", "--active"], timeout=10)
    return r.returncode == 0 and HOTSPOT_CON in [
        _split_terse(l)[0] for l in r.stdout.splitlines() if l
    ]


def has_uplink() -> bool:
    """True if some device other than the hotspot has an IPv4 address."""
    hotspot_dev = None
    if hotspot_active():
        for d in devices():
            if d["connection"] == HOTSPOT_CON:
                hotspot_dev = d["device"]
    for a in ip_addresses():
        if a["device"] != hotspot_dev and not a["address"].startswith("169.254."):
            return True
    return False


def current_ssid() -> Optional[str]:
    r = _run(["-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"], timeout=15)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[0] == "yes":
            return parts[1]
    return None


def saved_wifi() -> Optional[dict]:
    r = _run(["-t", "-f", "802-11-wireless.ssid", "connection", "show", WIFI_CON], timeout=10)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[0] == "802-11-wireless.ssid":
            return {"ssid": parts[1]}
    return None


# ──────────────────────────── scanning ────────────────────────────

_last_scan: list[dict] = []


def scan(rescan: bool = True) -> list[dict]:
    """List visible networks, strongest first, one entry per SSID.

    While the hotspot is up the radio usually can't scan; in that case we
    return the last successful scan (taken just before the AP came up).
    """
    global _last_scan
    r = _run(
        ["-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list",
         "--rescan", "yes" if rescan else "no"],
        timeout=40,
    )
    if r.returncode != 0:
        return _last_scan
    best: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        ssid, signal, security, in_use = parts[:4]
        if not ssid or ssid == HOTSPOT_CON:
            continue
        try:
            sig = int(signal)
        except ValueError:
            sig = 0
        entry = {
            "ssid": ssid, "signal": sig,
            "secure": bool(security.strip()) and security.strip() != "--",
            "in_use": in_use.strip() == "*",
        }
        if ssid not in best or sig > best[ssid]["signal"]:
            best[ssid] = entry
    result = sorted(best.values(), key=lambda e: -e["signal"])
    if result:
        _last_scan = result
    return result


# ──────────────────────────── hotspot ────────────────────────────

def hotspot_up(ssid: str, password: str, ifname: Optional[str] = None) -> tuple[bool, str]:
    ifname = ifname or wifi_device()
    if not ifname:
        return False, "no Wi-Fi device"
    if hotspot_active():
        return True, "already active"
    # Refresh the scan cache before the radio goes into AP mode.
    scan(rescan=True)
    # (Re)create the profile each time so SSID/password changes apply.
    _run(["connection", "delete", HOTSPOT_CON], timeout=15)
    args = [
        "connection", "add", "type", "wifi", "ifname", ifname,
        "con-name", HOTSPOT_CON, "autoconnect", "no", "ssid", ssid,
        "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
        "ipv4.method", "shared", "ipv4.addresses", f"{HOTSPOT_IP}/24",
        "ipv6.method", "disabled",
    ]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    r = _run(args, timeout=30)
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip()
    r = _run(["connection", "up", HOTSPOT_CON], timeout=45)
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip()
    log.info("hotspot %s up on %s (%s)", ssid, ifname, HOTSPOT_IP)
    return True, "ok"


def hotspot_down() -> None:
    if hotspot_active():
        _run(["connection", "down", HOTSPOT_CON], timeout=30)
        log.info("hotspot down")


# ──────────────────────────── client Wi-Fi ────────────────────────────

def set_country(code: str) -> tuple[bool, str]:
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return False, "country must be a 2-letter code"
    if shutil.which("raspi-config"):
        r = subprocess.run(["raspi-config", "nonint", "do_wifi_country", code],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, "ok"
    if shutil.which("iw"):
        r = subprocess.run(["iw", "reg", "set", code], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True)
            return True, "ok"
        return False, r.stderr.strip()
    return False, "no tool available to set the Wi-Fi country"


def save_wifi(ssid: str, password: str, hidden: bool = False) -> tuple[bool, str]:
    ifname = wifi_device()
    if not ifname:
        return False, "no Wi-Fi device"
    _run(["connection", "delete", WIFI_CON], timeout=15)
    args = [
        "connection", "add", "type", "wifi", "ifname", ifname,
        "con-name", WIFI_CON, "autoconnect", "yes",
        "connection.autoconnect-priority", "10",
        "ssid", ssid,
    ]
    if hidden:
        args += ["802-11-wireless.hidden", "yes"]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    r = _run(args, timeout=30)
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip()
    return True, "ok"


def connect_wifi(timeout: int = 45) -> tuple[bool, str]:
    """Bring the saved profile up. Caller must have taken the hotspot down."""
    r = _run(["--wait", str(timeout), "connection", "up", WIFI_CON], timeout=timeout + 15)
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip() or "connection failed"
    return True, "ok"


def forget_wifi() -> None:
    _run(["connection", "delete", WIFI_CON], timeout=15)
