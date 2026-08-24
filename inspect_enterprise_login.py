import os
import yaml
from playwright.sync_api import sync_playwright


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
        page.wait_for_timeout(10000)
        print("url:", page.url)
        print("title:", page.title())
        try:
            text = page.inner_text("body", timeout=5000)
            print("body text:", text[:1200])
        except Exception as e:
            print("text error:", e)

        selectors = [
            "img.wechat-qrcode",
            "iframe img.wechat-qrcode",
            "img[src^='data:image']",
            "canvas",
            "iframe",
            "[class*=qrcode]",
            "[class*=qr]",
            "[class*=code]",
        ]
        for selector in selectors:
            try:
                els = page.query_selector_all(selector)
                print(f"selector {selector}: {len(els)}")
                for i, el in enumerate(els[:10]):
                    print(i, "box", el.bounding_box(), "class", el.get_attribute("class"), "src", (el.get_attribute("src") or "")[:80])
                    try:
                        el.screenshot(path=f"enterprise_qr_{selector.replace('*','x').replace('[','_').replace(']','_').replace('=','_').replace('^','_').replace(':','_').replace(' ','_').replace('/','_')}_{i}.png", timeout=5000, animations="disabled")
                    except Exception as e:
                        print("screenshot error", e)
            except Exception as e:
                print(selector, "error", e)

        for idx, frame in enumerate(page.frames):
            print("frame", idx, frame.url)
            try:
                text = frame.inner_text("body", timeout=3000)
                print("frame text", text[:500])
                imgs = frame.query_selector_all("img")
                canvases = frame.query_selector_all("canvas")
                print("frame imgs", len(imgs), "canvases", len(canvases))
                for i, img in enumerate(imgs[:10]):
                    print(" frame img", i, img.bounding_box(), img.get_attribute("class"), (img.get_attribute("src") or "")[:100])
                    try:
                        img.screenshot(path=f"enterprise_frame_{idx}_img_{i}.png", timeout=5000, animations="disabled")
                    except Exception as e:
                        print(" frame img screenshot error", e)
            except Exception as e:
                print("frame inspect error", e)

        page.screenshot(path="enterprise_login_full.png", timeout=10000, animations="disabled")
        browser.close()

if __name__ == "__main__":
    main()
