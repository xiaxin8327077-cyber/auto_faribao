import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from src.config import Config

logger = logging.getLogger(__name__)

DOC_BASE = "https://doc.weixin.qq.com"


class CookiesError(Exception):
    pass


def check_cookies(cfg: Config) -> bool:
    """Check if smart sheet cookies are still valid.
    Returns True if valid, raises CookiesError if expired.
    """
    source = cfg.source
    doc_url = f"{DOC_BASE}/smartsheet/{source.doc_id}?scode={source.scode}&tab={source.tab_id}&viewId={source.view_id}"
    js_cookies = _build_cookies(source)

    logger.info("Checking smart sheet cookies...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        context.add_cookies(js_cookies)
        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            page.goto(doc_url, timeout=90000, wait_until="commit")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeout:
                logger.info("Document DOMContentLoaded did not finish; continuing with SDK wait")
            page.wait_for_timeout(5000)
            if "login" in page.url.lower():
                raise CookiesError("Cookies expired: redirected to login page")
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
                timeout=90000,
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

            page_text = page.inner_text("body")[:500]
            if "登录" in page_text or "login" in page_text.lower():
                raise CookiesError("Cookies expired: redirected to login page")
            if "无权限" in page_text or "没有权限" in page_text:
                raise CookiesError("Cookies expired: no permission to access document")

            raise CookiesError(f"Cookies may be expired: {result.get('reason', 'unknown')}")

        except PlaywrightTimeout:
            raise CookiesError("Page load timeout - network issue or cookies expired")
        except CookiesError:
            raise
        except Exception as e:
            raise CookiesError(f"Cookies check failed: {e}")
        finally:
            browser.close()


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
