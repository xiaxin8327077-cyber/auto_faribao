"""独立估值库。不修改正式账本 SCHEMA_VERSION。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from src.beijing_time import TZ_CN
from src.fund_estimate_types import (
    ESTIMATE_SCHEMA_VERSION,
    FactorObservation,
    FundIdentity,
    ModelVersion,
    NavObservation,
    ProfileVersion,
    ensure_aware,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_versions (
    profile_version TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    code TEXT NOT NULL,
    share_class TEXT NOT NULL,
    portfolio_group_id TEXT,
    report_as_of TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    source_url TEXT,
    document_hash TEXT,
    fields_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_profiles (
    code TEXT NOT NULL,
    share_class TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, share_class)
);
CREATE TABLE IF NOT EXISTS nav_observations (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    nav_date TEXT NOT NULL,
    unit_nav TEXT NOT NULL,
    cash_dividend_per_share TEXT,
    dividend_coverage TEXT NOT NULL,
    available_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    event_status TEXT NOT NULL,
    UNIQUE (code, nav_date, source, source_version)
);
CREATE TABLE IF NOT EXISTS factor_observations (
    id INTEGER PRIMARY KEY,
    factor_set_id TEXT NOT NULL,
    source_family TEXT NOT NULL,
    as_of TEXT NOT NULL,
    basis_date TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    market_as_of TEXT,
    values_bp_json TEXT NOT NULL,
    calendar_days INTEGER,
    basis_status TEXT NOT NULL,
    missing_fields_json TEXT NOT NULL,
    source_version TEXT NOT NULL,
    UNIQUE (factor_set_id, source_family, as_of, source_version)
);
CREATE TABLE IF NOT EXISTS model_versions (
    model_version TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    code TEXT NOT NULL,
    share_class TEXT NOT NULL,
    model_kind TEXT NOT NULL,
    trained_through TEXT NOT NULL,
    available_at TEXT NOT NULL,
    feature_names_json TEXT NOT NULL,
    feature_units_json TEXT NOT NULL,
    coefficients_json TEXT NOT NULL,
    preprocessing_json TEXT NOT NULL,
    source_versions_json TEXT NOT NULL,
    profile_version TEXT,
    validation_json TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS estimate_snapshots (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    market_as_of TEXT,
    source_version TEXT NOT NULL,
    UNIQUE (product_id, trade_date, slot, source_version)
);
CREATE TABLE IF NOT EXISTS current_snapshots (
    product_id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    market_as_of TEXT,
    payload_json TEXT NOT NULL,
    source_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_estimate_snapshots_verification
    ON estimate_snapshots(product_id, generated_at);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY,
    product_id TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    snapshot_generated_at TEXT NOT NULL,
    realized_return_pct TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (product_id, trade_date, snapshot_generated_at)
);
CREATE TABLE IF NOT EXISTS daily_verifications (
    product_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (product_id, trade_date)
);
CREATE TABLE IF NOT EXISTS jobs (
    idempotency_key TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    product_id TEXT,
    code TEXT,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    error TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermarks (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_documents (
    document_hash TEXT PRIMARY KEY,
    source_url TEXT,
    content_text TEXT,
    fetched_at TEXT NOT NULL,
    ai_extracted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_model_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    jobs_enabled INTEGER NOT NULL DEFAULT 0,
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS curve_level_versions (
    source_family TEXT NOT NULL,
    as_of TEXT NOT NULL,
    quote_time TEXT NOT NULL,
    version TEXT NOT NULL,
    levels_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source_family, as_of, quote_time, version)
);
"""


def parse_time(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_CN)
    return parsed.astimezone(TZ_CN)


def iso_now(value: datetime) -> str:
    return ensure_aware(value, TZ_CN).isoformat(timespec="seconds")


class FundEstimateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(factor_observations)')}
            if 'mapping_evidence_json' not in columns:
                conn.execute("ALTER TABLE factor_observations ADD COLUMN mapping_evidence_json TEXT NOT NULL DEFAULT 'null'")
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (str(ESTIMATE_SCHEMA_VERSION),),
                )
            elif int(row["value"]) != ESTIMATE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"估值库版本 {row['value']} 与程序 {ESTIMATE_SCHEMA_VERSION} 不一致"
                )
            conn.execute(
                """INSERT INTO ai_model_settings
                   (id, enabled, jobs_enabled, base_url, model, api_key, updated_at)
                   VALUES (1, 0, 0, '', '', '', ?)
                   ON CONFLICT(id) DO NOTHING""",
                (iso_now(datetime.now(TZ_CN)),),
            )

    def backup(self, dest) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, sqlite3.connect(dest) as target:
            source.backup(target)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def publish_profile(self, profile: ProfileVersion) -> ProfileVersion:
        identity = profile.identity
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO profile_versions (
                       profile_version, product_id, code, share_class, portfolio_group_id,
                       report_as_of, published_at, fetched_at, source_url, document_hash,
                       fields_json, evidence_json, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile.profile_version,
                    identity.product_id,
                    identity.code,
                    identity.share_class,
                    identity.portfolio_group_id,
                    profile.report_as_of,
                    profile.published_at,
                    profile.fetched_at,
                    profile.source_url,
                    profile.document_hash,
                    json.dumps(profile.fields, ensure_ascii=False, sort_keys=True),
                    json.dumps(profile.evidence, ensure_ascii=False),
                    profile.status,
                ),
            )
            if profile.status == "error" or not profile.published_at:
                return self._current_profile(conn, identity.code, identity.share_class) or profile
            current = conn.execute(
                """SELECT published_at, fetched_at FROM current_profiles
                   WHERE code = ? AND share_class = ?""",
                (identity.code, identity.share_class),
            ).fetchone()
            replace = current is None or _is_newer(
                profile.published_at, profile.fetched_at, current["published_at"], current["fetched_at"]
            )
            if replace:
                conn.execute(
                    """INSERT INTO current_profiles
                       (code, share_class, profile_version, published_at, fetched_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(code, share_class) DO UPDATE SET
                           profile_version = excluded.profile_version,
                           published_at = excluded.published_at,
                           fetched_at = excluded.fetched_at""",
                    (
                        identity.code,
                        identity.share_class,
                        profile.profile_version,
                        profile.published_at,
                        profile.fetched_at,
                    ),
                )
        return self.current_profile(identity.code, identity.share_class) or profile

    def record_profile_failure(self, code: str, share_class: str, message: str, at: str) -> None:
        self.set_watermark(f"profile-error:{code}:{share_class}", message, at)

    def current_profile(self, code: str, share_class: str) -> ProfileVersion | None:
        with self._connect() as conn:
            return self._current_profile(conn, code, share_class)

    def _current_profile(self, conn, code: str, share_class: str) -> ProfileVersion | None:
        row = conn.execute(
            """SELECT v.* FROM current_profiles AS c
               JOIN profile_versions AS v ON v.profile_version = c.profile_version
               WHERE c.code = ? AND c.share_class = ?""",
            (code, share_class),
        ).fetchone()
        return _profile_from_row(row) if row else None

    def profile_as_of(self, code: str, share_class: str, cutoff_at: str) -> ProfileVersion | None:
        cutoff = parse_time(cutoff_at)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM profile_versions
                   WHERE code = ? AND share_class = ? AND status != 'error'
                     AND published_at IS NOT NULL AND published_at != ''""",
                (code, share_class),
            ).fetchall()
        visible = [row for row in rows if parse_time(row["published_at"]) <= cutoff]
        if not visible:
            return None
        visible.sort(key=lambda row: (parse_time(row["published_at"]), parse_time(row["fetched_at"])))
        return _profile_from_row(visible[-1])

    def list_profile_versions(self, code: str, share_class: str) -> list[ProfileVersion]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM profile_versions
                   WHERE code = ? AND share_class = ? ORDER BY fetched_at, profile_version""",
                (code, share_class),
            ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def save_nav(self, observation: NavObservation) -> None:
        # 来源净值版本不含分红；完整经济内容必须独立版本化，恢复覆盖不抹掉旧观测。
        semantic = json.dumps([observation.unit_nav, observation.cash_dividend_per_share,
                               observation.dividend_coverage, observation.event_status], ensure_ascii=False)
        digest = hashlib.sha256(semantic.encode()).hexdigest()[:16]
        base_version = observation.source_version.split(':economic:')[0]
        observation = replace(observation, source_version=f'{base_version}:economic:{digest}')
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO nav_observations (
                       code, nav_date, unit_nav, cash_dividend_per_share, dividend_coverage,
                       available_at, source, source_version, event_status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.code,
                    observation.nav_date,
                    observation.unit_nav,
                    observation.cash_dividend_per_share,
                    observation.dividend_coverage,
                    observation.available_at,
                    observation.source,
                    observation.source_version,
                    observation.event_status,
                ),
            )

    def nav_as_of(self, code: str, cutoff_at: str) -> list[NavObservation]:
        cutoff = parse_time(cutoff_at)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nav_observations WHERE code = ? ORDER BY id",
                (code,),
            ).fetchall()
        visible = [row for row in rows if parse_time(row["available_at"]) <= cutoff]
        latest: dict[str, sqlite3.Row] = {}
        for row in visible:
            current = latest.get(row["nav_date"])
            if current is None or parse_time(row["available_at"]) >= parse_time(current["available_at"]):
                latest[row["nav_date"]] = row
        return [_nav_from_row(latest[key]) for key in sorted(latest)]

    def save_factor(self, observation: FactorObservation) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO factor_observations (
                       factor_set_id, source_family, as_of, basis_date, published_at, fetched_at,
                       market_as_of, values_bp_json, calendar_days, basis_status,
                       missing_fields_json, source_version, mapping_evidence_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.factor_set_id,
                    observation.source_family,
                    observation.as_of,
                    observation.basis_date,
                    observation.published_at,
                    observation.fetched_at,
                    observation.market_as_of,
                    json.dumps(observation.values_bp, ensure_ascii=False, sort_keys=True),
                    observation.calendar_days,
                    observation.basis_status,
                    json.dumps(observation.missing_fields, ensure_ascii=False),
                    observation.source_version,
                    json.dumps(observation.mapping_evidence, ensure_ascii=False, sort_keys=True),
                ),
            )
            return cursor.rowcount > 0

    def factors_as_of(self, cutoff_at: str, source_family: str | None = None) -> list[FactorObservation]:
        cutoff = parse_time(cutoff_at)
        with self._connect() as conn:
            if source_family:
                rows = conn.execute(
                    "SELECT * FROM factor_observations WHERE source_family = ?",
                    (source_family,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM factor_observations").fetchall()
        visible = []
        for row in rows:
            published = row["published_at"] or row["fetched_at"]
            if parse_time(published) <= cutoff:
                visible.append(row)
        latest: dict[tuple, sqlite3.Row] = {}
        for row in visible:
            key = (row["source_family"], row["as_of"])
            current = latest.get(key)
            if current is None or parse_time(row["fetched_at"]) >= parse_time(current["fetched_at"]):
                latest[key] = row
        return [_factor_from_row(latest[key]) for key in sorted(latest)]

    def save_model(self, model: ModelVersion, *, active: bool = False) -> None:
        identity = model.identity
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR REPLACE INTO model_versions (
                       model_version, product_id, code, share_class, model_kind, trained_through,
                       available_at, feature_names_json, feature_units_json, coefficients_json,
                       preprocessing_json, source_versions_json, profile_version, validation_json,
                       artifact_hash, active
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model.model_version,
                    identity.product_id,
                    identity.code,
                    identity.share_class,
                    model.model_kind,
                    model.trained_through,
                    model.available_at,
                    json.dumps(list(model.feature_names), ensure_ascii=False),
                    json.dumps(model.feature_units, ensure_ascii=False, sort_keys=True),
                    json.dumps(model.coefficients, ensure_ascii=False, sort_keys=True),
                    json.dumps(model.preprocessing, ensure_ascii=False, sort_keys=True),
                    json.dumps(model.source_versions, ensure_ascii=False),
                    model.profile_version,
                    json.dumps(model.validation, ensure_ascii=False, sort_keys=True),
                    model.artifact_hash,
                    1 if active else 0,
                ),
            )
            if active:
                conn.execute(
                    """UPDATE model_versions SET active = 0
                       WHERE code = ? AND share_class = ? AND model_version != ?""",
                    (identity.code, identity.share_class, model.model_version),
                )

    def activate_model(self, code: str, share_class: str, model_version: str) -> None:
        with self._lock, self._connect() as conn:
            found = conn.execute(
                """SELECT model_version FROM model_versions
                   WHERE code = ? AND share_class = ? AND model_version = ?""",
                (code, share_class, model_version),
            ).fetchone()
            if found is None:
                raise ValueError("模型版本不存在")
            conn.execute(
                "UPDATE model_versions SET active = 0 WHERE code = ? AND share_class = ?",
                (code, share_class),
            )
            conn.execute(
                "UPDATE model_versions SET active = 1 WHERE model_version = ?",
                (model_version,),
            )

    def active_model(self, code: str, share_class: str) -> ModelVersion | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM model_versions
                   WHERE code = ? AND share_class = ? AND active = 1""",
                (code, share_class),
            ).fetchone()
        return _model_from_row(row) if row else None

    def list_models(self, code: str, share_class: str) -> list[ModelVersion]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM model_versions
                   WHERE code = ? AND share_class = ? ORDER BY available_at, model_version""",
                (code, share_class),
            ).fetchall()
        return [_model_from_row(row) for row in rows]

    def save_snapshot(self, snapshot, *, slot: str, source_version: str) -> None:
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO estimate_snapshots (
                       product_id, code, trade_date, slot, payload_json, generated_at,
                       market_as_of, source_version
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.product_id,
                    snapshot.code,
                    snapshot.trade_date,
                    slot,
                    payload,
                    snapshot.generated_at,
                    snapshot.market_as_of,
                    source_version,
                ),
            )
            current = conn.execute(
                "SELECT market_as_of, generated_at FROM current_snapshots WHERE product_id = ?",
                (snapshot.product_id,),
            ).fetchone()
            if _snapshot_should_replace(current, snapshot.market_as_of, snapshot.generated_at):
                conn.execute(
                    """INSERT INTO current_snapshots
                       (product_id, generated_at, market_as_of, payload_json, source_version)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(product_id) DO UPDATE SET
                           generated_at = excluded.generated_at,
                           market_as_of = excluded.market_as_of,
                           payload_json = excluded.payload_json,
                           source_version = excluded.source_version""",
                    (
                        snapshot.product_id,
                        snapshot.generated_at,
                        snapshot.market_as_of,
                        payload,
                        source_version,
                    ),
                )

    def latest_snapshots(self, product_ids: list[str] | None = None) -> dict[str, dict]:
        with self._connect() as conn:
            if product_ids is None:
                rows = conn.execute("SELECT product_id, payload_json FROM current_snapshots").fetchall()
            elif not product_ids:
                return {}
            else:
                marks = ",".join("?" for _ in product_ids)
                rows = conn.execute(
                    f"SELECT product_id, payload_json FROM current_snapshots WHERE product_id IN ({marks})",
                    tuple(product_ids),
                ).fetchall()
        return {row["product_id"]: json.loads(row["payload_json"]) for row in rows}

    def snapshots_for_trade_date(self, product_id: str, trade_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT payload_json FROM estimate_snapshots
                   WHERE product_id = ? AND trade_date = ? ORDER BY generated_at""",
                (product_id, trade_date),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def snapshot_trade_dates(self, product_id: str) -> list[str]:
        """已保存预测的日期边界；不以当前快照代替历史快照。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM estimate_snapshots WHERE product_id = ? ORDER BY trade_date",
                (product_id,),
            ).fetchall()
        return [row["trade_date"] for row in rows]

    def save_daily_verification(self, item: dict) -> bool:
        """按产品及净值日更新；相同内容保留原更新时间，返回内容是否变化。"""
        payload = dict(item)
        stamp = payload.pop("updated_at", None) or iso_now(datetime.now(TZ_CN))
        payload.pop("recorded_at", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO daily_verifications(product_id, trade_date, payload_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(product_id, trade_date) DO UPDATE SET
                     payload_json = excluded.payload_json, updated_at = excluded.updated_at
                   WHERE daily_verifications.payload_json != excluded.payload_json""",
                (payload["product_id"], payload["trade_date"], encoded, stamp),
            )
            return cursor.rowcount > 0

    def read_daily_verifications(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json, updated_at FROM daily_verifications ORDER BY trade_date, product_id"
            ).fetchall()
        return [{**json.loads(row["payload_json"]), "updated_at": row["updated_at"]} for row in rows]

    def pending_verification_dates(self, product_id: str) -> list[str]:
        """找出尚未配到正式收益的快照日期，供净值轮询幂等补跑。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT s.trade_date FROM estimate_snapshots s
                   LEFT JOIN verifications v
                     ON v.product_id = s.product_id AND v.trade_date = s.trade_date
                    AND v.snapshot_generated_at = s.generated_at
                   WHERE s.product_id = ?
                     AND (v.id IS NULL OR v.realized_return_pct IS NULL
                          OR v.realized_return_pct = '')
                   ORDER BY s.trade_date""",
                (product_id,),
            ).fetchall()
        return [row["trade_date"] for row in rows]

    def save_verification(
        self,
        *,
        product_id: str,
        code: str,
        trade_date: str,
        snapshot_generated_at: str,
        realized_return_pct: str | None,
        recorded_at: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO verifications (
                       product_id, code, trade_date, snapshot_generated_at,
                       realized_return_pct, recorded_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(product_id, trade_date, snapshot_generated_at) DO UPDATE SET
                     realized_return_pct=excluded.realized_return_pct, recorded_at=excluded.recorded_at""",
                (
                    product_id,
                    code,
                    trade_date,
                    snapshot_generated_at,
                    realized_return_pct,
                    recorded_at,
                ),
            )

    def read_verifications(self) -> list[dict]:
        """预估快照与真实收益的配对明细；predicted 为当时快照的 return_pct。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT v.product_id, v.code, v.trade_date, v.snapshot_generated_at,
                          v.realized_return_pct, v.recorded_at, s.payload_json
                   FROM verifications v LEFT JOIN estimate_snapshots s
                     ON s.product_id = v.product_id AND s.generated_at = v.snapshot_generated_at
                   ORDER BY v.trade_date, v.snapshot_generated_at"""
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            except (TypeError, ValueError):
                payload = {}
            result.append({
                "product_id": row["product_id"], "code": row["code"],
                "trade_date": row["trade_date"], "snapshot_generated_at": row["snapshot_generated_at"],
                "realized_return_pct": row["realized_return_pct"], "recorded_at": row["recorded_at"],
                "predicted": payload.get("return_pct"),
                "base_nav": payload.get("base_nav"),
                "base_nav_date": payload.get("base_nav_date"),
            })
        return result

    def enqueue_job(self, key: str, job_type: str, product_id: str, code: str, payload: dict, now: str) -> bool:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT status FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing and existing["status"] in {"queued", "running", "succeeded"}:
                return False
            conn.execute(
                """INSERT INTO jobs (
                       idempotency_key, job_type, product_id, code, status, payload_json,
                       attempts, next_retry_at, error, updated_at
                   ) VALUES (?, ?, ?, ?, 'queued', ?, 0, NULL, '', ?)
                   ON CONFLICT(idempotency_key) DO UPDATE SET
                       status = 'queued',
                       payload_json = excluded.payload_json,
                       error = '',
                       updated_at = excluded.updated_at
                   WHERE jobs.status IN ('failed', 'cancelled')""",
                (key, job_type, product_id, code, json.dumps(payload, ensure_ascii=False), now),
            )
        return True

    def list_jobs(self, code: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if code is None:
                rows = conn.execute("SELECT * FROM jobs ORDER BY idempotency_key").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE code = ? ORDER BY idempotency_key",
                    (code,),
                ).fetchall()
        return [_job_dict(row) for row in rows]

    def claim_jobs(self, now: str, limit: int) -> list[dict]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE (status = 'queued' OR (status = 'failed' AND next_retry_at IS NOT NULL))
                     AND (next_retry_at IS NULL OR next_retry_at <= ?)
                   ORDER BY updated_at, idempotency_key""",
                (now,),
            ).fetchall()
            claimed = []
            busy = {
                row["code"]
                for row in conn.execute("SELECT code FROM jobs WHERE status = 'running'").fetchall()
                if row["code"]
            }
            for row in rows:
                if len(claimed) >= limit:
                    break
                if row["code"] and row["code"] in busy:
                    continue
                conn.execute(
                    """UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?
                       WHERE idempotency_key = ? AND status IN ('queued', 'failed')""",
                    (now, row["idempotency_key"]),
                )
                claimed.append(_job_dict(row))
                if row["code"]:
                    busy.add(row["code"])
            return claimed

    def finish_job(self, key: str, now: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', error = '', updated_at = ? WHERE idempotency_key = ?",
                (now, key),
            )

    def fail_job(self, key: str, message: str, retry_at: str | None, now: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET status = 'failed', error = ?, next_retry_at = ?, updated_at = ?
                   WHERE idempotency_key = ?""",
                (message[:300], retry_at, now, key),
            )

    def resume_running_jobs(self, now: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (now,),
            )
            return cursor.rowcount

    def cancel_queued(self, job_type: str, codes_to_keep: set[str], now: str) -> None:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT idempotency_key, code FROM jobs WHERE job_type = ? AND status = 'queued'",
                (job_type,),
            ).fetchall()
            for row in rows:
                if row["code"] not in codes_to_keep:
                    conn.execute(
                        """UPDATE jobs SET status = 'cancelled', updated_at = ?
                           WHERE idempotency_key = ?""",
                        (now, row["idempotency_key"]),
                    )

    def cancel_older_queued(self, job_type: str, code: str, keep_key: str, now: str) -> None:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT idempotency_key FROM jobs
                   WHERE job_type = ? AND code = ? AND status = 'queued' AND idempotency_key != ?""",
                (job_type, code, keep_key),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE idempotency_key = ?",
                    (now, row["idempotency_key"]),
                )

    def set_watermark(self, key: str, value: str, at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO watermarks (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, at),
            )

    def get_watermark(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM watermarks WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def save_curve_levels(self, family: str, day: str, quote_time: str, levels: dict, fetched_at: str) -> None:
        payload = json.dumps(levels, sort_keys=True, ensure_ascii=False)
        version = hashlib.sha256(payload.encode()).hexdigest()[:24]
        with self._lock, self._connect() as conn:
            conn.execute('''INSERT OR IGNORE INTO curve_level_versions
                (source_family, as_of, quote_time, version, levels_json, fetched_at) VALUES (?,?,?,?,?,?)''',
                (family, day, quote_time, version, payload, fetched_at))

    def read_curve_levels(self, family: str, day: str, quote_time: str, cutoff: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute('''SELECT levels_json, fetched_at FROM curve_level_versions
                WHERE source_family=? AND as_of=? AND quote_time=? AND fetched_at<=?
                ORDER BY fetched_at DESC, rowid DESC LIMIT 1''', (family, day, quote_time, cutoff)).fetchone()
        return None if row is None else {'levels': json.loads(row['levels_json']), 'fetched_at': row['fetched_at']}

    def document_by_url(self, source_url: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute('''SELECT document_hash, source_url, content_text, fetched_at
                FROM raw_documents WHERE source_url=? ORDER BY fetched_at DESC LIMIT 1''', (source_url,)).fetchone()
        return None if row is None else dict(row)

    def remember_document(self, document_hash: str, source_url: str, content_text: str, fetched_at: str) -> bool:
        """返回该哈希是否已经成功提取过。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT ai_extracted FROM raw_documents WHERE document_hash = ?",
                (document_hash,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO raw_documents (document_hash, source_url, content_text, fetched_at, ai_extracted)
                       VALUES (?, ?, ?, ?, 0)""",
                    (document_hash, source_url, content_text, fetched_at),
                )
                return False
            return bool(row["ai_extracted"])

    def mark_document_extracted(self, document_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE raw_documents SET ai_extracted = 1 WHERE document_hash = ?",
                (document_hash,),
            )

    def public_ai_settings(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT enabled, jobs_enabled, base_url, model,
                          CASE WHEN api_key = '' THEN 0 ELSE 1 END AS configured,
                          api_key
                   FROM ai_model_settings WHERE id = 1"""
            ).fetchone()
        if row is None:
            return _empty_settings()
        hint = _key_hint(row["api_key"])
        return {
            "available": True,
            "enabled": bool(row["enabled"]),
            "jobs_enabled": bool(row["jobs_enabled"]),
            "base_url": row["base_url"],
            "model": row["model"],
            "api_key_configured": bool(row["configured"]),
            "api_key_hint": hint,
        }

    def secret_ai_settings(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled, jobs_enabled, base_url, model, api_key FROM ai_model_settings WHERE id = 1"
            ).fetchone()
        if row is None:
            return {**_empty_settings(), "api_key": ""}
        return {
            "enabled": bool(row["enabled"]),
            "jobs_enabled": bool(row["jobs_enabled"]),
            "base_url": row["base_url"],
            "model": row["model"],
            "api_key": row["api_key"],
        }

    def save_ai_settings(
        self,
        *,
        enabled: bool,
        jobs_enabled: bool,
        base_url: str,
        model: str,
        api_key: str | None,
        clear_api_key: bool,
        updated_at: str,
    ) -> dict:
        with self._lock, self._connect() as conn:
            current = conn.execute(
                "SELECT api_key FROM ai_model_settings WHERE id = 1"
            ).fetchone()
            stored = "" if current is None else current["api_key"]
            if clear_api_key:
                stored = ""
            elif api_key:
                stored = api_key
            conn.execute(
                """INSERT INTO ai_model_settings
                   (id, enabled, jobs_enabled, base_url, model, api_key, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       enabled = excluded.enabled,
                       jobs_enabled = excluded.jobs_enabled,
                       base_url = excluded.base_url,
                       model = excluded.model,
                       api_key = excluded.api_key,
                       updated_at = excluded.updated_at""",
                (int(enabled), int(jobs_enabled), base_url, model, stored, updated_at),
            )
        return self.public_ai_settings()

    def count_rows(self, table: str) -> int:
        if not table.isidentifier():
            raise ValueError("invalid table")
        with self._connect() as conn:
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _empty_settings() -> dict:
    return {
        "available": True,
        "enabled": False,
        "jobs_enabled": False,
        "base_url": "",
        "model": "",
        "api_key_configured": False,
        "api_key_hint": "",
    }


def _key_hint(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 4:
        return "已配置"
    return "····" + api_key[-4:]


def _is_newer(published_at: str, fetched_at: str, current_published: str, current_fetched: str) -> bool:
    published = parse_time(published_at)
    current = parse_time(current_published)
    if published != current:
        return published > current
    return parse_time(fetched_at) > parse_time(current_fetched)


def _snapshot_should_replace(current, market_as_of: str | None, generated_at: str) -> bool:
    if current is None:
        return True
    old_market = current["market_as_of"]
    if market_as_of and old_market:
        new_market = parse_time(market_as_of)
        previous = parse_time(old_market)
        if new_market < previous:
            return False
        if new_market > previous:
            return True
    elif old_market and not market_as_of:
        return False
    return parse_time(generated_at) >= parse_time(current["generated_at"])


def _identity_from_row(row) -> FundIdentity:
    return FundIdentity(
        product_id=row["product_id"],
        code=row["code"],
        provider="",
        share_class=row["share_class"],
        portfolio_group_id=row["portfolio_group_id"],
        fund_type="",
    )


def _profile_from_row(row) -> ProfileVersion:
    return ProfileVersion(
        profile_version=row["profile_version"],
        identity=_identity_from_row(row),
        report_as_of=row["report_as_of"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        source_url=row["source_url"] or "",
        document_hash=row["document_hash"] or "",
        fields=json.loads(row["fields_json"]),
        evidence=json.loads(row["evidence_json"]),
        status=row["status"],
    )


def _nav_from_row(row) -> NavObservation:
    return NavObservation(
        code=row["code"],
        nav_date=row["nav_date"],
        unit_nav=row["unit_nav"],
        cash_dividend_per_share=row["cash_dividend_per_share"],
        available_at=row["available_at"],
        source=row["source"],
        source_version=row["source_version"],
        event_status=row["event_status"],
        dividend_coverage=row["dividend_coverage"],
    )


def _factor_from_row(row) -> FactorObservation:
    return FactorObservation(
        factor_set_id=row["factor_set_id"],
        source_family=row["source_family"],
        as_of=row["as_of"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        market_as_of=row["market_as_of"],
        values_bp=json.loads(row["values_bp_json"]),
        calendar_days=row["calendar_days"],
        basis_status=row["basis_status"],
        missing_fields=json.loads(row["missing_fields_json"]),
        basis_date=row["basis_date"],
        source_version=row["source_version"],
        mapping_evidence=json.loads(row['mapping_evidence_json']) if 'mapping_evidence_json' in row.keys() else None,
    )


def _model_from_row(row) -> ModelVersion:
    return ModelVersion(
        model_version=row["model_version"],
        identity=FundIdentity(
            product_id=row["product_id"],
            code=row["code"],
            provider="",
            share_class=row["share_class"],
            portfolio_group_id=None,
            fund_type="",
        ),
        model_kind=row["model_kind"],
        trained_through=row["trained_through"],
        available_at=row["available_at"],
        feature_names=tuple(json.loads(row["feature_names_json"])),
        feature_units=json.loads(row["feature_units_json"]),
        coefficients=json.loads(row["coefficients_json"]),
        preprocessing=json.loads(row["preprocessing_json"]),
        source_versions=json.loads(row["source_versions_json"]),
        profile_version=row["profile_version"],
        validation=json.loads(row["validation_json"]),
        artifact_hash=row["artifact_hash"],
    )


def _job_dict(row) -> dict:
    return {
        "idempotency_key": row["idempotency_key"],
        "job_type": row["job_type"],
        "product_id": row["product_id"],
        "code": row["code"],
        "status": row["status"],
        "payload": json.loads(row["payload_json"]),
        "attempts": row["attempts"],
        "next_retry_at": row["next_retry_at"],
        "error": row["error"] or "",
        "updated_at": row["updated_at"],
    }
