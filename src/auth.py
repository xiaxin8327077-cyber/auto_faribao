import logging
import requests
from src.config import CaptchaConfig
from src.captcha import recognize_captcha, CaptchaError

logger = logging.getLogger(__name__)

CAPTCHA_API = "/prod-api/captchaImage"
LOGIN_API = "/prod-api/login"


class AuthError(Exception):
    pass


def get_captcha(base_url: str, timeout: int = 15) -> tuple[str, str]:
    """Fetch captcha image and uuid from the API.

    Returns (uuid, image_base64).
    """
    url = base_url.rstrip("/") + CAPTCHA_API
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise AuthError(f"Failed to fetch captcha: {e}")
    except ValueError as e:
        raise AuthError(f"Invalid captcha response: {e}")

    uuid = data.get("uuid", "")
    img = data.get("img", "")
    if not uuid or not img:
        raise AuthError(f"Captcha response missing uuid or img: {list(data.keys())}")
    return uuid, img


def login(base_url: str, username: str, password: str,
          code: str, uuid: str, timeout: int = 15) -> str:
    """Submit login request. Returns the JWT token on success."""
    url = base_url.rstrip("/") + LOGIN_API
    payload = {
        "username": username,
        "password": password,
        "code": code,
        "uuid": uuid,
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        data = resp.json()
    except requests.RequestException as e:
        raise AuthError(f"Login request failed: {e}")
    except ValueError as e:
        raise AuthError(f"Invalid login response: {e}")

    token = data.get("token", "")
    if data.get("code") != 200 or not token:
        raise AuthError(f"Login rejected: {data.get('msg', 'unknown')}")
    logger.info("Login successful")
    return token


def login_with_captcha(base_url: str, username: str, password: str,
                       captcha_cfg: CaptchaConfig, max_retries: int = 3) -> str:
    """Full login flow with retry on captcha errors. Returns JWT token."""
    last_error = None

    for attempt in range(max_retries):
        uuid, img_b64 = get_captcha(base_url)

        img_bytes = __import__("base64").b64decode(img_b64)
        try:
            code = recognize_captcha(img_bytes, captcha_cfg)
        except CaptchaError as e:
            raise AuthError(f"Captcha recognition failed: {e}")

        if not code or len(code.strip()) != 4:
            raise AuthError(f"Captcha recognition returned invalid result: {code!r}")

        code = code.strip()
        logger.info(f"Captcha resolved: {code} (uuid={uuid[:8]}...), attempt {attempt + 1}/{max_retries}")

        try:
            return login(base_url, username, password, code, uuid)
        except AuthError as e:
            last_error = e
            if "验证码" in str(e):
                logger.warning(f"Captcha wrong, retrying ({attempt + 1}/{max_retries})")
                continue
            raise

    raise AuthError(
        f"验证码识别连续{max_retries}次失败，无法自动登录OA系统，请手动提交日报。"
        f"（最后一次错误: {last_error}）")
