from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_REACTION_PATH = ROOT / "baumgartner2019_reaction_data.csv"
SOURCE_COMPOUND_PATH = ROOT / "baumgartner2019_compound_info.csv"
OUTPUT_DIR = ROOT / "processed_data"
METADATA_PATH = OUTPUT_DIR / "baumgartner2019_cc_metadata.csv"


def load_name_to_smiles() -> dict[str, str]:
    with SOURCE_COMPOUND_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {row["name"].strip(): row["smiles"].strip() for row in rows}


def lookup_smiles(name_to_smiles: dict[str, str], name: str) -> str:
    key = str(name).strip()
    try:
        return name_to_smiles[key]
    except KeyError as exc:
        raise KeyError(f"Unknown compound name in Baumgartner dataset: {key!r}") from exc


def transform_row(row: dict[str, str], name_to_smiles: dict[str, str]) -> dict[str, str]:
    return {
        "precatalyst_SMILES": lookup_smiles(name_to_smiles, row["Precatalyst"]),
        "base_SMILES": lookup_smiles(name_to_smiles, row["Base"]),
        "base_concentration": row["Base concentration (M)"],
        "base_equivalents": row["Base equivalents"],
        "temperature": row["Temperature (degC)"],
        "t_res": row["Residence Time Actual (s)"],
        "yld": row["Reaction Yield"],
    }


def process_csv() -> list[Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name_to_smiles = load_name_to_smiles()

    with SOURCE_REACTION_PATH.open("r", encoding="utf-8", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    rows = [transform_row(row, name_to_smiles) for row in source_rows]

    fieldnames = [
        "precatalyst_SMILES",
        "base_SMILES",
        "base_concentration",
        "base_equivalents",
        "temperature",
        "t_res",
        "yld",
    ]

    out_paths: list[Path] = []
    metadata_rows: list[dict[str, str]] = []

    all_path = OUTPUT_DIR / "baumgartner2019_cc_all.csv"
    with all_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    out_paths.append(all_path)
    metadata_rows.append(
        {
            "file_name": all_path.name,
            "reaction_scope": "all",
            "nucleophile_name": "mixed",
            "nucleophile_SMILES": "mixed",
            "solvent_name": "mixed",
            "solvent_SMILES": "mixed",
        }
    )

    nucleophile_order: list[str] = []
    nucleophile_metadata: dict[str, dict[str, str]] = {}
    for source_row, row in zip(source_rows, rows):
        nucleophile_name = source_row["N-H nucleophile "].strip()
        if nucleophile_name not in nucleophile_order:
            nucleophile_order.append(nucleophile_name)
            nucleophile_metadata[nucleophile_name] = {
                "safe_name": nucleophile_name.lower().replace(" ", "_"),
                "nucleophile_SMILES": lookup_smiles(name_to_smiles, nucleophile_name),
                "solvent_name": source_row["Make-Up Solvent ID"].strip(),
                "solvent_SMILES": lookup_smiles(name_to_smiles, source_row["Make-Up Solvent ID"]),
            }

    for nucleophile_name in nucleophile_order:
        subset = [
            row
            for source_row, row in zip(source_rows, rows)
            if source_row["N-H nucleophile "].strip() == nucleophile_name
        ]
        safe_name = nucleophile_metadata[nucleophile_name]["safe_name"]
        out_path = OUTPUT_DIR / f"baumgartner2019_cc_{safe_name}.csv"
        with out_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(subset)
        out_paths.append(out_path)
        metadata_rows.append(
            {
                "file_name": out_path.name,
                "reaction_scope": "single_nucleophile",
                "nucleophile_name": nucleophile_name,
                "nucleophile_SMILES": nucleophile_metadata[nucleophile_name]["nucleophile_SMILES"],
                "solvent_name": nucleophile_metadata[nucleophile_name]["solvent_name"],
                "solvent_SMILES": nucleophile_metadata[nucleophile_name]["solvent_SMILES"],
            }
        )

    metadata_fieldnames = [
        "file_name",
        "reaction_scope",
        "nucleophile_name",
        "nucleophile_SMILES",
        "solvent_name",
        "solvent_SMILES",
    ]
    with METADATA_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=metadata_fieldnames)
        writer.writeheader()
        writer.writerows(metadata_rows)
    out_paths.append(METADATA_PATH)

    return out_paths


def main() -> None:
    if not SOURCE_REACTION_PATH.exists():
        raise FileNotFoundError(f"Missing reaction data: {SOURCE_REACTION_PATH}")
    if not SOURCE_COMPOUND_PATH.exists():
        raise FileNotFoundError(f"Missing compound metadata: {SOURCE_COMPOUND_PATH}")

    written_paths = process_csv()
    print(f"Processed {len(written_paths)} files into {OUTPUT_DIR}")
    for path in written_paths:
        print(path)


if __name__ == "__main__":
    main()
