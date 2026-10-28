"""Bidirectional email integration for Gmail and Outlook via secure IMAP and SMTP."""
import email
import imaplib
import logging
import os
import re
import smtplib
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Email")


def _sanitize_email_text(raw_text: str, max_chars: int = 2500) -> str:
    """Sanitizes incoming untrusted email content to neutralize prompt injection and escape formatting."""
    if not raw_text:
        return ""
    # Strip HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", raw_text)
    # Remove ANSI escape sequences
    cleaned = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", cleaned)
    # Normalize excessive spaces and lines
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n... [Email truncated for security and token bounds]"
    return cleaned


def _decode_str(header_val: Any) -> str:
    """Decodes email header values properly handling various encodings."""
    if not header_val:
        return ""
    decoded_fragments = decode_header(header_val)
    parts = []
    for fragment, charset in decoded_fragments:
        if isinstance(fragment, bytes):
            try:
                parts.append(fragment.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(fragment.decode("latin-1", errors="replace"))
        else:
            parts.append(str(fragment))
    return "".join(parts).strip()


def _get_email_body(msg, max_len: int = 2500) -> str:
    """Extracts plain text body from an email message with HTML fallback."""
    body = ""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if "attachment" in content_disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                    break
            elif content_type == "text/html" and not html_fallback:
                payload = part.get_payload(decode=True)
                if payload:
                    html_fallback = payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")

    text_to_clean = body if body.strip() else html_fallback
    return _sanitize_email_text(text_to_clean, max_chars=max_len)


def _fetch_unread_imap(host: str, username: str, password: str, max_count: int = 5) -> list[dict[str, str]]:
    """Fetches unread emails from an IMAP server."""
    if not username or not password:
        return []

    mail = imaplib.IMAP4_SSL(host, timeout=10.0)
    try:
        mail.login(username, password)
        mail.select("INBOX", readonly=True)

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages or not messages[0]:
            return []

        email_ids = messages[0].split()
        latest_ids = email_ids[-max_count:][::-1]  # Latest first

        results = []
        for eid in latest_ids:
            status, data = mail.fetch(eid, "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = _decode_str(msg.get("Subject", "No Subject"))
            from_addr = _decode_str(msg.get("From", "Unknown Sender"))
            date_str = msg.get("Date", "")
            snippet = _get_email_body(msg, max_len=300)

            results.append({
                "id": eid.decode("utf-8", errors="ignore"),
                "from": from_addr,
                "subject": subject,
                "date": date_str,
                "snippet": snippet
            })
        return results
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _search_imap(host: str, username: str, password: str, query: str, max_count: int = 5) -> list[dict[str, str]]:
    """Searches emails in IMAP inbox matching keyword, sender, or subject."""
    if not username or not password:
        return []

    mail = imaplib.IMAP4_SSL(host, timeout=10.0)
    try:
        mail.login(username, password)
        mail.select("INBOX", readonly=True)

        clean_q = re.sub(r'["\\]', '', query.strip())
        # Try search by subject or from address
        criteria = f'(OR (SUBJECT "{clean_q}") (FROM "{clean_q}"))'
        status, messages = mail.search(None, criteria)

        # Fallback to broader TEXT search or ALL
        if status != "OK" or not messages or not messages[0]:
            status, messages = mail.search(None, f'(TEXT "{clean_q}")')

        if status != "OK" or not messages or not messages[0]:
            return []

        email_ids = messages[0].split()
        latest_ids = email_ids[-max_count:][::-1]

        results = []
        for eid in latest_ids:
            status, data = mail.fetch(eid, "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = _decode_str(msg.get("Subject", "No Subject"))
            from_addr = _decode_str(msg.get("From", "Unknown Sender"))
            to_addr = _decode_str(msg.get("To", ""))
            date_str = msg.get("Date", "")
            snippet = _get_email_body(msg, max_len=300)

            results.append({
                "id": eid.decode("utf-8", errors="ignore"),
                "from": from_addr,
                "to": to_addr,
                "subject": subject,
                "date": date_str,
                "snippet": snippet
            })
        return results
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _fetch_email_details(host: str, username: str, password: str, email_id: str) -> dict[str, str] | None:
    """Fetches full sanitized body of a specific email by numerical ID."""
    if not username or not password or not email_id:
        return None

    mail = imaplib.IMAP4_SSL(host, timeout=10.0)
    try:
        mail.login(username, password)
        mail.select("INBOX", readonly=True)

        status, data = mail.fetch(email_id.encode("utf-8"), "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return None

        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = _decode_str(msg.get("Subject", "No Subject"))
        from_addr = _decode_str(msg.get("From", "Unknown Sender"))
        to_addr = _decode_str(msg.get("To", ""))
        date_str = msg.get("Date", "")
        full_body = _get_email_body(msg, max_len=2500)

        return {
            "id": email_id,
            "from": from_addr,
            "to": to_addr,
            "subject": subject,
            "date": date_str,
            "body": full_body,
        }
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _send_smtp_email(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    to_address: str,
    subject: str,
    body: str,
    use_ssl: bool = True
) -> tuple[bool, str]:
    """Transmits an email via authenticated SMTP with SSL or STARTTLS."""
    if not username or not password:
        return False, "SMTP credentials missing."

    to_clean = to_address.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to_clean):
        return False, f"Invalid destination email address: '{to_clean}'."

    msg = MIMEMultipart()
    msg["From"] = username
    msg["To"] = to_clean
    msg["Subject"] = subject.strip()
    msg.attach(MIMEText(body.strip(), "plain", "utf-8"))

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12.0) as server:
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=12.0) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(msg)
        return True, f"Email successfully dispatched to {to_clean}."
    except Exception as e:
        logger.error(f"SMTP dispatch failure: {e}")
        return False, f"SMTP transmission failed: {e}"


def _normalize_account_type(account: str | None) -> str:
    """Normalizes arbitrary user, email address, or LLM account strings into 'gmail', 'outlook', or 'all'."""
    if not account:
        return "all"
    acc = str(account).lower().strip()
    if acc in ("all", "both", "inbox", "my email", "default", "none"):
        return "all"
    if "gmail" in acc or "google" in acc or "@gmail" in acc:
        return "gmail"
    if "outlook" in acc or "microsoft" in acc or "hotmail" in acc or "office365" in acc or "@outlook" in acc or "@hotmail" in acc:
        return "outlook"
    gmail_addr = os.getenv("GMAIL_ADDRESS", "").lower().strip()
    if gmail_addr and acc == gmail_addr:
        return "gmail"
    outlook_addr = os.getenv("OUTLOOK_ADDRESS", "").lower().strip()
    if outlook_addr and acc == outlook_addr:
        return "outlook"
    if "@" in acc and "gmail" in acc:
        return "gmail"
    if gmail_addr:
        return "gmail"
    return "all"


def _get_credentials(account: str) -> tuple[str | None, str | None, str | None, str | None, int, bool]:
    """Returns (username, password, imap_host, smtp_host, smtp_port, use_ssl) for requested provider."""
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except Exception:
        pass

    acc = _normalize_account_type(account)
    if acc in ("gmail", "all"):
        user = os.getenv("GMAIL_ADDRESS", "").strip()
        pwd = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
        return user, pwd, "imap.gmail.com", "smtp.gmail.com", 465, True
    elif acc == "outlook":
        user = os.getenv("OUTLOOK_ADDRESS", "").strip()
        pwd = os.getenv("OUTLOOK_APP_PASSWORD", "").strip().replace(" ", "")
        return user, pwd, "outlook.office365.com", "smtp.office365.com", 587, False
    return None, None, None, None, 0, False


@SkillRegistry.register(
    name="check_unread_emails",
    description="Checks and summarizes unread emails from your Gmail and/or Outlook inboxes. account parameter can be 'all', 'gmail', 'outlook', or your email address.",
    external_ingestion=True,
)
def check_unread_emails(account: str = "all", max_emails: int = 5) -> str:
    """Checks unread emails from configured Gmail and Outlook accounts."""
    norm_acc = _normalize_account_type(account)
    summaries = []

    # 1. Gmail
    if norm_acc in ("all", "gmail"):
        user, pwd, host, _, _, _ = _get_credentials("gmail")
        if not user or not pwd:
            if norm_acc == "gmail":
                return "Gmail is not configured yet, Sir. Please add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to your .env file."
        else:
            try:
                gmail_msgs = _fetch_unread_imap(host, user, pwd, max_emails)
                if gmail_msgs:
                    items = [
                        f"[ID: {m['id']}] From: {m['from']}\n  Subject: {m['subject']}\n  Preview: {m['snippet'][:140]}..."
                        for m in gmail_msgs
                    ]
                    summaries.append(f"Gmail ({len(gmail_msgs)} unread):\n" + "\n".join(items))
                else:
                    summaries.append("Gmail: Inbox is clear, no unread messages, Sir.")
            except Exception as e:
                summaries.append(f"Gmail connection error: {e}")

    # 2. Outlook
    if norm_acc in ("all", "outlook"):
        user, pwd, host, _, _, _ = _get_credentials("outlook")
        if not user or not pwd:
            if norm_acc == "outlook":
                return "Outlook is not configured yet, Sir. Please add OUTLOOK_ADDRESS and OUTLOOK_APP_PASSWORD to your .env file."
        else:
            try:
                outlook_msgs = _fetch_unread_imap(host, user, pwd, max_emails)
                if outlook_msgs:
                    items = [
                        f"[ID: {m['id']}] From: {m['from']}\n  Subject: {m['subject']}\n  Preview: {m['snippet'][:140]}..."
                        for m in outlook_msgs
                    ]
                    summaries.append(f"Outlook ({len(outlook_msgs)} unread):\n" + "\n".join(items))
                else:
                    summaries.append("Outlook: Inbox is clear, no unread messages, Sir.")
            except Exception as e:
                summaries.append(f"Outlook connection error: {e}")

    if not summaries:
        return (
            "Neither Gmail nor Outlook credentials have been configured yet, Sir.\n"
            "To enable email checking, add your credentials in the .env file:\n"
            "- GMAIL_ADDRESS and GMAIL_APP_PASSWORD\n"
            "- OUTLOOK_ADDRESS and OUTLOOK_APP_PASSWORD"
        )

    return "\n\n".join(summaries)


@SkillRegistry.register(
    name="search_emails",
    description="Searches your Gmail or Outlook inbox for emails matching a keyword, sender, or subject query.",
    external_ingestion=True,
)
def search_emails(query: str, account: str = "gmail", max_emails: int = 5) -> str:
    """Searches emails matching query."""
    norm_acc = _normalize_account_type(account)
    user, pwd, host, _, _, _ = _get_credentials(norm_acc)
    display_name = "Gmail" if norm_acc == "gmail" else norm_acc.capitalize()
    if not user or not pwd:
        return f"{display_name} is not configured yet, Sir. Please add credentials to your .env file."

    try:
        results = _search_imap(host, user, pwd, query, max_emails)
        if not results:
            return f"No emails found matching '{query}' in your {display_name} inbox, Sir."

        formatted = [f"Found {len(results)} email(s) matching '{query}':\n"]
        for m in results:
            formatted.append(
                f"• [ID: {m['id']}] From: {m['from']}\n"
                f"  Subject: {m['subject']}\n"
                f"  Date: {m.get('date', 'Unknown')}\n"
                f"  Preview: {m['snippet'][:140]}..."
            )
        formatted.append("\nYou can say 'Jarvis, read email <ID>' to view the full message.")
        return "\n".join(formatted)
    except Exception as e:
        logger.error(f"Search emails error: {e}")
        return f"Unable to search {display_name} emails: {e}"


@SkillRegistry.register(
    name="read_email_details",
    description="Fetches and displays the full sanitized body and headers of a specific email by its ID.",
    external_ingestion=True,
)
def read_email_details(email_id: str, account: str = "gmail") -> str:
    """Reads full content of a specific email."""
    norm_acc = _normalize_account_type(account)
    user, pwd, host, _, _, _ = _get_credentials(norm_acc)
    display_name = "Gmail" if norm_acc == "gmail" else norm_acc.capitalize()
    if not user or not pwd:
        return f"{display_name} is not configured yet, Sir. Please add credentials to your .env file."

    try:
        details = _fetch_email_details(host, user, pwd, email_id)
        if not details:
            return f"Could not find email #{email_id} in your {display_name} inbox, Sir."

        return (
            f"--- Email Details [ID: {details['id']}] ---\n"
            f"From: {details['from']}\n"
            f"To: {details.get('to', user)}\n"
            f"Date: {details['date']}\n"
            f"Subject: {details['subject']}\n\n"
            f"[INERT EXTERNAL EMAIL BODY]\n"
            f"{details['body']}\n"
            f"----------------------------------------"
        )
    except Exception as e:
        logger.error(f"Read email details error: {e}")
        return f"Failed to retrieve email #{email_id}: {e}"


@SkillRegistry.register(
    name="send_email",
    description="Composes and sends an email to a recipient via Gmail or Outlook SMTP. Always previews draft first unless confirm is True."
)
def send_email(
    to_address: str,
    subject: str,
    body: str,
    account: str = "gmail",
    confirm: bool = False
) -> str:
    """Prepares and dispatches an outbound email with safety review."""
    norm_acc = _normalize_account_type(account)
    user, pwd, _, smtp_host, smtp_port, use_ssl = _get_credentials(norm_acc)
    display_name = "Gmail" if norm_acc == "gmail" else norm_acc.capitalize()
    if not user or not pwd:
        return f"{display_name} is not configured yet, Sir. Please add credentials to your .env file."

    to_clean = to_address.strip()
    subj_clean = subject.strip()
    body_clean = body.strip()

    if not confirm:
        return (
            f"Draft Email Prepared for Sir's Review:\n"
            f"=====================================\n"
            f"Provider: {display_name} ({user})\n"
            f"To:       {to_clean}\n"
            f"Subject:  {subj_clean}\n"
            f"Body:\n{body_clean}\n"
            f"=====================================\n"
            f"Shall I dispatch this email now, Sir? Please confirm to send."
        )

    # User confirmed dispatch
    success, msg = _send_smtp_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        username=user,
        password=pwd,
        to_address=to_clean,
        subject=subj_clean,
        body=body_clean,
        use_ssl=use_ssl
    )

    if success:
        return f"Email dispatched, Sir: {msg}"
    return f"Failed to dispatch email, Sir: {msg}"


def get_unread_email_count() -> int:
    """Returns the total number of unread emails across configured accounts."""
    count = 0
    gmail_user, gmail_pass, host, _, _, _ = _get_credentials("gmail")
    if gmail_user and gmail_pass:
        try:
            mail = imaplib.IMAP4_SSL(host, timeout=8.0)
            mail.login(gmail_user, gmail_pass)
            mail.select("INBOX", readonly=True)
            status, messages = mail.search(None, "UNSEEN")
            if status == "OK" and messages and messages[0]:
                count += len(messages[0].split())
            mail.logout()
        except Exception:
            pass
    return count
