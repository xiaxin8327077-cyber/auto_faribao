import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.portfolio_db import PortfolioDatabase
from src.portfolio_repairs import (
    preview_known_wallet_redemption_repair,
    repair_known_wallet_redemption,
)
from src.portfolio_repository import PortfolioRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="修正已核实的钱包Plus周末赎回实时到账日期",
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    database = PortfolioDatabase(Path(args.database))
    repository = PortfolioRepository(database)
    if args.execute:
        preview_known_wallet_redemption_repair(repository)
        result = repair_known_wallet_redemption(repository)
    else:
        result = preview_known_wallet_redemption_repair(repository)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
