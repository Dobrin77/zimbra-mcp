"""Security notices put straight into a user's Inbox."""

from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid


def notice_mime(org_name: str, email: str, subject: str, text: str, host: str | None = None) -> str:
    """A plain-text notice from no-reply@<the user's domain>, as raw MIME for AddMsgRequest.

    It is delivered with AddMsgRequest, so it never leaves the Zimbra server and
    cannot be intercepted or spoofed from outside.
    """
    domain = email.rsplit("@", 1)[1]
    msg = EmailMessage()
    msg["From"] = f"{org_name} Claude <no-reply@{domain}>"
    msg["To"] = email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=host or domain)
    msg.set_content(text)
    return msg.as_string(policy=SMTP)
