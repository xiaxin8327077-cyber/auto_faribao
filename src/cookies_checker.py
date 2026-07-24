import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from src.browser_lock import browser_operation, launch_browser
from src.config import Config

logger = logging.getLogger(__name__)

DOC_BASE = "https://doc.weixin.qq.com"


class CookiesError(Exception):
    pass


class CookiesNetworkError(CookiesError):
    pass


def check_cookies(cfg: Config) -> bool:
    """Check if smart sheet cookies are still valid.
    Returns True if valid, raises CookiesError if expired.
    Retries once on network timeout before raising CookiesNetworkError.
    """
    for attempt in range(2):
        try:
            return _check_cookies_once(cfg)
        except CookiesNetworkError:
            if attempt == 1:
                raise
            logger.warning("Smart sheet network check timed out; retrying once")
    return False


def _check_cookies_once(cfg: Config) -> bool:
    source = cfg.source
    doc_url = f"{DOC_BASE}/smartsheet/{source.doc_id}?scode={source.scode}&tab={source.tab_id}&viewId={source.view_id}"
    js_cookies = _build_cookies(source)

    logger.info("Checking smart sheet cookies...")

    try:
        with browser_operation():
            with sync_playwright() as p:
                browser = launch_browser(p)
                try:
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        locale="zh-CN",
                    )
                    context.add_cookies(js_cookies)
                    page = context.new_page()
                    page.set_default_timeout(30000)

                    page.goto(doc_url, timeout=30000, wait_until="commit")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except PlaywrightTimeout:
                        logger.info("Document DOMContentLoaded did not finish; continuing with SDK wait")
                    page.wait_for_timeout(3000)

                    page_text = page.inner_text("body")[:500]
                    if "login" in page.url.lower() or "登录" in page_text:
                        raise CookiesError("Cookies expired: redirected to login page")
                    if "无权限" in page_text or "没有权限" in page_text:
                        raise CookiesError("Cookies expired: no permission to access document")

                    page.wait_for_function(
                        """() => {
                            return !!(
                                window.ContainerApp &&
                                window.ContainerApp.containerSdk &&
                                window.ContainerApp.containerSdk.smartSheetSdk &&
                                window.ContainerApp.containerSdk.smartSheetSdk.editor &&
                                window.ContainerApp.containerSdk.smartSheetSdk.editor.getCore
                            );
                        }""",
                        timeout=30000,
                    )

                    result = page.evaluate("""() => {
                        try {
                            const core = window.ContainerApp?.containerSdk?.smartSheetSdk?.editor?.getCore();
                            if (!core) return {ok: false, reason: 'SDK not loaded'};
                            const table = core.base?.getTableByTableId && core.base.getTableByTableId('__check__');
                            return {ok: true};
                        } catch(e) {
                            return {ok: false, reason: e.message || String(e)};
                        }
                    }""")

                    if result.get("ok"):
                        logger.info("Cookies are valid")
                        return True

                    reason = result.get('reason', 'unknown')
                    raise CookiesNetworkError(
                        f"Smart sheet SDK check failed: {reason}"
                    )

                except PlaywrightTimeout as exc:
                    raise CookiesNetworkError(
                        "Smart sheet page or SDK load timed out"
                    ) from exc
                except CookiesError:
                    raise
                except Exception as exc:
                    raise CookiesNetworkError(
                        f"Smart sheet access failed: {exc}"
                    ) from exc
                finally:
                    browser.close()
    except CookiesError:
        raise
    except CookiesNetworkError:
        raise
    except Exception as exc:
        raise CookiesNetworkError(f"Smart sheet browser or network error: {exc}") from exc


def _build_cookies(source) -> list[dict]:
    cookies = []
    domain = ".weixin.qq.com"
    for name in ["low_login_enable", "utype", "TOK", "traceid", "hashkey",
                 "tdoc_uid", "wedoc_openid", "wedoc_sid", "wedoc_sids",
                 "wedoc_skey", "wedoc_ticket", "language", "fingerprint"]:
        val = getattr(source, name, None) or ""
        if val:
            cookies.append({"name": name, "value": val, "domain": domain, "path": "/"})
    return cookies
