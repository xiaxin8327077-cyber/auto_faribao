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
    """Parse cookie key=value pairs from email/wechat text.
    
    Supported formats:
    1. key=value; key2=value2;  (标准格式)
    2. key: value              (冒号格式)
    3. key value               (空格分隔)
    4. 浏览器完整Cookie字符串  (直接从DevTools复制)
    5. 带引号的值: key="value"
    6. 不区分大小写: TOK= / tok= / Tok=
    
    Examples:
        TOK=abc123; traceid=456;
        TOK: abc123
        TOK abc123
        TOK=abc123; traceid=456; hashkey=789  (只写部分也可以)
    """
    cookies = {}
    for field in COOKIE_FIELDS:
        patterns = [
            rf'{field}\s*[=:]\s*([^\s;,]+)',
            rf'{field}\s+([^\s;,]+)',
            rf'{field}\s*[=:]\s*"([^"]+)"',
            rf'{field}\s*[=:]\s*\'([^\']+)\'',
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


def update_cookies_from_wechat(config_path: str, text_content: str, cfg) -> tuple[bool, list, str]:
    new_cookies = parse_cookies_from_text(text_content)
    logger.info(f"Parsed {len(new_cookies)} cookie fields from wechat: {list(new_cookies.keys())}")

    if not new_cookies:
        return False, [], "未解析到有效的 Cookie 字段，请检查格式"

    updated_fields = _get_updated_fields(config_path, new_cookies)
    if not updated_fields:
        return True, [], "没有需要更新的 Cookie 字段（值与配置相同）"

    old_cookies = _get_current_cookie_values(config_path, updated_fields)
    if not update_config_cookies(config_path, new_cookies):
        return False, [], "更新配置文件失败"

    logger.info("Verifying new cookies...")
    from src.config import load_config
    from src.cookies_checker import check_cookies, CookiesError, CookiesNetworkError
    from src.wechat_notifier import notify_cookies_valid, notify_cookies_invalid

    new_cfg = load_config(config_path)
    try:
        check_cookies(new_cfg)
        logger.info("New cookies are valid!")
        notify_cookies_valid(new_cfg, updated_fields)
        return True, updated_fields, ""
    except CookiesNetworkError as e:
        logger.warning(f"New cookies verification encountered network error: {e}")
        return False, [], f"配置已写入但验证时网络超时，未同步运行时。请稍后检查或重试。\n错误：{e}"
    except CookiesError as e:
        logger.error(f"New cookies verification failed: {e}")
        _revert_config(config_path, old_cookies)
        notify_cookies_invalid(new_cfg, str(e))
        return False, [], f"Cookies无效，已回滚配置。\n错误：{e}"
    except Exception as e:
        logger.error(f"Verification error: {e}", exc_info=True)
        err_msg = str(e)[:100] + "..." if len(str(e)) > 100 else str(e)
        return True, updated_fields, f"配置已更新，但验证时出错（不影响使用）：{err_msg}"
