"""Extract task data from WeChat smart sheet via Playwright + JS API."""
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from src.config import SourceConfig

logger = logging.getLogger(__name__)
DOC_BASE = "https://doc.weixin.qq.com"


class ExtractError(Exception):
    pass


def extract_tasks(cfg: SourceConfig) -> list[str]:
    """Extract task names matching the configured filters."""
    doc_url = f"{DOC_BASE}/smartsheet/{cfg.doc_id}?scode={cfg.scode}&tab={cfg.tab_id}&viewId={cfg.view_id}"
    js_cookies = _build_cookies(cfg)
    logger.info(f"Opening document: {doc_url[:80]}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        context.add_cookies(js_cookies)
        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            page.goto(doc_url, timeout=90000, wait_until="commit")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeout:
                logger.info("Document DOMContentLoaded did not finish; continuing with SDK wait")
            page.wait_for_timeout(5000)
            if "login" in page.url.lower():
                raise ExtractError("WeChat document login required; smart sheet cookies may be expired")
            page.wait_for_function(
                """() => {
                    return !!(
                        window.ContainerApp &&
                        window.ContainerApp.containerSdk &&
                        window.ContainerApp.containerSdk.smartSheetSdk &&
                        window.ContainerApp.containerSdk.smartSheetSdk.editor &&
                        window.ContainerApp.containerSdk.smartSheetSdk.editor.getCore
                    );
                }""",
                timeout=90000,
            )
            page.wait_for_timeout(5000)

            tasks = page.evaluate(
                """(cfg) => {
                    const core = window.ContainerApp.containerSdk.smartSheetSdk.editor.getCore();
                    const table = core.base.getTableByTableId(cfg.tabId);
                    if (!table) return {error: 'table not found'};

                    const fields = table.getFields();
                    const fieldMap = {};
                    const optionMaps = {};
                    for (const f of fields) {
                        fieldMap[f.title] = {id: f.id, type: f.type};
                        // Build option map for select fields (type 17)
                        if (f.type === 17) {
                            try {
                                const prop = f.getProperty ? f.getProperty() : null;
                                if (prop && prop.options) {
                                    const opts = {};
                                    for (const o of prop.options) {
                                        opts[o.id] = o.text;
                                    }
                                    optionMaps[f.id] = opts;
                                }
                            } catch(e) {}
                        }
                    }

                    // Build user map for person fields (type 7)
                    const userService = core.userService;
                    const userMap = {};
                    try {
                        if (typeof userService.getUserInfoFromUserMap === 'function') {
                            // This returns a cached user map
                        }
                    } catch(e) {}

                    const recordIds = table.getRecordIdList();
                    const tasks = [];

                    // Field IDs
                    const personFid = fieldMap[cfg.personField]?.id;
                    const statusFid = fieldMap[cfg.statusField]?.id;
                    const nameFid = fieldMap[cfg.nameField]?.id;
                    const descFid = cfg.descField ? fieldMap[cfg.descField]?.id : null;
                    const maxDescLen = cfg.maxDescLen || 25;

                    // Helper to extract text from a type-1 cell value
                    const extractText = function(cell) {
                        if (!cell || !cell.value) return '';
                        if (Array.isArray(cell.value)) {
                            return cell.value.map(function(t) { return t.text || ''; }).join('');
                        }
                        return String(cell.value);
                    };

                    if (!personFid || !statusFid || !nameFid) {
                        return {error: 'fields not found', fields: Object.keys(fieldMap)};
                    }

                    for (let i = 0; i < recordIds.length; i++) {
                        const rid = recordIds[i];

                        // Get and parse status (resolve option ID)
                        const statusCell = table.getCell(rid, statusFid);
                        let statusText = '';
                        if (statusCell && statusCell.value && Array.isArray(statusCell.value) && statusCell.value.length > 0) {
                            const optMap = optionMaps[statusFid] || {};
                            statusText = statusCell.value.map(function(id) { return optMap[id] || id; }).join(',');
                        }

                        // Check status filter
                        if (!cfg.statusValues.some(function(s) { return statusText.includes(s); })) continue;

                        // Get and parse person
                        const personCell = table.getCell(rid, personFid);
                        let personIds = [];
                        if (personCell && personCell.value && Array.isArray(personCell.value)) {
                            personIds = personCell.value.map(function(u) { return u.id; });
                        }
                        if (personIds.length === 0) continue;

                        // Resolve person names
                        let personName = '';
                        for (const pid of personIds) {
                            try {
                                const userInfo = userService.getUserInfoFromUserMap(pid);
                                if (userInfo) {
                                    const name = userInfo.name || userInfo.userName || '';
                                    if (name) personName += (personName ? ',' : '') + name;
                                }
                            } catch(e) {
                                // Fallback: try direct userMap
                            }
                        }
                        // Fallback: try table.getUserMap or table.userMapPartial
                        if (!personName) {
                            try {
                                const um = table.userMapPartial || {};
                                for (const pid of personIds) {
                                    const u = um[pid] || {};
                                    const n = u.name || u.userName || '';
                                    if (n) personName += (personName ? ',' : '') + n;
                                }
                            } catch(e) {}
                        }

                        // Check person filter
                        if (!cfg.personNames.some(function(n) { return personName.includes(n); })) continue;

                        // Get task name and description
                        const taskName = extractText(table.getCell(rid, nameFid));
                        let taskText = taskName;
                        if (descFid) {
                            const descText = extractText(table.getCell(rid, descFid));
                            if (descText) {
                                taskText = taskName + '：' + descText;
                            }
                        }

                        if (taskText) tasks.push(taskText);
                    }
                    return {tasks: tasks};
                }""",
                {
                    "tabId": cfg.tab_id,
                    "personField": cfg.person_field,
                    "statusField": cfg.status_field,
                    "nameField": cfg.name_field,
                    "personNames": cfg.person_names,
                    "statusValues": cfg.status_values,
                    "descField": cfg.desc_field,
                },
            )

            if "error" in tasks:
                raise ExtractError(f"JS extraction failed: {tasks['error']}")

            task_list = tasks.get("tasks", [])
            logger.info(f"Extracted {len(task_list)} tasks")
            return task_list

        except PlaywrightTimeout as e:
            raise ExtractError(f"Page load timeout: {e}")
        except Exception as e:
            raise ExtractError(f"Extraction failed: {e}")
        finally:
            browser.close()


def _build_cookies(cfg: SourceConfig) -> list[dict]:
    cookies = []
    domain = ".weixin.qq.com"
    for name in ["low_login_enable", "utype", "TOK", "traceid", "hashkey",
                 "tdoc_uid", "wedoc_openid", "wedoc_sid", "wedoc_sids",
                 "wedoc_skey", "wedoc_ticket", "language", "fingerprint"]:
        val = getattr(cfg, name, None) or ""
        if val:
            cookies.append({"name": name, "value": val, "domain": domain, "path": "/"})
    return cookies
