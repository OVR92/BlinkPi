"""Outbound notifications: email (SMTP) and a generic JSON webhook.

Used by the sync loop (new-clip alerts, AI summaries), the watchdog
(silence / failure / recovery alerts) and the setup page (test buttons).

Everything here is best-effort: a failed notification is logged and
reported back as False, never raised into the caller. Secrets (SMTP
password) come from `secrets.yaml` next to config.yaml, not from
config.yaml itself.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import smtplib
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Iterable, Optional

from . import config as cfg

log = logging.getLogger(__name__)


# ──────────────────────────── email ────────────────────────────

def send_email(
    *, host: str, port: int, security: str, username: str, password: str,
    from_addr: str, to: Iterable[str], subject: str, body: str,
    attachments: Iterable[Path] = (), timeout: int = 30,
) -> tuple[bool, str]:
    """Send one email. Returns (ok, message). Never raises."""
    to = [t.strip() for t in to if t and t.strip()]
    if not host or not to:
        return False, "SMTP host and at least one recipient are required"
    from_addr = from_addr.strip() or username.strip()
    if not from_addr:
        return False, "a From address (or username) is required"

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=socket.gethostname() or "blinkpi")
    msg.set_content(body)
    for att in attachments:
        # Each attachment is a Path, or a (Path, display_name) tuple.
        display = None
        if isinstance(att, tuple):
            att, display = att
        try:
            att = Path(att)
            data = att.read_bytes()
        except OSError as e:
            log.warning("attachment %s skipped: %s", att, e)
            continue
        name = display or att.name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)

    security = (security or "starttls").lower()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port or 465, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port or 587, timeout=timeout)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(msg)
        return True, f"sent to {', '.join(to)}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"authentication failed: {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e.smtp_error}"
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        return False, f"{type(e).__name__}: {e}"


# ──────────────────────────── webhook ────────────────────────────

def post_webhook(url: str, payload: dict, timeout: int = 15) -> tuple[bool, str]:
    """POST a JSON document. Returns (ok, message). Never raises."""
    if not url:
        return False, "webhook URL is empty"
    if not url.lower().startswith(("http://", "https://")):
        return False, "webhook URL must start with http:// or https://"
    data = json.dumps(payload, default=str).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "BlinkPi"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


# ──────────────────────────── notifier ────────────────────────────

class Notifier:
    """Fan a message out to every enabled channel."""

    def __init__(self, c: cfg.Config):
        self.c = c

    @property
    def any_enabled(self) -> bool:
        return self.c.notify.email.enabled or self.c.notify.webhook.enabled

    def send(
        self, subject: str, body: str, *, event: str, data: Optional[dict] = None,
        attachments: Iterable[Path] = (), email: bool = True, webhook: bool = True,
    ) -> dict[str, bool]:
        """Returns {channel: ok} for each channel that was attempted."""
        results: dict[str, bool] = {}
        em = self.c.notify.email
        if email and em.enabled:
            ok, msg = send_email(
                host=em.host, port=em.port, security=em.security, username=em.username,
                password=self.c.secrets.get("smtp_password", ""), from_addr=em.from_addr,
                to=em.to, subject=subject, body=body, attachments=attachments,
            )
            results["email"] = ok
            (log.info if ok else log.error)("email [%s]: %s", subject, msg)
        wh = self.c.notify.webhook
        if webhook and wh.enabled:
            payload = {
                "event": event, "subject": subject, "body": body,
                "host": socket.gethostname(), "time": datetime.now().isoformat(timespec="seconds"),
                **(data or {}),
            }
            ok, msg = post_webhook(wh.url, payload)
            results["webhook"] = ok
            (log.info if ok else log.error)("webhook [%s]: %s", event, msg)
        return results
