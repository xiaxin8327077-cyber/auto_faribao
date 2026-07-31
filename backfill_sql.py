import sys
sys.path.insert(0, '/home/ubuntu/daily_report')
from src.portfolio_db import PortfolioDatabase
db = PortfolioDatabase('/home/ubuntu/daily_report/data/portfolio.db')
with db.connection() as conn:
    conn.execute("DROP TRIGGER IF EXISTS prevent_confirmed_transaction_mutation")
    # 回填 confirmed_at: 已确认交易用 created_at 作为确认时间
    rows = conn.execute("SELECT id, created_at FROM transactions WHERE confirmed_at IS NULL AND status = 'confirmed'").fetchall()
    for r in rows:
        conn.execute("UPDATE transactions SET confirmed_at = ? WHERE id = ?", (r[1], r[0]))
    print('confirmed_at backfilled:', len(rows))
    conn.commit()
print('done')
