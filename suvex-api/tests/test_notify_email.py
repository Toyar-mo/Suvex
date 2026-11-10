"""邮件通知走 SMTP（v1.84）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.settings import dispatch


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None, context=None):  # noqa: ANN001
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        return (250, b"ok")

    def starttls(self, context=None):  # noqa: ANN001
        self.started_tls = True

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


def settings(**overrides):
    base = {
        "smtp_configured": True,
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": "bot",
        "smtp_password": "secret",
        "smtp_from": "suvex@example.test",
        "smtp_starttls": True,
        "smtp_ssl": False,
        "smtp_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def channel(target="security@example.test, soc@example.test"):
    return SimpleNamespace(
        id="ntc_1",
        label="安全组",
        kind="email",
        target=target,
    )


def payload():
    return dispatch.NoticePayload(
        scan_id="scan_1",
        target="https://target.test",
        status="completed",
        summary="发现 2 条高危",
        severity="high",
        critical_count=1,
        high_count=1,
    )


def test_smtp_not_configured_is_reported(monkeypatch):
    monkeypatch.setattr(dispatch, "get_settings", lambda: settings(smtp_configured=False))
    reason = dispatch.channel_problem("email", "a@b.test")
    assert reason is not None
    assert "SUPEX_SMTP_HOST" in reason


def test_email_is_sent_to_every_recipient(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(dispatch, "get_settings", lambda: settings())
    monkeypatch.setattr(dispatch.smtplib, "SMTP", FakeSMTP)

    result = dispatch.dispatch_one(channel(), payload())

    assert result.sent is True
    client = FakeSMTP.instances[0]
    assert client.host == "smtp.example.test"
    assert client.started_tls is True
    assert client.logged_in == ("bot", "secret")
    message = client.sent[0]
    assert message["To"] == "security@example.test, soc@example.test"
    assert "target.test" in message["Subject"]
    assert "发现 2 条高危" in message.get_content()


def test_smtp_failure_is_not_reported_as_sent(monkeypatch):
    class RefusingSMTP(FakeSMTP):
        def send_message(self, message):  # noqa: ANN001
            raise OSError("connection refused")

    monkeypatch.setattr(dispatch, "get_settings", lambda: settings())
    monkeypatch.setattr(dispatch.smtplib, "SMTP", RefusingSMTP)

    result = dispatch.dispatch_one(channel(), payload())
    assert result.sent is False
    assert "connection refused" in result.reason
