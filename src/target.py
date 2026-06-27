import logging
import re
from datetime import date
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout
from src.config import Config, TargetConfig
from src.auth import login_with_captcha, AuthError

logger = logging.getLogger(__name__)

REPORT_PAGE = "/project/prmProjectWorkLog"
ADD_BTN = 'button:has-text("添加项目日志")'
DIALOG_SUBMIT = '.el-dialog__wrapper:not([style*="display: none"]) .el-dialog button:has-text("确 定")'
WORK_HOURS = "8"
DATE_PATTERN = re.compile(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}")

FORM_SELECTORS = {
    "project_input": '.el-dialog input[placeholder="请选择项目"]',
    "date_start": '.el-dialog input[placeholder="开始日期"]',
    "date_end": '.el-dialog input[placeholder="结束日期"]',
    "hours": '.el-dialog .el-form-item:has-text("工时") input.el-input__inner',
    "detail": '.el-dialog textarea, .el-dialog .el-textarea__inner',
}


class TargetError(Exception):
    pass


def _domain_from_url(url: str) -> str:
    return urlparse(url).hostname or ""


def _safe_screenshot(page: Page, name: str) -> str:
    try:
        import time
        path = f"{name}_{int(time.time())}.png"
        page.screenshot(path=path, full_page=True)
        logger.info(f"Screenshot saved: {path}")
        return path
    except Exception as e:
        logger.debug(f"Screenshot failed: {e}")
        return ""


def _normalize_report_date(report_date: str = None) -> str:
    if not report_date:
        from src.beijing_time import today_str
        return today_str()
    try:
        return date.fromisoformat(str(report_date).strip().replace("/", "-")).isoformat()
    except ValueError:
        raise TargetError(f"日报日期格式不正确: {report_date}")


def _find_existing_report_row(page: Page, report_date: str):
    try:
        page.wait_for_selector("table tbody tr, .el-table__body tr", timeout=30000)
        page.wait_for_timeout(3000)
    except PlaywrightTimeout:
        logger.info("No table found, assuming no existing report")
        return None

    rows = page.query_selector_all("table tbody tr, .el-table__body tr")
    for row in rows:
        text = row.inner_text()
        if report_date in text or report_date.replace("-", "/") in text:
            logger.info(f"Found existing report for {report_date}")
            return row

    logger.info(f"No existing report found for {report_date}")
    return None


def _check_existing_report(page: Page, report_date: str) -> bool:
    return _find_existing_report_row(page, report_date) is not None


def _delete_existing_report(page: Page, report_date: str) -> bool:
    """Delete existing report for the given date. Returns True if deleted, False if no delete button (approved)."""
    row = _find_existing_report_row(page, report_date)
    if not row:
        return True
    del_btn = row.query_selector("button:has-text('删除')")
    if not del_btn:
        logger.warning(f"Found existing report for {report_date} but no delete button (probably already approved)")
        return False
    try:
        row.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        del_btn.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        try:
            del_btn.click()
        except Exception:
            del_btn.evaluate("el => el.click()")
        page.wait_for_timeout(1500)

        for attempt in range(3):
            confirm_clicked = page.evaluate('''() => {
                const wrappers = document.querySelectorAll('.el-message-box__wrapper, .el-dialog__wrapper');
                for (const w of wrappers) {
                    const style = window.getComputedStyle(w);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const btns = w.querySelectorAll('.el-message-box__btns button, .el-dialog__footer button, button');
                    for (const b of btns) {
                        const t = (b.textContent || '').trim().replace(/\\s+/g, '');
                        if (t === '确定' || t === '确认') { b.click(); return true; }
                    }
                }
                return false;
            }''')
            if confirm_clicked:
                page.wait_for_timeout(1500)
                still_exists = _check_existing_report(page, report_date)
                if not still_exists:
                    logger.info(f"Existing report for {report_date} deleted")
                    return True
                page.wait_for_timeout(1000)
            else:
                break

        still_exists = _check_existing_report(page, report_date)
        if not still_exists:
            page.evaluate('''() => {
                const boxes = document.querySelectorAll('.el-message-box__wrapper');
                boxes.forEach(b => b.style.display = 'none');
            }''')
            page.wait_for_timeout(500)
            logger.info(f"Existing report for {report_date} deleted")
            return True
        logger.error(f"Failed to delete existing report for {report_date} - still present after attempts")
        raise TargetError(f"删除已有日报失败，删除后日报仍然存在")
    except TargetError:
        raise
    except Exception as e:
        logger.error(f"Failed to delete existing report: {e}")
        raise TargetError(f"删除已有日报失败: {e}")


def _open_existing_report_edit(page: Page, report_date: str) -> bool:
    row = _find_existing_report_row(page, report_date)
    if not row:
        return False
    button = row.query_selector("button:has-text('修改'), button:has-text('编辑')")
    if not button:
        logger.warning(f"Found existing report for {report_date} but no edit button (probably already approved), skipping overwrite")
        return None
    try:
        row.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        button.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        button.click()
    except Exception as e:
        logger.warning(f"Normal click failed, trying JS click: {e}")
        try:
            button.evaluate("el => el.click()")
        except Exception as e2:
            logger.error(f"JS click also failed: {e2}")
            raise TargetError(f"点击修改按钮失败: {e}")
    page.wait_for_timeout(3000)
    logger.info(f"Edit dialog opened for existing report {report_date}")
    return True


def submit_daily_report(content: str, cfg: Config, dry_run: bool = False,
                        report_date: str = None) -> tuple[bool, str, dict]:
    """report_date: optional date string like '2026-05-29' for non-today reports."""
    target = cfg.target
    try:
        actual_report_date = _normalize_report_date(report_date)
    except TargetError as e:
        return False, str(e), {}

    logger.info("Logging in via API...")
    try:
        token = login_with_captcha(
            target.url, target.username, target.password, cfg.captcha,
        )
    except AuthError as e:
        logger.error(f"Login failed: {e}")
        return False, str(e), {}

    domain = _domain_from_url(target.url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        context.add_cookies([
            {"name": "DQMS-Token", "value": token, "domain": domain, "path": "/"},
            {"name": "LoginModeKey", "value": "1", "domain": domain, "path": "/"},
        ])

        page = context.new_page()
        page.set_default_timeout(target.element_timeout)

        try:
            report_url = target.url.rstrip("/") + REPORT_PAGE
            logger.info(f"Navigating to: {report_url}")
            page.goto(report_url, timeout=target.page_timeout)
            try:
                page.wait_for_selector("table, .el-table", timeout=60000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(5000)

            if dry_run:
                page.click(ADD_BTN)
                page.wait_for_timeout(2000)
                _fill_report_form(page, content, target, actual_report_date)
                page.screenshot(path="dry_run_form.png", full_page=True)
                logger.info("Dry-run: form filled, screenshot saved to dry_run_form.png")
                return True, "DRY RUN — 表单已填写（未提交），截图: dry_run_form.png", {}

            has_existing = _check_existing_report(page, actual_report_date)
            if has_existing:
                can_delete = _delete_existing_report(page, actual_report_date)
                if not can_delete:
                    return True, f"今日日报已存在（已审核，无法删除/修改），跳过重复提交", {
                        "project": target.default_project,
                        "hours": WORK_HOURS,
                        "travel": "否",
                        "log_type": "实施日志",
                        "report_date": actual_report_date,
                        "action": "skipped",
                    }
            page.click(ADD_BTN)
            page.wait_for_timeout(2000)
            _fill_report_form(page, content, target, actual_report_date)
            _submit_dialog(page)
            action = "覆盖" if has_existing else "提交"
            return True, f"日报{action}成功\n> {content[:200]}", {
                "project": target.default_project,
                "hours": WORK_HOURS,
                "travel": "否",
                "log_type": "实施日志",
                "content": content,
                "report_date": actual_report_date,
                "action": action,
            }
        except TargetError as e:
            logger.error(f"Submit failed: {e}")
            screenshot = _safe_screenshot(page, "submit_error_target")
            return False, str(e), {"screenshot": screenshot}
        except PlaywrightTimeout as e:
            logger.error(f"Timeout: {e}")
            screenshot = _safe_screenshot(page, "submit_error_timeout")
            return False, f"页面操作超时: {e}", {"screenshot": screenshot}
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            screenshot = _safe_screenshot(page, "submit_error_unexpected")
            return False, f"未知错误: {e}", {"screenshot": screenshot}
        finally:
            browser.close()


def modify_daily_report(content: str, cfg: Config) -> tuple[bool, str, dict]:
    """Modify today's submitted daily report with new content."""
    target = cfg.target

    logger.info("Logging in via API for modification...")
    try:
        token = login_with_captcha(
            target.url, target.username, target.password, cfg.captcha,
        )
    except AuthError as e:
        logger.error(f"Login failed for modify: {e}")
        return False, str(e), {}

    domain = _domain_from_url(target.url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        context.add_cookies([
            {"name": "DQMS-Token", "value": token, "domain": domain, "path": "/"},
            {"name": "LoginModeKey", "value": "1", "domain": domain, "path": "/"},
        ])
        page = context.new_page()
        page.set_default_timeout(target.element_timeout)

        try:
            _navigate_and_click_edit(page, target)
            _fill_report_form(page, content, target)
            _submit_dialog(page)
            return True, f"日报修改成功\n> {content[:200]}", {
                "project": target.default_project,
                "hours": WORK_HOURS,
                "travel": "否",
                "log_type": "实施日志",
                "content": content,
            }
        except TargetError as e:
            return False, str(e), {}
        except PlaywrightTimeout as e:
            return False, f"页面操作超时: {e}", {}
        except Exception as e:
            logger.error(f"Modify failed: {e}", exc_info=True)
            return False, f"未知错误: {e}", {}
        finally:
            browser.close()


def get_previous_report_content(cfg: Config, before_date: str = None) -> str:
    """Read the latest previous report content from OA for fallback submissions.

    Multi-strategy reading:
    1. Try to read directly from table row cells
    2. Try to click view/detail/edit button and read from dialog
       - From textarea/input (edit mode)
       - From plain text elements (view mode)
    """
    target = cfg.target
    cutoff = _normalize_report_date(before_date) if before_date else None

    logger.info("Logging in via API to read previous report...")
    try:
        token = login_with_captcha(
            target.url, target.username, target.password, cfg.captcha,
        )
    except AuthError as e:
        raise TargetError(f"Login failed while reading previous report: {e}")

    domain = _domain_from_url(target.url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        context.add_cookies([
            {"name": "DQMS-Token", "value": token, "domain": domain, "path": "/"},
            {"name": "LoginModeKey", "value": "1", "domain": domain, "path": "/"},
        ])
        page = context.new_page()
        page.set_default_timeout(target.element_timeout)

        try:
            report_url = target.url.rstrip("/") + REPORT_PAGE
            logger.info(f"Navigating to read previous report: {report_url}")
            page.goto(report_url, timeout=target.page_timeout)
            try:
                page.wait_for_selector("table tbody tr, .el-table__body tr", timeout=60000)
            except PlaywrightTimeout:
                raise TargetError("Report table not found while reading previous report")
            page.wait_for_timeout(5000)

            rows = page.query_selector_all("table tbody tr, .el-table__body tr")
            for row in rows:
                text = row.inner_text()
                row_date = _extract_row_date(text)
                if cutoff and row_date and row_date >= cutoff:
                    continue

                logger.debug(f"Trying row with date {row_date or 'unknown'}")
                content = _read_content_by_expand_row(page, row)
                if content:
                    logger.info(f"Previous report loaded via expand row from {row_date or 'latest row'}")
                    return content

                content = _read_content_from_row(row)
                if content:
                    logger.info(f"Previous report loaded from table row of {row_date or 'latest row'}")
                    return content

                content = _read_content_by_open_dialog(page, row)
                if content:
                    logger.info(f"Previous report loaded from dialog of {row_date or 'latest row'}")
                    return content

            raise TargetError("No previous report content found")
        finally:
            browser.close()


def _read_content_by_expand_row(page: Page, row) -> str:
    """Try to expand the row (click the expand arrow) and read detail content from the expanded area.

    This is the most reliable method because it doesn't depend on edit permissions.
    """
    try:
        expand_btn = None
        first_cell = row.query_selector("td:first-child, .el-table__cell:first-child")
        if first_cell:
            cell_text = (first_cell.inner_text() or "").strip()
            if cell_text and cell_text in ("∨", "∧", "▼", "▲", "˅", "˄", "↓", "↑", "展开", "收起"):
                expand_btn = first_cell
            else:
                icons = first_cell.query_selector_all("svg, i, span[class*='arrow'], span[class*='expand'], .el-icon, .el-table__expand-icon")
                if icons:
                    expand_btn = icons[0]
                else:
                    all_spans = first_cell.query_selector_all("span, div, i")
                    for el in all_spans:
                        txt = (el.inner_text() or "").strip()
                        if txt and txt in ("∨", "∧", "▼", "▲", "˅", "˄", "↓", "↑"):
                            expand_btn = el
                            break

        if not expand_btn:
            expand_candidates = row.query_selector_all(
                ".el-table__expand-icon, .el-table__expand-column .cell, "
                "[class*='expand-icon'], [class*='expand-column']"
            )
            if expand_candidates:
                expand_btn = expand_candidates[0]

        if not expand_btn:
            return ""

        try:
            expand_btn.click()
        except Exception:
            try:
                if first_cell:
                    first_cell.click()
            except Exception:
                return ""

        page.wait_for_timeout(1500)

        row_index = None
        all_rows = page.query_selector_all("table tbody tr, .el-table__body tr")
        for idx, r in enumerate(all_rows):
            if r == row:
                row_index = idx
                break

        if row_index is not None:
            for offset in range(1, 5):
                next_row_idx = row_index + offset
                if next_row_idx >= len(all_rows):
                    break
                next_row = all_rows[next_row_idx]
                class_name = next_row.get_attribute("class") or ""
                if ("expand" in class_name.lower() or "detail" in class_name.lower()
                        or "expanded" in class_name.lower() or "hide" not in class_name.lower()):
                    content = _extract_detail_from_expanded_row(next_row)
                    if content:
                        return content

        expanded_selectors = [
            ".el-table__expanded-cell",
            ".el-table__expand-row",
            "tr.el-table__row--expanded",
            "tr[class*='expand']",
            "tr[class*='detail']",
            ".el-table__row + tr:not([class*='el-table__row'])",
        ]
        for sel in expanded_selectors:
            cells = page.query_selector_all(sel)
            for cell in cells:
                if cell.is_visible():
                    content = _extract_detail_from_expanded_row(cell)
                    if content:
                        return content

        return ""
    except Exception as e:
        logger.debug(f"Failed to read content by expanding row: {e}")
        return ""


def _extract_detail_from_expanded_row(element) -> str:
    """Extract report content from an expanded row/detail area.

    Must find a '详情' (detail) label, then return the content after it.
    If '详情' is not found, return empty string to try next strategy.
    """
    try:
        text = (element.inner_text() or "").strip()
        if not text or len(text) < 10:
            return ""

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            return ""

        detail_idx = -1
        for i, line in enumerate(lines):
            if line in ("详情", "工作内容", "日志内容", "内容", "工作详情"):
                detail_idx = i
                break

        if detail_idx < 0:
            return ""

        if detail_idx + 1 >= len(lines):
            return ""

        content_lines = lines[detail_idx + 1:]
        result_lines = []
        for line in content_lines:
            if len(line) < 1:
                continue
            if line.startswith(("项目名称", "项目", "日期", "开始日期", "结束日期",
                                 "工时", "出差", "类型", "状态", "审核",
                                 "提交人", "创建人", "创建时间", "更新时间",
                                 "所属部门", "员工", "部门")):
                continue
            result_lines.append(line)

        if result_lines:
            result = "\n".join(result_lines).strip()
            if len(result) >= 5:
                return result

        return ""
    except Exception as e:
        logger.debug(f"Failed to extract detail from expanded row: {e}")
        return ""


def _read_content_from_row(row) -> str:
    """Try to extract report content directly from table row cells.

    Skips date, project name, status, action button columns.
    Only returns content that looks like actual report text (>= 50 chars).
    """
    try:
        cells = row.query_selector_all("td, .el-table__cell")
        if not cells:
            return ""

        candidates = []
        for cell in cells:
            cell_text = (cell.inner_text() or "").strip()
            if not cell_text:
                continue
            if len(cell_text) < 30:
                continue
            if DATE_PATTERN.search(cell_text):
                continue
            if cell_text in ("修改", "编辑", "查看", "详情", "删除", "预览"):
                continue
            if cell.query_selector("button"):
                continue
            if "小时" in cell_text and len(cell_text) < 20:
                continue
            if cell_text.isdigit():
                continue
            candidates.append(cell_text)

        if candidates:
            best = max(candidates, key=len)
            if len(best) >= 50:
                return best.strip()
        return ""
    except Exception as e:
        logger.debug(f"Failed to read content from row: {e}")
        return ""


def _read_content_by_open_dialog(page: Page, row) -> str:
    """Try to open a dialog by clicking any available button and read content from it.

    Supports edit/view/detail/preview buttons.
    Tries multiple selectors for reading content: textarea, div, p, etc.
    """
    try:
        button_selectors = [
            "button:has-text('查看')",
            "button:has-text('详情')",
            "button:has-text('预览')",
            "button:has-text('修改')",
            "button:has-text('编辑')",
        ]
        button = None
        for sel in button_selectors:
            btn = row.query_selector(sel)
            if btn:
                button = btn
                break

        if not button:
            return ""

        button.click()
        page.wait_for_timeout(2000)

        content = _read_detail_from_dialog(page)
        if content:
            _close_dialog(page)
            return content

        content = _read_detail_from_view_dialog(page)
        if content:
            _close_dialog(page)
            return content

        _close_dialog(page)
        return ""
    except Exception as e:
        logger.debug(f"Failed to read content by opening dialog: {e}")
        _close_dialog(page)
        return ""


def _read_detail_from_view_dialog(page: Page) -> str:
    """Read report content from a view-mode dialog (plain text, not form).

    Must find content after a '详情' label.
    """
    try:
        dialog = page.locator('.el-dialog__wrapper:not([style*="display: none"]) .el-dialog').first
        if not dialog.is_visible():
            return ""

        text_selectors = [
            ".el-form-item:has-text('工作内容') .el-form-item__content",
            ".el-form-item:has-text('日志内容') .el-form-item__content",
            ".el-form-item:has-text('内容') .el-form-item__content",
            ".el-dialog__body .content",
            ".el-dialog__body .detail",
        ]

        for sel in text_selectors:
            el = dialog.locator(sel).first
            if el.count() > 0 and el.is_visible():
                text = (el.inner_text() or "").strip()
                if text and len(text) >= 5:
                    return text

                try:
                    val = (el.input_value() or "").strip()
                    if val and len(val) >= 5:
                        return val
                except Exception:
                    pass

        all_text = (dialog.locator(".el-dialog__body").inner_text() or "").strip()
        if not all_text:
            return ""

        lines = [l.strip() for l in all_text.split("\n") if l.strip()]
        detail_idx = -1
        for i, line in enumerate(lines):
            if line in ("详情", "工作内容", "日志内容", "内容", "工作详情"):
                detail_idx = i
                break

        if detail_idx < 0 or detail_idx + 1 >= len(lines):
            return ""

        content_lines = lines[detail_idx + 1:]
        result_lines = []
        for line in content_lines:
            if len(line) < 1:
                continue
            if line.startswith(("项目名称", "项目", "日期", "开始日期", "结束日期",
                                 "工时", "出差", "类型", "状态", "审核",
                                 "提交人", "创建人", "创建时间", "更新时间",
                                 "所属部门", "员工", "部门")):
                continue
            result_lines.append(line)

        if result_lines:
            result = "\n".join(result_lines).strip()
            if len(result) >= 5:
                return result

        return ""
    except Exception as e:
        logger.debug(f"Failed to read from view dialog: {e}")
        return ""


def _navigate_and_click_edit(page: Page, target: TargetConfig):
    """Navigate to work log page and click the edit button on the first (today's) entry."""
    report_url = target.url.rstrip("/") + REPORT_PAGE
    page.goto(report_url, timeout=target.page_timeout)
    try:
        page.wait_for_selector("table, .el-table", timeout=30000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(3000)

    # Click the "修改" button on the first row (today's entry)
    page.click("table tbody tr:first-child button:has-text('修改'), .el-table__body tr:first-child button:has-text('修改')")
    page.wait_for_timeout(2000)
    logger.info("Edit dialog opened")


def _navigate_and_open_dialog(page: Page, target: TargetConfig):
    report_url = target.url.rstrip("/") + REPORT_PAGE
    logger.info(f"Navigating to: {report_url}")
    page.goto(report_url, timeout=target.page_timeout)

    # Wait for the page to fully load (table appears)
    try:
        page.wait_for_selector("table, .el-table", timeout=30000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(3000)

    # Click add button
    page.click(ADD_BTN)
    logger.info("Dialog opened")
    page.wait_for_timeout(2000)


def _fill_report_form(page: Page, content: str, target: TargetConfig, report_date: str = None):
    _select_project(page, target)
    _set_dates(page, report_date)
    _set_travel_no(page)
    _fill_detail(page, content)


def _set_dates(page: Page, report_date: str = None):
    """Set start and end date."""
    if report_date is None:
        return

    date_inputs = page.query_selector_all('.el-dialog .el-date-editor input[type="text"]')
    if len(date_inputs) >= 2:
        start_input = date_inputs[0]
        end_input = date_inputs[1]
    else:
        start_input = page.query_selector(FORM_SELECTORS["date_start"])
        end_input = page.query_selector(FORM_SELECTORS["date_end"])

    if not start_input or not end_input:
        raise TargetError("Date inputs not found in dialog")

    start_val = (start_input.input_value() or "").strip().replace("/", "-")
    end_val = (end_input.input_value() or "").strip().replace("/", "-")
    if start_val == report_date and end_val == report_date:
        logger.info(f"Date already set to {report_date}, skipping")
        return

    for label, input_box in (("Start", start_input), ("End", end_input)):
        try:
            input_box.click()
            page.wait_for_timeout(300)
        except Exception:
            try:
                input_box.evaluate("el => el.click()")
                page.wait_for_timeout(300)
            except Exception:
                pass
        try:
            input_box.fill("")
            page.wait_for_timeout(200)
            input_box.type(report_date, delay=50)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
        except Exception as e:
            current_val = (input_box.input_value() or "").strip().replace("/", "-")
            if current_val == report_date:
                logger.info(f"{label} date already correct after fill failure: {report_date}")
                continue
            raise TargetError(f"{label} date set failed: {e}")
        value = input_box.input_value().strip().replace("/", "-")
        if value != report_date:
            raise TargetError(f"{label} date was not set correctly: expected {report_date}, got {value or '-'}")
        logger.info(f"{label} date set to {report_date}")


def _set_travel_no(page: Page):
    """Set 出差 radio to N (否)."""
    try:
        label = page.query_selector('.el-dialog .el-radio:has-text("否")')
        if label:
            label.click()
            page.wait_for_timeout(500)
            logger.info("Travel set to N")
    except Exception:
        pass


def _select_project(page: Page, target: TargetConfig):
    project_name = getattr(target, "default_project", None) or ""
    if not project_name:
        logger.info("No default_project configured, skipping project selection")
        return

    try:
        page.wait_for_selector(FORM_SELECTORS["project_input"], timeout=10000)
    except PlaywrightTimeout:
        raise TargetError("Project selector not found in dialog")

    # Click the el-select to open dropdown
    page.click(FORM_SELECTORS["project_input"])
    page.wait_for_timeout(1000)

    # Type into the dropdown's filter input (el-select with filterable)
    search_input = page.query_selector('.el-select-dropdown .el-input__inner, .el-select-dropdown__wrap input')
    if search_input:
        search_input.fill(project_name)
    else:
        # Fallback: click on the select input again and use page.keyboard
        page.click(FORM_SELECTORS["project_input"])
        page.wait_for_timeout(500)
        page.keyboard.type(project_name)

    page.wait_for_timeout(2000)

    # Click the matching dropdown option
    selected = page.evaluate('''(name) => {
        const opts = document.querySelectorAll('.el-select-dropdown__item');
        for (const o of opts) {
            const text = (o.textContent || '').trim();
            if (text && text.includes(name)) { o.click(); return text; }
        }
        return "";
    }''', project_name)
    if not selected:
        raise TargetError(f"Project option not found: {project_name}")
    page.wait_for_timeout(1000)
    logger.info(f"Project selected: {selected}")


def _fill_detail(page: Page, content: str):
    page.wait_for_selector(FORM_SELECTORS["detail"], timeout=10000)
    page.fill(FORM_SELECTORS["detail"], content)
    logger.info("Report content filled")


def _submit_dialog(page: Page):
    try:
        page.click(DIALOG_SUBMIT, timeout=10000)
    except (PlaywrightTimeout, Exception):
        # Fallback: click the visible confirm button via JS
        clicked = page.evaluate('''() => {
            const wrappers = document.querySelectorAll('.el-dialog__wrapper');
            for (const w of wrappers) {
                if (w.style.display === 'none') continue;
                const btns = w.querySelectorAll('button');
                for (const b of btns) {
                    const t = (b.textContent || '').trim().replace(/\\s+/g, '');
                    if (t === '确定') { b.click(); return true; }
                }
            }
            return false;
        }''')
        if not clicked:
            raise TargetError("Submit confirm button not found")

    page.wait_for_timeout(1000)
    error_text = _visible_error_text(page)
    if error_text:
        raise TargetError(f"Submit rejected: {error_text}")

    try:
        page.wait_for_function(
            """() => {
                const wrappers = Array.from(document.querySelectorAll('.el-dialog__wrapper'));
                return wrappers.every(w => {
                    const style = window.getComputedStyle(w);
                    return style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || 1) === 0;
                });
            }""",
            timeout=15000,
        )
    except PlaywrightTimeout:
        error_text = _visible_error_text(page)
        raise TargetError(f"Submit dialog did not close{': ' + error_text if error_text else ''}")

    page.wait_for_timeout(3000)
    logger.info("Dialog submitted")


def _visible_error_text(page: Page) -> str:
    return page.evaluate("""() => {
        const selectors = [
            '.el-message--error',
            '.el-notification.error',
            '.el-form-item__error',
        ];
        for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
                const style = window.getComputedStyle(node);
                const text = (node.textContent || '').trim();
                if (text && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0) {
                    return text;
                }
            }
        }
        return '';
    }""")


def _extract_row_date(text: str) -> str:
    match = DATE_PATTERN.search(text or "")
    if not match:
        return ""
    try:
        return date.fromisoformat(match.group(0).replace("/", "-")).isoformat()
    except ValueError:
        return ""


def _read_detail_from_dialog(page: Page) -> str:
    try:
        page.wait_for_selector(FORM_SELECTORS["detail"], timeout=10000)
    except PlaywrightTimeout:
        return ""
    return (page.locator(FORM_SELECTORS["detail"]).first.input_value() or "").strip()


def _close_dialog(page: Page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass


def _collect_all_rows(page: Page) -> tuple[list, int]:
    """Collect all report rows by paginating through all pages.
    Returns (report_data_list, total_pages)
    Each item in report_data_list is a dict with keys:
      date_str, text, has_delete_btn, has_modify_btn
    """
    all_data = []
    seen_dates = set()
    current_page = 1
    max_pages = 12
    no_new_data_pages = 0
    last_page_num = 0

    while current_page <= max_pages:
        try:
            page.wait_for_selector("table tbody tr, .el-table__body tr", timeout=8000)
            page.wait_for_timeout(800)
        except PlaywrightTimeout:
            break

        page_num = _get_current_page(page)
        if page_num == last_page_num and page_num > 1:
            break
        last_page_num = page_num

        rows = page.query_selector_all("table tbody tr, .el-table__body tr")
        new_on_page = 0
        for row in rows:
            try:
                text = row.inner_text()
                row_date = _extract_row_date(text)
                if row_date and row_date not in seen_dates:
                    seen_dates.add(row_date)
                    has_delete = row.query_selector("button:has-text('删除')") is not None
                    has_modify = row.query_selector("button:has-text('修改')") is not None
                    all_data.append({
                        "date_str": row_date,
                        "text": text,
                        "has_delete_btn": has_delete,
                        "has_modify_btn": has_modify,
                    })
                    new_on_page += 1
            except Exception:
                continue

        if new_on_page == 0:
            no_new_data_pages += 1
            if no_new_data_pages >= 2:
                break
        else:
            no_new_data_pages = 0

        has_next = _go_to_next_page(page)
        if not has_next:
            break

        current_page += 1
        try:
            page.wait_for_timeout(600)
            page.wait_for_selector("table tbody tr, .el-table__body tr", timeout=8000)
            page.wait_for_timeout(400)
        except PlaywrightTimeout:
            break

    logger.info(f"Collected {len(all_data)} unique report entries from {current_page} pages")
    return all_data, current_page


def _get_current_page(page: Page) -> int:
    """Get current active page number from pagination."""
    try:
        active = page.query_selector(".el-pager li.is-active, .pagination .active, .el-pagination .is-active")
        if active:
            text = active.inner_text().strip()
            if text.isdigit():
                return int(text)
    except Exception:
        pass
    return 1


def _go_to_next_page(page: Page) -> bool:
    """Try to click next page button. Returns True if clicked, False if no more pages."""
    try:
        next_btn = page.query_selector(
            "button.btn-next, .el-pagination .btn-next, .el-pagination button.btn-next"
        )
        if not next_btn:
            btns = page.query_selector_all(".el-pagination button")
            for b in btns:
                if "下一页" in b.inner_text() or "Next" in b.inner_text():
                    next_btn = b
                    break

        if not next_btn:
            return False

        class_attr = next_btn.get_attribute("class") or ""
        if "disabled" in class_attr or "is-disabled" in class_attr:
            return False

        try:
            next_btn.scroll_into_view_if_needed(timeout=2000)
            next_btn.click(timeout=3000)
            return True
        except Exception:
            try:
                page.evaluate("(btn) => btn.click()", next_btn)
                return True
            except Exception:
                return False
    except Exception:
        return False


def _login_and_navigate(cfg: Config):
    target = cfg.target
    token = login_with_captcha(target.url, target.username, target.password, cfg.captcha)
    domain = _domain_from_url(target.url)

    browser = None
    context = None
    page = None
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        context.add_cookies([
            {"name": "DQMS-Token", "value": token, "domain": domain, "path": "/"},
            {"name": "LoginModeKey", "value": "1", "domain": domain, "path": "/"},
        ])
        page = context.new_page()
        page.set_default_timeout(target.element_timeout)

        report_url = target.url.rstrip("/") + REPORT_PAGE
        logger.info(f"Navigating to: {report_url}")
        page.goto(report_url, timeout=target.page_timeout)
        try:
            page.wait_for_selector("table, .el-table", timeout=60000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(5000)
        return pw, browser, context, page
    except Exception:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        pw.stop()
        raise


def get_report_status(cfg: Config, report_date: str = None) -> dict:
    """Get report status for a given date.
    Returns dict with keys: exists (bool), date (str), status (str),
    content (str), project (str), approved (bool)
    """
    actual_date = _normalize_report_date(report_date)
    pw, browser, context, page = _login_and_navigate(cfg)
    try:
        row = _find_existing_report_row(page, actual_date)
        if not row:
            return {"exists": False, "date": actual_date, "status": "未提交",
                    "content": "", "project": "", "approved": False}

        text = row.inner_text()
        has_delete_btn = row.query_selector("button:has-text('删除')") is not None
        has_modify_btn = row.query_selector("button:has-text('修改')") is not None
        approved = not has_delete_btn and not has_modify_btn

        content = ""
        try:
            content = _read_content_by_expand_row(page, row)
        except Exception:
            pass
        if not content:
            try:
                content = _read_content_from_row(row)
            except Exception:
                pass
        if not content:
            try:
                content = _read_content_by_open_dialog(page, row)
            except Exception:
                pass

        project_match = re.search(r"项目[：:]\s*(\S+)", text)
        project = project_match.group(1) if project_match else ""

        status_text = "已审核" if approved else ("待审核" if has_delete_btn else "已提交")

        return {
            "exists": True,
            "date": actual_date,
            "status": status_text,
            "content": content,
            "project": project,
            "approved": approved,
        }
    finally:
        browser.close()
        pw.stop()


def delete_daily_report(cfg: Config, report_date: str = None) -> tuple[bool, str]:
    """Delete report for a given date.
    Returns (success, message)
    """
    actual_date = _normalize_report_date(report_date)
    pw, browser, context, page = _login_and_navigate(cfg)
    try:
        row = _find_existing_report_row(page, actual_date)
        if not row:
            return True, f"{actual_date} 没有日报，无需删除"

        result = _delete_existing_report(page, actual_date)
        if result:
            return True, f"{actual_date} 日报已成功删除"
        else:
            return False, f"{actual_date} 日报已审核，无法删除"
    except Exception as e:
        logger.error(f"Delete report failed: {e}", exc_info=True)
        return False, f"删除失败: {e}"
    finally:
        browser.close()
        pw.stop()


def get_recent_reports(cfg: Config, count: int = 5) -> list[dict]:
    """Get recent N reports from OA system.
    Returns list of dicts with keys: date, status, project, content_preview
    """
    pw, browser, context, page = _login_and_navigate(cfg)
    try:
        try:
            page.wait_for_selector("table tbody tr, .el-table__body tr", timeout=30000)
            page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            return []

        rows = page.query_selector_all("table tbody tr, .el-table__body tr")
        reports = []
        for row in rows:
            if len(reports) >= count:
                break
            text = row.inner_text()
            row_date = _extract_row_date(text)
            if not row_date:
                continue

            has_delete_btn = row.query_selector("button:has-text('删除')") is not None
            approved = not has_delete_btn and row.query_selector("button:has-text('修改')") is None

            content = ""
            try:
                content = _read_content_from_row(row)
            except Exception:
                pass

            project_match = re.search(r"项目[：:]\s*(\S+)", text)
            project = project_match.group(1) if project_match else ""

            status_text = "已审核" if approved else "已提交"

            reports.append({
                "date": row_date,
                "status": status_text,
                "project": project,
                "content_preview": content[:50] + "..." if len(content) > 50 else content,
            })

        return reports
    finally:
        browser.close()
        pw.stop()


def get_monthly_statistics(cfg: Config, year: int = None, month: int = None) -> dict:
    """Get monthly report statistics.
    Returns dict with keys: total_days, work_days, submitted_days, missing_days,
    approved_days, pending_days, submitted_dates
    """
    from src.beijing_time import today as beijing_today
    from calendar import monthrange
    from src.workday_calendar import get_calendar

    today = beijing_today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    _, last_day = monthrange(year, month)

    pw, browser, context, page = _login_and_navigate(cfg)
    try:
        all_rows, total_pages = _collect_all_rows(page)
        logger.info(f"Collected {len(all_rows)} unique rows from {total_pages} pages")

        calendar = get_calendar()
        work_days = 0
        total_days = last_day
        submitted_dates = set()
        approved_count = 0
        pending_count = 0

        for day in range(1, last_day + 1):
            d = date(year, month, day)
            if calendar.is_workday(d):
                work_days += 1

        for item in all_rows:
            row_date_str = item["date_str"]
            if not row_date_str:
                continue
            try:
                row_date = date.fromisoformat(row_date_str)
            except ValueError:
                continue
            if row_date.year == year and row_date.month == month:
                submitted_dates.add(row_date_str)
                approved = not item["has_delete_btn"] and not item["has_modify_btn"]
                if approved:
                    approved_count += 1
                else:
                    pending_count += 1

        submitted_count = len(submitted_dates)
        missing_count = work_days - submitted_count
        if missing_count < 0:
            missing_count = 0

        return {
            "year": year,
            "month": month,
            "total_days": total_days,
            "work_days": work_days,
            "submitted_days": submitted_count,
            "approved_days": approved_count,
            "pending_days": pending_count,
            "missing_days": missing_count,
            "submitted_dates": sorted(submitted_dates),
        }
    finally:
        browser.close()
        pw.stop()


def get_weekly_statistics(cfg: Config, year: int = None, month: int = None, day: int = None) -> dict:
    """Get weekly report statistics (Monday to Sunday).
    Returns dict with keys: year, week_num, start_date, end_date,
    work_days, submitted_days, approved_days, pending_days, missing_days, submitted_dates
    """
    from src.beijing_time import today as beijing_today
    from datetime import timedelta
    from src.workday_calendar import get_calendar

    today = beijing_today()
    if year is None or month is None or day is None:
        ref_date = today
    else:
        ref_date = date(year, month, day)

    weekday = ref_date.weekday()
    start_date = ref_date - timedelta(days=weekday)
    end_date = start_date + timedelta(days=6)
    week_num = ref_date.isocalendar()[1]

    pw, browser, context, page = _login_and_navigate(cfg)
    try:
        all_rows, total_pages = _collect_all_rows(page)
        logger.info(f"Collected {len(all_rows)} unique rows from {total_pages} pages")

        calendar = get_calendar()
        work_days = 0
        submitted_dates = set()
        approved_count = 0
        pending_count = 0

        for i in range(7):
            d = start_date + timedelta(days=i)
            if calendar.is_workday(d):
                work_days += 1

        for item in all_rows:
            row_date_str = item["date_str"]
            if not row_date_str:
                continue
            try:
                row_date = date.fromisoformat(row_date_str)
            except ValueError:
                continue
            if start_date <= row_date <= end_date:
                submitted_dates.add(row_date_str)
                approved = not item["has_delete_btn"] and not item["has_modify_btn"]
                if approved:
                    approved_count += 1
                else:
                    pending_count += 1

        submitted_count = len(submitted_dates)
        missing_count = work_days - submitted_count
        if missing_count < 0:
            missing_count = 0

        return {
            "year": ref_date.year,
            "week_num": week_num,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "work_days": work_days,
            "submitted_days": submitted_count,
            "approved_days": approved_count,
            "pending_days": pending_count,
            "missing_days": missing_count,
            "submitted_dates": sorted(submitted_dates),
        }
    finally:
        browser.close()
        pw.stop()
