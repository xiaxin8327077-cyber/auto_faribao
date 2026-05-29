import logging

from src.extractor import extract_tasks
from src.processor import format_report
from src.target import get_previous_report_content

logger = logging.getLogger(__name__)


class ReportBuildError(ValueError):
    def __init__(self, message: str, smart_doc_status: str = "unknown", smart_doc_error: str = ""):
        super().__init__(message)
        self.report_source = "generation_failed"
        self.smart_doc_status = smart_doc_status
        self.smart_doc_error = smart_doc_error


def build_report_content(cfg, report_date: str = None) -> tuple[str, str]:
    """Build report content from smart sheet, falling back to previous OA report."""
    report, source, _ = build_report_with_meta(cfg, report_date=report_date)
    return report, source


def build_report_with_meta(cfg, report_date: str = None) -> tuple[str, str, dict]:
    """Build report content and return metadata used by email notifications."""
    smart_error = None
    try:
        tasks = extract_tasks(cfg.source)
        report = format_report(tasks)
        if report.strip():
            logger.info(f"Report content built from smart sheet: {len(tasks)} tasks")
            return report, "smart_sheet", {
                "report_source": "smart_sheet",
                "smart_doc_status": "normal",
                "smart_doc_error": "",
            }
        logger.warning("Smart sheet produced no matching tasks; falling back to previous OA report")
    except Exception as exc:
        smart_error = exc
        logger.warning("Smart sheet extraction failed; falling back to previous OA report: %s", exc)

    try:
        fallback = get_previous_report_content(cfg, before_date=report_date)
    except Exception as exc:
        if smart_error:
            raise ReportBuildError(
                f"智能文档获取失败，且上一次日报兜底失败。智能文档错误: {smart_error}; 上一次日报错误: {exc}",
                smart_doc_status="error",
                smart_doc_error=str(smart_error),
            ) from exc
        raise ReportBuildError(
            f"智能文档没有符合条件的数据，且上一次日报兜底失败: {exc}",
            smart_doc_status="normal",
            smart_doc_error="",
        ) from exc
    if not fallback.strip():
        if smart_error:
            raise ReportBuildError(
                f"智能文档获取失败，且上一次日报内容为空。智能文档错误: {smart_error}",
                smart_doc_status="error",
                smart_doc_error=str(smart_error),
            )
        raise ReportBuildError(
            "智能文档没有符合条件的数据，且上一次日报内容为空",
            smart_doc_status="normal",
            smart_doc_error="",
        )
    return fallback, "previous_report", {
        "report_source": "previous_report",
        "smart_doc_status": "error" if smart_error else "normal",
        "smart_doc_error": str(smart_error) if smart_error else "",
    }
