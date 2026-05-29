import sys, os
sys.path.insert(0, "/home/ubuntu/daily_report")

date = sys.argv[1] if len(sys.argv) > 1 else None
from src.config import load_config
from src.report_builder import build_report_with_meta
from src.target import submit_daily_report
from src.email_notifier import notify_report_success, notify_report_failure

cfg = load_config("config.yaml")
try:
    report, source, meta = build_report_with_meta(cfg, report_date=date)
    print(f"Report source: {source}")
    print(f"Smart doc status: {meta.get('smart_doc_status', '')}")
    if meta.get("smart_doc_error"):
        print(f"Smart doc error: {meta.get('smart_doc_error')}")
    print(f"Extracted: {report[:200]}...")
except Exception as e:
    smart_doc_status = getattr(e, "smart_doc_status", "unknown")
    smart_doc_error = getattr(e, "smart_doc_error", "")
    print(f"Smart doc status: {smart_doc_status}")
    if smart_doc_error:
        print(f"Smart doc error: {smart_doc_error}")
    notify_report_failure(
        cfg,
        f"日报内容生成失败: {e}",
        report_date=date,
        report_source=getattr(e, "report_source", "generation_failed"),
        smart_doc_status=smart_doc_status,
        smart_doc_error=smart_doc_error,
    )
    print("FAILURE_EMAIL_SENT")
    print(f"FAILED: 日报内容生成失败: {e}")
    sys.exit(1)

success, msg, info = submit_daily_report(report, cfg, report_date=date)
if success:
    notify_report_success(
        cfg,
        report,
        info,
        report_date=date,
        report_source=source,
        smart_doc_status=meta.get("smart_doc_status"),
        smart_doc_error=meta.get("smart_doc_error"),
    )
    print(f"SUCCESS: {msg}")
else:
    notify_report_failure(
        cfg,
        msg,
        report_date=date,
        report_source=source,
        smart_doc_status=meta.get("smart_doc_status"),
        smart_doc_error=meta.get("smart_doc_error"),
    )
    print("FAILURE_EMAIL_SENT")
    print(f"FAILED: {msg}")
    sys.exit(1)
