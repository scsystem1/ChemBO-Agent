from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "ori_data"
OUTPUT_DIR = ROOT / "processed_data"


PRECATALYST_SCAFFOLD_SMILES = {
    "P1": "COS(=O)(=O)[O-].[Pd+](Nc1ccccc1-c2ccccc2)",
    "P2": "Cl[Pd](Nc1ccccc1-c2ccccc2)",
}


LIGAND_SMILES = {
    "L1": "P(C4CCCCC4)(C3CCCCC3)c1c(cccc1)c2c(cc(cc2C(C)C)C(C)C)C(C)C",  # XPhos
    "L2": "P(C4CCCCC4)(C3CCCCC3)c1c(cccc1)c2c(cccc2OC)OC",  # SPhos
    "L3": "CC(C)Oc1cccc(OC(C)C)c1-c2ccccc2P(C3CCCCC3)C4CCCCC4",  # RuPhos
    "L4": "CC1(c2c(Oc3c1cccc3P(c4ccccc4)c5ccccc5)c(P(c6ccccc6)c7ccccc7)ccc2)C",  # Xantphos
    "L5": "C1CCC(CC1)P(C2CCCCC2)C3CCCCC3",  # PCy3
    "L6": "P(c1ccccc1)(c2ccccc2)c3ccccc3",  # PPh3
    "L7": "CC(C)(C)P(C(C)(C)C)C(C)(C)C",  # PtBu3
}


def split_catalyst_id(catalyst_id: str) -> tuple[str, str]:
    parts = str(catalyst_id).strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"Unexpected catalyst format: {catalyst_id!r}")
    return parts[0], parts[1]


def transform_row(row: dict[str, str]) -> dict[str, str]:
    scaffold_id, ligand_id = split_catalyst_id(row["catalyst"])
    try:
        scaffold_smiles = PRECATALYST_SCAFFOLD_SMILES[scaffold_id]
    except KeyError as exc:
        raise KeyError(f"Unknown precatalyst scaffold id: {scaffold_id}") from exc
    try:
        ligand_smiles = LIGAND_SMILES[ligand_id]
    except KeyError as exc:
        raise KeyError(f"Unknown ligand id: {ligand_id}") from exc

    return {
        "precatalyst_scaffold_SMILES": scaffold_smiles,
        "ligand_SMILES": ligand_smiles,
        "t_res": row["t_res"],
        "temperature": row["temperature"],
        "catalyst_loading": row["catalyst_loading"],
        "yld": row["yld"],
    }


def process_csv(path: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / path.name
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = [transform_row(row) for row in csv.DictReader(handle)]

    fieldnames = [
        "precatalyst_scaffold_SMILES",
        "ligand_SMILES",
        "t_res",
        "temperature",
        "catalyst_loading",
        "yld",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def main() -> None:
    input_paths = sorted(SOURCE_DIR.glob("reizman_suzuki_case_*.csv"))
    if not input_paths:
        raise FileNotFoundError(f"No Suzuki source CSV files found in {SOURCE_DIR}")

    written_paths = [process_csv(path) for path in input_paths]
    print(f"Processed {len(written_paths)} files into {OUTPUT_DIR}")
    for path in written_paths:
        print(path)


if __name__ == "__main__":
    main()
