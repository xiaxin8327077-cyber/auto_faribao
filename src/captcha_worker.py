"""验证码识别子进程 — 独立进程加载 dddocr,识别完退出释放内存。

被 captcha.py 的 DdddOcrSolver 调用。
从 stdin 读取图片字节,输出 4 位数字到 stdout。
"""
import sys


def main():
    image_bytes = sys.stdin.buffer.read()
    if not image_bytes:
        sys.exit(1)

    import ddddocr
    ocr = ddddocr.DdddOcr(show_ad=False)
    raw = ocr.classification(image_bytes)
    sys.stdout.write(raw or "")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
