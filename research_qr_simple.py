"""
调研智能文档二维码登录流程 (快速截图版)
"""
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
        
        print(f"访问文档: {doc_url}")
        page.goto(doc_url, timeout=30000, wait_until="commit")
        
        for i in range(3):
            page.wait_for_timeout(3000)
            print(f"步骤 {i}: 当前URL: {page.url}")
            try:
                page.screenshot(path=f"research_step_{i}.png", timeout=10000, animations="disabled")
                print(f"  截图已保存 research_step_{i}.png")
            except Exception as e:
                print(f"  截图失败: {e}")
            
            try:
                page_text = page.inner_text("body", timeout=3000)
                keywords = ["二维码", "扫码", "登录", "微信", "企业"]
                found = [k for k in keywords if k in page_text]
                print(f"  找到关键词: {found}")
                print(f"  页面文本前200字: {page_text[:200]}")
            except Exception as e:
                print(f"  读取文本失败: {e}")
        
        # 查找 canvas 和 img
        try:
            imgs = page.query_selector_all("img")
            canvases = page.query_selector_all("canvas")
            print(f"图片数量: {len(imgs)}, canvas数量: {len(canvases)}")
            for i, img in enumerate(imgs[:5]):
                print(f"img {i}: {img.get_attribute('src')[:100] if img.get_attribute('src') else '无src'}")
        except Exception as e:
            print(f"查找元素失败: {e}")
        
        browser.close()
        print("调研完成")

if __name__ == "__main__":
    main()
