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


# ---- Task 4: modify_daily_report 真三态 + 按日期修改 + 标记生命周期 ----

def test_modify_rejects_non_today(monkeypatch):
    import src.target as t
    from types import SimpleNamespace
    cfg = SimpleNamespace(target=SimpleNamespace(default_project="P", url="http://x", page_timeout=1000))
    ok, msg, meta = t.modify_daily_report("x", cfg, "2099-01-01")
    assert ok is False and meta["action"] == "failed"


def test_modify_failure_pre_submit_is_failed():
    import src.target as t
    _, _, meta = t._modify_failure_result(False, Exception("e"), "2026-07-29")
    assert meta["action"] == "failed"


def test_modify_failure_post_submit_is_unknown():
    import src.target as t
    _, _, meta = t._modify_failure_result(True, Exception("e"), "2026-07-29")
    assert meta["action"] == "unknown"


class _FakeRow:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text

    def query_selector(self, sel):
        return None


class _FakeNextBtn:
    def __init__(self, page):
        self._page = page

    def is_disabled(self):
        return False

    def click(self):
        self._page._on_page2 = True


class _FakePage:
    def __init__(self, rows1, rows2):
        self._rows1 = rows1
        self._rows2 = rows2
        self._on_page2 = False
        self.goto_count = 0

    def goto(self, *a, **k):
        self.goto_count += 1

    def wait_for_selector(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        pass

    def query_selector_all(self, sel):
        return self._rows2 if self._on_page2 else self._rows1

    def query_selector(self, sel):
        if ("下一页" in sel or "btn-next" in sel) and not self._on_page2:
            return _FakeNextBtn(self)
        return None


class _NoNextPage(_FakePage):
    def __init__(self, rows1):
        super().__init__(rows1, [])

    def query_selector(self, sel):
        return None  # 无下一页


def test_find_row_table_timeout_is_error():
    import src.target as t

    class ErrPage:
        def wait_for_selector(self, *a, **k):
            raise t.PlaywrightTimeout("x")

        def wait_for_timeout(self, *a, **k):
            pass

        def query_selector(self, *a):
            return None

        def query_selector_all(self, *a):
            return []

    status, row = t._find_report_row_on_pages(ErrPage(), "2026-07-29")
    assert status == "error" and row is None


def test_find_row_absent_when_loaded_no_match():
    import src.target as t
    page = _NoNextPage([_FakeRow("2026-07-28")])  # 表格加载但无目标、无下一页
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "absent"


def test_find_row_empty_table_is_absent():
    import src.target as t
    page = _NoNextPage([])  # 表格容器加载但无任何行（空表）
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "absent"


def test_find_row_found_on_first_page():
    import src.target as t
    page = _NoNextPage([_FakeRow("2026-07-29")])
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "found" and row is not None


def test_find_row_paginates_to_second_page():
    import src.target as t
    page = _FakePage([_FakeRow("2026-07-28")], [_FakeRow("2026-07-29")])
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "found" and row is not None and page._on_page2 is True


def test_pre_modify_aborts_on_hash_mismatch(monkeypatch):
    import src.target as t
    from src.daily_report_edit_confirmation import content_hash
    page = _NoNextPage([_FakeRow("2026-07-29")])
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "旧正文")
    ok, msg, _, _ = t._check_pre_modify(page, "2026-07-29", content_hash("不同的"))
    assert ok is False and "发生变化" in msg


def test_pre_modify_returns_row(monkeypatch):
    import src.target as t
    from src.daily_report_edit_confirmation import content_hash
    page = _NoNextPage([_FakeRow("2026-07-29")])
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "旧正文")
    ok, msg, current, row = t._check_pre_modify(page, "2026-07-29", content_hash("旧正文"))
    assert ok is True and current == "旧正文" and row is not None


def test_verify_after_modify_re_navigates_and_compares(monkeypatch):
    import src.target as t
    from types import SimpleNamespace
    page = _NoNextPage([_FakeRow("2026-07-29")])
    target = SimpleNamespace(url="http://x", page_timeout=1000)
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "新正文")
    before = page.goto_count
    assert t._verify_after_modify(page, "2026-07-29", "新正文", target) is True
    assert page.goto_count > before


def test_modify_marker_lifecycle_under_lock(monkeypatch, tmp_path):
    import src.target as t
    import src.daily_report_edit_confirmation as ec
    import src.beijing_time as bt
    monkeypatch.setattr(bt, "today_str", lambda: "2026-07-29")
    events = []
    monkeypatch.setattr(ec, "begin_modify_marker", lambda u, d, h: events.append(("begin", u)))
    monkeypatch.setattr(ec, "clear_modify_marker", lambda u: events.append(("clear", u)))

    class FakePw:
        def stop(self):
            events.append(("pw_stop",))

    class FakeBrowser:
        def close(self):
            events.append(("browser_close",))

    class FakeBtn:
        def click(self):
            events.append(("edit_click",))

    class ClickableRow:
        def inner_text(self):
            return "2026-07-29"  # 命中当天日期

        def query_selector(self, sel):
            return FakeBtn()  # 返回修改按钮

    fake_page = _NoNextPage([ClickableRow()])
    monkeypatch.setattr(t, "_login_and_navigate", lambda cfg: (FakePw(), FakeBrowser(), None, fake_page))
    monkeypatch.setattr(t, "_BROWSER_LOCK", type("L", (), {"release": staticmethod(lambda: events.append(("lock_release",)))})())
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "旧正文")
    monkeypatch.setattr(t, "_fill_report_form", lambda *a, **k: events.append(("fill",)))
    monkeypatch.setattr(t, "_submit_dialog", lambda *a, **k: events.append(("submit",)))
    monkeypatch.setattr(t, "_verify_after_modify", lambda p, d, tc, target: (events.append(("verify",)), True)[1])
    from src.daily_report_edit_confirmation import content_hash
    from types import SimpleNamespace
    cfg = SimpleNamespace(target=SimpleNamespace(default_project="P", url="http://x", page_timeout=1000))
    ok, msg, meta = t.modify_daily_report("新正文", cfg, "2026-07-29",
                                          expected_content_hash=content_hash("旧正文"), user_id="u1")
    assert ok and meta["action"] == "overwrite"
    assert ("submit",) in events  # submitted 在 submit 之前/之后无所谓，但 submit 发生
    begin_i = events.index(("begin", "u1"))
    clear_i = events.index(("clear", "u1"))
    rel_i = events.index(("lock_release",))
    assert begin_i < clear_i < rel_i  # begin→clear→release
