-- 真实历史 v1 组合库结构（取自提交 bbf23bc "feat: add portfolio sqlite schema"）。
-- 关键：transactions 无 trade_time/settlement_date；sip_plans 无
-- frequency/schedule_day/deleted_at/schedule_effective_date。
-- 用于验证 v1 就地升级链能补齐全部后续列。
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    code TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    product_type TEXT NOT NULL CHECK (
        product_type IN ('wealth_nav', 'cash_management', 'public_fund')
    ),
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    registration_code TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'CNY',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, code)
);
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    quote_date TEXT NOT NULL,
    unit_nav TEXT,
    cumulative_nav TEXT,
    income_per_10k TEXT,
    seven_day_annualized_rate TEXT,
    source TEXT NOT NULL,
    source_timestamp TEXT,
    raw_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, quote_date)
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    transaction_type TEXT NOT NULL,
    status TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    confirmation_date TEXT,
    amount TEXT,
    shares TEXT,
    fee_amount TEXT,
    fee_rate TEXT,
    confirmation_nav TEXT,
    linked_transaction_id TEXT REFERENCES transactions(id),
    plan_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,
    reversed_at TEXT
);
CREATE TABLE IF NOT EXISTS sip_plans (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    daily_amount TEXT NOT NULL,
    purchase_fee_rate TEXT NOT NULL,
    source_cash_product_id TEXT REFERENCES products(id),
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused')),
    start_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paused_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_executions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES sip_plans(id),
    intended_trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    transaction_id TEXT REFERENCES transactions(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id, intended_trade_date)
);
CREATE TABLE IF NOT EXISTS positions (
    product_id TEXT PRIMARY KEY REFERENCES products(id),
    available_shares TEXT NOT NULL,
    locked_shares TEXT NOT NULL,
    total_shares TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS legacy_profit_history (
    id TEXT PRIMARY KEY,
    profit_date TEXT NOT NULL,
    amount TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_json TEXT NOT NULL,
    UNIQUE(profit_date, source_kind, source_json)
);
CREATE INDEX IF NOT EXISTS idx_quotes_product_date
    ON quotes(product_id, quote_date DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_product_date
    ON transactions(product_id, trade_date, created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_status
    ON transactions(status, trade_date);
