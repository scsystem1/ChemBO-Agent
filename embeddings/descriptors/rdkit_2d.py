from __future__ import annotations

from typing import Any


DEFAULT_RDKIT_2D = [
    "MolWt",
    "ExactMolWt",
    "MolLogP",
    "TPSA",
    "NumHAcceptors",
    "NumHDonors",
    "NumRotatableBonds",
    "HeavyAtomCount",
    "NHOHCount",
    "NOCount",
    "RingCount",
    "NumAromaticRings",
    "FractionCSP3",
    "FormalCharge",
    "NumValenceElectrons",
    "BertzCT",
    "LabuteASA",
]

SMARTS_COUNTERS = {
    "aryl_halide_count": "[c][F,Cl,Br,I]",
    "aryl_bromide_count": "[c][Br]",
    "aryl_chloride_count": "[c][Cl]",
    "aryl_iodide_count": "[c][I]",
    "boronic_acid_group_count": "[B;X3](O)O",
    "boronate_ester_group_count": "[B;X3]1OCCO1",
    "trifluoroborate_group_count": "[B-](F)(F)F",
    "phosphine_donor_count": "[P;X3]",
}


def _rdkit_modules() -> tuple[Any, Any, Any, Any]:
    try:
        from rdkit import Chem
        from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"RDKit is required for RDKit descriptors: {type(exc).__name__}: {exc}") from exc
    return Chem, Crippen, Descriptors, rdMolDescriptors


def mol_from_smiles(smiles: str) -> Any:
    Chem, _Crippen, _Descriptors, _rdMolDescriptors = _rdkit_modules()
    mol = Chem.MolFromSmiles(str(smiles or "").strip())
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles}")
    return mol


def canonicalize_smiles(smiles: str) -> str:
    Chem, _Crippen, _Descriptors, _rdMolDescriptors = _rdkit_modules()
    mol = mol_from_smiles(smiles)
    return str(Chem.MolToSmiles(mol, canonical=True))


def looks_like_smiles(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        mol_from_smiles(text)
    except Exception:
        return False
    return True


def calc_rdkit_2d(smiles: str, names: list[str] | None = None) -> dict[str, float]:
    _Chem, Crippen, Descriptors, rdMolDescriptors = _rdkit_modules()
    mol = mol_from_smiles(smiles)
    names = list(names or DEFAULT_RDKIT_2D)
    out: dict[str, float] = {}
    for name in names:
        if name == "MolLogP":
            value = Crippen.MolLogP(mol)
        elif name == "TPSA":
            value = rdMolDescriptors.CalcTPSA(mol)
        elif name == "FormalCharge":
            value = sum(atom.GetFormalCharge() for atom in mol.GetAtoms())
        elif name == "NumAromaticRings":
            value = rdMolDescriptors.CalcNumAromaticRings(mol)
        elif name == "RingCount":
            value = rdMolDescriptors.CalcNumRings(mol)
        elif hasattr(Descriptors, name):
            value = getattr(Descriptors, name)(mol)
        else:
            raise KeyError(f"Unsupported RDKit descriptor: {name}")
        out[name] = float(value)
    return out


def calc_smarts_counts(smiles: str, names: list[str]) -> dict[str, float]:
    Chem, _Crippen, _Descriptors, _rdMolDescriptors = _rdkit_modules()
    mol = mol_from_smiles(smiles)
    out: dict[str, float] = {}
    for name in names:
        if name not in SMARTS_COUNTERS:
            raise KeyError(f"Unsupported SMARTS counter: {name}")
        patt = Chem.MolFromSmarts(SMARTS_COUNTERS[name])
        if patt is None:
            raise ValueError(f"Invalid SMARTS for {name}: {SMARTS_COUNTERS[name]}")
        out[name] = float(len(mol.GetSubstructMatches(patt)))
    return out

