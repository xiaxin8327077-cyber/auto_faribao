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


def _normalize_report_date(report_date: str = None) -> str:
    if not report_date:
        from src.beijing_time import today_str
        return today_str()
    try:
        return date.fromisoformat(str(report_date).strip().replace("/", "-")).isoformat()
    except ValueError:
        raise TargetError(f"日报日期格式不正确: {report_date}")


def _check_existing_report(page: Page, report_date: str) -> bool:
    """Check if a report already exists for the given date."""
    try:
        page.wait_for_selector("table tbody tr, .el-table__body tr", timeout=30000)
        page.wait_for_timeout(3000)
    except PlaywrightTimeout:
        logger.info("No table found, assuming no existing report")
        return False

    rows = page.query_selector_all("table tbody tr, .el-table__body tr")
    for row in rows:
        text = row.inner_text()
        if report_date in text or report_date.replace("-", "/") in text:
            logger.info(f"Found existing report for {report_date}")
            return True

    logger.info(f"No existing report found for {report_date}")
    return False


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

            if not dry_run and _check_existing_report(page, actual_report_date):
                return False, f"日报已提交，无法重复提交（日期：{actual_report_date}）", {}

            if dry_run:
                page.click(ADD_BTN)
                page.wait_for_timeout(2000)
                _fill_report_form(page, content, target, actual_report_date)
                page.screenshot(path="dry_run_form.png", full_page=True)
                logger.info("Dry-run: form filled, screenshot saved to dry_run_form.png")
                return True, "DRY RUN — 表单已填写（未提交），截图: dry_run_form.png", {}

            page.click(ADD_BTN)
            page.wait_for_timeout(2000)
            _fill_report_form(page, content, target, actual_report_date)
            _submit_dialog(page)
            return True, f"日报提交成功\n> {content[:200]}", {
                "project": target.default_project,
                "hours": WORK_HOURS,
                "travel": "否",
                "log_type": "实施日志",
                "content": content,
                "report_date": actual_report_date,
            }
        except TargetError as e:
            logger.error(f"Submit failed: {e}")
            return False, str(e), {}
        except PlaywrightTimeout as e:
            logger.error(f"Timeout: {e}")
            return False, f"页面操作超时: {e}", {}
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return False, f"未知错误: {e}", {}
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
    """Read the latest previous report content from OA for fallback submissions."""
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
                button = row.query_selector("button:has-text('修改'), button:has-text('查看'), button:has-text('详情')")
                if not button:
                    continue
                button.click()
                page.wait_for_timeout(2000)
                content = _read_detail_from_dialog(page)
                if content:
                    logger.info(f"Previous report content loaded from {row_date or 'latest row'}")
                    return content
                _close_dialog(page)

            raise TargetError("No previous report content found")
        finally:
            browser.close()


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

    for label, input_box in (("Start", start_input), ("End", end_input)):
        input_box.click()
        page.wait_for_timeout(300)
        input_box.fill("")
        page.wait_for_timeout(200)
        input_box.type(report_date, delay=50)
        page.wait_for_timeout(500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
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
            '.el-message-box__message'
        ];
        for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
                const style = window.getComputedStyle(node);
                const text = (node.textContent || '').trim();
                if (text && style.display !== 'none' && style.visibility !== 'hidden') {
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
