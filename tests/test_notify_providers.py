import asyncio
import smtplib
import socket
import ssl
import threading

import httpx
import pytest
import respx

from app.notify import providers
from app.notify.providers import (
    PROVIDERS, ACSProvider, Message, NotifyError, SMTP2GOProvider, SMTPProvider, all_private,
)

MSG = Message(subject="Hi", plain="plain body", html="<p>html body</p>",
              recipients=("a@example.com", "b@example.com"))
KEY = "api-key-SECRET-123"


def run(coro):
    return asyncio.run(coro)


def send_smtp2go(config=None):
    async def go():
        async with httpx.AsyncClient() as http:
            await SMTP2GOProvider().send(
                config or {"api_key": KEY, "sender": "me@example.com"}, MSG, http)
    return run(go())


def err_of(fn):
    with pytest.raises(NotifyError) as e:
        fn()
    return str(e.value)


# --- SMTP2GO ---

@respx.mock
def test_smtp2go_request_shape():
    route = respx.post("https://api.smtp2go.com/v3/email/send").respond(
        200, json={"data": {"succeeded": 1, "failed": 0}})
    send_smtp2go()
    req = route.calls.last.request
    assert req.headers["X-Smtp2go-Api-Key"] == KEY
    import json
    body = json.loads(req.content)
    assert body == {"sender": "me@example.com", "to": ["a@example.com", "b@example.com"],
                    "subject": "Hi", "text_body": "plain body", "html_body": "<p>html body</p>"}


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_smtp2go_login_refused(status):
    respx.post("https://api.smtp2go.com/v3/email/send").respond(status, json={"error": KEY})
    msg = err_of(send_smtp2go)
    assert msg == "The email service refused the login"
    assert KEY not in msg


@respx.mock
def test_smtp2go_other_4xx_rejected():
    respx.post("https://api.smtp2go.com/v3/email/send").respond(400, text="bad")
    assert err_of(send_smtp2go) == "The email service rejected the message"


@respx.mock
def test_smtp2go_succeeded_zero_rejected():
    respx.post("https://api.smtp2go.com/v3/email/send").respond(
        200, json={"data": {"succeeded": 0, "failed": 2}})
    assert err_of(send_smtp2go) == "The email service rejected the message"


@respx.mock
def test_smtp2go_non_json_rejected():
    respx.post("https://api.smtp2go.com/v3/email/send").respond(200, text="oops")
    assert err_of(send_smtp2go) == "The email service rejected the message"


@respx.mock
def test_smtp2go_connect_error():
    respx.post("https://api.smtp2go.com/v3/email/send").mock(side_effect=httpx.ConnectError(KEY))
    msg = err_of(send_smtp2go)
    assert msg == "The email service couldn't be reached"
    assert KEY not in msg


@respx.mock
def test_smtp2go_timeout():
    respx.post("https://api.smtp2go.com/v3/email/send").mock(side_effect=httpx.ReadTimeout("t"))
    assert err_of(send_smtp2go) == "The email service didn't answer in time"


# --- SMTP ---

class FakeSMTP:
    instances = []
    fail = {}

    def __init__(self, host, port, timeout=None, context=None, **kw):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        self.sent = None
        self.closed = False
        type(self).instances.append(self)
        if "connect" in self.fail:
            raise self.fail["connect"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        if "exit" in self.fail:
            raise self.fail["exit"]
        return False

    def starttls(self, context=None):
        self.context = context
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if "login" in self.fail:
            raise self.fail["login"]

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append("send")
        if "send" in self.fail:
            raise self.fail["send"]
        self.sent = (msg, from_addr, to_addrs)


class FakeSMTPSSL(FakeSMTP):
    kind = "ssl"


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail = {}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setattr(providers.socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    return FakeSMTP


def smtp_cfg(**over):
    cfg = {"host": "smtp.example.com", "port": "587", "security": "starttls",
           "username": "", "password": "", "sender": "me@example.com"}
    cfg.update(over)
    return cfg


def send_smtp(cfg):
    async def go():
        async with httpx.AsyncClient() as http:
            await SMTPProvider().send(cfg, MSG, http)
    return run(go())


def test_smtp_starttls_with_login(fake_smtp):
    send_smtp(smtp_cfg(username="u", password="p"))
    s = fake_smtp.instances[0]
    assert type(s) is FakeSMTP
    assert (s.host, s.port, s.timeout) == ("smtp.example.com", 587, 20)
    assert s.calls == ["starttls", ("login", "u", "p"), "send"]
    msg, from_addr, to_addrs = s.sent
    assert from_addr == "me@example.com"
    assert list(to_addrs) == ["a@example.com", "b@example.com"]
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert "plain body" in parts[0].get_content()
    assert "html body" in parts[1].get_content()
    assert msg["Subject"] == "Hi"


def test_smtp_no_login_without_username(fake_smtp):
    send_smtp(smtp_cfg())
    assert fake_smtp.instances[0].calls == ["starttls", "send"]


def test_smtp_tls_uses_ssl_class(fake_smtp):
    send_smtp(smtp_cfg(security="tls", port="465"))
    s = fake_smtp.instances[0]
    assert type(s) is FakeSMTPSSL
    assert "starttls" not in s.calls
    assert s.context is not None


def test_smtp_none_public_refused_before_connecting(fake_smtp):
    msg = err_of(lambda: send_smtp(smtp_cfg(security="none")))
    assert msg == "Unencrypted SMTP is only allowed to private addresses"
    assert fake_smtp.instances == []


def test_smtp_none_private_allowed(fake_smtp, monkeypatch):
    monkeypatch.setattr(providers.socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))])
    send_smtp(smtp_cfg(security="none", host="10.0.0.5", port="25"))
    assert fake_smtp.instances[0].calls == ["send"]


@pytest.mark.parametrize("exc,expected", [
    (smtplib.SMTPAuthenticationError(535, b"bad pass hunter2"), "The email service refused the login"),
    (smtplib.SMTPRecipientsRefused({}), "The email service rejected the message"),
    (smtplib.SMTPDataError(554, b"no"), "The email service rejected the message"),
    (smtplib.SMTPSenderRefused(550, b"no", "me@example.com"), "The email service rejected the message"),
    (ConnectionRefusedError("hunter2"), "The email service couldn't be reached"),
    (smtplib.SMTPConnectError(421, b"busy"), "The email service couldn't be reached"),
    (socket.timeout("slow"), "The email service didn't answer in time"),
])
def test_smtp_error_mapping(fake_smtp, exc, expected):
    fake_smtp.fail = {"send": exc}
    msg = err_of(lambda: send_smtp(smtp_cfg()))
    assert msg == expected
    assert "hunter2" not in msg


def test_smtp_login_error_mapping(fake_smtp):
    fake_smtp.fail = {"login": smtplib.SMTPAuthenticationError(535, b"x")}
    assert err_of(lambda: send_smtp(smtp_cfg(username="u", password="p"))) == \
        "The email service refused the login"


def test_smtp_connect_timeout(fake_smtp):
    fake_smtp.fail = {"connect": socket.timeout()}
    assert err_of(lambda: send_smtp(smtp_cfg())) == "The email service didn't answer in time"


def test_all_private(monkeypatch):
    def fake(addrs):
        return lambda host, *a, **k: [(socket.AF_INET, 1, 6, "", (ip, 0)) for ip in addrs]
    monkeypatch.setattr(providers.socket, "getaddrinfo", fake(["10.0.0.5", "127.0.0.1", "169.254.1.1"]))
    assert all_private("x")
    monkeypatch.setattr(providers.socket, "getaddrinfo", fake(["10.0.0.5", "8.8.8.8"]))
    assert not all_private("x")

    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(providers.socket, "getaddrinfo", boom)
    assert not all_private("x")


# --- ACS ---

class FakeACSClient:
    def __init__(self, fail=None, finishes=True):
        self.messages = []
        self.fail = fail
        self.finishes = finishes
        self.waited = []

    def begin_send(self, message):
        self.messages.append(message)
        fail, finishes = self.fail, self.finishes
        waited = self.waited

        class Poller:
            def result(self_, timeout=None):
                waited.append(timeout)
                if fail:
                    raise fail

            def done(self_):
                return finishes
        return Poller()


def send_acs(factory):
    async def go():
        async with httpx.AsyncClient() as http:
            await ACSProvider(factory=factory).send(
                {"connection_string": "endpoint=https://x;accesskey=SECRETKEY", "sender": "me@example.com"},
                MSG, http)
    return run(go())


def test_acs_sends_message():
    client = FakeACSClient()
    seen = []
    send_acs(lambda cs: seen.append(cs) or client)
    assert seen == ["endpoint=https://x;accesskey=SECRETKEY"]
    assert client.messages == [{
        "senderAddress": "me@example.com",
        "recipients": {"to": [{"address": "a@example.com"}, {"address": "b@example.com"}]},
        "content": {"subject": "Hi", "html": "<p>html body</p>", "plainText": "plain body"},
    }]


def test_acs_exception_rejected():
    msg = err_of(lambda: send_acs(lambda cs: FakeACSClient(fail=RuntimeError("accesskey=SECRETKEY"))))
    assert msg == "The email service rejected the message"


def test_acs_client_gets_timeouts_and_a_low_retry_count(monkeypatch):
    seen = {}

    class Poller:
        def result(self, timeout=None):
            seen["timeout"] = timeout

        def done(self):
            return True

    class Client:
        def begin_send(self, message):
            return Poller()

    def from_connection_string(cs, **kw):
        seen["kw"] = kw
        return Client()

    from azure.communication.email import EmailClient
    monkeypatch.setattr(EmailClient, "from_connection_string", from_connection_string)
    send_acs(None)
    assert seen["kw"] == {"connection_timeout": 10, "read_timeout": 20, "retry_total": 1,
                          "polling_interval": 2}
    assert seen["timeout"] == 25


def test_acs_unfinished_send_is_no_answer():
    client = FakeACSClient(finishes=False)
    msg = err_of(lambda: send_acs(lambda cs: client))
    assert msg == "The email service didn't answer in time" and client.waited == [25]


def test_acs_keeps_the_fixed_reason_of_a_not_private_style_error():
    # NotifyError raised inside the worker is passed on unchanged, not flattened.
    class Odd(ACSProvider):
        def _send(self, config, message):
            raise NotifyError(providers.NO_ANSWER)

    async def go():
        async with httpx.AsyncClient() as http:
            await Odd().send({}, MSG, http)
    assert err_of(lambda: run(go())) == providers.NO_ANSWER


async def test_hung_sends_cannot_starve_other_thread_work():
    release = threading.Event()
    try:
        hung = [asyncio.ensure_future(providers.run_blocking(release.wait, 30))
                for _ in range(6)]  # more than the pool's 4 workers
        await asyncio.sleep(0.1)
        # An unrelated to_thread call (stand-in for password hashing) is not held up.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "hashed"), 2) == "hashed"
    finally:
        release.set()
        await asyncio.gather(*hung, return_exceptions=True)
        providers.shutdown_notify_executor()


def test_notify_pool_is_bounded_named_and_restartable():
    pool = providers.notify_executor()
    assert pool._max_workers == 4 and pool._thread_name_prefix == "notify"
    providers.shutdown_notify_executor()
    assert providers.notify_executor() is not pool
    providers.shutdown_notify_executor()


# --- validate / registry ---

def test_registry():
    assert set(PROVIDERS) == {"acs", "smtp2go", "smtp"}
    assert all(p.key == k for k, p in PROVIDERS.items())


def test_validate_required_and_email():
    p = SMTP2GOProvider()
    assert p.validate({"api_key": "k", "sender": "me@example.com"}) == {}
    errs = p.validate({"api_key": "", "sender": "nope"})
    assert set(errs) == {"api_key", "sender"}
    assert set(ACSProvider().validate({})) == {"connection_string", "sender"}


def test_validate_smtp():
    p = SMTPProvider()
    assert p.validate(smtp_cfg()) == {}
    assert set(p.validate(smtp_cfg(host=""))) == {"host"}
    for bad in ("0", "65536", "abc", ""):
        assert set(p.validate(smtp_cfg(port=bad))) == {"port"}
    assert set(p.validate(smtp_cfg(security="ssl"))) == {"security"}
    assert p.validate(smtp_cfg(port="65535")) == {}


def test_presets():
    assert providers.SMTP_PRESETS["m365"] == {
        "host": "smtp.office365.com", "port": "587", "security": "starttls"}
    assert providers.SMTP_PRESETS["smtp2go"]["host"] == "mail.smtp2go.com"


def _strict(ctx):
    return ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED


def test_smtp_verifies_certificates(fake_smtp):
    send_smtp(smtp_cfg())
    assert _strict(fake_smtp.instances[0].context)
    send_smtp(smtp_cfg(security="tls"))
    assert _strict(fake_smtp.instances[1].context)


def test_smtp_connection_closed(fake_smtp):
    send_smtp(smtp_cfg())
    assert fake_smtp.instances[0].closed


def test_smtp_close_error_after_send_is_success(fake_smtp):
    fake_smtp.fail = {"exit": OSError("boom")}
    send_smtp(smtp_cfg())  # no raise
    assert fake_smtp.instances[0].sent is not None


def test_smtp_subject_newlines_collapsed(fake_smtp):
    async def go():
        async with httpx.AsyncClient() as http:
            await SMTPProvider().send(smtp_cfg(), Message(
                subject="a\r\nBcc: x@evil.test", plain="p", html="h",
                recipients=("a@example.com",)), http)
    run(go())
    msg = fake_smtp.instances[0].sent[0]
    assert msg["Subject"] == "a Bcc: x@evil.test"
    assert "Bcc" not in msg


def test_smtp_only_raises_notify_error(fake_smtp):
    bad_recipient = Message(subject="s", plain="p", html="h",
                            recipients=("a@example.com\r\nBcc: x@evil.test",))

    async def go(cfg, msg=MSG):
        async with httpx.AsyncClient() as http:
            await SMTPProvider().send(cfg, msg, http)
    for cfg, msg in [(smtp_cfg(port="x"), MSG), (smtp_cfg(port="\u00b2"), MSG),
                     (smtp_cfg(security="bogus"), MSG), (smtp_cfg(), bad_recipient),
                     (smtp_cfg(sender="me@example.com\r\nBcc: x"), MSG)]:
        with pytest.raises(NotifyError) as e:
            run(go(cfg, msg))
        assert str(e.value) == "The email service rejected the message"


def test_smtp_non_ascii_password_does_not_leak(fake_smtp, monkeypatch):
    def login(self, user, password):
        password.encode("ascii")  # what smtplib does; raises UnicodeEncodeError
    monkeypatch.setattr(fake_smtp, "login", login)
    with pytest.raises(NotifyError) as e:
        send_smtp(smtp_cfg(username="u", password="pässwörd"))
    for text in (str(e.value), repr(e.value), repr(e.value.__cause__), repr(e.value.__context__)):
        assert "pässwörd" not in text and "ä" not in text and "ö" not in text


def test_smtp_inputs_stripped(fake_smtp):
    send_smtp(smtp_cfg(sender=" me@example.com ", username=" u ", password="p", host=" smtp.example.com "))
    s = fake_smtp.instances[0]
    assert s.host == "smtp.example.com"
    assert s.calls[1] == ("login", "u", "p")
    assert s.sent[1] == "me@example.com"


def test_validate_port_unicode_digit():
    assert set(SMTPProvider().validate(smtp_cfg(port="\u00b2"))) == {"port"}
    assert set(SMTPProvider().validate(smtp_cfg(port="\u0665"))) == {"port"}
    assert set(SMTPProvider().validate(smtp_cfg(security=""))) == {"security"}
