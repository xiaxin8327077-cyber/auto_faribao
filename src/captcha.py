import base64
import io
import logging
import re
import tempfile
import time
from pathlib import Path

from src.config import CaptchaConfig

logger = logging.getLogger(__name__)

CAPTCHA_TEMPLATE_FILE = Path("data/captcha/templates.json")
CAPTCHA_FAILED_DIR = Path("data/captcha/failed")


class CaptchaError(Exception):
    pass


def recognize_captcha(image_bytes: bytes, cfg: CaptchaConfig) -> str:
    solvers = [
        DdddOcrSolver(),
        DashScopeSolver(cfg),
        TemplateCaptchaSolver(),
    ]
    for solver in solvers:
        try:
            result = solver.solve(image_bytes)
            if result and len(result) == 4 and result.isdigit():
                logger.info(f"Captcha solved by {solver.__class__.__name__}: {result}")
                return result
        except Exception as e:
            logger.debug(f"{solver.__class__.__name__} failed: {e}")

    save_failed_captcha(image_bytes)
    raise CaptchaError("All captcha solvers failed")


class DdddOcrSolver:
    def __init__(self):
        self._ocr = None

    def solve(self, image_bytes: bytes):
        try:
            import ddddocr
        except ImportError:
            return None
        try:
            if self._ocr is None:
                self._ocr = ddddocr.DdddOcr(show_ad=False)
            raw = self._ocr.classification(image_bytes)
            return _normalize_digits(raw)
        except Exception:
            return None


class DashScopeSolver:
    def __init__(self, cfg: CaptchaConfig):
        self.cfg = cfg

    def solve(self, image_bytes: bytes):
        if not self.cfg.api_key:
            return None
        try:
            from openai import OpenAI
        except ImportError:
            return None

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/png;base64,{image_b64}"
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url)

        response = client.chat.completions.create(
            model=self.cfg.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.cfg.prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            max_tokens=10,
            temperature=0,
        )
        raw = (response.choices[0].message.content or "").strip()
        return _normalize_digits(raw)


class TemplateCaptchaSolver:
    def __init__(self, template_file: Path = CAPTCHA_TEMPLATE_FILE,
                 max_average_score: float = 20.0):
        self.template_file = template_file
        self.max_average_score = max_average_score

    def solve(self, image_bytes: bytes):
        templates = _load_templates(self.template_file)
        if not templates:
            return None
        samples = _split_captcha(image_bytes)
        if len(samples) != 4:
            return None
        digits = []
        scores = []
        for sample in samples:
            best_digit = None
            best_score = None
            for digit, prototypes in templates.items():
                for prototype in prototypes:
                    score = _hamming_distance(sample, prototype)
                    if best_score is None or score < best_score:
                        best_score = score
                        best_digit = digit
            if best_digit is None:
                return None
            scores.append(best_score or 0)
            digits.append(best_digit)
        if sum(scores) / len(scores) > self.max_average_score:
            return None
        return "".join(digits)


def _normalize_digits(text):
    if not text:
        return None
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    translations = str.maketrans({
        "O": "0", "o": "0", "Q": "0", "D": "0",
        "I": "1", "l": "1", "|": "1",
        "B": "8", "S": "5", "s": "5",
        "Z": "2", "z": "2",
    })
    digits = re.sub(r"\D", "", text.translate(translations))
    return digits[-4:] if len(digits) >= 4 else None


def _split_captcha(image_bytes: bytes):
    try:
        from PIL import Image
    except ImportError:
        return []

    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    width, height = image.size
    if width >= 120 and height >= 40:
        cropped = image.crop((
            int(width * 0.15), int(height * 0.08),
            int(width * 0.92), int(height * 0.95),
        ))
    else:
        dark_pixels = [
            (x, y) for y in range(height) for x in range(width)
            if image.getpixel((x, y)) < 180
        ]
        if not dark_pixels:
            return []
        min_x = max(0, min(x for x, _ in dark_pixels) - 2)
        max_x = min(width - 1, max(x for x, _ in dark_pixels) + 2)
        min_y = max(0, min(y for _, y in dark_pixels) - 2)
        max_y = min(height - 1, max(y for _, y in dark_pixels) + 2)
        cropped = image.crop((min_x, min_y, max_x + 1, max_y + 1))

    digit_width = cropped.size[0] / 4
    samples = []
    for index in range(4):
        left = int(index * digit_width)
        right = int((index + 1) * digit_width)
        part = cropped.crop((left, 0, right, cropped.size[1]))
        part = part.resize((18, 28))
        bits = ["1" if part.getpixel((x, y)) < 180 else "0"
                for y in range(28) for x in range(18)]
        samples.append("".join(bits))
    return samples


def _hamming_distance(left: str, right: str) -> int:
    size = min(len(left), len(right))
    return sum(1 for i in range(size) if left[i] != right[i]) + abs(len(left) - len(right))


def _load_templates(path: Path = CAPTCHA_TEMPLATE_FILE) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): [str(item) for item in v] for k, v in data.items()}


def save_failed_captcha(image_bytes: bytes):
    CAPTCHA_FAILED_DIR.mkdir(parents=True, exist_ok=True)
    path = CAPTCHA_FAILED_DIR / f"failed-{int(time.time())}.jpg"
    path.write_bytes(image_bytes)
    logger.info(f"Failed captcha saved to {path}")
