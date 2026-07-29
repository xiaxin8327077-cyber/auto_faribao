import pytest


def _mod():
    import src.daily_report_edit_confirmation as m
    return m


def test_save_confirm_single_consume(monkeypatch):
    m = _mod()
    clk = [1000.0]
    store = m.EditConfirmationStore(clock=lambda: clk[0])
    item = m.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
        original_content_hash=m.content_hash("原正文"), original_preview="原正文", final_content="新正文",
        created_at=clk[0], expires_at=clk[0] + m.TTL_SECONDS)
    store.save(item)
    assert store.confirm("u1").final_content == "新正文"
    assert store.confirm("u1") is None


def test_peek_filters_expired(monkeypatch):
    m = _mod()
    clk = [1000.0]
    store = m.EditConfirmationStore(clock=lambda: clk[0])
    item = m.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
        original_content_hash="h", original_preview="p", final_content="c",
        created_at=clk[0], expires_at=clk[0] + m.TTL_SECONDS)
    store.save(item)
    assert store.peek("u1") is not None
    clk[0] += m.TTL_SECONDS + 1
    assert store.peek("u1") is None


def test_content_hash_trailing_only_not_leading():
    m = _mod()
    assert m.content_hash("一、a\n二、b") == m.content_hash("一、a \n二、b  ")
    assert m.content_hash("一、a\n二、b") != m.content_hash("一、a\n 二、b")
    assert m.content_hash("一、a\r\n二、b") == m.content_hash("一、a\n二、b")


def test_marker_per_user_peek_no_delete(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setattr(m, "_marker_dir", lambda: tmp_path)
    m.begin_modify_marker("u1", "2026-07-29", "hashA")
    m.begin_modify_marker("u2", "2026-07-29", "hashB")
    stale = m.peek_stale_modify_markers()
    assert {s["user_id"] for s in stale} == {"u1", "u2"}
    assert len(m.peek_stale_modify_markers()) == 2  # peek 不删除
    m.clear_modify_marker("u1")
    assert {s["user_id"] for s in m.peek_stale_modify_markers()} == {"u2"}
