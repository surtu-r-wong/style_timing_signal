"""Generate the two production styles from one common, code-verified snapshot."""
import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile

from backtest.momentum_scan import momentum_pair_factor
from signals.common.style_prices import price_panels
from signals.slope20.generate_signal import FAMILY, LENGTH, SKIP, Z_WINDOW, SMOOTHING
from signals.equal_weight.generate_signal import (
    CONFIG_FILE, ROOT, calculate_contrast_equal_weight_signal, load_pair_configs,
)


def ew_output(panels, configs, lookback=20, z_window=40, smoothing=5):
    def calculate(prices):
        return calculate_contrast_equal_weight_signal(prices, lookback, z_window, smoothing, configs)
    standard, pure = panels
    out = calculate(standard)
    out["factor_value_pure"] = calculate(pure)["factor_value"]
    return out.round(4)


def slope_output(panels, configs):
    pairs = [c.effective_columns() for c in configs]

    def calculate(prices):
        return momentum_pair_factor(prices, pairs, FAMILY, LENGTH, SKIP, Z_WINDOW, SMOOTHING)
    standard, pure = panels
    out = calculate(standard).rename("factor_value").to_frame()
    out["factor_value_pure"] = calculate(pure)
    return out


def load_panels(configs, start=None, end=None):
    names = list(dict.fromkeys(n for c in configs for n in (c.left_column, c.right_column)))
    return price_panels(names, start, end)


def generate(directory, end=None):
    configs = load_pair_configs(CONFIG_FILE)
    panels = load_panels(configs, end=end)
    outputs = {
        "equal_weight/equal_weight_signal_20d40z.csv": ew_output(panels, configs),
        "equal_weight/equal_weight_signal_5d20z.csv": ew_output(panels, configs, 5, 20, 0),
        "slope20/slope20_signal_L20zw120.csv": slope_output(panels, configs),
    }
    staged = []
    try:
        for name, frame in outputs.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temp = Path(stream.name)
                staged.append((temp, path))
                frame.round(4).to_csv(stream, index_label="date")
            temp.chmod(0o644)
            row = frame.iloc[-1]
            print(f"{name} {frame.index[-1].date()} 标准 {row.factor_value:+.4f} 纯风格 {row.factor_value_pure:+.4f}")
        for temp, path in staged:
            temp.replace(path)
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--end")
    args = parser.parse_args()
    generate(args.output_dir, args.end)
