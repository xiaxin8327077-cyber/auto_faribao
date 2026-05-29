import imaplib
import email
from email.header import decode_header
import logging

logger = logging.getLogger(__name__)

SUBJECT_KEYWORD = "新cookies"
SENDER_KEYWORD = "350006418@qq.com"


def read_cookies_email(cfg) -> str:
    """Read the latest cookies email from inbox.
    Returns the email body text, or empty string if not found.
    """
    e = cfg.email
    if not e.imap_host:
        logger.warning("IMAP not configured")
        return ""

    try:
        mail = imaplib.IMAP4_SSL(e.imap_host, e.imap_port)
        mail.login(e.sender, e.password)
        mail.select("INBOX")

        _, data = mail.search(None, 'ALL')
        mail_ids = data[0].split()

        if not mail_ids:
            logger.info("Inbox is empty")
            mail.logout()
            return ""

        for mid in reversed(mail_ids):
            _, msg_data = mail.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])

            subject = _decode_header(msg.get("Subject", ""))
            from_addr = msg.get("From", "")

            if SUBJECT_KEYWORD not in subject:
                continue
            if SENDER_KEYWORD not in from_addr:
                continue

            body = _extract_body(msg)
            logger.info(f"Found cookies email: subject='{subject}', id={mid}")

            mail.store(mid, "+FLAGS", "\\Deleted")
            mail.expunge()
            mail.logout()
            return body

        mail.logout()
        logger.info("No cookies email found")
        return ""

    except Exception as e:
        logger.error(f"Failed to read cookies email: {e}")
        return ""


def _decode_header(header_value: str) -> str:
    parts = decode_header(header_value)
    result = []
    for content, charset in parts:
        if isinstance(content, bytes):
            result.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(content)
    return "".join(result)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""
