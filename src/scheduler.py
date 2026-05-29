import logging
import threading
import time
from src.beijing_time import now as beijing_now, today

logger = logging.getLogger(__name__)

COOKIE_CHECK_HOUR = 17
COOKIE_CHECK_MINUTE = 0
REPORT_SUBMIT_HOUR = 17
REPORT_SUBMIT_MINUTE = 30
COOKIES_EMAIL_CHECK_INTERVAL = 300


def start(cfg):
    thread = threading.Thread(
        target=_run, args=(cfg,), daemon=True, name="scheduler"
    )
    thread.start()
    logger.info(
        f"Scheduler started: cookies check at {COOKIE_CHECK_HOUR:02d}:{COOKIE_CHECK_MINUTE:02d}, "
        f"report submit at {REPORT_SUBMIT_HOUR:02d}:{REPORT_SUBMIT_MINUTE:02d} on weekdays"
    )


def _run(cfg):
    last_cookies_date = None
    last_submit_date = None
    last_email_check = 0

    while True:
        now = beijing_now()
        today_str = now.strftime("%Y-%m-%d")
        now_ts = time.time()

        if _is_workday(now.date()):
            if (now.hour == COOKIE_CHECK_HOUR and
                    now.minute == COOKIE_CHECK_MINUTE and
                    last_cookies_date != today_str):
                last_cookies_date = today_str
                logger.info("Scheduler triggered: cookies check")
                _run_cookies_check(cfg)

            if (now.hour == REPORT_SUBMIT_HOUR and
                    now.minute == REPORT_SUBMIT_MINUTE and
                    last_submit_date != today_str):
                last_submit_date = today_str
                logger.info("Scheduler triggered: report auto-submit")
                _run_auto_submit(cfg)

        if now_ts - last_email_check >= COOKIES_EMAIL_CHECK_INTERVAL:
            last_email_check = now_ts
            _run_cookies_email_check(cfg)

        time.sleep(30)


def _is_workday(day):
    from src.workday_calendar import is_workday
    return is_workday(day)


def _run_cookies_check(cfg):
    from src.cookies_checker import check_cookies, CookiesError
    from src.extractor import extract_tasks, ExtractError
    from src.email_notifier import notify_cookies_expired

    try:
        check_cookies(cfg)
        logger.info("Cookies check passed")
    except CookiesError as e:
        logger.error(f"Cookies check failed: {e}")
        notify_cookies_expired(cfg, f"Cookies 过期或无效: {e}")
        return
    except Exception as e:
        logger.error(f"Cookies check error: {e}", exc_info=True)
        notify_cookies_expired(cfg, f"Cookies 检查异常: {e}")
        return

    try:
        tasks = extract_tasks(cfg.source)
        if tasks:
            logger.info(f"Smart document read check passed: {len(tasks)} matching tasks")
        else:
            logger.info("Smart document read check passed, no matching tasks; no email sent")
    except ExtractError as e:
        logger.error(f"Smart document read failed: {e}")
        notify_cookies_expired(cfg, f"智能文档读取异常: {e}")
    except Exception as e:
        logger.error(f"Smart document read error: {e}", exc_info=True)
        notify_cookies_expired(cfg, f"智能文档读取异常: {e}")


def _run_auto_submit(cfg):
    from src.server import auto_submit_if_needed
    from src.email_notifier import notify_report_failure
    try:
        auto_submit_if_needed(cfg)
    except Exception as e:
        logger.error(f"Scheduler auto-submit failed: {e}", exc_info=True)
        notify_report_failure(cfg, str(e), report_source="unknown", smart_doc_status="unknown")


def _run_cookies_email_check(cfg):
    from src.auto_cookies_updater import try_update_cookies
    import os
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    try:
        updated = try_update_cookies(config_path)
        if updated:
            logger.info("Cookies auto-updated from email!")
    except Exception as e:
        logger.debug(f"Cookies email check: {e}")
