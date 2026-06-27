import os
import yaml
from playwright.sync_api import sync_playwright

KEYWORDS = ["企业身份登录", "切换个人身份登录", "二维码", "扫码", "微信", "登录"]

def inspect(page, label):
    print(f"\n=== {label} ===")
    print("url:", page.url)
    try:
        print("title:", page.title())
    except Exception as e:
        print("title error:", e)
    try:
        text = page.inner_text("body", timeout=5000)
        print("body text:", text[:1000])
        print("keywords:", [k for k in KEYWORDS if k in text])
    except Exception as e:
        print("body text error:", e)

    print("frames:")
    for f in page.frames:
        print(" -", f.url)

    for selector in ["img", "canvas", "svg", "iframe", "button", "a", "input", "[role=button]", "[class*=qr]", "[class*=code]", "[class*=login]", "[class*=Login]", "[class*=qrcode]"]:
        try:
            els = page.query_selector_all(selector)
            print(f"selector {selector}: {len(els)}")
            for i, el in enumerate(els[:20]):
                try:
                    box = el.bounding_box()
                    txt = (el.inner_text(timeout=1000) if selector not in ["img", "canvas", "svg", "input"] else "")
                    attrs = {}
                    for attr in ["class", "id", "src", "href", "role", "type", "value", "style"]:
                        v = el.get_attribute(attr)
                        if v:
                            attrs[attr] = v[:160]
                    print(f"  {i}: box={box}, text={txt[:80]!r}, attrs={attrs}")
                except Exception as e:
                    print(f"  {i}: inspect error {e}")
        except Exception as e:
            print(f"selector {selector} error: {e}")


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
        page.set_default_timeout(8000)
        page.goto(doc_url, timeout=30000, wait_until="commit")
        page.wait_for_timeout(10000)
        inspect(page, "initial")

        for text in ["切换个人身份登录", "企业身份登录", "微信登录", "扫码登录"]:
            try:
                loc = page.get_by_text(text).first
                if loc.count() > 0:
                    print(f"\ntry click: {text}")
                    loc.click(timeout=5000)
                    page.wait_for_timeout(5000)
                    inspect(page, f"after click {text}")
            except Exception as e:
                print(f"click {text} failed: {e}")

        browser.close()

if __name__ == "__main__":
    main()
