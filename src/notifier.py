import logging

from src.wechat_notifier import (
    notify_report_success as wechat_notify_report_success,
    notify_report_failure as wechat_notify_report_failure,
    notify_cookies_expired as wechat_notify_cookies_expired,
    notify_cookies_valid as wechat_notify_cookies_valid,
    notify_cookies_invalid as wechat_notify_cookies_invalid,
)

logger = logging.getLogger(__name__)


def _has_wechat_config(cfg) -> bool:
    return hasattr(cfg, "wechat") and cfg.wechat and cfg.wechat.corpid and cfg.wechat.corpsecret


def notify_report_success(cfg, content: str, report_info: dict = None, report_date: str = None,
                          report_source: str = None, smart_doc_status: str = None,
                          smart_doc_error: str = None):
    if _has_wechat_config(cfg):
        wechat_notify_report_success(cfg, content, report_info, report_date,
                                     report_source, smart_doc_status, smart_doc_error)


def notify_report_failure(cfg, error: str, report_date: str = None, report_source: str = None,
                          smart_doc_status: str = None, smart_doc_error: str = None,
                          screenshot: str = None):
    if _has_wechat_config(cfg):
        wechat_notify_report_failure(cfg, error, report_date, report_source,
                                     smart_doc_status, smart_doc_error,
                                     screenshot=screenshot)


def notify_cookies_expired(cfg, error: str):
    if _has_wechat_config(cfg):
        wechat_notify_cookies_expired(cfg, error)


def notify_cookies_valid(cfg, updated_fields: list):
    if _has_wechat_config(cfg):
        wechat_notify_cookies_valid(cfg, updated_fields)


def notify_cookies_invalid(cfg, error: str):
    if _has_wechat_config(cfg):
        wechat_notify_cookies_invalid(cfg, error)
