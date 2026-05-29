#!/usr/bin/env python3
import argparse
import logging
import sys

from src.config import load_config, ConfigError
from src.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("daily_send.log"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG = "config.yaml"


def main():
    parser = argparse.ArgumentParser(description="日报自动提交服务")
    parser.add_argument(
        "--config", "-c", default=DEFAULT_CONFIG,
        help="配置文件路径 (默认: config.yaml)",
    )
    parser.add_argument(
        "--test-login", action="store_true",
        help="仅测试目标系统登录和验证码识别，不启动服务",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="填写表单但不实际提交，截图保存到 dry_run_form.png",
    )
    parser.add_argument(
        "--extract", action="store_true",
        help="从智能表格提取当日任务并打印",
    )
    parser.add_argument(
        "--submit", action="store_true",
        help="实际提交日报到目标系统",
    )
    parser.add_argument(
        "--check-cookies", action="store_true",
        help="检查智能表格 Cookies 是否有效",
    )
    parser.add_argument(
        "--message", "-m", default="完成今日开发任务：1. 日报自动提交功能开发与测试 2. 验证码识别优化 3. 接口联调",
        help="日报内容（dry-run 或 submit 模式下使用）",
    )
    parser.add_argument(
        "--date", "-d", default=None,
        help="指定日报日期 (格式: 2026-05-29)，不指定则使用当天",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        logger.error(f"Config error: {e}")
        return 1

    logger.info(f"Target: {cfg.target.url}")
    logger.info(f"Captcha model: {cfg.captcha.model}")
    logger.info(f"Email: {'configured' if cfg.email.sender else 'NOT configured'}")

    if args.test_login:
        return _test_login(cfg)

    if args.extract:
        return _test_extract(cfg)

    if args.check_cookies:
        return _check_cookies(cfg)

    if args.submit:
        return _submit_once(cfg, args.message, args.date)

    if args.dry_run:
        return _dry_run_report(cfg, args.message, args.date)

    # Start scheduler for cookies check (17:00) and auto-submit (17:30)
    from src.scheduler import start as start_scheduler
    start_scheduler(cfg)

    app = create_app(cfg)
    logger.info(f"Starting server on {cfg.host}:{cfg.port}")
    app.run(host=cfg.host, port=cfg.port, debug=False)


def _test_login(cfg) -> int:
    """Test login and captcha flow without submitting a report."""
    from urllib.parse import urlparse
    from playwright.sync_api import sync_playwright
    from src.auth import login_with_captcha, AuthError

    logger.info("=== Testing login flow ===")
    target = cfg.target

    try:
        token = login_with_captcha(
            target.url, target.username, target.password, cfg.captcha,
        )
    except AuthError as e:
        logger.error(f"Login failed: {e}")
        return 1

    logger.info("Login successful, verifying redirect...")
    domain = urlparse(target.url).hostname or ""

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
            index_url = target.url.rstrip("/") + "/index"
            page.goto(index_url, timeout=target.page_timeout)
            page.wait_for_timeout(3000)
            page.screenshot(path="login_result.png")
            logger.info("Login result screenshot saved to login_result.png")

            if "login" in page.url.lower():
                logger.error("Login appears to have failed - redirected back to login")
                return 1

            logger.info("Login test completed successfully")
            return 0
        except Exception as e:
            logger.error(f"Browser verification failed: {e}")
            return 1
        finally:
            browser.close()


def _test_extract(cfg) -> int:
    from src.extractor import extract_tasks
    from src.processor import format_report

    logger.info("=== Testing smart sheet extraction ===")
    try:
        tasks = extract_tasks(cfg.source)
        logger.info(f"Extracted {len(tasks)} tasks:")
        for t in tasks:
            logger.info(f"  - {t}")
        report = format_report(tasks)
        logger.info(f"Report:\n{report}")
        return 0
    except Exception as e:
        logger.error(f"Extraction failed: {e}", exc_info=True)
        return 1


def _check_cookies(cfg) -> int:
    from src.cookies_checker import check_cookies, CookiesError
    from src.email_notifier import notify_cookies_expired

    logger.info("=== Checking smart sheet cookies ===")
    try:
        check_cookies(cfg)
        logger.info("Cookies are valid")
        return 0
    except CookiesError as e:
        logger.error(f"Cookies check failed: {e}")
        notify_cookies_expired(cfg, str(e))
        return 1
    except Exception as e:
        logger.error(f"Cookies check error: {e}", exc_info=True)
        notify_cookies_expired(cfg, str(e))
        return 1


def _dry_run_report(cfg, message: str, report_date: str = None) -> int:
    """Fill the report form and screenshot without submitting."""
    from src.target import submit_daily_report

    logger.info("=== Dry-run: filling report form ===")
    logger.info(f"Message: {message}")
    if report_date:
        logger.info(f"Date: {report_date}")

    success, msg, _ = submit_daily_report(message, cfg, dry_run=True, report_date=report_date)
    if success:
        logger.info(f"Dry-run OK: {msg}")
    else:
        logger.error(f"Dry-run failed: {msg}")
    return 0 if success else 1


def _submit_once(cfg, message: str, report_date: str = None) -> int:
    from src.target import submit_daily_report
    from src.email_notifier import notify_report_success, notify_report_failure

    logger.info("=== Submitting daily report ===")
    logger.info(f"Message: {message}")
    if report_date:
        logger.info(f"Date: {report_date}")

    success, msg, report_info = submit_daily_report(message, cfg, dry_run=False, report_date=report_date)
    if success:
        logger.info(f"Submit OK: {msg}")
        notify_report_success(
            cfg,
            message,
            report_info,
            report_date=report_date,
            report_source="manual",
            smart_doc_status="not_used",
        )
    else:
        logger.error(f"Submit failed: {msg}")
        notify_report_failure(
            cfg,
            msg,
            report_date=report_date,
            report_source="manual",
            smart_doc_status="not_used",
        )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
