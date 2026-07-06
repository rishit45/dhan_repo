import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_BACKTEST = ROOT / "strategy_" / "backtest.py"


def main():
    spec = importlib.util.spec_from_file_location("strategy_backtest", STRATEGY_BACKTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()


if __name__ == "__main__":
    main()
