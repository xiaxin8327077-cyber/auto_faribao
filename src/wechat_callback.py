import base64
import hashlib
import struct
import logging
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES

logger = logging.getLogger(__name__)


class WechatCallbackCrypto:
    def __init__(self, token: str, aes_key: str, corpid: str):
        self.token = token
        self.aes_key = aes_key
        self.corpid = corpid
        self.key = base64.b64decode(aes_key + "=")

    def _pkcs7_decode(self, text: bytes) -> bytes:
        pad = text[-1]
        return text[:-pad]

    def _decrypt(self, encrypt_msg: str) -> str:
        cryptor = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain_text = cryptor.decrypt(base64.b64decode(encrypt_msg))
        plain_text = self._pkcs7_decode(plain_text)
        content = plain_text[16:]
        xml_len = socket_to_int32(content[:4])
        xml_content = content[4:4 + xml_len].decode("utf-8")
        return xml_content

    def _get_signature(self, timestamp: str, nonce: str, encrypt_msg: str) -> str:
        sign_list = sorted([self.token, timestamp, nonce, encrypt_msg])
        sign_str = "".join(sign_list).encode("utf-8")
        sha1 = hashlib.sha1()
        sha1.update(sign_str)
        return sha1.hexdigest()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        sign = self._get_signature(timestamp, nonce, echostr)
        if sign != msg_signature:
            raise ValueError("Signature verify failed")
        return self._decrypt(echostr)

    def decrypt_message(self, msg_signature: str, timestamp: str, nonce: str, encrypt_msg: str) -> str:
        sign = self._get_signature(timestamp, nonce, encrypt_msg)
        if sign != msg_signature:
            raise ValueError("Signature verify failed")
        return self._decrypt(encrypt_msg)


def socket_to_int32(byte_str: bytes) -> int:
    return struct.unpack("!i", byte_str)[0]


def parse_message(xml_content: str) -> dict:
    root = ET.fromstring(xml_content)
    msg = {}
    for child in root:
        msg[child.tag] = child.text
    return msg


def extract_text_content(msg: dict) -> str:
    content = msg.get("Content", "") or ""
    content = content.strip()
    if content.startswith("<a"):
        return ""
    at_prefix = "@日报机器人"
    if content.startswith(at_prefix):
        content = content[len(at_prefix):].strip()
    return content


def is_cookies_update_message(content: str) -> bool:
    content_lower = content.lower()
    cookie_keywords = ["tok=", "traceid=", "hashkey=", "tdoc_uid=", 
                       "wedoc_openid=", "wedoc_sid=", "wedoc_sids=", 
                       "wedoc_skey=", "wedoc_ticket=", "fingerprint="]
    for keyword in cookie_keywords:
        if keyword in content_lower:
            return True
    return False
