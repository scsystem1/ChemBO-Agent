#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def parse_method_name(folder_name: str) -> str:
    # e.g. kimi-k2.5_dar_problem_fingerprint_concat_DAR_run03 -> kimi-k2.5_dar_problem_fingerprint_concat_DAR
    m = re.match(r"^(.*)_run\d+$", folder_name)
    return m.group(1) if m else folder_name


def best_so_far(values: np.ndarray) -> np.ndarray:
    # maximize
    return np.maximum.accumulate(values)


def load_run_curve(csv_path: Path) -> np.ndarray:
    df = pd.read_csv(csv_path)
    if "yield" not in df.columns:
        raise ValueError(f"'yield' column not found in {csv_path}")
    y = df["yield"].to_numpy(dtype=float)
    return best_so_far(y)


def align_curves(curves: List[np.ndarray]) -> np.ndarray:
    """
    Align to max length by carrying forward last best-so-far value.
    Return shape: [n_runs, max_len]
    """
    max_len = max(len(c) for c in curves)
    arr = np.full((len(curves), max_len), np.nan, dtype=float)
    for i, c in enumerate(curves):
        arr[i, : len(c)] = c
        if len(c) < max_len:
            arr[i, len(c) :] = c[-1]
    return arr


def collect_method_curves(root: Path) -> Dict[str, List[np.ndarray]]:
    method_curves: Dict[str, List[np.ndarray]] = {}
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        csv_path = run_dir / "experiment_records.csv"
        if not csv_path.exists():
            continue
        method = parse_method_name(run_dir.name)
        curve = load_run_curve(csv_path)
        method_curves.setdefault(method, []).append(curve)
    return method_curves


def plot_best_so_far_with_std(method_curves: Dict[str, List[np.ndarray]], out_png: Path) -> None:
    plt.figure(figsize=(10, 6))

    for method, curves in sorted(method_curves.items()):
        if len(curves) == 0:
            continue
        aligned = align_curves(curves)
        mean = np.nanmean(aligned, axis=0)
        std = np.nanstd(aligned, axis=0)

        x = np.arange(1, len(mean) + 1)
        plt.plot(x, mean, label=f"{method} (n={len(curves)})")
        plt.fill_between(x, mean - std, mean + std, alpha=0.2)

    plt.xlabel("Iteration")
    plt.ylabel("Best-so-far yield")
    plt.title("Best-so-far Comparison (mean ± std)")
    plt.grid(True, alpha=0.3)
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(out_png, dpi=200)
    plt.close()


def main():
    root = Path("outputs/dar_problem_embedding_compare")
    if not root.exists():
        raise FileNotFoundError(f"Directory not found: {root}")

    method_curves = collect_method_curves(root)
    if not method_curves:
        raise RuntimeError(f"No valid run folders with experiment_records.csv under {root}")

    out_png = root / "best_so_far_mean_std.png"
    plot_best_so_far_with_std(method_curves, out_png)
    print(f"Saved plot to: {out_png}")


if __name__ == "__main__":
    main()