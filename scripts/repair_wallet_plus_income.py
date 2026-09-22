import argparse
from datetime import date
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.portfolio_db import PortfolioDatabase
from src.portfolio_repository import PortfolioRepository
from src.portfolio_income_repair import preview_wallet_income_repair, repair_wallet_income


def main():
    parser = argparse.ArgumentParser(description="预演并修复钱包Plus历史收益，保留人工校准目标")
    parser.add_argument("--database", required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--plan", required=True, help="预演结果JSON路径；执行时必须提供同一计划")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    repository = PortfolioRepository(PortfolioDatabase(Path(args.database)))
    plan_path = Path(args.plan)
    if args.execute:
        if args.start or args.end:
            parser.error("执行时日期以预演计划为准，不再传入 --start/--end")
        result = repair_wallet_income(repository, json.loads(plan_path.read_text(encoding="utf-8")))
    else:
        if not args.start or not args.end:
            parser.error("预演需要 --start 和 --end")
        result = preview_wallet_income_repair(repository, args.start, args.end)
        plan_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
