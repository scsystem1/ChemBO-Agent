from __future__ import annotations

import math
import re

import numpy as np


ELEMENT_DATA: dict[str, dict[str, float]] = {
    "Al": {"atomic_number": 13.0, "atomic_weight": 26.9815, "group": 13.0, "period": 3.0, "pauling_electronegativity": 1.61, "covalent_radius_A": 1.21},
    "B": {"atomic_number": 5.0, "atomic_weight": 10.81, "group": 13.0, "period": 2.0, "pauling_electronegativity": 2.04, "covalent_radius_A": 0.84},
    "Ba": {"atomic_number": 56.0, "atomic_weight": 137.327, "group": 2.0, "period": 6.0, "pauling_electronegativity": 0.89, "covalent_radius_A": 2.15},
    "Br": {"atomic_number": 35.0, "atomic_weight": 79.904, "group": 17.0, "period": 4.0, "pauling_electronegativity": 2.96, "covalent_radius_A": 1.20},
    "C": {"atomic_number": 6.0, "atomic_weight": 12.011, "group": 14.0, "period": 2.0, "pauling_electronegativity": 2.55, "covalent_radius_A": 0.76},
    "Ca": {"atomic_number": 20.0, "atomic_weight": 40.078, "group": 2.0, "period": 4.0, "pauling_electronegativity": 1.00, "covalent_radius_A": 1.76},
    "Ce": {"atomic_number": 58.0, "atomic_weight": 140.116, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.12, "covalent_radius_A": 2.04},
    "Cl": {"atomic_number": 17.0, "atomic_weight": 35.45, "group": 17.0, "period": 3.0, "pauling_electronegativity": 3.16, "covalent_radius_A": 1.02},
    "Co": {"atomic_number": 27.0, "atomic_weight": 58.9332, "group": 9.0, "period": 4.0, "pauling_electronegativity": 1.88, "covalent_radius_A": 1.26},
    "Cs": {"atomic_number": 55.0, "atomic_weight": 132.905, "group": 1.0, "period": 6.0, "pauling_electronegativity": 0.79, "covalent_radius_A": 2.44},
    "Cu": {"atomic_number": 29.0, "atomic_weight": 63.546, "group": 11.0, "period": 4.0, "pauling_electronegativity": 1.90, "covalent_radius_A": 1.32},
    "Eu": {"atomic_number": 63.0, "atomic_weight": 151.964, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.20, "covalent_radius_A": 1.98},
    "F": {"atomic_number": 9.0, "atomic_weight": 18.998, "group": 17.0, "period": 2.0, "pauling_electronegativity": 3.98, "covalent_radius_A": 0.57},
    "Fe": {"atomic_number": 26.0, "atomic_weight": 55.845, "group": 8.0, "period": 4.0, "pauling_electronegativity": 1.83, "covalent_radius_A": 1.32},
    "H": {"atomic_number": 1.0, "atomic_weight": 1.008, "group": 1.0, "period": 1.0, "pauling_electronegativity": 2.20, "covalent_radius_A": 0.31},
    "Hf": {"atomic_number": 72.0, "atomic_weight": 178.49, "group": 4.0, "period": 6.0, "pauling_electronegativity": 1.30, "covalent_radius_A": 1.75},
    "I": {"atomic_number": 53.0, "atomic_weight": 126.904, "group": 17.0, "period": 5.0, "pauling_electronegativity": 2.66, "covalent_radius_A": 1.39},
    "K": {"atomic_number": 19.0, "atomic_weight": 39.0983, "group": 1.0, "period": 4.0, "pauling_electronegativity": 0.82, "covalent_radius_A": 2.03},
    "La": {"atomic_number": 57.0, "atomic_weight": 138.905, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.10, "covalent_radius_A": 2.07},
    "Li": {"atomic_number": 3.0, "atomic_weight": 6.94, "group": 1.0, "period": 2.0, "pauling_electronegativity": 0.98, "covalent_radius_A": 1.28},
    "Mg": {"atomic_number": 12.0, "atomic_weight": 24.305, "group": 2.0, "period": 3.0, "pauling_electronegativity": 1.31, "covalent_radius_A": 1.41},
    "Mn": {"atomic_number": 25.0, "atomic_weight": 54.938, "group": 7.0, "period": 4.0, "pauling_electronegativity": 1.55, "covalent_radius_A": 1.39},
    "Mo": {"atomic_number": 42.0, "atomic_weight": 95.95, "group": 6.0, "period": 5.0, "pauling_electronegativity": 2.16, "covalent_radius_A": 1.54},
    "N": {"atomic_number": 7.0, "atomic_weight": 14.007, "group": 15.0, "period": 2.0, "pauling_electronegativity": 3.04, "covalent_radius_A": 0.71},
    "Na": {"atomic_number": 11.0, "atomic_weight": 22.99, "group": 1.0, "period": 3.0, "pauling_electronegativity": 0.93, "covalent_radius_A": 1.66},
    "Nb": {"atomic_number": 41.0, "atomic_weight": 92.906, "group": 5.0, "period": 5.0, "pauling_electronegativity": 1.60, "covalent_radius_A": 1.64},
    "Nd": {"atomic_number": 60.0, "atomic_weight": 144.242, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.14, "covalent_radius_A": 2.01},
    "Ni": {"atomic_number": 28.0, "atomic_weight": 58.6934, "group": 10.0, "period": 4.0, "pauling_electronegativity": 1.91, "covalent_radius_A": 1.24},
    "O": {"atomic_number": 8.0, "atomic_weight": 15.999, "group": 16.0, "period": 2.0, "pauling_electronegativity": 3.44, "covalent_radius_A": 0.66},
    "P": {"atomic_number": 15.0, "atomic_weight": 30.974, "group": 15.0, "period": 3.0, "pauling_electronegativity": 2.19, "covalent_radius_A": 1.07},
    "Pd": {"atomic_number": 46.0, "atomic_weight": 106.42, "group": 10.0, "period": 5.0, "pauling_electronegativity": 2.20, "covalent_radius_A": 1.39},
    "S": {"atomic_number": 16.0, "atomic_weight": 32.06, "group": 16.0, "period": 3.0, "pauling_electronegativity": 2.58, "covalent_radius_A": 1.05},
    "Si": {"atomic_number": 14.0, "atomic_weight": 28.085, "group": 14.0, "period": 3.0, "pauling_electronegativity": 1.90, "covalent_radius_A": 1.11},
    "Sr": {"atomic_number": 38.0, "atomic_weight": 87.62, "group": 2.0, "period": 5.0, "pauling_electronegativity": 0.95, "covalent_radius_A": 1.95},
    "Tb": {"atomic_number": 65.0, "atomic_weight": 158.925, "group": 3.0, "period": 6.0, "pauling_electronegativity": 1.10, "covalent_radius_A": 1.94},
    "Ti": {"atomic_number": 22.0, "atomic_weight": 47.867, "group": 4.0, "period": 4.0, "pauling_electronegativity": 1.54, "covalent_radius_A": 1.60},
    "V": {"atomic_number": 23.0, "atomic_weight": 50.942, "group": 5.0, "period": 4.0, "pauling_electronegativity": 1.63, "covalent_radius_A": 1.53},
    "W": {"atomic_number": 74.0, "atomic_weight": 183.84, "group": 6.0, "period": 6.0, "pauling_electronegativity": 2.36, "covalent_radius_A": 1.62},
    "Y": {"atomic_number": 39.0, "atomic_weight": 88.906, "group": 3.0, "period": 5.0, "pauling_electronegativity": 1.22, "covalent_radius_A": 1.80},
    "Zn": {"atomic_number": 30.0, "atomic_weight": 65.38, "group": 12.0, "period": 4.0, "pauling_electronegativity": 1.65, "covalent_radius_A": 1.22},
    "Zr": {"atomic_number": 40.0, "atomic_weight": 91.224, "group": 4.0, "period": 5.0, "pauling_electronegativity": 1.33, "covalent_radius_A": 1.75},
}


FORMULA_ALIASES: dict[str, str] = {
    "BEA": "Al2Si30O64",
    "ZSM-5": "Al2Si94O192",
}


def parse_formula_to_counts(formula: str) -> dict[str, float] | None:
    compact = str(formula or "").strip()
    compact = FORMULA_ALIASES.get(compact, compact)
    if not compact or any(token in compact for token in {"/", ".", "[", "]", "(", ")", "+", "-", "@", "="}):
        return None
    matches = list(re.finditer(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)", compact))
    if not matches:
        return None
    if "".join(match.group(0) for match in matches) != compact:
        return None
    counts: dict[str, float] = {}
    for match in matches:
        symbol = match.group(1)
        if symbol not in ELEMENT_DATA:
            return None
        count = float(match.group(2)) if match.group(2) else 1.0
        counts[symbol] = counts.get(symbol, 0.0) + count
    return counts or None


def element_descriptor(symbol: str, name: str) -> float | None:
    data = ELEMENT_DATA.get(str(symbol or "").strip())
    if not data:
        return None
    if name in data:
        return float(data[name])
    if name == "vdw_radius_A":
        return None
    return None


def formula_descriptor(formula: str, name: str) -> float | None:
    counts = parse_formula_to_counts(formula)
    if not counts:
        return None
    total_atoms = float(sum(counts.values()))
    if total_atoms <= 0:
        return None
    formula_weight = sum(ELEMENT_DATA[symbol]["atomic_weight"] * count for symbol, count in counts.items())
    if name == "formula_weight_g_mol_formula_unit":
        return float(formula_weight)
    weighted = {
        key: sum(ELEMENT_DATA[symbol][key] * count for symbol, count in counts.items()) / total_atoms
        for key in ("atomic_number", "atomic_weight", "pauling_electronegativity")
    }
    if name == "mean_atomic_number":
        return float(weighted["atomic_number"])
    if name == "mean_atomic_weight":
        return float(weighted["atomic_weight"])
    if name == "mean_pauling_electronegativity":
        return float(weighted["pauling_electronegativity"])
    if name == "std_pauling_electronegativity":
        values = np.asarray(
            [ELEMENT_DATA[symbol]["pauling_electronegativity"] for symbol in counts for _ in range(int(max(counts[symbol], 1)))],
            dtype=float,
        )
        return float(np.std(values)) if len(values) else 0.0
    if name == "oxygen_atomic_fraction":
        return float(counts.get("O", 0.0) / total_atoms)
    if name == "metal_atomic_fraction":
        non_metals = {"B", "C", "H", "N", "O", "P", "S", "Si", "F", "Cl", "Br", "I"}
        metals = sum(count for symbol, count in counts.items() if symbol not in non_metals)
        return float(metals / total_atoms)
    if name == "oxygen_to_metal_ratio":
        non_metals = {"B", "C", "H", "N", "O", "P", "S", "Si", "F", "Cl", "Br", "I"}
        metals = sum(count for symbol, count in counts.items() if symbol not in non_metals)
        if metals <= 0:
            return math.inf
        return float(counts.get("O", 0.0) / metals)
    return None

