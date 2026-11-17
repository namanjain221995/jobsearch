"""E-mail delivery against a real SMTP conversation.

The other tests replace the mail sender with a stand-in. These run the real one against a small SMTP server on
127.0.0.1, so the claims the application makes about e-mail are checked end to end:

- a message the server accepts arrives with the right sender, recipient, subject and link, and only then is the
  send reported as successful;
- a refused recipient, an unreachable server, a server without STARTTLS and a server with an untrusted certificate
  are all reported as not sent, and in the last two cases neither the SMTP password nor the message leaves;
- an invitation created through the API reaches the mailbox, and the link in the mail activates the account.
"""
import base64
import email
import email.policy
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage

import pytest

from app.config import Settings
from app.portal.services.emailer import Emailer, invitation_mail, reset_mail
from .conftest import PASSWORD, token_of

SENDER = "portal@example.com"


@dataclass
class Delivered:
    sender: str
    recipients: list[str]
    mail: EmailMessage
    encrypted: bool


@dataclass
class Mailbox:
    """A small SMTP server for tests. It records what a client says and delivers."""
    refuse_recipients: bool = False
    tls: tuple[str, str] | None = None            # (certificate file, key file): STARTTLS is offered
    implicit_tls: bool = False                    # with `tls`: encrypted from the first byte, as on port 465
    auth: bool = False                            # AUTH PLAIN is offered
    messages: list[Delivered] = field(default_factory=list)
    logins: list[tuple[str, bool]] = field(default_factory=list)      # (user name, was the connection encrypted)
    commands: list[str] = field(default_factory=list)
    port: int = 0

    def __enter__(self) -> "Mailbox":
        box = self

        class Conversation(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                connection, reader, writer, encrypted = self.connection, self.rfile, self.wfile, False
                sender, recipients = "", []
                if box.implicit_tls:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(*box.tls)
                    try:
                        connection = context.wrap_socket(connection, server_side=True)
                    except (ssl.SSLError, OSError):
                        box.commands.append("(handshake refused by the client)")
                        return
                    reader, writer, encrypted = connection.makefile("rb"), connection.makefile("wb"), True

                def say(line: str) -> None:
                    writer.write(line.encode() + b"\r\n")
                    writer.flush()

                say("220 test.invalid ESMTP")
                while True:
                    raw = reader.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    verb = line.split(" ", 1)[0].upper()
                    box.commands.append(verb)
                    if verb in ("EHLO", "HELO"):
                        offers = ["test.invalid"]
                        if box.tls and not encrypted:
                            offers.append("STARTTLS")
                        if box.auth:
                            offers.append("AUTH PLAIN")
                        for offer in offers[:-1]:
                            say("250-" + offer)
                        say("250 " + offers[-1])
                    elif verb == "STARTTLS" and box.tls and not encrypted:
                        say("220 Ready to start TLS")
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        context.load_cert_chain(*box.tls)
                        try:
                            connection = context.wrap_socket(connection, server_side=True)
                        except (ssl.SSLError, OSError):
                            box.commands.append("(handshake refused by the client)")
                            return
                        reader, writer, encrypted = connection.makefile("rb"), connection.makefile("wb"), True
                    elif verb == "AUTH" and box.auth:
                        parts = line.split(" ")
                        user = base64.b64decode(parts[2]).split(b"\0")[1].decode() if len(parts) == 3 else ""
                        box.logins.append((user, encrypted))
                        say("235 Authentication successful")
                    elif verb == "MAIL":
                        sender = line.partition(":")[2].strip().split(" ")[0].strip("<>")
                        say("250 OK")
                    elif verb == "RCPT":
                        if box.refuse_recipients:
                            say("550 No such user here")
                        else:
                            recipients.append(line.partition(":")[2].strip().split(" ")[0].strip("<>"))
                            say("250 OK")
                    elif verb == "DATA":
                        say("354 End data with <CR><LF>.<CR><LF>")
                        lines = []
                        while True:
                            data = reader.readline()
                            if not data or data.rstrip(b"\r\n") == b".":
                                break
                            lines.append(data[1:] if data.startswith(b"..") else data)
                        mail = email.message_from_bytes(b"".join(lines), policy=email.policy.default)
                        box.messages.append(Delivered(sender, list(recipients), mail, encrypted))
                        say("250 OK: queued")
                    elif verb == "RSET":
                        sender, recipients = "", []
                        say("250 OK")
                    elif verb == "QUIT":
                        say("221 Bye")
                        return
                    else:
                        say("502 Command not implemented")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Conversation)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def mailer_for(port: int, **overrides) -> Emailer:
    values = {"smtp_host": "127.0.0.1", "smtp_port": port, "smtp_from": SENDER, "smtp_starttls": False, **overrides}
    return Emailer(Settings(_env_file=None, **values))


def make_certificate(folder, name: str, alternative_names: str) -> tuple[str, str]:
    """A self-signed certificate. No certificate authority on this machine vouches for it."""
    if not shutil.which("openssl"):
        pytest.skip("the openssl command is needed to make a test certificate")
    cert, key = str(folder / f"{name}-cert.pem"), str(folder / f"{name}-key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={name}",
                    "-addext", f"subjectAltName={alternative_names}", "-keyout", key, "-out", cert],
                   check=True, capture_output=True, timeout=60)
    return cert, key


@pytest.fixture(scope="module")
def certificate(tmp_path_factory) -> tuple[str, str]:
    """For a mail server reached as localhost."""
    return make_certificate(tmp_path_factory.mktemp("smtp-tls"), "localhost", "DNS:localhost,IP:127.0.0.1")


async def test_an_accepted_message_arrives_and_only_then_counts_as_sent():
    link = "http://portal.example/invite/accept?token=" + "a" * 43
    subject, body = invitation_mail("Rita Example", "Team Alpha", "MEMBER", link, 72)
    with Mailbox() as box:
        mailer = mailer_for(box.port)
        assert mailer.backend == "smtp"
        assert await mailer.send("rita@example.com", subject, body) is True
    [message] = box.messages
    assert message.sender == SENDER and message.recipients == ["rita@example.com"]
    assert message.mail["From"] == SENDER and message.mail["To"] == "rita@example.com" and message.mail["Subject"] == subject
    text = message.mail.get_content()
    assert link in text and "Hello Rita Example" in text and "expires in 72 hours" in text

    subject, body = reset_mail("Rita Example", "http://portal.example/reset-password?token=" + "b" * 43, 30)
    with Mailbox() as box:
        assert await mailer_for(box.port).send("rita@example.com", subject, body) is True
    assert "expires in 30 minutes" in box.messages[0].mail.get_content()


async def test_a_message_that_was_not_accepted_is_never_reported_as_sent(caplog):
    with Mailbox(refuse_recipients=True) as box:                    # the server refuses the address
        assert await mailer_for(box.port).send("nobody@example.com", "Subject", "Body") is False
    assert box.messages == []

    closed = socket.socket()                                         # nothing listens here
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    assert await mailer_for(port).send("rita@example.com", "Subject", "Body with token=secret-link") is False

    none = Emailer(Settings(_env_file=None, smtp_host="", email_backend=""))
    assert none.backend == "none" and await none.send("rita@example.com", "Subject", "Body") is False
    console = Emailer(Settings(_env_file=None, smtp_host="", email_backend="console"))
    assert console.backend == "console" and await console.send("rita@example.com", "Subject", "Body") is False

    failures = [record.getMessage() for record in caplog.records if "SMTP delivery failed" in record.getMessage()]
    assert len(failures) == 2
    assert not any("secret-link" in line or "example.com" in line for line in failures)      # the error log names no one


async def test_encryption_that_is_asked_for_is_never_skipped():
    """SMTP_STARTTLS is on (the default) and the server does not offer it: no password, no message, not sent."""
    with Mailbox(auth=True) as box:
        mailer = mailer_for(box.port, smtp_starttls=True, smtp_username="portal", smtp_password="Mail-Password-1")
        assert await mailer.send("rita@example.com", "Subject", "Body") is False
    assert box.messages == [] and box.logins == []
    assert "AUTH" not in box.commands and "MAIL" not in box.commands


async def test_a_mail_server_with_an_untrusted_certificate_gets_nothing(certificate):
    """Without certificate checking, anyone between this server and the mail server could read the SMTP password and
    every invitation and reset link. smtplib alone does not check; the sender gives it a verifying context."""
    with Mailbox(tls=certificate, auth=True) as box:
        mailer = mailer_for(box.port, smtp_host="localhost", smtp_starttls=True, smtp_username="portal", smtp_password="Mail-Password-1")
        assert await mailer.send("rita@example.com", "Subject", "Body") is False
    assert box.messages == [] and box.logins == []
    assert "STARTTLS" in box.commands and "AUTH" not in box.commands and "MAIL" not in box.commands


async def test_a_trusted_mail_server_receives_the_message_encrypted(certificate, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", certificate[0])              # how a private certificate authority is trusted
    with Mailbox(tls=certificate, auth=True) as box:
        mailer = mailer_for(box.port, smtp_host="localhost", smtp_starttls=True, smtp_username="portal", smtp_password="Mail-Password-1")
        assert await mailer.send("rita@example.com", "Subject", "Body over TLS") is True
    [message] = box.messages
    assert message.encrypted is True and "Body over TLS" in message.mail.get_content()
    assert box.logins == [("portal", True)]                          # the password travelled inside the encrypted connection


async def test_tls_from_the_first_byte_is_verified_the_same_way(certificate, monkeypatch):
    """Port 465 style: no STARTTLS step. An untrusted server gets nothing; a trusted one gets the message."""
    account = {"smtp_host": "localhost", "smtp_implicit_tls": True, "smtp_username": "portal", "smtp_password": "Mail-Password-1"}
    with Mailbox(tls=certificate, implicit_tls=True, auth=True) as box:
        assert await mailer_for(box.port, **account).send("rita@example.com", "Subject", "Body") is False
    assert box.messages == [] and box.logins == [] and "EHLO" not in box.commands

    monkeypatch.setenv("SSL_CERT_FILE", certificate[0])
    with Mailbox(tls=certificate, implicit_tls=True, auth=True) as box:
        assert await mailer_for(box.port, **account).send("rita@example.com", "Subject", "Body on 465") is True
    [message] = box.messages
    assert message.encrypted is True and "Body on 465" in message.mail.get_content() and box.logins == [("portal", True)]
    assert "STARTTLS" not in box.commands

    assert Settings(_env_file=None).smtp_implicit_tls is None           # decided by the port unless it is set


async def test_a_trusted_certificate_for_another_host_gets_nothing(tmp_path, monkeypatch):
    """The certificate is trusted, but it was issued for another machine: the name is checked as well as the chain."""
    other = make_certificate(tmp_path, "mail.example.test", "DNS:mail.example.test")
    monkeypatch.setenv("SSL_CERT_FILE", other[0])
    with Mailbox(tls=other, auth=True) as box:
        mailer = mailer_for(box.port, smtp_host="localhost", smtp_starttls=True, smtp_username="portal", smtp_password="Mail-Password-1")
        assert await mailer.send("rita@example.com", "Subject", "Body") is False
    assert box.messages == [] and box.logins == []
    assert "STARTTLS" in box.commands and "AUTH" not in box.commands and "MAIL" not in box.commands


async def test_an_invitation_reaches_the_mailbox_and_its_link_activates_the_account(world):
    admin = await world.as_admin()
    anonymous = await world.client()
    with Mailbox() as box:
        world.app.state.emailer = Emailer(world.settings.model_copy(update={
            "smtp_host": "127.0.0.1", "smtp_port": box.port, "smtp_from": SENDER, "smtp_starttls": False}))
        created = await admin.post("/api/invitations", json={"name": "Nora New", "email": "nora@example.com", "role": "MEMBER",
                                                             "team_id": str(world.ids["team_alpha"])})
        assert created.status_code == 201, created.text
        body = created.json()
        # Delivered by mail: the answer says so and does not also hand the link to the person who invited.
        assert body["email_sent"] is True and body["delivery"] == "email" and body["invite_url"] == ""
        [message] = box.messages
        assert message.recipients == ["nora@example.com"] and message.mail["Subject"] == "You are invited to AI Job Finder"
        text = message.mail.get_content()
        assert "Hello Nora New" in text and "Team Alpha" in text
        token = token_of(text)
        assert f"http://testserver/invite/accept?token={token}" in text       # the page of the web app that sets the password

        accepted = await anonymous.post("/api/invitations/accept", json={"token": token, "password": PASSWORD})
        assert accepted.status_code == 200, accepted.text
        nora = await world.client("nora@example.com")
        me = await nora.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["role"] == "MEMBER" and me.json()["team"]["name"] == "Team Alpha"

        # A reset link asked for by a manager is mailed the same way and is not shown to the manager.
        reset = await admin.post(f"/api/users/{me.json()['id']}/reset-link")
        assert reset.status_code == 200 and reset.json()["email_sent"] is True and reset.json()["reset_url"] == ""
        assert len(box.messages) == 2 and box.messages[1].recipients == ["nora@example.com"]
        reset_token = token_of(box.messages[1].mail.get_content())
        changed = await anonymous.post("/api/auth/reset-password", json={"token": reset_token, "password": "Another-Secret-91"})
        assert changed.status_code == 200, changed.text
        assert (await nora.get("/api/auth/me")).status_code == 401           # the old session ended with the old password

    # The mail server is gone: the invitation is still created, reported as not sent, and the link is handed over instead.
    lost = await admin.post("/api/invitations", json={"name": "Omar Offline", "email": "omar@example.com", "role": "MEMBER",
                                                      "team_id": str(world.ids["team_alpha"])})
    assert lost.status_code == 201 and lost.json()["email_sent"] is False and lost.json()["delivery"] == "manual"
    assert "token=" in lost.json()["invite_url"]
