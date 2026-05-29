import re
import yaml
import logging

logger = logging.getLogger(__name__)

COOKIE_FIELDS = [
    "TOK", "traceid", "hashkey", "tdoc_uid",
    "wedoc_openid", "wedoc_sid", "wedoc_sids",
    "wedoc_skey", "wedoc_ticket", "fingerprint",
]


def parse_cookies_from_text(text: str) -> dict:
    """Parse cookie key=value pairs from email text.
    Supports formats like:
        TOK=xxx; traceid=xxx; ...
        TOK: xxx
        TOK xxx
    """
    cookies = {}
    for field in COOKIE_FIELDS:
        patterns = [
            rf'{field}\s*[=:]\s*([^\s;,]+)',
            rf'{field}\s+([^\s;,]+)',
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                cookies[field] = m.group(1).strip().strip('"').strip("'")
                break
    return cookies


def update_config_cookies(config_path: str, new_cookies: dict) -> bool:
    """Update source cookies in config.yaml."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        source = data.get("source", {})
        updated_fields = []
        for field in COOKIE_FIELDS:
            if field in new_cookies and new_cookies[field]:
                old_val = source.get(field, "")
                new_val = new_cookies[field]
                if old_val != new_val:
                    source[field] = new_val
                    updated_fields.append(field)

        if not updated_fields:
            logger.info("No cookie fields changed")
            return False

        data["source"] = source
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(f"Updated cookies: {updated_fields}")
        return True

    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        return False


def try_update_cookies(config_path: str) -> bool:
    """Main entry: read email, parse cookies, update config, verify.
    Returns True if cookies were updated successfully.
    """
    from src.config import load_config
    from src.cookies_checker import check_cookies, CookiesError
    from src.email_reader import read_cookies_email
    from src.email_notifier import notify_cookies_valid, notify_cookies_invalid

    cfg = load_config(config_path)
    email_text = read_cookies_email(cfg)
    if not email_text:
        logger.info("No new cookies email found")
        return False

    new_cookies = parse_cookies_from_text(email_text)
    logger.info(f"Parsed cookies: {list(new_cookies.keys())}")

    if not new_cookies:
        logger.warning("No valid cookies found in email")
        return False

    updated_fields = _get_updated_fields(config_path, new_cookies)
    if not updated_fields:
        logger.info("No cookie fields changed")
        return False

    old_cookies = _get_current_cookie_values(config_path, updated_fields)
    if not update_config_cookies(config_path, new_cookies):
        return False

    logger.info("Verifying new cookies...")
    cfg = load_config(config_path)
    try:
        check_cookies(cfg)
        logger.info("New cookies are valid!")
        notify_cookies_valid(cfg, updated_fields)
        return True
    except CookiesError as e:
        logger.error(f"New cookies verification failed: {e}")
        _revert_config(config_path, old_cookies)
        notify_cookies_invalid(cfg, str(e))
        return False


def _get_updated_fields(config_path: str, new_cookies: dict) -> list:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        source = data.get("source", {})
        updated = []
        for field in COOKIE_FIELDS:
            if field in new_cookies and new_cookies[field]:
                if source.get(field, "") != new_cookies[field]:
                    updated.append(field)
        return updated
    except Exception:
        return []


def _get_current_cookie_values(config_path: str, fields: list) -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        source = data.get("source", {})
        return {field: source.get(field, "") for field in fields}
    except Exception:
        return {}


def _revert_config(config_path: str, old_cookies: dict):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        source = data.get("source", {})
        for field, value in old_cookies.items():
            source[field] = value
        data["source"] = source
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info("Config reverted after verification failure")
    except Exception as e:
        logger.error(f"Failed to revert config: {e}")
