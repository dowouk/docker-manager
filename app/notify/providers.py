"""Notification providers: how a message leaves the app (ACS, SMTP2GO, SMTP).

Providers are small and stateless. Errors are always a NotifyError carrying one of a few fixed
reasons – provider text can contain credentials, so it is never passed on.
"""
import asyncio
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal, Protocol

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 20

LOGIN_REFUSED = "The email service refused the login"
UNREACHABLE = "The email service couldn't be reached"
REJECTED = "The email service rejected the message"
NO_ANSWER = "The email service didn't answer in time"
NOT_PRIVATE = "Unencrypted SMTP is only allowed to private addresses"

ACS_CONNECT_TIMEOUT = 10
ACS_READ_TIMEOUT = 20
ACS_RETRIES = 1
ACS_RESULT_TIMEOUT = 25

# Provider work that blocks (SMTP, the Azure SDK) runs in its own small pool, not the loop's
# default executor: a send that hangs can then only delay notifications, never password
# hashing or anything else that uses asyncio.to_thread.
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def notify_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
        return _executor


def shutdown_notify_executor() -> None:
    """Called on app shutdown; doesn't wait for hung sends (their sockets time out on their own)."""
    global _executor
    with _executor_lock:
        pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def run_blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(notify_executor(), fn, *args)


@dataclass(frozen=True)
class Message:
    subject: str
    plain: str
    html: str
    recipients: tuple[str, ...]


class NotifyError(Exception):
    """A send failed; str(err) is one of the fixed reasons above."""


@dataclass(frozen=True)
class ProviderField:
    name: str
    label: str
    kind: Literal["text", "int", "email", "secret", "choice"]
    help: str = ""
    required: bool = True
    choices: tuple[tuple[str, str], ...] = ()
    default: str = ""


class Provider(Protocol):
    key: str
    label: str
    fields: tuple[ProviderField, ...]

    def validate(self, config: dict) -> dict[str, str]: ...

    async def send(self, config: dict, message: Message, http: httpx.AsyncClient) -> None: ...


EMAIL_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")


def _validate(fields: tuple[ProviderField, ...], config: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    for f in fields:
        value = str(config.get(f.name) or "").strip()
        if not value:
            if f.required:
                errors[f.name] = "Required"
            continue
        if f.kind == "email" and not EMAIL_ADDRESS.match(value):
            errors[f.name] = "Enter an email address"
        elif f.kind == "int":
            if not (value.isascii() and value.isdecimal()) or not 1 <= int(value) <= 65535:
                errors[f.name] = "Enter a number from 1 to 65535"
        elif f.kind == "choice" and value not in {c[0] for c in f.choices}:
            errors[f.name] = "Choose one of the options"
    return errors


def _one_line(text: str) -> str:
    return " ".join(text.split())


class ACSProvider:
    key = "acs"
    label = "Azure Communication Services"
    fields = (
        ProviderField("connection_string", "Connection string", "secret"),
        ProviderField("sender", "Sender address", "email"),
    )

    def __init__(self, factory=None):
        self._factory = factory

    def validate(self, config: dict) -> dict[str, str]:
        return _validate(self.fields, config)

    def _send(self, config: dict, message: Message) -> None:
        if self._factory:
            client = self._factory(config["connection_string"])
        else:
            from azure.communication.email import EmailClient

            # The SDK polls every 30 s by default, longer than our result timeout.
            client = EmailClient.from_connection_string(
                config["connection_string"], connection_timeout=ACS_CONNECT_TIMEOUT,
                read_timeout=ACS_READ_TIMEOUT, retry_total=ACS_RETRIES,
                polling_interval=2)
        poller = client.begin_send({
            "senderAddress": config["sender"],
            "recipients": {"to": [{"address": a} for a in message.recipients]},
            "content": {"subject": message.subject, "html": message.html,
                        "plainText": message.plain},
        })
        poller.result(timeout=ACS_RESULT_TIMEOUT)
        if not poller.done():  # result() returns quietly when the wait runs out
            raise NotifyError(NO_ANSWER)

    async def send(self, config: dict, message: Message, http: httpx.AsyncClient) -> None:
        try:
            await run_blocking(self._send, config, message)
        except NotifyError:
            raise
        except Exception:  # SDK errors can echo the connection string; keep only a fixed reason
            raise NotifyError(REJECTED) from None


class SMTP2GOProvider:
    key = "smtp2go"
    label = "SMTP2GO"
    fields = (
        ProviderField("api_key", "API key", "secret",
                      help="An API key with permission to send email."),
        ProviderField("sender", "Sender address", "email",
                      help="Must be on a verified sender domain."),
    )
    url = "https://api.smtp2go.com/v3/email/send"

    def validate(self, config: dict) -> dict[str, str]:
        return _validate(self.fields, config)

    async def send(self, config: dict, message: Message, http: httpx.AsyncClient) -> None:
        body = {"sender": config["sender"], "to": list(message.recipients),
                "subject": message.subject, "text_body": message.plain,
                "html_body": message.html}
        try:
            resp = await http.post(self.url, json=body, timeout=TIMEOUT,
                                   headers={"X-Smtp2go-Api-Key": config["api_key"]})
        except httpx.TimeoutException:
            raise NotifyError(NO_ANSWER) from None
        except httpx.HTTPError:
            raise NotifyError(UNREACHABLE) from None
        if resp.status_code in (401, 403):
            raise NotifyError(LOGIN_REFUSED)
        if resp.status_code >= 300:
            raise NotifyError(REJECTED)
        try:
            succeeded = int(resp.json()["data"]["succeeded"])
        except (ValueError, KeyError, TypeError):
            raise NotifyError(REJECTED) from None
        if succeeded < 1:
            raise NotifyError(REJECTED)


SMTP_PRESETS = {
    "smtp2go": {"host": "mail.smtp2go.com", "port": "587", "security": "starttls"},
    "m365": {"host": "smtp.office365.com", "port": "587", "security": "starttls"},
}


def all_private(host: str) -> bool:
    """True when every address the host resolves to is private, loopback or link-local."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


class SMTPProvider:
    key = "smtp"
    label = "SMTP"
    fields = (
        ProviderField("host", "Host", "text"),
        ProviderField("port", "Port", "int", default="587",
                      help="Usually 587 (STARTTLS) or 465 (TLS). SMTP2GO also accepts 2525."),
        ProviderField("security", "Security", "choice", default="starttls", choices=(
            ("starttls", "STARTTLS"), ("tls", "TLS"),
            ("none", "None (private addresses only)"))),
        ProviderField("username", "Username", "text", required=False),
        ProviderField("password", "Password", "secret", required=False),
        ProviderField("sender", "Sender address", "email"),
    )

    def validate(self, config: dict) -> dict[str, str]:
        return _validate(self.fields, config)

    def _deliver(self, config: dict, message: Message, sent: list) -> None:
        host = str(config["host"]).strip()
        port = int(str(config["port"]).strip())
        security = str(config.get("security") or "").strip()
        sender = str(config["sender"]).strip()
        username = str(config.get("username") or "").strip()
        if security not in ("starttls", "tls", "none"):
            raise ValueError("security")
        if security == "none" and not all_private(host):
            raise NotifyError(NOT_PRIVATE)
        mail = EmailMessage()
        mail["Subject"] = _one_line(message.subject)
        mail["From"] = sender
        mail["To"] = ", ".join(message.recipients)
        mail.set_content(message.plain)
        mail.add_alternative(message.html, subtype="html")
        ctx = ssl.create_default_context()
        if security == "tls":
            conn = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
        else:
            conn = smtplib.SMTP(host, port, timeout=TIMEOUT)
        with conn:
            if security == "starttls":
                conn.starttls(context=ctx)
            if username:
                conn.login(username, config.get("password") or "")
            conn.send_message(mail, sender, list(message.recipients))
            sent.append(True)

    def _send(self, config: dict, message: Message) -> None:
        sent: list = []
        try:
            self._deliver(config, message, sent)
        except NotifyError:
            raise
        except Exception as e:
            if sent:  # delivered; only closing the connection failed
                log.debug("SMTP close failed after send: %s", type(e).__name__)
                return
            reason = self._reason(e)
        else:
            return
        # Raised outside the except block so the original error (which may quote the
        # password) isn't kept as __context__ for tracebacks and logs.
        raise NotifyError(reason)

    @staticmethod
    def _reason(e: Exception) -> str:
        if isinstance(e, socket.timeout):
            return NO_ANSWER
        if isinstance(e, smtplib.SMTPAuthenticationError):
            return LOGIN_REFUSED
        if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError,
                          smtplib.SMTPSenderRefused)):
            return REJECTED
        if isinstance(e, OSError):  # includes SMTPConnectError and other SMTPException subclasses
            return UNREACHABLE
        return REJECTED  # ValueError/UnicodeError from bad config or characters; never echo them

    async def send(self, config: dict, message: Message, http: httpx.AsyncClient) -> None:
        await run_blocking(self._send, config, message)


PROVIDERS: dict[str, Provider] = {p.key: p for p in (ACSProvider(), SMTP2GOProvider(), SMTPProvider())}
