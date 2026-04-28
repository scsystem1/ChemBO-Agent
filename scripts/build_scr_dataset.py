#!/usr/bin/env python3
"""
Build a dataset-oracle-ready SCR benchmark table from the raw SCR.csv export.

The raw dataset contains a small number of exact duplicate condition rows with
different reported conversion values. DatasetOracle expects a one-to-one mapping
from candidate conditions to target value, so we aggregate duplicates by taking
the mean Conversion and retain replicate metadata columns for inspection.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


FEATURE_COLUMNS = [
    "Fe",
    "Cu",
    "species",
    "Si_Al",
    "calcination_temp",
    "calcination_time",
    "Aging_O2",
    "Aging_H2O",
    "Aging_CO2",
    "Aging_Temp",
    "Aging_Time",
    "NO",
    "NH3",
    "O2",
    "H2O",
    "GHSV",
    "Measurement_Temp",
]
TARGET_COLUMN = "Conversion"


def build_scr_dataset(input_csv: Path, output_csv: Path) -> None:
    df = pd.read_csv(input_csv)
    missing = [column for column in FEATURE_COLUMNS + [TARGET_COLUMN] if column not in df.columns]
    if missing:
        raise ValueError(f"SCR dataset is missing required columns: {missing}")

    aggregated = (
        df.groupby(FEATURE_COLUMNS, as_index=False, dropna=False)
        .agg(
            Conversion=(TARGET_COLUMN, "mean"),
            replicate_count=(TARGET_COLUMN, "size"),
            Conversion_std=(TARGET_COLUMN, "std"),
        )
        .sort_values(FEATURE_COLUMNS, kind="stable")
        .reset_index(drop=True)
    )
    aggregated["Conversion_std"] = aggregated["Conversion_std"].fillna(0.0)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    aggregated.to_csv(output_csv, index=False)

    duplicate_groups = int((aggregated["replicate_count"] > 1).sum())
    removed_rows = int(len(df) - len(aggregated))
    print(f"Input rows: {len(df)}")
    print(f"Aggregated rows: {len(aggregated)}")
    print(f"Removed duplicate rows: {removed_rows}")
    print(f"Duplicate condition groups aggregated: {duplicate_groups}")
    print(f"Output written to: {output_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="data/SCR.csv",
        help="Path to the raw SCR CSV.",
    )
    parser.add_argument(
        "--output",
        default="data/SCR_aggregated.csv",
        help="Path to the aggregated SCR CSV.",
    )
    args = parser.parse_args()
    build_scr_dataset(Path(args.input).resolve(), Path(args.output).resolve())


if __name__ == "__main__":
    main()
