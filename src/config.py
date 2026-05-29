import os
import sys
import yaml


class ConfigError(Exception):
    pass


class SourceConfig:
    """Smart sheet data source configuration."""
    def __init__(self, data: dict):
        self.doc_id = data.get("doc_id", "")
        self.scode = data.get("scode", "")
        self.tab_id = data.get("tab_id", "tZdOxZ")
        self.view_id = data.get("view_id", "vtvpys")
        # Field mappings
        self.name_field = data.get("name_field", "任务名称")
        self.start_field = data.get("start_field", "启动时间")
        self.end_field = data.get("end_field", "预计完成时间")
        self.person_field = data.get("person_field", "负责人")
        self.status_field = data.get("status_field", "任务状态")
        self.desc_field = data.get("desc_field", "任务描述")
        self.max_desc_len = int(data.get("max_desc_len", 25))
        # Filter values
        self.person_names = data.get("person_names", ["刘非凡"])
        self.status_values = data.get("status_values", ["进行中", "已完成"])
        # Cookies
        self.low_login_enable = data.get("low_login_enable", "1")
        self.utype = data.get("utype", "ww")
        self.TOK = data.get("TOK", "")
        self.traceid = data.get("traceid", "")
        self.hashkey = data.get("hashkey", "")
        self.tdoc_uid = data.get("tdoc_uid", "")
        self.wedoc_openid = data.get("wedoc_openid", "")
        self.wedoc_sid = data.get("wedoc_sid", "")
        self.wedoc_sids = data.get("wedoc_sids", "")
        self.wedoc_skey = data.get("wedoc_skey", "")
        self.wedoc_ticket = data.get("wedoc_ticket", "")
        self.language = data.get("language", "zh-CN")
        self.fingerprint = data.get("fingerprint", "")


class TargetConfig:
    def __init__(self, data: dict):
        self.url = data.get("url", "")
        self.username = data.get("username", "")
        self.password = data.get("password", "")
        self.login_path = data.get("login_path", "/login")
        self.report_path = data.get("report_path", "/report")
        self.default_project = data.get("default_project", "")
        self.page_timeout = int(data.get("page_timeout", 30000))
        self.element_timeout = int(data.get("element_timeout", 10000))


class CaptchaConfig:
    def __init__(self, data: dict):
        self.api_key = data.get("api_key", "")
        self.base_url = data.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = data.get("model", "qwen3-vl-flash")
        self.prompt = data.get("prompt",
            "这是4位数字验证码，请仔细识别后只返回4位数字，不要其他内容。\n"
            "识别规则：长线先排除，字符分四区；贯穿线条是干扰，成形才算数字；"
            "闭圈辨0和8，带尾巴多是9；右弧常为3，斜竖多1或7；折角看作4，上弯下收是2").strip()


class EmailConfig:
    def __init__(self, data: dict):
        self.smtp_host = data.get("smtp_host", "")
        self.smtp_port = int(data.get("smtp_port", 465))
        self.sender = data.get("sender", "")
        self.password = data.get("password", "")
        self.recipient = data.get("recipient", "")
        self.imap_host = data.get("imap_host", "")
        self.imap_port = int(data.get("imap_port", 993))


class Config:
    def __init__(self, data: dict):
        self.source = SourceConfig(data.get("source", {}))
        self.target = TargetConfig(data.get("target", {}))
        self.captcha = CaptchaConfig(data.get("captcha", {}))
        self.email = EmailConfig(data.get("email", {}))
        self.host = data.get("host", "0.0.0.0")
        self.port = int(data.get("port", 8080))


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")

    file_mode = os.stat(path).st_mode & 0o777
    if file_mode != 0o600:
        print(
            f"Warning: {path} permissions are {oct(file_mode)}. "
            f"Consider running: chmod 600 {path}",
            file=sys.stderr,
        )

    with open(path, "r") as f:
        data = yaml.safe_load(f)

    if data is None:
        raise ConfigError(f"Config file is empty: {path}")

    return Config(data)
