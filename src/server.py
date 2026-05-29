import threading
import logging
from flask import Flask, request
from src.config import Config
from src.email_notifier import notify_report_success, notify_report_failure
from src.target import submit_daily_report

logger = logging.getLogger(__name__)


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def index():
        return _render_web_ui()

    @app.route("/api/submit", methods=["POST"])
    def api_submit():
        from flask import jsonify
        data = request.get_json(silent=True) or {}
        content = data.get("message", "")
        report_date = data.get("date", "").strip() or None

        if not content.strip():
            return jsonify({"success": False, "msg": "日报内容不能为空"}), 400

        thread = threading.Thread(
            target=_handle_manual_submit,
            args=(content, report_date, cfg),
            daemon=True,
        )
        thread.start()
        return jsonify({"success": True, "msg": "请求已接收，将在后台处理"})

    return app


def _handle_manual_submit(content: str, report_date: str, cfg: Config):
    try:
        success, msg, report_info = submit_daily_report(content, cfg, report_date=report_date)
        if success:
            notify_report_success(
                cfg,
                content,
                report_info,
                report_date=report_date,
                report_source="manual",
                smart_doc_status="not_used",
            )
        else:
            notify_report_failure(
                cfg,
                msg,
                report_date=report_date,
                report_source="manual",
                smart_doc_status="not_used",
            )
    except Exception as e:
        logger.error(f"Manual submit error: {e}", exc_info=True)
        notify_report_failure(
            cfg,
            f"网页手动提交异常: {e}",
            report_date=report_date,
            report_source="manual",
            smart_doc_status="not_used",
        )


def _render_web_ui():
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日报提交系统</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: Microsoft YaHei, sans-serif; background: #f0f2f5; min-height: 100vh; }}
.header {{ background: linear-gradient(135deg, #1890ff, #096dd9); color: white; padding: 24px 32px; }}
.header h1 {{ font-size: 22px; margin-bottom: 4px; }}
.header p {{ opacity: 0.85; font-size: 14px; }}
.container {{ max-width: 720px; margin: 24px auto; padding: 0 16px; }}
.card {{ background: white; border-radius: 8px; padding: 24px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.card h2 {{ font-size: 16px; margin-bottom: 16px; color: #333; border-left: 3px solid #1890ff; padding-left: 10px; }}
.form-group {{ margin-bottom: 16px; }}
.form-group label {{ display: block; margin-bottom: 6px; color: #555; font-size: 14px; }}
.form-group input, .form-group textarea {{ width: 100%; padding: 10px 12px; border: 1px solid #d9d9d9; border-radius: 4px; font-size: 14px; font-family: Microsoft YaHei, sans-serif; transition: border 0.2s; }}
.form-group input:focus, .form-group textarea:focus {{ border-color: #1890ff; outline: none; box-shadow: 0 0 0 2px rgba(24,144,255,0.2); }}
.form-group textarea {{ min-height: 150px; resize: vertical; }}
.btn {{ display: inline-block; padding: 10px 32px; border: none; border-radius: 4px; font-size: 15px; cursor: pointer; font-family: Microsoft YaHei, sans-serif; transition: all 0.2s; }}
.btn-primary {{ background: #1890ff; color: white; }}
.btn-primary:hover {{ background: #096dd9; }}
.btn-primary:disabled {{ background: #91d5ff; cursor: not-allowed; }}
.result {{ margin-top: 12px; padding: 12px; border-radius: 4px; display: none; font-size: 14px; }}
.result.success {{ display: block; background: #f6ffed; border: 1px solid #b7eb8f; color: #52c41a; }}
.result.error {{ display: block; background: #fff2f0; border: 1px solid #ffccc7; color: #ff4d4f; }}
.result.loading {{ display: flex; align-items: center; gap: 8px; background: #e6f7ff; border: 1px solid #91d5ff; color: #1890ff; }}
.spinner {{ display: inline-block; width: 16px; height: 16px; border: 2px solid #91d5ff; border-top-color: #1890ff; border-radius: 50%; animation: spin 0.6s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.quick-dates {{ display: flex; gap: 8px; margin-top: 4px; }}
.quick-date {{ padding: 4px 12px; border: 1px solid #d9d9d9; border-radius: 4px; background: white; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
.quick-date:hover {{ border-color: #1890ff; color: #1890ff; }}
.quick-date.active {{ background: #1890ff; color: white; border-color: #1890ff; }}
</style>
</head>
<body>
<div class="header">
  <h1>日报提交系统</h1>
  <p>OA 企业信息管理平台 — 自动填报服务</p>
</div>
<div class="container">
  <div class="card">
    <h2>填写日报</h2>
    <div class="form-group">
      <label>日报日期</label>
      <input type="date" id="reportDate" value="{today_str}">
      <div class="quick-dates">
        <button class="quick-date active" onclick="setQuickDate('{today_str}', this)">今天</button>
        <button class="quick-date" onclick="setQuickDate(getOffsetDate(-1), this)">昨天</button>
        <button class="quick-date" onclick="setQuickDate(getOffsetDate(-2), this)">前天</button>
        <button class="quick-date" onclick="setQuickDate(getLastFriday(), this)">上周五</button>
      </div>
    </div>
    <div class="form-group">
      <label>日报内容</label>
      <textarea id="reportContent" placeholder="请输入日报内容，每行一条...">1. 完成今日开发任务
2. 系统功能测试与验证</textarea>
    </div>
    <button class="btn btn-primary" id="submitBtn" onclick="submitReport()">提交日报</button>
    <div class="result" id="result"></div>
  </div>
</div>
<script>
function getOffsetDate(offset) {{
  const d = new Date(new Date().getTime() + offset * 86400000);
  return d.toISOString().split('T')[0];
}}
function getLastFriday() {{
  const d = new Date();
  const day = d.getDay();
  const diff = day <= 5 ? day + 5 : day - 2 + 7;
  d.setDate(d.getDate() - diff);
  return d.toISOString().split('T')[0];
}}
function setQuickDate(dateStr, el) {{
  document.getElementById('reportDate').value = dateStr;
  document.querySelectorAll('.quick-date').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}}
async function submitReport() {{
  const btn = document.getElementById('submitBtn');
  const result = document.getElementById('result');
  const content = document.getElementById('reportContent').value.trim();
  const date = document.getElementById('reportDate').value;
  if (!content) {{
    result.className = 'result loading';
    result.textContent = '请填写日报内容';
    return;
  }}
  btn.disabled = true;
  result.className = 'result loading';
  result.textContent = '提交中请稍候...';
  try {{
    const resp = await fetch('/api/submit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ message: content, date: date }})
    }});
    const data = await resp.json();
    if (data.success) {{
      result.className = 'result success';
      result.textContent = '请求已提交！系统将在后台处理，完成后会发送邮件通知。日期: ' + (date || '当天');
    }} else {{
      result.className = 'result error';
      result.textContent = '失败: ' + data.msg;
    }}
  }} catch(e) {{
    result.className = 'result error';
    result.textContent = '网络错误: ' + e.message;
  }}
  btn.disabled = false;
}}
</script>
</body>
</html>"""


def auto_submit_if_needed(cfg: Config):
    """Called by scheduler at 17:45. Auto-submits if no manual report was provided."""
    logger.info("No manual report today — extracting from smart sheet...")
    try:
        from src.report_builder import build_report_with_meta
        report, source, meta = build_report_with_meta(cfg)
        logger.info(f"Auto report content source: {source}")
    except Exception as e:
        logger.error(f"Auto-extraction failed: {e}")
        notify_report_failure(
            cfg,
            f"自动提取失败: {e}",
            report_source=getattr(e, "report_source", "generation_failed"),
            smart_doc_status=getattr(e, "smart_doc_status", "unknown"),
            smart_doc_error=getattr(e, "smart_doc_error", ""),
        )
        return

    _submit_and_notify(report, cfg, source, meta)


def _submit_and_notify(content: str, cfg: Config, report_source: str = None, report_meta: dict = None) -> bool:
    report_meta = report_meta or {}
    try:
        success, msg, report_info = submit_daily_report(content, cfg)
        if success:
            notify_report_success(
                cfg,
                content,
                report_info,
                report_source=report_source,
                smart_doc_status=report_meta.get("smart_doc_status"),
                smart_doc_error=report_meta.get("smart_doc_error"),
            )
            return True
        notify_report_failure(
            cfg,
            msg,
            report_source=report_source,
            smart_doc_status=report_meta.get("smart_doc_status"),
            smart_doc_error=report_meta.get("smart_doc_error"),
        )
        return False
    except Exception as e:
        logger.error(f"Submit flow crashed: {e}", exc_info=True)
        notify_report_failure(
            cfg,
            f"日报提交流程异常: {e}",
            report_source=report_source,
            smart_doc_status=report_meta.get("smart_doc_status"),
            smart_doc_error=report_meta.get("smart_doc_error"),
        )
        return False
