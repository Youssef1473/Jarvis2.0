"""Comprehensive verification suite for Bidirectional Gmail Integration:
1. Untrusted content sanitization & prompt injection neutralization
2. IMAP search, unread fetching, and message detail retrieval (with mock IMAP)
3. SMTP email draft preparation and safe dispatch (with mock SMTP)
4. Brain intent pruning coverage for email commands
"""
import sys
from pathlib import Path
import os
import unittest
from unittest.mock import MagicMock, patch
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
import jarvis.skills.email_manager as email_mgr
from jarvis.skills.email_manager import (
    _sanitize_email_text,
    _decode_str,
    _get_email_body,
    check_unread_emails,
    search_emails,
    read_email_details,
    send_email,
    get_unread_email_count,
)
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.core.brain import JarvisBrain


def test_email_sanitization():
    print("\n--- 1. Testing Email Content Sanitization & Prompt Injection Defense ---")
    
    # 1. HTML tag stripping
    html_raw = "<div>Hello Sir, <b>action required</b> on server 3.</div>"
    clean = _sanitize_email_text(html_raw)
    assert "<b>" not in clean
    assert "Hello Sir, action required on server 3." in clean
    print("HTML stripping: PASSED")

    # 2. ANSI escape code stripping
    ansi_raw = "\x1B[31mDangerous red text\x1B[0m and \x1B[1mBold\x1B[0m"
    clean_ansi = _sanitize_email_text(ansi_raw)
    assert "\x1B[" not in clean_ansi
    assert "Dangerous red text and Bold" in clean_ansi
    print("ANSI escape stripping: PASSED")

    # 3. Truncation bounds
    long_raw = "A" * 4000
    truncated = _sanitize_email_text(long_raw, max_chars=500)
    assert len(truncated) <= 600
    assert "[Email truncated" in truncated
    print("Length truncation & token bounds: PASSED")


def test_unread_emails_formatting():
    print("\n--- 2. Testing Unread Emails and Search via Mock IMAP ---")

    # Mock IMAP server response
    mock_mail = MagicMock()
    mock_mail.search.return_value = ("OK", [b"101 102"])

    # Build mock email 102
    msg = MIMEMultipart()
    msg["From"] = "Pepper Potts <pepper@starkindustries.com>"
    msg["To"] = "tony@starkindustries.com"
    msg["Subject"] = "Q3 Clean Energy Report"
    msg["Date"] = "Mon, 12 Oct 2026 12:00:00 +0000"
    msg.attach(MIMEText("Tony, the new arc reactor grid deployment is ready for your review.", "plain"))

    mock_mail.fetch.return_value = ("OK", [(b"1", msg.as_bytes())])

    with patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@starkindustries.com", "dummy_password", "imap.gmail.com", "smtp.gmail.com", 465, True)), \
         patch("imaplib.IMAP4_SSL", return_value=mock_mail):
        
        # Test check_unread_emails
        res_unread = check_unread_emails(account="gmail")
        print("Unread Emails Output:\n", res_unread)
        assert "Q3 Clean Energy Report" in res_unread
        assert "Pepper Potts" in res_unread
        assert "ID: 102" in res_unread

        # Test search_emails
        res_search = search_emails(query="Clean Energy", account="gmail")
        print("\nSearch Emails Output:\n", res_search)
        assert "Found 2 email(s)" in res_search
        assert "Q3 Clean Energy Report" in res_search

        # Test read_email_details
        res_details = read_email_details(email_id="102", account="gmail")
        print("\nEmail Details Output:\n", res_details)
        assert "Email Details [ID: 102]" in res_details
        assert "Tony, the new arc reactor grid deployment is ready" in res_details
        assert "[INERT EXTERNAL EMAIL BODY]" in res_details

    print("Mock IMAP Integration: PASSED")


def test_send_email_pipeline():
    print("\n--- 3. Testing Send Email Drafting & Mock SMTP Dispatch ---")

    with patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@starkindustries.com", "dummy_password", "imap.gmail.com", "smtp.gmail.com", 465, True)):
        
        # 1. Test draft preview (confirm=False)
        draft = send_email(
            to_address="col_rhodey@usaf.mil",
            subject="War Machine Upgrades",
            body="Rhodey, the new flight telemetry modules are staged at the hangar.",
            account="gmail",
            confirm=False
        )
        print("Draft Output:\n", draft)
        assert "Draft Email Prepared for Sir's Review" in draft
        assert "col_rhodey@usaf.mil" in draft
        assert "War Machine Upgrades" in draft
        assert "Please confirm to send" in draft
        print("Draft Preview Gate: PASSED")

        # 2. Test verified dispatch (confirm=True) with mock SMTP
        mock_smtp = MagicMock()
        with patch("smtplib.SMTP_SSL", return_value=mock_smtp):
            mock_smtp.__enter__.return_value = mock_smtp
            res_send = send_email(
                to_address="col_rhodey@usaf.mil",
                subject="War Machine Upgrades",
                body="Rhodey, the new flight telemetry modules are staged at the hangar.",
                account="gmail",
                confirm=True
            )
            print("Dispatch Output:\n", res_send)
            assert "Email dispatched, Sir" in res_send
            mock_smtp.login.assert_called_once_with("tony@starkindustries.com", "dummy_password")
            mock_smtp.send_message.assert_called_once()
            print("SMTP Dispatch Pipeline: PASSED")


def test_intent_routing_coverage():
    print("\n--- 4. Testing Brain Intent Routing for Email Skills ---")
    cfg = load_config()
    brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))

    queries = {
        "Jarvis, check my unread emails": "check_unread_emails",
        "Jarvis, check my Gmail inbox": "check_unread_emails",
        "Jarvis, search emails from Pepper": "search_emails",
        "Jarvis, read email 102": "read_email_details",
        "Jarvis, send an email to Rhodey": "send_email",
        "Jarvis, draft an email to the board": "send_email",
    }

    for query, expected_tool in queries.items():
        tools = brain._select_tools_for_query(query)
        selected_names = set(tools)
        print(f"Query: '{query}' -> Selected: {selected_names}")
        assert expected_tool in selected_names, f"Expected '{expected_tool}' for query '{query}'"

    print("Brain Intent Routing Coverage: PASSED")


if __name__ == "__main__":
    test_email_sanitization()
    test_unread_emails_formatting()
    test_send_email_pipeline()
    test_intent_routing_coverage()
    print("\n========================================================")
    print("ALL GMAIL INTEGRATION VERIFICATION TESTS PASSED 100%")
    print("========================================================\n")
