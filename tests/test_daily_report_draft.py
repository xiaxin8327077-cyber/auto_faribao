import json
import threading

import pytest


def _mod():
    import src.daily_report_draft as m
    return m


def test_save_and_load_multiline(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    body = "一、【项目A】任务：描述\n二、【项目B】任务：描述"
    assert m.save_draft("2026-07-29", body, "manual").revision == 1
    assert m.load_draft().content == body


def test_append_preserves_newlines_and_origin(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    m.save_draft("2026-07-29", "一、原内容", "manual")
    draft = m.append_draft("2026-07-29", "二、追加内容")
    assert draft.content == "一、原内容\n二、追加内容" and draft.origin == "manual" and draft.revision == 2


def test_save_rejects_invalid_real_date(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    with pytest.raises(m.DraftError):
        m.save_draft("2026-99-99", "x", "manual")
    with pytest.raises(m.DraftError):
        m.save_draft("2026/07/29", "x", "manual")
    assert not p.exists()


def test_empty_and_oversize_rejected(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    with pytest.raises(m.DraftError):
        m.save_draft("2026-07-29", "   ", "manual")
    with pytest.raises(m.DraftError):
        m.save_draft("2026-07-29", "x" * (m.MAX_CONTENT_CHARS + 1), "manual")


def test_corrupt_and_bad_fields_raise(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(m.DraftCorruptError):
        m.load_draft()
    p.write_text(json.dumps({"schema_version": 1, "report_date": "2026-99-99", "content": "x",
        "origin": "manual", "revision": 1, "created_at": "2026-07-29T16:30:00+08:00",
        "updated_at": "2026-07-29T16:30:00+08:00"}), encoding="utf-8")
    with pytest.raises(m.DraftCorruptError):
        m.load_draft()
    p.write_text(json.dumps({"schema_version": 1, "report_date": "2026-07-29", "content": "x",
        "origin": "unknown", "revision": 1, "created_at": "2026-07-29T16:30:00+08:00",
        "updated_at": "2026-07-29T16:30:00+08:00"}), encoding="utf-8")
    with pytest.raises(m.DraftCorruptError):
        m.load_draft()
    p.write_text(json.dumps({"schema_version": 1, "report_date": "2026-07-29", "content": "x",
        "origin": "manual", "revision": True, "created_at": "2026-07-29T16:30:00+08:00",
        "updated_at": "2026-07-29T16:30:00+08:00"}), encoding="utf-8")
    with pytest.raises(m.DraftCorruptError):
        m.load_draft()


def test_expired_draft_returned_not_deleted(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    m.save_draft("2026-07-28", "昨日", "manual")
    loaded = m.load_draft_for_date("2026-07-29")
    assert loaded is not None and loaded.report_date == "2026-07-28" and p.exists()


def test_clear_requires_match(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    draft = m.save_draft("2026-07-29", "今日", "manual")
    assert m.clear_draft(expected_report_date="2026-07-29", expected_revision=draft.revision + 1) is False
    assert m.clear_draft(expected_report_date="2026-07-29", expected_revision=draft.revision) is True
    assert not p.exists()


def test_concurrent_append_no_loss(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    m.save_draft("2026-07-29", "基线", "manual")
    lines = [f"<<{i}>>line" for i in range(10)]

    def append_one(ln):
        m.append_draft("2026-07-29", ln)

    ts = [threading.Thread(target=append_one, args=(ln,)) for ln in lines]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    content = m.load_draft().content
    for ln in lines:
        assert ln in content
