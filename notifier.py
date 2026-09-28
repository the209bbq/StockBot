"""Pluggable notifier: Discord webhook and/or email. No-op if unset."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any, Callable, Protocol


class Notifier(Protocol):
    def notify(self, event: str, message: str, extra: dict[str, Any] | None = None) -> None: ...


class NullNotifier:
    def notify(self, event: str, message: str, extra: dict[str, Any] | None = None) -> None:
        return None


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, opener: Callable[[urllib.request.Request], Any] | None = None) -> None:
        self.webhook_url = webhook_url
        self._opener = opener or urllib.request.urlopen

    def notify(self, event: str, message: str, extra: dict[str, Any] | None = None) -> None:
        payload = {"content": f"**{event}**\n{message}"}
        if extra:
            payload["content"] += "\n```json\n" + json.dumps(extra, default=str)[:1500] + "\n```"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=10):
                pass
        except (urllib.error.URLError, TimeoutError, OSError):
            # Alerting must never crash the rebalance path.
            return


class EmailNotifier:
    def __init__(
        self,
        to_addr: str,
        *,
        host: str,
        port: int = 587,
        user: str = "",
        password: str = "",
        from_addr: str = "",
        use_tls: bool = True,
        smtp_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.to_addr = to_addr
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr or user or "stockbot@localhost"
        self.use_tls = use_tls
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def notify(self, event: str, message: str, extra: dict[str, Any] | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[StockBot] {event}"
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        body = message
        if extra:
            body += "\n\n" + json.dumps(extra, indent=2, default=str)
        msg.set_content(body)
        try:
            with self._smtp_factory(self.host, self.port, timeout=10) as smtp:
                if self.use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException):
            return


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def notify(self, event: str, message: str, extra: dict[str, Any] | None = None) -> None:
        for n in self.notifiers:
            n.notify(event, message, extra)


def build_notifier(env: dict[str, str] | None = None) -> Notifier:
    environ = env if env is not None else os.environ
    notifiers: list[Notifier] = []
    webhook = (environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if webhook:
        notifiers.append(DiscordNotifier(webhook))
    to_addr = (environ.get("NOTIFY_EMAIL_TO") or "").strip()
    host = (environ.get("SMTP_HOST") or "").strip()
    if to_addr and host:
        notifiers.append(
            EmailNotifier(
                to_addr,
                host=host,
                port=int(environ.get("SMTP_PORT") or 587),
                user=environ.get("SMTP_USER", ""),
                password=environ.get("SMTP_PASSWORD", ""),
                from_addr=environ.get("SMTP_FROM", ""),
                use_tls=str(environ.get("SMTP_USE_TLS", "1")).lower() not in {"0", "false", "no"},
            )
        )
    if not notifiers:
        return NullNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return MultiNotifier(notifiers)
