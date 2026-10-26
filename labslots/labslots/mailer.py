"""Outgoing email for enrollment decisions. Off until SMTP settings are configured."""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger("labslots.mailer")


@dataclass
class Mailer:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.sender)

    def send(self, to: list[str], subject: str, body: str, on_result=None) -> None:
        """Send in a background thread; on_result(ok, detail) is called when done."""
        if not self.enabled or not to:
            if on_result:
                on_result(False, "email is not configured")
            return

        def _go():
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = self.sender, ", ".join(sorted(set(to))), subject
            msg.set_content(body)
            try:
                with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                    s.starttls(context=ssl.create_default_context())
                    if self.user:
                        s.login(self.user, self.password)
                    s.send_message(msg)
                ok, detail = True, f"sent to {len(set(to))} recipient(s)"
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {e}"
                log.warning("email failed: %s", detail)
            if on_result:
                on_result(ok, detail)

        threading.Thread(target=_go, daemon=True, name="mailer").start()
