#!/usr/bin/env python
"""Plot cumulative-best yield (or another metric) from an experiment CSV."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _resolve_metric_column(frame: pd.DataFrame, requested: str) -> str:
    if requested in frame.columns:
        return requested
    normalized = {str(col).strip().lower(): col for col in frame.columns}
    key = requested.strip().lower()
    if key in normalized:
        return str(normalized[key])
    available = ", ".join(map(str, frame.columns))
    raise ValueError(f"Metric column '{requested}' not found. Available columns: {available}")


def _default_output_path(csv_path: Path, metric: str, direction: str) -> Path:
    safe_metric = metric.strip().replace(" ", "_")
    return csv_path.with_name(f"{csv_path.stem}_{safe_metric}_cumulative_best_{direction}.png")


def plot_cumulative_best(
    csv_path: Path,
    metric: str = "yield",
    direction: str = "maximize",
    output_path: Path | None = None,
    dpi: int = 200,
) -> Path:
    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {csv_path}")

    frame = pd.read_csv(csv_path)
    metric_col = _resolve_metric_column(frame, metric)

    values = pd.to_numeric(frame[metric_col], errors="coerce")
    valid_mask = values.notna()
    frame = frame.loc[valid_mask].copy()
    values = values.loc[valid_mask]
    if len(values) == 0:
        raise ValueError(f"No numeric data found in column '{metric_col}'.")

    if "iteration" in frame.columns:
        x_raw = pd.to_numeric(frame["iteration"], errors="coerce")
        fallback = pd.Series(range(1, len(frame) + 1), index=frame.index, dtype=float)
        x = x_raw.ffill().combine_first(fallback)
    else:
        x = pd.Series(range(1, len(frame) + 1), index=frame.index)

    if direction == "minimize":
        cumulative = values.cummin()
        best_idx = values.idxmin()
    else:
        cumulative = values.cummax()
        best_idx = values.idxmax()

    best_x = float(x.loc[best_idx])
    best_y = float(values.loc[best_idx])

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(x, values, marker="o", linewidth=1.2, alpha=0.35, label=f"Observed {metric_col}")
    ax.plot(x, cumulative, linewidth=2.4, label=f"Cumulative best {metric_col}")
    ax.scatter([best_x], [best_y], color="crimson", zorder=5, label=f"Best = {best_y:.4g}")

    ax.set_xlabel("Iteration")
    ax.set_ylabel(metric_col)
    ax.set_title(f"Cumulative Best {metric_col} ({direction})")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()

    final_output = output_path or _default_output_path(csv_path, metric_col, direction)
    final_output = final_output.resolve()
    final_output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(final_output, dpi=dpi)
    plt.close(fig)
    return final_output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot cumulative-best metric from experiment CSV.")
    parser.add_argument("--input", required=True, help="Path to experiment_records.csv")
    parser.add_argument("--metric", default="yield", help="Metric column name (default: yield)")
    parser.add_argument(
        "--direction",
        default="maximize",
        choices=("maximize", "minimize"),
        help="Optimization direction (default: maximize)",
    )
    parser.add_argument("--output", default=None, help="Output PNG path")
    parser.add_argument("--dpi", type=int, default=200, help="Output image DPI")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = plot_cumulative_best(
        csv_path=Path(args.input).expanduser().resolve(),
        metric=args.metric,
        direction=args.direction,
        output_path=Path(args.output).expanduser().resolve() if args.output else None,
        dpi=args.dpi,
    )
    print(f"Saved figure: {output}")


if __name__ == "__main__":
    main()
