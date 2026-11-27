"""Outgoing e-mail. Delivery is reported truthfully: `send` returns True only when an SMTP server accepted
the message. Without SMTP the caller shows a link for the authorized user to copy instead.

The messages carry one-time links and the connection carries the SMTP password, so the connection is encrypted
(STARTTLS, or TLS from the first byte on port 465) and the server's certificate is verified, chain and host name.
smtplib does not verify unless it is given a context. A server that does not offer STARTTLS, or whose certificate
is not trusted, gets no message and no password: the send is reported as failed. A private certificate authority
is trusted the usual OpenSSL way (SSL_CERT_FILE)."""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from ...config import Settings

log = logging.getLogger("jobfinder.mail")


class Emailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def backend(self) -> str:
        choice = self.settings.email_backend.strip().lower()
        if choice == "console":
            return "console"
        return "smtp" if self.settings.smtp_configured and choice in ("", "smtp") else "none"

    def _send_smtp(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.smtp_from, to, subject
        message.set_content(body)
        context = ssl.create_default_context()
        implicit = s.smtp_port == 465 if s.smtp_implicit_tls is None else s.smtp_implicit_tls
        connection = (smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15, context=context) if implicit
                      else smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15))
        with connection as smtp:
            if s.smtp_starttls and not implicit:
                smtp.starttls(context=context)
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(message)

    async def send(self, to: str, subject: str, body: str) -> bool:
        backend = self.backend
        if backend == "smtp":
            try:
                await asyncio.to_thread(self._send_smtp, to, subject, body)
                return True
            except Exception as exc:                      # never leak credentials or content into logs
                log.error("SMTP delivery failed: %s", type(exc).__name__)
                return False
        if backend == "console":
            log.warning("E-mail not sent (console backend). To: %s | Subject: %s\n%s", to, subject, body)
        return False


def invitation_mail(name: str, team: str | None, role: str, url: str, hours: int) -> tuple[str, str]:
    if role == "ADMIN":
        # An Admin belongs to no team. The mail says what the account can do, so a link that reached the wrong
        # person is recognised as one that matters.
        invited = ("You have been invited as an Administrator. An Administrator can see and change everything "
                   "in the portal: every team, every person, every candidate and every setting.")
    else:
        where = f" to join {team}" if team else ""
        invited = f"You have been invited{where} as a {'Team Lead' if role == 'TEAM_LEAD' else 'team member'}."
    return ("You are invited to AI Job Finder",
            f"Hello {name},\n\n{invited}\n"
            f"Create your password to activate the account:\n\n{url}\n\n"
            f"The link works once and expires in {hours} hours.\nIf you did not expect this message, ignore it.\n")


def reset_mail(name: str, url: str, minutes: int) -> tuple[str, str]:
    return ("Reset your AI Job Finder password",
            f"Hello {name},\n\nUse this link to choose a new password:\n\n{url}\n\n"
            f"The link works once and expires in {minutes} minutes.\nIf you did not ask for it, ignore this message.\n")
