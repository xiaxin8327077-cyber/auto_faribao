import argparse
from datetime import date
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.portfolio_db import PortfolioDatabase
from src.portfolio_repairs import (
    preview_known_redemption_repairs,
    repair_known_redemptions,
)
from src.portfolio_repository import PortfolioRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="修正已核实的赎回提前计入钱包Plus记录",
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    database = PortfolioDatabase(Path(args.database))
    repository = PortfolioRepository(database)
    if args.execute:
        database.initialize()
        result = repair_known_redemptions(repository, args.as_of)
    else:
        result = preview_known_redemption_repairs(repository, args.as_of)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
