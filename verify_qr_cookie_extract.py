import os
import time
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

COOKIE_FIELDS = [
    "TOK", "traceid", "hashkey", "tdoc_uid", "wedoc_openid", "wedoc_sid",
    "wedoc_sids", "wedoc_skey", "wedoc_ticket", "language", "fingerprint",
]


def main():
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    source = cfg["source"]
    doc_url = f"https://doc.weixin.qq.com/smartsheet/{source['doc_id']}?scode={source['scode']}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1024}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(10000)
        page.goto(doc_url, timeout=30000, wait_until="commit")
        page.wait_for_timeout(5000)

        try:
            page.get_by_text("切换个人身份登录").first.click(timeout=5000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print("switch failed:", e)

        qr = page.locator("img.wechat-qrcode").first
        if qr.count() == 0:
            raise RuntimeError("未找到二维码 img.wechat-qrcode")
        src = qr.get_attribute("src")
        print("QR src prefix:", src[:60] if src else None)
        qr.screenshot(path="qr_test.png", timeout=10000, animations="disabled")
        print("二维码已保存到 /home/ubuntu/daily_report/qr_test.png")
        print("等待扫码登录，最多 120 秒...")

        deadline = time.time() + 120
        success = False
        last_url = page.url
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            url = page.url
            if url != last_url:
                print("url changed:", url)
                last_url = url
            text = ""
            try:
                text = page.inner_text("body", timeout=2000)
            except Exception:
                pass
            if "企业身份登录" not in text and "个人身份登录" not in text and "请使用微信扫描二维码登录" not in text:
                success = True
                break
            if "login" not in url.lower() and "doc.weixin.qq.com/smartsheet" in url:
                try:
                    page.wait_for_function(
                        """() => !!(window.ContainerApp && window.ContainerApp.containerSdk)""",
                        timeout=5000,
                    )
                    success = True
                    break
                except Exception:
                    pass

        cookies = context.cookies()
        matched = {c["name"]: c["value"] for c in cookies if c.get("name") in COOKIE_FIELDS}
        print("success:", success)
        print("matched fields:", sorted(matched.keys()))
        for k, v in matched.items():
            print(f"{k}={v[:80]}")
        browser.close()

if __name__ == "__main__":
    main()
