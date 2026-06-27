"""
调研智能文档二维码登录流程 (headless 版)
"""
import os
import yaml
from playwright.sync_api import sync_playwright

def main():
    # 加载配置
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    source = cfg["source"]

    doc_url = f"https://doc.weixin.qq.com/smartsheet/{source['doc_id']}?scode={source['scode']}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        
        # 先不加 Cookies（模拟过期）
        page = context.new_page()
        
        print(f"访问文档: {doc_url}")
        page.goto(doc_url, timeout=60000)
        
        print(f"当前URL:", page.url)
        
        # 等待页面加载
        page.wait_for_timeout(8000)
        
        # 截图整个页面
        page.screenshot(path="research_full.png", full_page=True)
        print("已保存全屏截图: research_full.png")
        
        # 打印页面标题
        print("\n页面标题:", page.title())
        
        # 查找二维码相关元素
        print("\n查找二维码元素...")
        
        qr_selectors = [
            "img[src*='qrcode']",
            "img[src*='qr']",
            "img[src*='QR']",
            ".qrcode",
            ".qr-code",
            "#qrcode",
            "[class*='qrcode']",
            "[class*='qr-code']",
            "canvas",
        ]
        
        found_qr = False
        for sel in qr_selectors:
            try:
                elem = page.wait_for_selector(sel, timeout=3000)
                if elem:
                    print(f"找到元素: {sel}")
                    safe_name = sel.replace('/', '_').replace('[', '_').replace(']', '_')
                    elem.screenshot(path=f"research_{safe_name}.png")
                    found_qr = True
            except:
                pass
        
        if not found_qr:
            print("未找到明显的二维码元素，截图页面所有图片")
            images = page.query_selector_all("img")
            print(f"页面共有 {len(images)} 个 img 元素")
            for i, img in enumerate(images[:10]):  # 只看前10个
                src = img.get_attribute("src")
                print(f"  图片 {i}: {src[:120] if src else '无src'}")
                if src and ("qrcode" in src.lower() or "qr" in src.lower()):
                    print(f"  -> 可能是二维码，截图中...")
                    img.screenshot(path=f"research_img_{i}.png")
        
        # 查找所有包含"二维码"、"扫码"、"登录"的文本
        page_text = page.inner_text("body")
        print("\n=== 页面文本关键词 ===")
        if "二维码" in page_text:
            print("✅ 找到了'二维码'文本")
        if "扫码" in page_text:
            print("✅ 找到了'扫码'文本")
        if "登录" in page_text:
            print("✅ 找到了'登录'文本")
        if "微信" in page_text:
            print("✅ 找到了'微信'文本")
        if "企业" in page_text:
            print("✅ 找到了'企业'文本")
        
        # 尝试截取页面中间部分（二维码通常在中间）
        print("\n截取页面中间区域...")
        page.screenshot(
            path="research_center.png",
            clip={"x": 500, "y": 100, "width": 920, "height": 800}
        )
        print("已保存中间区域截图: research_center.png")
        
        browser.close()
        print("\n调研完成！请查看生成的 png 截图文件！")

if __name__ == "__main__":
    main()
