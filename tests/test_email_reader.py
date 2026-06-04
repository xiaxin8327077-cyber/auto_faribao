import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.email_reader import read_cookies_email


def test_read_cookies_email_sets_imap_timeout(monkeypatch):
    captured = {}

    class FakeIMAP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def login(self, sender, password):
            pass

        def select(self, mailbox):
            pass

        def search(self, charset, criterion):
            return "OK", [b""]

        def logout(self):
            pass

    monkeypatch.setattr("src.email_reader.imaplib.IMAP4_SSL", FakeIMAP)
    cfg = SimpleNamespace(
        email=SimpleNamespace(
            imap_host="imap.example.com",
            imap_port=993,
            sender="sender@example.com",
            password="secret",
        )
    )

    assert read_cookies_email(cfg) == ""
    assert captured == {
        "host": "imap.example.com",
        "port": 993,
        "timeout": 30,
    }
