"""Idempotent fund dividends: record-date rights, receivable, then settlement."""
from contextlib import nullcontext
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from uuid import uuid4

from src.portfolio_dividend_provider import FundDividend, PublicFundDividendProvider, DividendSourceConflict
from src.portfolio_models import ProductType, Transaction, TransactionStatus as Status, TransactionType as Kind, RedemptionSettlementStatus as Settlement, decimal_text
from src.portfolio_positions import PositionProjector
from src.portfolio_reconciliation import reconciled_transaction
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_wallet import WALLET_PRODUCT_ID, is_wallet_plus_product

ZERO = Decimal("0")


DIVIDEND_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fund_dividend_state (
    product_id TEXT PRIMARY KEY REFERENCES products(id),
    enabled_date TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fund_dividend_events (
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id),
    record_date TEXT NOT NULL, ex_dividend_date TEXT NOT NULL, payment_date TEXT NOT NULL,
    per_share TEXT NOT NULL, source_url TEXT NOT NULL,
    reinvest_nav_date TEXT, reinvest_confirmation_date TEXT, reinvest_rule_source TEXT NOT NULL DEFAULT '',
    mode TEXT, status TEXT NOT NULL DEFAULT 'announced', eligible_shares TEXT, amount TEXT,
    amount_override TEXT, override_source TEXT NOT NULL DEFAULT '', explicitly_included INTEGER NOT NULL DEFAULT 0,
    dividend_transaction_id TEXT, settlement_transaction_id TEXT, reinvest_shares TEXT,
    reinvest_precision TEXT NOT NULL DEFAULT 'internal_decimal',
    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, ex_dividend_date, per_share)
);
"""


def ensure_dividend_schema(conn):
    # Do not use executescript: it commits an enclosing maintenance transaction.
    for statement in DIVIDEND_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    if "reinvest_precision" not in {row[1] for row in conn.execute("PRAGMA table_info(fund_dividend_events)")}:
        conn.execute("ALTER TABLE fund_dividend_events ADD COLUMN reinvest_precision TEXT NOT NULL DEFAULT 'internal_decimal'")


def dividend_mode(product):
    try:
        metadata = json.loads(product.metadata_json)
    except (TypeError, ValueError):
        metadata = {}
    mode = metadata.get("dividend_mode") if isinstance(metadata, dict) else None
    return mode if mode in {"cash", "reinvest"} else "cash"


def dividend_records(repository, product_id, conn=None):
    with (nullcontext(conn) if conn is not None else repository.database.connection()) as conn:
        product = repository.require_product(product_id, conn=conn)
        if product.product_type is not ProductType.PUBLIC_FUND:
            raise ValueError("仅公募基金支持分红")
        rows = conn.execute("SELECT * FROM fund_dividend_events WHERE product_id=? ORDER BY ex_dividend_date DESC, id", (product_id,)).fetchall()
        state = conn.execute("SELECT * FROM fund_dividend_state WHERE product_id=?", (product_id,)).fetchone()
        keys = ("id", "record_date", "ex_dividend_date", "payment_date", "per_share", "amount", "status", "reinvest_shares", "source_url", "error", "reinvest_confirmation_date")
        events = []
        for row in rows:
            historical = bool(state and not row["explicitly_included"] and row["ex_dividend_date"] < state["enabled_date"])
            events.append({**{key: row[key] for key in keys}, "mode": row["mode"] or dividend_mode(product),
                           "historical": historical, "is_upcoming": not historical and row["status"] != "settled"})
        return {"product_id": product_id, "dividend_mode": dividend_mode(product), "events": events, "error": state["error"] if state else ""}


def dividend_upcoming_count(repository, product_id, conn=None):
    with (nullcontext(conn) if conn is not None else repository.database.connection()) as conn:
        return conn.execute("""SELECT COUNT(*) FROM fund_dividend_events event
            JOIN fund_dividend_state state ON state.product_id=event.product_id
            WHERE event.product_id=? AND event.status!='settled'
              AND (event.explicitly_included=1 OR event.ex_dividend_date>=state.enabled_date)""", (product_id,)).fetchone()[0]


def _event_id(product_id, event):
    identity = f"{product_id}|{event.ex_dividend_date.isoformat()}|{decimal_text(event.per_share)}"
    return "fund-dividend:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


class FundDividendService:
    def __init__(self, repository, provider_factory=None):
        self.repository = repository
        self.provider_factory = provider_factory or (lambda product: PublicFundDividendProvider())
        self.projector = PositionProjector(repository)
        self.transactions = PortfolioTransactionService(repository, self.projector)

    def include_event(self, product_id, event, amount_override=None, mode_override=None,
                      override_source="", as_of=None, conn=None):
        """Explicitly admit a verified historical event. Returns its stable string ID."""
        if mode_override not in {None, "cash", "reinvest"}:
            raise ValueError("分红方式必须为 cash 或 reinvest")
        if amount_override is not None:
            amount_override = Decimal(str(amount_override))
            if not amount_override.is_finite() or amount_override <= ZERO or amount_override != amount_override.quantize(Decimal(".01")):
                raise ValueError("已核实分红金额必须为正数且精确到分")
            if not override_source:
                override_source = "user-confirmed:" + decimal_text(amount_override)
        with (nullcontext(conn) if conn is not None else self.repository.database.transaction()) as conn:
            product = self.repository.require_product(product_id, conn=conn)
            if product.product_type is not ProductType.PUBLIC_FUND:
                raise ValueError("仅公募基金支持分红")
            eid = self._store(product, event, as_of or date.today(), conn, explicitly_included=True)
            if eid != _event_id(product_id, event):
                raise ValueError("同一除息日分红方案冲突，需核实")
            row = conn.execute("SELECT * FROM fund_dividend_events WHERE id=?", (eid,)).fetchone()
            if row["dividend_transaction_id"]:
                if ((amount_override is not None and row["amount_override"] != decimal_text(amount_override))
                    or (mode_override is not None and row["mode"] != mode_override)):
                    raise ValueError("已入账分红事件不可改写")
                return eid
            conn.execute("UPDATE fund_dividend_events SET explicitly_included=1, amount_override=?, override_source=?, mode=COALESCE(?,mode) WHERE id=?",
                         (decimal_text(amount_override) if amount_override is not None else row["amount_override"], override_source or row["override_source"], mode_override, eid))
            self.repository.append_audit(str(uuid4()), "include_fund_dividend", "fund_dividend", eid,
                after_json=json.dumps({"amount_override": decimal_text(amount_override) if amount_override is not None else None,
                                       "mode_override": mode_override, "override_source": override_source, "source_url": event.source_url}), source="fund_dividend", conn=conn)
            return eid

    def _store(self, product, event, as_of, conn, explicitly_included=False):
        if (not isinstance(event.per_share, Decimal) or not event.per_share.is_finite() or event.per_share <= ZERO
            or event.record_date > event.ex_dividend_date or event.payment_date < event.ex_dividend_date or not event.source_url.startswith("https://")):
            raise ValueError("公开分红日期、金额或来源不完整")
        eid = _event_id(product.id, event)
        conn.execute("INSERT OR IGNORE INTO fund_dividend_state(product_id,enabled_date) VALUES(?,?)", (product.id, as_of.isoformat()))
        row = conn.execute("SELECT * FROM fund_dividend_events WHERE id=?", (eid,)).fetchone()
        # Source contradictions never create another credit or rewrite the snapshot.
        other = conn.execute("SELECT id FROM fund_dividend_events WHERE product_id=? AND ex_dividend_date=? AND id!=?", (product.id, event.ex_dividend_date.isoformat(), eid)).fetchone()
        if other:
            conn.execute("UPDATE fund_dividend_events SET error='来源分红方案冲突，需核实' WHERE id=?", (other["id"],))
            return other["id"]
        values = (event.record_date.isoformat(), event.ex_dividend_date.isoformat(), event.payment_date.isoformat(), decimal_text(event.per_share), event.source_url,
                  event.reinvest_nav_date.isoformat() if event.reinvest_nav_date else None,
                  event.reinvest_confirmation_date.isoformat() if event.reinvest_confirmation_date else None, event.reinvest_rule_source)
        if row is None:
            conn.execute("INSERT INTO fund_dividend_events(id,product_id,record_date,ex_dividend_date,payment_date,per_share,source_url,reinvest_nav_date,reinvest_confirmation_date,reinvest_rule_source,explicitly_included) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (eid, product.id, *values, int(explicitly_included)))
            enabled = conn.execute("SELECT enabled_date FROM fund_dividend_state WHERE product_id=?", (product.id,)).fetchone()[0]
            if not explicitly_included and event.ex_dividend_date.isoformat() < enabled:
                conn.execute("UPDATE fund_dividend_events SET status='blocked',error='启用日期之前的历史公告，仅展示，未自动补账' WHERE id=?", (eid,))
        else:
            old = tuple(row[key] for key in ("record_date", "ex_dividend_date", "payment_date", "per_share", "source_url", "reinvest_nav_date", "reinvest_confirmation_date", "reinvest_rule_source"))
            if old[:4] != values[:4]:
                conn.execute("UPDATE fund_dividend_events SET status=CASE WHEN status='settled' THEN status ELSE 'blocked' END,error='来源分红日期或金额冲突，需核实' WHERE id=?", (eid,))
            else:
                conn.execute("UPDATE fund_dividend_events SET reinvest_nav_date=COALESCE(?,reinvest_nav_date),reinvest_confirmation_date=COALESCE(?,reinvest_confirmation_date),reinvest_rule_source=CASE WHEN ?!='' THEN ? ELSE reinvest_rule_source END WHERE id=?", (values[5],values[6],values[7],values[7],eid))
        return eid

    def run(self, as_of):
        changed = 0
        for product in self.repository.list_products():
            if product.product_type is not ProductType.PUBLIC_FUND:
                continue
            try:
                # Fetch before taking any SQLite write lock.
                events = self.provider_factory(product).fetch_dividends(product)
                with self.repository.database.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO fund_dividend_state(product_id,enabled_date) VALUES(?,?)", (product.id, as_of.isoformat()))
                    for event in events:
                        holding_transactions = self.repository.list_transactions(product_id=product.id, conn=conn)
                        if not any(tx.trade_date <= event.record_date and (tx.shares or ZERO) > ZERO
                                   and tx.transaction_type in {Kind.OPENING_POSITION,Kind.MANUAL_PURCHASE,Kind.SIP_PURCHASE,Kind.HOLDING_ADJUSTMENT}
                                   for tx in holding_transactions):
                            continue
                        self._store(product, event, as_of, conn)
                    conn.execute("UPDATE fund_dividend_state SET error='' WHERE product_id=?", (product.id,))
            except Exception as exc:
                with self.repository.database.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO fund_dividend_state(product_id,enabled_date) VALUES(?,?)", (product.id, as_of.isoformat()))
                    conn.execute("UPDATE fund_dividend_state SET error=? WHERE product_id=?", (f"分红抓取失败：{exc}"[:500], product.id))
                    if isinstance(exc, DividendSourceConflict):
                        conn.execute("UPDATE fund_dividend_events SET status='blocked',error=? WHERE product_id=? AND status!='settled'",
                                     (f"公开来源冲突，暂停自动结算：{exc}"[:500],product.id))
            with self.repository.database.connection() as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM fund_dividend_events WHERE product_id=?", (product.id,))]
            for eid in ids:
                try:
                    changed += int(self.process_known_event(eid, as_of))
                except Exception as exc:
                    with self.repository.database.transaction() as conn:
                        conn.execute("UPDATE fund_dividend_events SET error=?,status=CASE WHEN dividend_transaction_id IS NULL THEN 'blocked' ELSE status END WHERE id=?", (f"分红入账失败：{exc}"[:500],eid))
        return changed

    def _eligible_shares(self, product, record_date, conn):
        rows = self.repository.list_transactions(product_id=product.id, conn=conn)
        neutralized = PositionProjector._linked_reversal_pair_ids(rows)
        total = ZERO
        for tx in rows:
            if tx.id in neutralized or tx.transaction_type is Kind.REVERSAL:
                continue
            if tx.status not in {Status.CONFIRMED, Status.REVERSED}:
                if tx.trade_date < record_date and tx.status in {Status.PENDING_QUOTE,Status.PENDING_CONFIRMATION}:
                    raise ValueError("登记日前交易尚未确认，无法核实登记权益")
                continue
            effective = tx.confirmation_date or tx.trade_date
            if tx.transaction_type in {Kind.MANUAL_PURCHASE,Kind.SIP_PURCHASE}:
                if tx.trade_date < record_date and effective <= record_date:
                    total += tx.shares or ZERO
            elif tx.transaction_type in {Kind.MANUAL_REDEMPTION,Kind.CASH_TRANSFER_OUT}:
                if tx.trade_date < record_date and effective <= record_date:
                    total -= tx.shares or ZERO
            elif tx.transaction_type in {Kind.OPENING_POSITION,Kind.HOLDING_ADJUSTMENT} and effective <= record_date:
                total += tx.shares or ZERO
        if total < ZERO:
            raise ValueError("登记日权益份额无效")
        return total

    def process_known_event(self, event_id, as_of, conn=None):
        with (nullcontext(conn) if conn is not None else reconciled_transaction(self.repository,"fund_dividend")) as conn:
            created = False
            row = conn.execute("SELECT * FROM fund_dividend_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("分红事件不存在")
            if row["dividend_transaction_id"]:
                rows=self.repository.list_transactions(product_id=row["product_id"],conn=conn)
                if row["dividend_transaction_id"] in PositionProjector._linked_reversal_pair_ids(rows):
                    conn.execute("UPDATE fund_dividend_events SET status='blocked',error='分红流水已冲正，自动处理已停止' WHERE id=?",(event_id,))
                    return False
                if row["status"] == "settled":
                    return False
            state = conn.execute("SELECT * FROM fund_dividend_state WHERE product_id=?", (row["product_id"],)).fetchone()
            if not row["explicitly_included"] and row["ex_dividend_date"] < state["enabled_date"]:
                return False
            if "冲突" in row["error"]:
                return False
            product = self.repository.require_product(row["product_id"],conn=conn)
            if row["record_date"] > as_of.isoformat():
                return False
            mode = row["mode"] or dividend_mode(product)
            if not row["mode"]:
                conn.execute("UPDATE fund_dividend_events SET mode=? WHERE id=?",(mode,event_id))
            if row["dividend_transaction_id"]:
                dividend = self.repository.get_transaction_by_id(row["dividend_transaction_id"],conn=conn)
                if dividend.status is not Status.CONFIRMED:
                    conn.execute("UPDATE fund_dividend_events SET status='blocked',error='分红流水已冲正，自动处理已停止' WHERE id=?",(event_id,))
                    return False
            else:
                txs=self.repository.list_transactions(product_id=product.id,conn=conn)
                if any(tx.status is Status.CONFIRMED and tx.trade_date.isoformat()==row["ex_dividend_date"] and tx.transaction_type in {Kind.LATEST_PROFIT_ADJUSTMENT,Kind.PROFIT_ADJUSTMENT,Kind.CASH_DIVIDEND} for tx in txs):
                    conn.execute("UPDATE fund_dividend_events SET status='blocked',error='除息日存在人工收益校准或分红流水，需先核实并冲正重叠记录' WHERE id=?",(event_id,))
                    return False
                try:
                    shares=self._eligible_shares(product,date.fromisoformat(row["record_date"]),conn)
                except ValueError as exc:
                    if row["amount_override"] is None:
                        conn.execute("UPDATE fund_dividend_events SET status='blocked',error=? WHERE id=?",(str(exc),event_id))
                        return False
                    shares=None
                amount = Decimal(row["amount_override"]) if row["amount_override"] is not None else ((shares or ZERO)*Decimal(row["per_share"])).quantize(Decimal(".01"),rounding=ROUND_HALF_UP)
                conn.execute("UPDATE fund_dividend_events SET eligible_shares=?,amount=? WHERE id=?",(decimal_text(shares) if shares is not None else None,decimal_text(amount),event_id))
                if row["ex_dividend_date"] > as_of.isoformat() or amount <= ZERO:
                    return False
                ex_quote = self.repository.get_quote(product.id,date.fromisoformat(row["ex_dividend_date"]),conn=conn)
                if ex_quote is None or ex_quote.unit_nav is None or ex_quote.unit_nav <= ZERO:
                    conn.execute("UPDATE fund_dividend_events SET status='announced',error='等待除息日官方净值，暂不确认分红净收益及应收资产' WHERE id=?",(event_id,))
                    return False
                destination=""
                if mode=="cash":
                    wallet=self.repository.get_product(WALLET_PRODUCT_ID,conn=conn)
                    if wallet is None or not is_wallet_plus_product(wallet):
                        conn.execute("UPDATE fund_dividend_events SET status='blocked',error='钱包Plus不存在，等待现金目标账户' WHERE id=?",(event_id,))
                        return False
                    destination=wallet.id
                # Always recognize the receivable first, even when catching up after payment.
                payment=date.fromisoformat(row["payment_date"])
                from datetime import timedelta
                dividend=self.transactions.record_cash_dividend(product.id,amount,date.fromisoformat(row["ex_dividend_date"]),event_id,
                    destination_cash_product_id=destination,actor="fund_dividend",payment_date=payment,
                    as_of_date=min(date.fromisoformat(row["ex_dividend_date"]), payment-timedelta(days=1)),conn=conn)
                conn.execute("UPDATE fund_dividend_events SET dividend_transaction_id=?,status='receivable',error='' WHERE id=?",(dividend.id,event_id))
                created = True
            row=conn.execute("SELECT * FROM fund_dividend_events WHERE id=?",(event_id,)).fetchone()
            if mode=="cash":
                if row["payment_date"]>as_of.isoformat():
                    return created
                self.transactions.settle_cash_dividends(as_of,conn=conn,transaction_id=dividend.id)
                dividend=self.repository.get_transaction_by_id(dividend.id,conn=conn)
                if dividend.settlement_status is not Settlement.SETTLED:
                    raise ValueError("分红应收尚未满足现金结算条件")
                conn.execute("UPDATE fund_dividend_events SET status='settled',settlement_transaction_id=?,error='' WHERE id=?",(dividend.linked_transaction_id,event_id))
                return True
            if not row["reinvest_nav_date"] or not row["reinvest_confirmation_date"] or not row["reinvest_rule_source"]:
                conn.execute("UPDATE fund_dividend_events SET error='等待官方红利再投净值日及份额确认规则' WHERE id=?",(event_id,))
                return created
            if row["reinvest_confirmation_date"]>as_of.isoformat():
                return created
            quote=self.repository.get_quote(product.id,date.fromisoformat(row["reinvest_nav_date"]),conn=conn)
            if quote is None or quote.unit_nav is None or quote.unit_nav<=ZERO:
                conn.execute("UPDATE fund_dividend_events SET error='等待官方指定日期的红利再投净值' WHERE id=?",(event_id,))
                return created
            amount=Decimal(row["amount"])
            reinvest_shares=amount/quote.unit_nav
            settlement=self.repository.create_transaction(Transaction(id=str(uuid4()),product_id=product.id,
                transaction_type=Kind.HOLDING_ADJUSTMENT,status=Status.CONFIRMED,
                trade_date=date.fromisoformat(row["reinvest_confirmation_date"]),confirmation_date=date.fromisoformat(row["reinvest_confirmation_date"]),
                idempotency_key=f"reinvest:{event_id}",amount=amount,shares=reinvest_shares,confirmation_nav=quote.unit_nav,
                linked_transaction_id=dividend.id,note="自动红利再投",created_by="fund_dividend"),conn)
            conn.execute("UPDATE transactions SET linked_transaction_id=?,settlement_status='settled',settled_at=CURRENT_TIMESTAMP WHERE id=?",(settlement.id,dividend.id))
            self.repository.replace_position(self.projector._calculate(product.id,conn),conn)
            conn.execute("UPDATE fund_dividend_events SET status='settled',settlement_transaction_id=?,reinvest_shares=?,error='' WHERE id=?",(settlement.id,decimal_text(reinvest_shares),event_id))
            self.repository.append_audit(str(uuid4()),"settle_reinvest_dividend","fund_dividend",event_id,
                after_json=json.dumps({"amount":decimal_text(amount),"shares":decimal_text(reinvest_shares),"nav":decimal_text(quote.unit_nav),"rule_source":row["reinvest_rule_source"],"share_precision":"internal_decimal","fees":"0"}),source="fund_dividend",conn=conn)
            return True
