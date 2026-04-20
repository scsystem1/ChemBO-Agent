"""
Component pools and lightweight BO runtime implementations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import itertools
import io
import math
import os
import sys
from pathlib import Path

import numpy as np

def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_import_torch_stack():
    """Import the optional torch/botorch stack only in environments likely to be stable.

    Some local macOS environments ship OpenMP runtimes that abort the process as
    soon as torch is imported. When that happens, the previous eager import made
    the entire module unusable even for fallback-only workflows that do not need
    BoTorch at all. To keep the rest of the system available, skip the torch
    stack by default on macOS unless the caller explicitly opts in.
    """

    if _env_flag("CHEMBO_DISABLE_TORCH_STACK"):
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "Torch/BoTorch stack disabled via CHEMBO_DISABLE_TORCH_STACK=1.",
        )

    if sys.platform == "darwin" and not _env_flag("CHEMBO_ENABLE_TORCH_STACK"):
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (
                "Torch/BoTorch import skipped on macOS. "
                "Set CHEMBO_ENABLE_TORCH_STACK=1 only in environments where torch import is known to be stable."
            ),
        )

    try:
        import torch as _torch
        from botorch.acquisition import LogExpectedImprovement as _LogExpectedImprovement, UpperConfidenceBound as _UpperConfidenceBound
        from botorch.fit import fit_gpytorch_mll as _fit_gpytorch_mll
        from botorch.models import SingleTaskGP as _SingleTaskGP
        from botorch.models.transforms.input import Normalize as _Normalize
        from botorch.models.transforms.outcome import Standardize as _Standardize
        from gpytorch.constraints import GreaterThan as _GreaterThan
        from gpytorch.kernels import AdditiveKernel as _AdditiveKernel, MaternKernel as _MaternKernel, ProductKernel as _ProductKernel, RBFKernel as _RBFKernel, ScaleKernel as _ScaleKernel
        from gpytorch.likelihoods import GaussianLikelihood as _GaussianLikelihood
        from gpytorch.mlls import ExactMarginalLogLikelihood as _ExactMarginalLogLikelihood
        try:
            from .smk_kernels import WrappedSMK as _WrappedSMK
        except ImportError:  # pragma: no cover
            from pools.smk_kernels import WrappedSMK as _WrappedSMK
    except Exception as exc:  # pragma: no cover
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            f"Torch/BoTorch stack unavailable: {type(exc).__name__}: {exc}",
        )

    return (
        _torch,
        _LogExpectedImprovement,
        _UpperConfidenceBound,
        _fit_gpytorch_mll,
        _SingleTaskGP,
        _Normalize,
        _Standardize,
        _GreaterThan,
        _AdditiveKernel,
        _MaternKernel,
        _ProductKernel,
        _RBFKernel,
        _ScaleKernel,
        _GaussianLikelihood,
        _ExactMarginalLogLikelihood,
        _WrappedSMK,
        None,
    )


(
    torch,
    LogExpectedImprovement,
    UpperConfidenceBound,
    fit_gpytorch_mll,
    SingleTaskGP,
    Normalize,
    Standardize,
    GreaterThan,
    AdditiveKernel,
    MaternKernel,
    ProductKernel,
    RBFKernel,
    ScaleKernel,
    GaussianLikelihood,
    ExactMarginalLogLikelihood,
    WrappedSMK,
    TORCH_STATUS_NOTE,
) = _safe_import_torch_stack()


def _safe_import_rdkit():
    """Import RDKit only when it is likely to be ABI-compatible.

    Some RDKit wheels are compiled against the NumPy 1.x C-API. Importing them
    under NumPy 2 can emit a long cascade of low-signal tracebacks and may even
    segfault the process before our Python fallback logic can help. To keep the
    core workflow usable, we skip RDKit by default on NumPy >= 2 unless the
    caller explicitly opts in via CHEMBO_ENABLE_RDKIT=1.
    """

    if _env_flag("CHEMBO_DISABLE_RDKIT"):
        return (None, None, None, None, None, None, None, None, "RDKit disabled via CHEMBO_DISABLE_RDKIT=1.")

    numpy_major = int(str(np.__version__).split(".", 1)[0])
    if numpy_major >= 2 and not _env_flag("CHEMBO_ENABLE_RDKIT"):
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (
                f"RDKit import skipped under NumPy {np.__version__}. "
                "Many RDKit builds are compiled against the NumPy 1.x ABI; "
                "downgrade to numpy<2 for chemistry-aware features, or set "
                "CHEMBO_ENABLE_RDKIT=1 if your RDKit build is known to support NumPy 2."
            ),
        )

    try:
        # Suppress noisy C-extension import tracebacks from optional RDKit paths.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from rdkit import Chem as _Chem
            from rdkit import DataStructs as _DataStructs
            from rdkit.Chem import AllChem as _AllChem
            from rdkit.Chem import Crippen as _Crippen
            from rdkit.Chem import Descriptors as _Descriptors
            from rdkit.Chem import Lipinski as _Lipinski
            from rdkit.Chem import rdFingerprintGenerator as _rdFingerprintGenerator
            from rdkit.Chem import rdMolDescriptors as _rdMolDescriptors
    except Exception as exc:  # pragma: no cover
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            f"RDKit unavailable: {type(exc).__name__}: {exc}",
        )

    return _Chem, _AllChem, _Crippen, _Descriptors, _Lipinski, _rdMolDescriptors, _rdFingerprintGenerator, _DataStructs, None


(
    Chem,
    AllChem,
    Crippen,
    Descriptors,
    Lipinski,
    rdMolDescriptors,
    rdFingerprintGenerator,
    DataStructs,
    RDKIT_STATUS_NOTE,
) = _safe_import_rdkit()


def _molclr_repo_candidates() -> list[str]:
    candidates = []
    env_path = os.getenv("CHEMBO_MOLCLR_PATH", "").strip()
    if env_path:
        candidates.append(env_path)
    repo_default = str(Path(__file__).resolve().parents[1] / "external" / "MolCLR")
    candidates.append(repo_default)
    seen = set()
    deduped = []
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _build_local_molclr_embed(repo_root: str):
    if torch is None:
        return None, "MolCLR local adapter unavailable because torch is not available."
    if Chem is None:
        return None, "MolCLR local adapter unavailable because RDKit is not available."
    try:
        import yaml  # type: ignore
        from torch_geometric.data import Data  # type: ignore
        from models.ginet_molclr import GINet  # type: ignore
    except Exception as exc:  # pragma: no cover
        return None, f"MolCLR local adapter unavailable: {type(exc).__name__}: {exc}"

    config_path = os.path.join(repo_root, "config.yaml")
    ckpt_path = os.path.join(repo_root, "ckpt", "pretrained_gin", "checkpoints", "model.pth")
    if not os.path.exists(config_path):
        return None, f"MolCLR local adapter unavailable: missing config.yaml in {repo_root}"
    if not os.path.exists(ckpt_path):
        return None, f"MolCLR local adapter unavailable: missing pretrained checkpoint at {ckpt_path}"

    try:
        config = yaml.load(Path(config_path).read_text(encoding="utf-8"), Loader=yaml.FullLoader)
        model_cfg = dict(config.get("model") or {})
        model = GINet(**model_cfg)
        state_dict = torch.load(ckpt_path, map_location="cpu")
        model.load_state_dict(state_dict)
        model.eval()
    except Exception as exc:  # pragma: no cover
        return None, f"MolCLR local adapter failed to load pretrained GIN: {type(exc).__name__}: {exc}"

    atom_list = list(range(1, 119))
    chirality_list = [
        Chem.rdchem.ChiralType.CHI_UNSPECIFIED,
        Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW,
        Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CCW,
        Chem.rdchem.ChiralType.CHI_OTHER,
    ]
    bond_list = [
        Chem.rdchem.BondType.SINGLE,
        Chem.rdchem.BondType.DOUBLE,
        Chem.rdchem.BondType.TRIPLE,
        Chem.rdchem.BondType.AROMATIC,
    ]
    bonddir_list = [
        Chem.rdchem.BondDir.NONE,
        Chem.rdchem.BondDir.ENDUPRIGHT,
        Chem.rdchem.BondDir.ENDDOWNRIGHT,
    ]

    def _smiles_to_data(smiles: str):
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        mol = Chem.AddHs(mol)
        type_idx = []
        chirality_idx = []
        for atom in mol.GetAtoms():
            atomic_num = atom.GetAtomicNum()
            if atomic_num not in atom_list:
                return None
            type_idx.append(atom_list.index(atomic_num))
            chiral_tag = atom.GetChiralTag()
            if chiral_tag not in chirality_list:
                chiral_tag = Chem.rdchem.ChiralType.CHI_OTHER
            chirality_idx.append(chirality_list.index(chiral_tag))

        x1 = torch.tensor(type_idx, dtype=torch.long).view(-1, 1)
        x2 = torch.tensor(chirality_idx, dtype=torch.long).view(-1, 1)
        x = torch.cat([x1, x2], dim=-1)

        row, col, edge_feat = [], [], []
        for bond in mol.GetBonds():
            start, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
            bond_type = bond.GetBondType()
            bond_dir = bond.GetBondDir()
            if bond_type not in bond_list:
                continue
            if bond_dir not in bonddir_list:
                bond_dir = Chem.rdchem.BondDir.NONE
            feature = [bond_list.index(bond_type), bonddir_list.index(bond_dir)]
            row += [start, end]
            col += [end, start]
            edge_feat.append(feature)
            edge_feat.append(feature)

        if edge_feat:
            edge_index = torch.tensor([row, col], dtype=torch.long)
            edge_attr = torch.tensor(np.asarray(edge_feat), dtype=torch.long)
        else:
            edge_index = torch.zeros((2, 0), dtype=torch.long)
            edge_attr = torch.zeros((0, 2), dtype=torch.long)
        data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)
        data.batch = torch.zeros(x.shape[0], dtype=torch.long)
        return data

    def _embed(smiles: str) -> np.ndarray | None:
        data = _smiles_to_data(str(smiles))
        if data is None:
            return None
        try:
            with torch.no_grad():
                h, _out = model(data)
        except Exception:
            return None
        array = h.detach().cpu().numpy().reshape(-1)
        if array.size == 0 or np.any(~np.isfinite(array)):
            return None
        return array

    return _embed, (
        "MolCLR local adapter loaded pretrained GIN from external repo; "
        "using the pooled penultimate representation `h` as the embedding."
    )


def _safe_import_molclr():
    """Best-effort import for optional MolCLR embedding runtime."""

    if _env_flag("CHEMBO_DISABLE_MOLCLR"):
        return None, "MolCLR disabled via CHEMBO_DISABLE_MOLCLR=1."
    candidate_paths = _molclr_repo_candidates()
    for candidate in candidate_paths:
        if candidate and os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)
    import_error_note = None
    try:
        module = importlib.import_module("molclr")
    except Exception as exc:  # pragma: no cover
        module = None
        import_error_note = f"MolCLR module import skipped: {type(exc).__name__}: {exc}"

    if module is not None:
        encode_fn = getattr(module, "encode_smiles", None)
        if callable(encode_fn):
            def _embed(smiles: str) -> np.ndarray | None:
                try:
                    result = encode_fn([smiles])
                except Exception:
                    return None
                if result is None:
                    return None
                array = np.asarray(result, dtype=float)
                if array.ndim == 2 and array.shape[0] >= 1:
                    return array[0].reshape(-1)
                if array.ndim == 1:
                    return array.reshape(-1)
                return None

            return _embed, None

        molclr_cls = getattr(module, "MolCLR", None)
        if callable(molclr_cls):
            model = None
            try:
                model = molclr_cls()
            except Exception:
                model = None
            if model is not None:
                for method_name in ("encode", "embed", "transform"):
                    method = getattr(model, method_name, None)
                    if not callable(method):
                        continue

                    def _embed(smiles: str, _method=method) -> np.ndarray | None:
                        try:
                            result = _method([smiles])
                        except Exception:
                            try:
                                result = _method(smiles)
                            except Exception:
                                return None
                        if result is None:
                            return None
                        array = np.asarray(result, dtype=float)
                        if array.ndim == 2 and array.shape[0] >= 1:
                            return array[0].reshape(-1)
                        if array.ndim == 1:
                            return array.reshape(-1)
                        return None

                    return _embed, None

    for candidate in candidate_paths:
        if not candidate or not os.path.isdir(candidate):
            continue
        embed, note = _build_local_molclr_embed(candidate)
        if callable(embed):
            if import_error_note:
                note = f"{import_error_note}; {note}"
            return embed, note

    if import_error_note:
        return None, import_error_note
    return None, "MolCLR import succeeded but no compatible encoder API was found."


MOLCLR_EMBED, MOLCLR_STATUS_NOTE = _safe_import_molclr()


_CHEMBERTA_CACHE: dict[str, Any] = {"embed": None, "note": None}


def _chemberta_snapshot_candidates() -> list[str]:
    candidates = []
    env_path = os.getenv("CHEMBO_CHEMBERTA_PATH", "").strip()
    if env_path:
        candidates.append(env_path)
    hf_root = Path.home() / ".cache" / "huggingface" / "hub" / "models--DeepChem--ChemBERTa-77M-MLM"
    ref_path = hf_root / "refs" / "main"
    if ref_path.exists():
        try:
            ref = ref_path.read_text(encoding="utf-8").strip()
            snap = hf_root / "snapshots" / ref
            if snap.exists():
                candidates.append(str(snap))
        except Exception:
            pass
    snapshots_dir = hf_root / "snapshots"
    if snapshots_dir.exists():
        for child in snapshots_dir.iterdir():
            if child.is_dir():
                candidates.append(str(child))
    seen = set()
    deduped = []
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _build_local_chemberta_embed(snapshot_path: str):
    if torch is None:
        return None, "ChemBERTa local adapter unavailable because torch is not available."
    try:
        from transformers import AutoModel, AutoTokenizer  # type: ignore
    except Exception as exc:  # pragma: no cover
        return None, f"ChemBERTa local adapter unavailable: {type(exc).__name__}: {exc}"

    snapshot = Path(snapshot_path)
    if not snapshot.exists():
        return None, f"ChemBERTa local adapter unavailable: missing snapshot path {snapshot_path}"

    try:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        tokenizer = AutoTokenizer.from_pretrained(str(snapshot), local_files_only=True)
        model = AutoModel.from_pretrained(str(snapshot), local_files_only=True)
        model.eval()
    except Exception as exc:  # pragma: no cover
        return None, f"ChemBERTa local adapter failed to load snapshot: {type(exc).__name__}: {exc}"

    def _embed(smiles: str) -> np.ndarray | None:
        try:
            batch = tokenizer([str(smiles)], return_tensors="pt", padding=True, truncation=True)
            with torch.no_grad():
                outputs = model(**batch)
            hidden = outputs.last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1.0)
        except Exception:
            return None
        array = pooled.squeeze(0).detach().cpu().numpy().reshape(-1)
        if array.size == 0 or np.any(~np.isfinite(array)):
            return None
        return array

    return _embed, (
        "ChemBERTa local adapter loaded cached DeepChem/ChemBERTa-77M-MLM snapshot; "
        "using attention-mask mean pooling of the last hidden state."
    )


def _safe_import_chemberta():
    cached_embed = _CHEMBERTA_CACHE.get("embed")
    cached_note = _CHEMBERTA_CACHE.get("note")
    if cached_embed is not None or cached_note is not None:
        return cached_embed, cached_note

    for candidate in _chemberta_snapshot_candidates():
        embed, note = _build_local_chemberta_embed(candidate)
        if callable(embed):
            _CHEMBERTA_CACHE["embed"] = embed
            _CHEMBERTA_CACHE["note"] = note
            return embed, note

    note = "ChemBERTa unavailable: no local cached DeepChem/ChemBERTa-77M-MLM snapshot found."
    _CHEMBERTA_CACHE["embed"] = None
    _CHEMBERTA_CACHE["note"] = note
    return None, note


CHEMBERTA_EMBED, CHEMBERTA_STATUS_NOTE = _safe_import_chemberta()


def refresh_optional_embedding_backends() -> None:
    global MOLCLR_EMBED, MOLCLR_STATUS_NOTE, CHEMBERTA_EMBED, CHEMBERTA_STATUS_NOTE
    _CHEMBERTA_CACHE["embed"] = None
    _CHEMBERTA_CACHE["note"] = None
    MOLCLR_EMBED, MOLCLR_STATUS_NOTE = _safe_import_molclr()
    CHEMBERTA_EMBED, CHEMBERTA_STATUS_NOTE = _safe_import_chemberta()


def describe_optional_embedding_backends() -> dict[str, Any]:
    capabilities = detect_runtime_capabilities()
    return {
        "runtime_capabilities": capabilities,
        "molclr": {
            "env_path": os.getenv("CHEMBO_MOLCLR_PATH", "").strip() or None,
            "candidate_paths": _molclr_repo_candidates(),
            "available": capabilities.get("molclr", False),
            "status_note": MOLCLR_STATUS_NOTE,
        },
        "chemberta": {
            "env_path": os.getenv("CHEMBO_CHEMBERTA_PATH", "").strip() or None,
            "candidate_paths": _chemberta_snapshot_candidates(),
            "available": capabilities.get("chemberta", False),
            "status_note": CHEMBERTA_STATUS_NOTE,
        },
    }

@dataclass
class PoolEntry:
    key: str
    display_name: str
    description: str
    tags: dict[str, Any] = field(default_factory=dict)
    factory: Callable[..., Any] | None = None


def detect_runtime_capabilities() -> dict[str, Any]:
    rdkit_available = Chem is not None and rdFingerprintGenerator is not None and DataStructs is not None
    molclr_available = callable(MOLCLR_EMBED)
    chemberta_available = callable(CHEMBERTA_EMBED)
    torch_stack_present = all(
        dependency is not None
        for dependency in (
            torch,
            SingleTaskGP,
            fit_gpytorch_mll,
            ExactMarginalLogLikelihood,
            ScaleKernel,
            MaternKernel,
            RBFKernel,
            LogExpectedImprovement,
            UpperConfidenceBound,
            Standardize,
        )
    )
    notes = []
    if RDKIT_STATUS_NOTE:
        notes.append(RDKIT_STATUS_NOTE)
    if MOLCLR_STATUS_NOTE:
        notes.append(MOLCLR_STATUS_NOTE)
    if CHEMBERTA_STATUS_NOTE:
        notes.append(CHEMBERTA_STATUS_NOTE)
    if TORCH_STATUS_NOTE:
        notes.append(TORCH_STATUS_NOTE)
    if not torch_stack_present:
        notes.append("BoTorch stack is unavailable in the current environment.")
    else:
        notes.append("BoTorch stack enabled.")
    return {
        "rdkit": rdkit_available,
        "molclr": molclr_available,
        "chemberta": chemberta_available,
        "torch_stack": bool(torch_stack_present),
        "runtime_mode": "botorch" if torch_stack_present else "fallback_only",
        "notes": notes,
    }


class BaseEncoder:
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        self.search_space = search_space
        self.params = params or {}
        self.metadata: dict[str, Any] = {"notes": []}
        self._dim = 0

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        raise NotImplementedError

    def encode_batch(self, candidates: list[dict[str, Any]]) -> np.ndarray:
        if not candidates:
            return np.zeros((0, self.dim), dtype=float)
        return np.vstack([self.encode(candidate) for candidate in candidates]).astype(float)

    def get_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return np.zeros(self.dim, dtype=float), np.ones(self.dim, dtype=float)

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        raise NotImplementedError


class OneHotEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.specs: list[dict[str, Any]] = []
        offset = 0
        for variable in search_space:
            variable_type = variable.get("type", "categorical")
            if variable_type == "continuous":
                low, high = _continuous_bounds(variable)
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "continuous",
                        "low": low,
                        "high": high,
                        "slice": slice(offset, offset + 1),
                    }
                )
                offset += 1
            elif _numeric_domain_spec(variable) is not None:
                numeric_spec = _numeric_domain_spec(variable)
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "numeric_categorical",
                        "labels": numeric_spec["labels"],
                        "value_map": numeric_spec["value_map"],
                        "low": numeric_spec["low"],
                        "high": numeric_spec["high"],
                        "slice": slice(offset, offset + 1),
                    }
                )
                self.metadata["notes"].append(
                    f"{variable['name']}: numeric categorical domain encoded as a normalized scalar passthrough."
                )
                offset += 1
            else:
                labels = _domain_labels(variable) or ["unknown"]
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels,
                        "slice": slice(offset, offset + len(labels)),
                    }
                )
                offset += len(labels)
        self._dim = offset

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                vector[spec["slice"]] = _normalize_continuous(
                    spec["value_map"].get(str(value), value),
                    spec["low"],
                    spec["high"],
                )
            else:
                labels = spec["labels"]
                vector[spec["slice"].start + _safe_index(labels, value)] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        decoded = {}
        for spec in self.specs:
            chunk = encoded[spec["slice"]]
            if spec["type"] == "continuous":
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                numeric_value = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
                decoded[spec["name"]] = min(
                    spec["labels"],
                    key=lambda label: abs(float(spec["value_map"][label]) - numeric_value),
                )
            else:
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded


class FingerprintConcatEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.radius = int(self.params.get("radius", 2))
        self.n_bits = int(self.params.get("n_bits", 256))
        self.pca_dim = int(self.params.get("pca_dim", 16))
        self.pca_fit_samples = max(32, int(self.params.get("pca_fit_samples", 2048)))
        self.specs: list[dict[str, Any]] = []
        offset = 0
        has_fp = False
        for variable in search_space:
            variable_type = variable.get("type", "categorical")
            if variable_type == "continuous":
                low, high = _continuous_bounds(variable)
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "continuous",
                        "low": low,
                        "high": high,
                        "slice": slice(offset, offset + 1),
                    }
                )
                offset += 1
                continue
            numeric_spec = _numeric_domain_spec(variable)
            if numeric_spec is not None:
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "numeric_categorical",
                        "labels": numeric_spec["labels"],
                        "value_map": numeric_spec["value_map"],
                        "low": numeric_spec["low"],
                        "high": numeric_spec["high"],
                        "slice": slice(offset, offset + 1),
                    }
                )
                self.metadata["notes"].append(
                    f"{variable['name']}: numeric categorical domain encoded as a normalized scalar passthrough."
                )
                offset += 1
                continue
            labels = _domain_labels(variable) or ["unknown"]
            fp_map = {}
            for label, smiles in _variable_smiles_map(variable).items():
                fingerprint = _fingerprint_from_smiles(smiles, self.radius, self.n_bits)
                if fingerprint is not None:
                    fp_map[label] = fingerprint
            if fp_map and len(fp_map) == len(labels):
                has_fp = True
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "fingerprint",
                        "labels": labels,
                        "fp_map": fp_map,
                        "slice": slice(offset, offset + self.n_bits),
                    }
                )
                offset += self.n_bits
            else:
                if _variable_smiles_map(variable):
                    self.metadata["notes"].append(
                        f"{variable['name']}: RDKit fingerprints unavailable or incomplete; using one-hot fallback."
                    )
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels,
                        "slice": slice(offset, offset + len(labels)),
                    }
                )
                offset += len(labels)
        if not has_fp:
            self.metadata["notes"].append("No valid molecular fingerprints found; encoder behaves like one-hot.")
        self._raw_dim = offset
        self._pca_mean: np.ndarray | None = None
        self._pca_components: np.ndarray | None = None
        self._initialize_pca()
        self._dim = self._pca_components.shape[0] if self._pca_components is not None else self._raw_dim

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = self._encode_raw(candidate)
        if self._pca_components is None or self._pca_mean is None:
            return vector
        centered = vector - self._pca_mean
        return self._pca_components @ centered

    def _encode_raw(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self._raw_dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                vector[spec["slice"]] = _normalize_continuous(
                    spec["value_map"].get(str(value), value),
                    spec["low"],
                    spec["high"],
                )
            elif spec["type"] == "fingerprint":
                vector[spec["slice"]] = spec["fp_map"].get(str(value), np.zeros(self.n_bits, dtype=float))
            else:
                labels = spec["labels"]
                vector[spec["slice"].start + _safe_index(labels, value)] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        if self._pca_components is not None and self._pca_mean is not None:
            encoded = self._pca_mean + self._pca_components.T @ encoded
        decoded = {}
        for spec in self.specs:
            chunk = encoded[spec["slice"]]
            if spec["type"] == "continuous":
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                numeric_value = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
                decoded[spec["name"]] = min(
                    spec["labels"],
                    key=lambda label: abs(float(spec["value_map"][label]) - numeric_value),
                )
            elif spec["type"] == "fingerprint":
                best_label = min(
                    spec["fp_map"],
                    key=lambda label: float(np.linalg.norm(chunk - spec["fp_map"][label])),
                )
                decoded[spec["name"]] = best_label
            else:
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded

    def _initialize_pca(self) -> None:
        target_dim = max(1, int(self.pca_dim))
        if self._raw_dim <= target_dim:
            self.metadata["notes"].append(
                f"FingerprintConcat raw dim={self._raw_dim}; PCA skipped because raw dim <= target dim {target_dim}."
            )
            return

        candidates: list[dict[str, Any]] = []
        discrete_total = discrete_search_space_size(self.search_space, max_candidates=self.pca_fit_samples)
        if discrete_total is not None and discrete_total <= self.pca_fit_samples:
            candidates = enumerate_discrete_candidates(self.search_space, max_candidates=self.pca_fit_samples)
        if not candidates:
            candidates = hybrid_sample_candidates(self.search_space, num_samples=self.pca_fit_samples, seed=0)
        if not candidates:
            self.metadata["notes"].append("PCA skipped: unable to build reference candidates.")
            return

        X = np.vstack([self._encode_raw(candidate) for candidate in candidates]).astype(float)
        n_samples, n_features = X.shape
        if n_samples < 2:
            self.metadata["notes"].append("PCA skipped: insufficient reference samples.")
            return

        max_rank = min(n_samples - 1, n_features)
        use_dim = min(target_dim, max_rank)
        if use_dim <= 0:
            self.metadata["notes"].append("PCA skipped: non-positive target rank.")
            return

        mean = np.mean(X, axis=0)
        centered = X - mean
        try:
            _, singular_values, vt = np.linalg.svd(centered, full_matrices=False)
        except np.linalg.LinAlgError as exc:
            self.metadata["notes"].append(f"PCA skipped: SVD failed ({type(exc).__name__}: {exc}).")
            return

        components = vt[:use_dim, :]
        total_var = float(np.sum(singular_values ** 2))
        kept_var = float(np.sum((singular_values[:use_dim]) ** 2))
        explained = kept_var / total_var if total_var > 0 else 0.0
        self._pca_mean = mean
        self._pca_components = components
        self.metadata["notes"].append(
            f"Applied PCA to fingerprint_concat: raw dim={self._raw_dim} -> pca dim={use_dim} "
            f"(explained_variance={explained:.3f}, fit_samples={n_samples})."
        )


class MolCLRConcatEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.pca_dim = int(self.params.get("pca_dim", 16))
        self.pca_fit_samples = max(32, int(self.params.get("pca_fit_samples", 2048)))
        self.specs: list[dict[str, Any]] = []
        molecular_offset = 0
        passthrough_offset = 0
        has_molclr = False

        if MOLCLR_STATUS_NOTE:
            self.metadata["notes"].append(MOLCLR_STATUS_NOTE)

        for variable in search_space:
            variable_type = variable.get("type", "categorical")
            if variable_type == "continuous":
                low, high = _continuous_bounds(variable)
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "continuous",
                        "low": low,
                        "high": high,
                        "passthrough_slice": slice(passthrough_offset, passthrough_offset + 1),
                    }
                )
                passthrough_offset += 1
                continue
            numeric_spec = _numeric_domain_spec(variable)
            if numeric_spec is not None:
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "numeric_categorical",
                        "labels": numeric_spec["labels"],
                        "value_map": numeric_spec["value_map"],
                        "low": numeric_spec["low"],
                        "high": numeric_spec["high"],
                        "passthrough_slice": slice(passthrough_offset, passthrough_offset + 1),
                    }
                )
                self.metadata["notes"].append(
                    f"{variable['name']}: numeric categorical domain encoded as a normalized scalar passthrough."
                )
                passthrough_offset += 1
                continue

            labels = _domain_labels(variable) or ["unknown"]
            smiles_map = _variable_smiles_map(variable)
            molclr_map: dict[str, np.ndarray] = {}
            embedding_dim: int | None = None
            if smiles_map and callable(MOLCLR_EMBED):
                for label, smiles in smiles_map.items():
                    vector = _molclr_embedding_from_smiles(smiles)
                    if vector is None:
                        continue
                    if embedding_dim is None:
                        embedding_dim = int(vector.shape[0])
                    if embedding_dim != int(vector.shape[0]):
                        continue
                    molclr_map[label] = vector

            if molclr_map and len(molclr_map) == len(labels) and embedding_dim is not None:
                has_molclr = True
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "molclr",
                        "labels": labels,
                        "molclr_map": molclr_map,
                        "embedding_dim": embedding_dim,
                        "molecular_slice": slice(molecular_offset, molecular_offset + embedding_dim),
                    }
                )
                molecular_offset += embedding_dim
            else:
                if smiles_map:
                    self.metadata["notes"].append(
                        f"{variable['name']}: MolCLR embeddings unavailable or incomplete; using one-hot fallback."
                    )
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels,
                        "passthrough_slice": slice(passthrough_offset, passthrough_offset + len(labels)),
                    }
                )
                passthrough_offset += len(labels)

        if not has_molclr:
            self.metadata["notes"].append("No valid MolCLR embeddings found; encoder behaves like one-hot.")

        self._molecular_raw_dim = molecular_offset
        self._passthrough_dim = passthrough_offset
        self._pca_mean: np.ndarray | None = None
        self._pca_components: np.ndarray | None = None
        self._initialize_pca()
        pca_out_dim = self._pca_components.shape[0] if self._pca_components is not None else self._molecular_raw_dim
        self._dim = pca_out_dim + self._passthrough_dim

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        molecular = self._encode_molecular_raw(candidate)
        passthrough = self._encode_passthrough(candidate)
        if self._pca_components is not None and self._pca_mean is not None:
            molecular = self._pca_components @ (molecular - self._pca_mean)
        return np.concatenate([molecular, passthrough]).astype(float)

    def _encode_molecular_raw(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self._molecular_raw_dim, dtype=float)
        for spec in self.specs:
            if spec["type"] == "molclr":
                value = candidate.get(spec["name"])
                emb = spec["molclr_map"].get(str(value))
                if emb is not None:
                    vector[spec["molecular_slice"]] = emb
        return vector

    def _encode_passthrough(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self._passthrough_dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["passthrough_slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                vector[spec["passthrough_slice"]] = _normalize_continuous(
                    spec["value_map"].get(str(value), value),
                    spec["low"],
                    spec["high"],
                )
            elif spec["type"] == "categorical":
                labels = spec["labels"]
                vector[spec["passthrough_slice"].start + _safe_index(labels, value)] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        pca_out_dim = self._pca_components.shape[0] if self._pca_components is not None else self._molecular_raw_dim
        molecular_chunk = encoded[:pca_out_dim]
        passthrough_chunk = encoded[pca_out_dim:]
        if self._pca_components is not None and self._pca_mean is not None:
            molecular_chunk = self._pca_mean + self._pca_components.T @ molecular_chunk
        decoded = {}
        for spec in self.specs:
            if spec["type"] == "continuous":
                chunk = passthrough_chunk[spec["passthrough_slice"]]
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                chunk = passthrough_chunk[spec["passthrough_slice"]]
                numeric_value = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
                decoded[spec["name"]] = min(
                    spec["labels"],
                    key=lambda label: abs(float(spec["value_map"][label]) - numeric_value),
                )
            elif spec["type"] == "molclr":
                chunk = molecular_chunk[spec["molecular_slice"]]
                decoded[spec["name"]] = min(
                    spec["molclr_map"],
                    key=lambda label: float(np.linalg.norm(chunk - spec["molclr_map"][label])),
                )
            else:
                chunk = passthrough_chunk[spec["passthrough_slice"]]
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded

    def _initialize_pca(self) -> None:
        target_dim = max(1, int(self.pca_dim))
        if self._molecular_raw_dim <= target_dim:
            self.metadata["notes"].append(
                f"MolCLRConcat molecular raw dim={self._molecular_raw_dim}; PCA skipped because raw molecular dim <= target dim {target_dim}."
            )
            return

        candidates: list[dict[str, Any]] = []
        discrete_total = discrete_search_space_size(self.search_space, max_candidates=self.pca_fit_samples)
        if discrete_total is not None and discrete_total <= self.pca_fit_samples:
            candidates = enumerate_discrete_candidates(self.search_space, max_candidates=self.pca_fit_samples)
        if not candidates:
            candidates = hybrid_sample_candidates(self.search_space, num_samples=self.pca_fit_samples, seed=0)
        if not candidates:
            self.metadata["notes"].append("PCA skipped: unable to build reference candidates.")
            return

        X = np.vstack([self._encode_molecular_raw(candidate) for candidate in candidates]).astype(float)
        n_samples, n_features = X.shape
        if n_samples < 2:
            self.metadata["notes"].append("PCA skipped: insufficient reference samples.")
            return

        max_rank = min(n_samples - 1, n_features)
        use_dim = min(target_dim, max_rank)
        if use_dim <= 0:
            self.metadata["notes"].append("PCA skipped: non-positive target rank.")
            return

        mean = np.mean(X, axis=0)
        centered = X - mean
        try:
            _, singular_values, vt = np.linalg.svd(centered, full_matrices=False)
        except np.linalg.LinAlgError as exc:
            self.metadata["notes"].append(f"PCA skipped: SVD failed ({type(exc).__name__}: {exc}).")
            return

        components = vt[:use_dim, :]
        total_var = float(np.sum(singular_values ** 2))
        kept_var = float(np.sum((singular_values[:use_dim]) ** 2))
        explained = kept_var / total_var if total_var > 0 else 0.0
        self._pca_mean = mean
        self._pca_components = components
        self.metadata["notes"].append(
            f"Applied PCA to molclr molecular block only: raw dim={self._molecular_raw_dim} -> pca dim={use_dim} "
            f"(passthrough_dim={self._passthrough_dim}, explained_variance={explained:.3f}, fit_samples={n_samples})."
        )


class ChemBERTaConcatEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.pca_dim = int(self.params.get("pca_dim", 16))
        self.pca_fit_samples = max(32, int(self.params.get("pca_fit_samples", 2048)))
        self.specs: list[dict[str, Any]] = []
        molecular_offset = 0
        passthrough_offset = 0
        has_chemberta = False

        if CHEMBERTA_STATUS_NOTE:
            self.metadata["notes"].append(CHEMBERTA_STATUS_NOTE)

        for variable in search_space:
            variable_type = variable.get("type", "categorical")
            if variable_type == "continuous":
                low, high = _continuous_bounds(variable)
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "continuous",
                        "low": low,
                        "high": high,
                        "passthrough_slice": slice(passthrough_offset, passthrough_offset + 1),
                    }
                )
                passthrough_offset += 1
                continue
            numeric_spec = _numeric_domain_spec(variable)
            if numeric_spec is not None:
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "numeric_categorical",
                        "labels": numeric_spec["labels"],
                        "value_map": numeric_spec["value_map"],
                        "low": numeric_spec["low"],
                        "high": numeric_spec["high"],
                        "passthrough_slice": slice(passthrough_offset, passthrough_offset + 1),
                    }
                )
                self.metadata["notes"].append(
                    f"{variable['name']}: numeric categorical domain encoded as a normalized scalar passthrough."
                )
                passthrough_offset += 1
                continue

            labels = _domain_labels(variable) or ["unknown"]
            smiles_map = _variable_smiles_map(variable)
            chemberta_map: dict[str, np.ndarray] = {}
            embedding_dim: int | None = None
            if smiles_map and callable(CHEMBERTA_EMBED):
                for label, smiles in smiles_map.items():
                    vector = _chemberta_embedding_from_smiles(smiles)
                    if vector is None:
                        continue
                    if embedding_dim is None:
                        embedding_dim = int(vector.shape[0])
                    if embedding_dim != int(vector.shape[0]):
                        continue
                    chemberta_map[label] = vector

            if chemberta_map and len(chemberta_map) == len(labels) and embedding_dim is not None:
                has_chemberta = True
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "chemberta",
                        "labels": labels,
                        "chemberta_map": chemberta_map,
                        "embedding_dim": embedding_dim,
                        "molecular_slice": slice(molecular_offset, molecular_offset + embedding_dim),
                    }
                )
                molecular_offset += embedding_dim
            else:
                if smiles_map:
                    self.metadata["notes"].append(
                        f"{variable['name']}: ChemBERTa embeddings unavailable or incomplete; using one-hot fallback."
                    )
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels,
                        "passthrough_slice": slice(passthrough_offset, passthrough_offset + len(labels)),
                    }
                )
                passthrough_offset += len(labels)

        if not has_chemberta:
            self.metadata["notes"].append("No valid ChemBERTa embeddings found; encoder behaves like one-hot.")

        self._molecular_raw_dim = molecular_offset
        self._passthrough_dim = passthrough_offset
        self._pca_mean: np.ndarray | None = None
        self._pca_components: np.ndarray | None = None
        self._initialize_pca()
        pca_out_dim = self._pca_components.shape[0] if self._pca_components is not None else self._molecular_raw_dim
        self._dim = pca_out_dim + self._passthrough_dim

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        molecular = self._encode_molecular_raw(candidate)
        passthrough = self._encode_passthrough(candidate)
        if self._pca_components is not None and self._pca_mean is not None:
            molecular = self._pca_components @ (molecular - self._pca_mean)
        return np.concatenate([molecular, passthrough]).astype(float)

    def _encode_molecular_raw(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self._molecular_raw_dim, dtype=float)
        for spec in self.specs:
            if spec["type"] == "chemberta":
                value = candidate.get(spec["name"])
                emb = spec["chemberta_map"].get(str(value))
                if emb is not None:
                    vector[spec["molecular_slice"]] = emb
        return vector

    def _encode_passthrough(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self._passthrough_dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["passthrough_slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                vector[spec["passthrough_slice"]] = _normalize_continuous(
                    spec["value_map"].get(str(value), value),
                    spec["low"],
                    spec["high"],
                )
            elif spec["type"] == "categorical":
                labels = spec["labels"]
                vector[spec["passthrough_slice"].start + _safe_index(labels, value)] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        pca_out_dim = self._pca_components.shape[0] if self._pca_components is not None else self._molecular_raw_dim
        molecular_chunk = encoded[:pca_out_dim]
        passthrough_chunk = encoded[pca_out_dim:]
        if self._pca_components is not None and self._pca_mean is not None:
            molecular_chunk = self._pca_mean + self._pca_components.T @ molecular_chunk
        decoded = {}
        for spec in self.specs:
            if spec["type"] == "continuous":
                chunk = passthrough_chunk[spec["passthrough_slice"]]
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                chunk = passthrough_chunk[spec["passthrough_slice"]]
                numeric_value = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
                decoded[spec["name"]] = min(
                    spec["labels"],
                    key=lambda label: abs(float(spec["value_map"][label]) - numeric_value),
                )
            elif spec["type"] == "chemberta":
                chunk = molecular_chunk[spec["molecular_slice"]]
                decoded[spec["name"]] = min(
                    spec["chemberta_map"],
                    key=lambda label: float(np.linalg.norm(chunk - spec["chemberta_map"][label])),
                )
            else:
                chunk = passthrough_chunk[spec["passthrough_slice"]]
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded

    def _initialize_pca(self) -> None:
        target_dim = max(1, int(self.pca_dim))
        if self._molecular_raw_dim <= target_dim:
            self.metadata["notes"].append(
                f"ChemBERTaConcat molecular raw dim={self._molecular_raw_dim}; PCA skipped because raw molecular dim <= target dim {target_dim}."
            )
            return

        candidates: list[dict[str, Any]] = []
        discrete_total = discrete_search_space_size(self.search_space, max_candidates=self.pca_fit_samples)
        if discrete_total is not None and discrete_total <= self.pca_fit_samples:
            candidates = enumerate_discrete_candidates(self.search_space, max_candidates=self.pca_fit_samples)
        if not candidates:
            candidates = hybrid_sample_candidates(self.search_space, num_samples=self.pca_fit_samples, seed=0)
        if not candidates:
            self.metadata["notes"].append("PCA skipped: unable to build reference candidates.")
            return

        X = np.vstack([self._encode_molecular_raw(candidate) for candidate in candidates]).astype(float)
        n_samples, n_features = X.shape
        if n_samples < 2:
            self.metadata["notes"].append("PCA skipped: insufficient reference samples.")
            return

        max_rank = min(n_samples - 1, n_features)
        use_dim = min(target_dim, max_rank)
        if use_dim <= 0:
            self.metadata["notes"].append("PCA skipped: non-positive target rank.")
            return

        mean = np.mean(X, axis=0)
        centered = X - mean
        try:
            _, singular_values, vt = np.linalg.svd(centered, full_matrices=False)
        except np.linalg.LinAlgError as exc:
            self.metadata["notes"].append(f"PCA skipped: SVD failed ({type(exc).__name__}: {exc}).")
            return

        components = vt[:use_dim, :]
        total_var = float(np.sum(singular_values ** 2))
        kept_var = float(np.sum((singular_values[:use_dim]) ** 2))
        explained = kept_var / total_var if total_var > 0 else 0.0
        self._pca_mean = mean
        self._pca_components = components
        self.metadata["notes"].append(
            f"Applied PCA to chemberta molecular block only: raw dim={self._molecular_raw_dim} -> pca dim={use_dim} "
            f"(passthrough_dim={self._passthrough_dim}, explained_variance={explained:.3f}, fit_samples={n_samples})."
        )


PHYSICOCHEM_DESCRIPTOR_NAMES = [
    "mw",
    "logp",
    "mr",
    "tpsa",
    "hbd",
    "hba",
    "rot_bonds",
    "rings",
    "aromatic_rings",
    "aliphatic_rings",
    "heavy_atoms",
    "hetero_atoms",
    "fraction_csp3",
    "amide_bonds",
    "chiral_centers",
]


class PhysicochemicalDescriptorEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.descriptor_names = list(PHYSICOCHEM_DESCRIPTOR_NAMES)
        self.specs: list[dict[str, Any]] = []
        offset = 0
        available = Chem is not None and Descriptors is not None and Crippen is not None
        if not available:
            self.metadata["notes"].append("RDKit descriptors unavailable; falling back to one-hot-like behavior.")
        for variable in search_space:
            variable_type = variable.get("type", "categorical")
            if variable_type == "continuous":
                low, high = _continuous_bounds(variable)
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "continuous",
                        "low": low,
                        "high": high,
                        "slice": slice(offset, offset + 1),
                    }
                )
                offset += 1
                continue
            numeric_spec = _numeric_domain_spec(variable)
            if numeric_spec is not None:
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "numeric_categorical",
                        "labels": numeric_spec["labels"],
                        "value_map": numeric_spec["value_map"],
                        "low": numeric_spec["low"],
                        "high": numeric_spec["high"],
                        "slice": slice(offset, offset + 1),
                    }
                )
                self.metadata["notes"].append(
                    f"{variable['name']}: numeric categorical domain encoded as a normalized scalar passthrough."
                )
                offset += 1
                continue
            smiles_map = _variable_smiles_map(variable)
            if available and smiles_map:
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "descriptor",
                        "labels": _domain_labels(variable) or ["unknown"],
                        "descriptor_map": {
                            label: _descriptor_vector_from_smiles(smiles)
                            for label, smiles in smiles_map.items()
                            if _descriptor_vector_from_smiles(smiles) is not None
                        },
                        "slice": slice(offset, offset + len(self.descriptor_names)),
                    }
                )
                offset += len(self.descriptor_names)
            else:
                labels = _domain_labels(variable) or ["unknown"]
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels,
                        "slice": slice(offset, offset + len(labels)),
                    }
                )
                offset += len(labels)
        self._dim = offset

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                vector[spec["slice"]] = _normalize_continuous(
                    spec["value_map"].get(str(value), value),
                    spec["low"],
                    spec["high"],
                )
            elif spec["type"] == "descriptor":
                descriptor = spec["descriptor_map"].get(str(value))
                if descriptor is not None:
                    vector[spec["slice"]] = descriptor
            else:
                labels = spec["labels"]
                vector[spec["slice"].start + _safe_index(labels, value)] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        decoded = {}
        for spec in self.specs:
            chunk = encoded[spec["slice"]]
            if spec["type"] == "continuous":
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            elif spec["type"] == "numeric_categorical":
                numeric_value = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
                decoded[spec["name"]] = min(
                    spec["labels"],
                    key=lambda label: abs(float(spec["value_map"][label]) - numeric_value),
                )
            elif spec["type"] == "descriptor" and spec["descriptor_map"]:
                decoded[spec["name"]] = min(
                    spec["descriptor_map"],
                    key=lambda label: float(np.linalg.norm(chunk - spec["descriptor_map"][label])),
                )
            else:
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded


class HybridDescriptorEncoder(BaseEncoder):
    """Descriptor/fingerprint for molecular variables, one-hot for others."""

    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        descriptor = PhysicochemicalDescriptorEncoder(search_space, params)
        fingerprint = FingerprintConcatEncoder(search_space, params)
        if descriptor.metadata.get("notes") and len(descriptor.metadata["notes"]) >= len(fingerprint.metadata["notes"]):
            self._delegate = fingerprint
            self.metadata = dict(fingerprint.metadata)
            self.metadata.setdefault("notes", []).append("Hybrid descriptor resolved to fingerprint_concat path.")
        else:
            self._delegate = descriptor
            self.metadata = dict(descriptor.metadata)
        self._dim = self._delegate.dim

    @property
    def dim(self) -> int:
        return self._delegate.dim

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        return self._delegate.encode(candidate)

    def encode_batch(self, candidates: list[dict[str, Any]]) -> np.ndarray:
        return self._delegate.encode_batch(candidates)

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        return self._delegate.decode(encoded)

    def get_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self._delegate.get_bounds()


class GuardedFallbackEncoder(BaseEncoder):
    def __init__(
        self,
        search_space: list[dict[str, Any]],
        params: dict[str, Any] | None,
        fallback_cls: type[BaseEncoder],
        reason: str,
    ):
        self._delegate = fallback_cls(search_space, params)
        self.search_space = self._delegate.search_space
        self.params = self._delegate.params
        self.metadata = dict(self._delegate.metadata)
        self.metadata.setdefault("notes", []).append(reason)
        self._dim = self._delegate.dim

    @property
    def dim(self) -> int:
        return self._delegate.dim

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        return self._delegate.encode(candidate)

    def encode_batch(self, candidates: list[dict[str, Any]]) -> np.ndarray:
        return self._delegate.encode_batch(candidates)

    def get_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self._delegate.get_bounds()

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        return self._delegate.decode(encoded)


def _create_physical_features_encoder(
    search_space: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> BaseEncoder:
    has_molecular_metadata = any(bool(_variable_smiles_map(variable)) for variable in search_space)
    if has_molecular_metadata and Chem is not None and Descriptors is not None and Crippen is not None:
        encoder = PhysicochemicalDescriptorEncoder(search_space, params)
        encoder.metadata.setdefault("resolved_key", "physicochemical_descriptors")
        encoder.metadata.setdefault("notes", []).append(
            "physical_features resolved to physicochemical_descriptors for SMILES-backed variables."
        )
        return encoder

    encoder = OneHotEncoder(search_space, params)
    encoder.metadata.setdefault("resolved_key", "one_hot")
    if has_molecular_metadata:
        encoder.metadata.setdefault("notes", []).append(
            "physical_features fell back to one_hot because RDKit physicochemical descriptors are unavailable."
        )
    else:
        encoder.metadata.setdefault("notes", []).append(
            "physical_features fell back to one_hot because the problem does not expose molecular descriptors."
        )
    return encoder


class BaseSurrogateModel:
    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}
        self.metadata: dict[str, Any] = {"notes": []}

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        raise NotImplementedError

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError


class BoTorchGPSurrogate(BaseSurrogateModel):
    def __init__(
        self,
        kernel_name: str,
        params: dict[str, Any] | None = None,
        kernel_params: dict[str, Any] | None = None,
    ):
        super().__init__(params)
        self.kernel_name = kernel_name
        self.kernel_params = kernel_params or {}
        self.model = None
        self.log_marginal_likelihood_: float | None = None
        self._x_mean: np.ndarray | None = None
        self._x_scale: np.ndarray | None = None

    def _transform_features(self, X: np.ndarray, *, fit: bool) -> np.ndarray:
        matrix = np.asarray(X, dtype=float)
        if matrix.ndim == 1:
            matrix = matrix.reshape(-1, 1)
        if fit or self._x_mean is None or self._x_scale is None:
            mean = np.mean(matrix, axis=0)
            scale = np.std(matrix, axis=0)
            scale = np.where(np.isfinite(scale) & (scale > 1e-8), scale, 1.0)
            self._x_mean = np.asarray(mean, dtype=float)
            self._x_scale = np.asarray(scale, dtype=float)
        transformed = (matrix - self._x_mean) / self._x_scale
        return np.asarray(transformed, dtype=float)

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if not detect_runtime_capabilities()["torch_stack"]:
            raise RuntimeError("BoTorch stack is unavailable")
        if X.size == 0:
            raise RuntimeError("Cannot fit GP without training data")
        from botorch.fit import fit_gpytorch_mll_scipy, fit_gpytorch_mll_torch

        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).reshape(-1)
        if not np.all(np.isfinite(X)):
            raise RuntimeError("Cannot fit GP with non-finite training features")
        if not np.all(np.isfinite(y)):
            raise RuntimeError("Cannot fit GP with non-finite training targets")

        X_scaled = self._transform_features(X, fit=True)
        train_X = _to_torch_matrix(X_scaled)
        train_Y = _to_torch_column(y)
        covar_module = _gpytorch_kernel(self.kernel_name, X_scaled.shape[1], self.metadata, self.kernel_params)
        if hasattr(covar_module, "initialize_from_data"):
            try:
                covar_module.initialize_from_data(train_X, train_Y.squeeze(-1))
                self.metadata.setdefault("notes", []).append(
                    f"Initialized kernel '{self.kernel_name}' from training data heuristics."
                )
            except Exception as exc:
                self.metadata.setdefault("notes", []).append(
                    f"Kernel '{self.kernel_name}' initialization skipped: {type(exc).__name__}: {exc}"
                )
        self.metadata.setdefault("notes", []).append(
            "Standardized training features with per-dimension z-score before GP fitting."
        )

        y_std = max(float(np.std(y)), 1e-6)
        noise_floor = max(float(self.params.get("noise_floor", 1e-4)), 1e-6)
        relative_noise_levels = self.params.get("noise_retries", [0.01, 0.05, 0.1, 0.2])
        retry_values: list[float] = []
        for candidate in relative_noise_levels:
            try:
                candidate_value = max(float(candidate) * y_std, noise_floor)
            except (TypeError, ValueError):
                continue
            if candidate_value not in retry_values:
                retry_values.append(candidate_value)
        if not retry_values:
            retry_values = [max(0.05 * y_std, noise_floor)]

        last_error: Exception | None = None
        scipy_maxiter = max(20, int(self.params.get("scipy_maxiter", 200)))
        torch_step_limit = max(50, int(self.params.get("torch_step_limit", 300)))

        def _build_model(noise_init: float):
            likelihood = GaussianLikelihood(noise_constraint=GreaterThan(noise_floor))
            likelihood.noise = max(float(noise_init), noise_floor)
            model = SingleTaskGP(
                train_X=train_X,
                train_Y=train_Y,
                likelihood=likelihood,
                covar_module=covar_module,
                outcome_transform=Standardize(m=1),
            )
            return model, ExactMarginalLogLikelihood(model.likelihood, model)

        for noise_init in retry_values:
            scipy_error: Exception | None = None
            try:
                self.model, mll = _build_model(noise_init)
                fit_gpytorch_mll_scipy(
                    mll,
                    method="L-BFGS-B",
                    options={"maxiter": scipy_maxiter},
                )
                self.metadata.setdefault("notes", []).append(
                    f"GP fit succeeded with SciPy optimizer; initial noise std={float(noise_init):.6g}."
                )
                last_error = None
                break
            except Exception as exc:
                scipy_error = exc
                self.metadata.setdefault("notes", []).append(
                    f"GP SciPy fit failed with initial noise std={float(noise_init):.6g}: {type(exc).__name__}: {exc}"
                )
                self.model = None

            try:
                self.model, mll = _build_model(noise_init)
                fit_gpytorch_mll_torch(
                    mll,
                    step_limit=torch_step_limit,
                    optimizer=torch.optim.Adam,
                )
                self.metadata.setdefault("notes", []).append(
                    f"GP fit succeeded with torch optimizer fallback; initial noise std={float(noise_init):.6g}."
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                self.metadata.setdefault("notes", []).append(
                    f"GP torch fit failed with initial noise std={float(noise_init):.6g}: {type(exc).__name__}: {exc}"
                )
                if scipy_error is not None:
                    self.metadata.setdefault("notes", []).append(
                        f"Previous SciPy failure for same retry: {type(scipy_error).__name__}: {scipy_error}"
                    )
                self.model = None
        if last_error is not None:
            raise last_error
        self.model.eval()
        self.model.likelihood.eval()
        self.log_marginal_likelihood_ = None

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.model is None:
            raise RuntimeError("GP model must be fit before prediction")
        X_scaled = self._transform_features(X, fit=False)
        posterior = self.model.posterior(_to_torch_matrix(X_scaled))
        mean = posterior.mean.squeeze(-1).detach().cpu().numpy()
        variance = posterior.variance.squeeze(-1).clamp_min(1e-12)
        std = variance.sqrt().detach().cpu().numpy()
        return np.asarray(mean, dtype=float), np.asarray(std, dtype=float)


class RandomForestSurrogate(BaseSurrogateModel):
    """Random Forest with empirical inter-tree variance as an uncertainty proxy."""

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self.n_estimators = int(self.params.get("n_estimators", 100))
        self.max_depth = self.params.get("max_depth")
        self.min_samples_leaf = int(self.params.get("min_samples_leaf", 2))
        self.random_state = int(self.params.get("random_state", 42))
        self._model = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        from sklearn.ensemble import RandomForestRegressor

        if X.size == 0:
            raise RuntimeError("Cannot fit RandomForest without training data")
        self._model = RandomForestRegressor(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            min_samples_leaf=self.min_samples_leaf,
            random_state=self.random_state,
            n_jobs=1,
        )
        self._model.fit(np.asarray(X, dtype=float), np.asarray(y, dtype=float).reshape(-1))
        self.metadata.setdefault("notes", []).append(
            "RandomForest uncertainty uses inter-tree predictive spread as a proxy."
        )

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self._model is None:
            raise RuntimeError("RF model must be fit before prediction")
        matrix = np.asarray(X, dtype=float)
        tree_predictions = np.asarray([tree.predict(matrix) for tree in self._model.estimators_], dtype=float)
        mean = np.mean(tree_predictions, axis=0)
        std = np.std(tree_predictions, axis=0) + 1e-6
        return np.asarray(mean, dtype=float), np.asarray(std, dtype=float)


class _GaussianDropoutRegressor(torch.nn.Module if torch is not None else object):
    def __init__(self, input_dim: int, hidden_sizes: list[int], dropout_rate: float, heteroscedastic: bool):
        if torch is None:  # pragma: no cover
            raise RuntimeError("PyTorch is required for neural surrogate models")
        super().__init__()
        layers: list[torch.nn.Module] = []
        prev = int(input_dim)
        for hidden in hidden_sizes:
            layers.append(torch.nn.Linear(prev, int(hidden)))
            layers.append(torch.nn.ReLU())
            if dropout_rate > 0:
                layers.append(torch.nn.Dropout(float(dropout_rate)))
            prev = int(hidden)
        self.backbone = torch.nn.Sequential(*layers)
        self.mean_head = torch.nn.Linear(prev, 1)
        self.logvar_head = torch.nn.Linear(prev, 1) if heteroscedastic else None
        self.heteroscedastic = heteroscedastic

    def forward(self, x):
        features = self.backbone(x)
        mean = self.mean_head(features)
        if self.logvar_head is None:
            return mean, None
        logvar = self.logvar_head(features).clamp(min=-8.0, max=4.0)
        return mean, logvar


class BNNSurrogate(BaseSurrogateModel):
    """Practical Bayesian-style surrogate using MC dropout and Gaussian NLL."""

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self.hidden_sizes = [int(value) for value in self.params.get("hidden_sizes", [64, 64])]
        self.dropout_rate = float(self.params.get("dropout_rate", 0.10))
        self.n_epochs = int(self.params.get("n_epochs", 200))
        self.learning_rate = float(self.params.get("learning_rate", 1e-3))
        self.weight_decay = float(self.params.get("weight_decay", 1e-4))
        self.mc_samples = int(self.params.get("mc_samples", 50))
        self.random_state = int(self.params.get("random_state", 42))
        self._model = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if torch is None:
            raise RuntimeError("PyTorch is required for BNN surrogate")
        if X.size == 0:
            raise RuntimeError("Cannot fit BNN without training data")
        torch.manual_seed(self.random_state)
        matrix = _to_torch_matrix(X)
        target = _to_torch_column(y)
        self._model = _GaussianDropoutRegressor(matrix.shape[1], self.hidden_sizes, self.dropout_rate, heteroscedastic=True)
        optimizer = torch.optim.Adam(
            self._model.parameters(),
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        self._model.train()
        for _ in range(self.n_epochs):
            optimizer.zero_grad()
            mean, logvar = self._model(matrix)
            variance = torch.exp(logvar).clamp_min(1e-6)
            loss = 0.5 * (((target - mean) ** 2) / variance + logvar).mean()
            loss.backward()
            optimizer.step()
        self.metadata.setdefault("notes", []).append(
            "BNN surrogate uses MC dropout with a Gaussian NLL head as a practical variational approximation."
        )

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self._model is None:
            raise RuntimeError("BNN model must be fit before prediction")
        matrix = _to_torch_matrix(X)
        self._model.train()
        mean_samples = []
        aleatoric_terms = []
        with torch.no_grad():
            for _ in range(max(self.mc_samples, 1)):
                mean, logvar = self._model(matrix)
                mean_samples.append(mean.squeeze(-1))
                aleatoric_terms.append(torch.exp(logvar).squeeze(-1))
        mean_stack = torch.stack(mean_samples, dim=0)
        aleatoric_stack = torch.stack(aleatoric_terms, dim=0)
        predictive_mean = mean_stack.mean(dim=0)
        epistemic = mean_stack.var(dim=0, unbiased=False)
        aleatoric = aleatoric_stack.mean(dim=0)
        predictive_std = torch.sqrt((epistemic + aleatoric).clamp_min(1e-6))
        self._model.eval()
        return (
            predictive_mean.detach().cpu().numpy().astype(float),
            predictive_std.detach().cpu().numpy().astype(float),
        )


class NNDropoutSurrogate(BaseSurrogateModel):
    """Neural network with MC dropout uncertainty."""

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self.hidden_sizes = [int(value) for value in self.params.get("hidden_sizes", [128, 128])]
        self.dropout_rate = float(self.params.get("dropout_rate", 0.15))
        self.n_epochs = int(self.params.get("n_epochs", 300))
        self.learning_rate = float(self.params.get("learning_rate", 1e-3))
        self.weight_decay = float(self.params.get("weight_decay", 1e-3))
        self.mc_samples = int(self.params.get("mc_samples", 100))
        self.random_state = int(self.params.get("random_state", 42))
        self._model = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if torch is None:
            raise RuntimeError("PyTorch is required for NN dropout surrogate")
        if X.size == 0:
            raise RuntimeError("Cannot fit NN dropout surrogate without training data")
        torch.manual_seed(self.random_state)
        matrix = _to_torch_matrix(X)
        target = _to_torch_column(y)
        self._model = _GaussianDropoutRegressor(matrix.shape[1], self.hidden_sizes, self.dropout_rate, heteroscedastic=False)
        optimizer = torch.optim.Adam(
            self._model.parameters(),
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        loss_fn = torch.nn.MSELoss()
        self._model.train()
        for _ in range(self.n_epochs):
            optimizer.zero_grad()
            mean, _ = self._model(matrix)
            loss = loss_fn(mean, target)
            loss.backward()
            optimizer.step()
        self.metadata.setdefault("notes", []).append(
            "NN dropout uncertainty uses empirical MC dropout spread across stochastic forward passes."
        )

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self._model is None:
            raise RuntimeError("NN dropout model must be fit before prediction")
        matrix = _to_torch_matrix(X)
        self._model.train()
        samples = []
        with torch.no_grad():
            for _ in range(max(self.mc_samples, 1)):
                mean, _ = self._model(matrix)
                samples.append(mean.squeeze(-1))
        sample_stack = torch.stack(samples, dim=0)
        predictive_mean = sample_stack.mean(dim=0)
        predictive_std = torch.sqrt(sample_stack.var(dim=0, unbiased=False).clamp_min(1e-6))
        self._model.eval()
        return (
            predictive_mean.detach().cpu().numpy().astype(float),
            predictive_std.detach().cpu().numpy().astype(float),
        )


class AcquisitionFunction:
    def __init__(self, key: str, params: dict[str, Any] | None = None):
        self.key = key
        self.params = params or {}
        self.metadata: dict[str, Any] = {"notes": []}

    def score(
        self,
        surrogate: BaseSurrogateModel,
        X: np.ndarray,
        best_f: float | None,
        rng: np.random.Generator,
    ) -> np.ndarray:
        if not isinstance(surrogate, BoTorchGPSurrogate) or surrogate.model is None:
            raise RuntimeError("BoTorch acquisition scoring requires a fit BoTorch GP surrogate")
        X_tensor = _to_torch_matrix(X).unsqueeze(-2)

        if self.key in {"log_ei", "qlog_ei"}:
            if self.key == "qlog_ei":
                self.metadata.setdefault("notes", []).append("Batch q-LogEI is ranked with pointwise BoTorch LogEI in Phase 1.")
            scorer = LogExpectedImprovement(
                model=surrogate.model,
                best_f=float(best_f if best_f is not None else 0.0),
            )
            with torch.no_grad():
                return scorer(X_tensor).detach().cpu().numpy().reshape(-1)
        if self.key == "ucb":
            beta = float(self.params.get("beta", 0.4))
            scorer = UpperConfidenceBound(model=surrogate.model, beta=beta)
            with torch.no_grad():
                return scorer(X_tensor).detach().cpu().numpy().reshape(-1)
        if self.key == "ts":
            with torch.no_grad():
                posterior = surrogate.model.posterior(_to_torch_matrix(X))
                sample = posterior.rsample(sample_shape=torch.Size([1])).squeeze(0).squeeze(-1)
            return sample.detach().cpu().numpy().reshape(-1)
        raise RuntimeError(f"Unsupported acquisition function: {self.key}")


def _algorithm_profile(
    *,
    what_it_is: str,
    best_for: list[str],
    avoid_when: list[str],
    space_support: str,
    data_regime: str,
    uncertainty_quality: str,
    cost: str,
    interpretability: str,
    dependencies: list[str],
    implementation_status: str,
    fallback_to: str | None,
    fallback_trigger: str | None,
    selection_hints: list[str],
    **extra: Any,
) -> dict[str, Any]:
    profile = {
        "what_it_is": what_it_is,
        "best_for": best_for,
        "avoid_when": avoid_when,
        "space_support": space_support,
        "data_regime": data_regime,
        "uncertainty_quality": uncertainty_quality,
        "cost": cost,
        "interpretability": interpretability,
        "dependencies": dependencies,
        "implementation_status": implementation_status,
        "fallback_to": fallback_to,
        "fallback_trigger": fallback_trigger,
        "selection_hints": selection_hints,
    }
    profile.update(extra)
    return profile


EMBEDDING_POOL: dict[str, PoolEntry] = {
    "one_hot": PoolEntry(
        key="one_hot",
        display_name="One-Hot + Normalized Continuous",
        description="Stable baseline encoder for mixed spaces with low data and small categorical domains.",
        tags=_algorithm_profile(
            what_it_is="One-hot encoding for categorical variables with normalized continuous scalars.",
            best_for=["baseline comparison", "very low data (<10 observations)", "small categorical spaces"],
            avoid_when=["molecular similarity should matter", "categorical cardinality is very high"],
            space_support="categorical + continuous",
            data_regime="works from 1-2 observations upward",
            uncertainty_quality="depends on downstream surrogate",
            cost="negligible",
            interpretability="high",
            dependencies=[],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=[
                "Prefer when you need a safe baseline.",
                "Prefer when advanced chemistry-aware encoders are unavailable.",
            ],
            chemistry_aware=False,
        ),
        factory=lambda search_space, params=None: OneHotEncoder(search_space, params),
    ),
    "fingerprint_concat": PoolEntry(
        key="fingerprint_concat",
        display_name="Morgan Fingerprint + PCA + OHE",
        description="Uses Morgan fingerprints for molecular categorical variables, concatenates other features, then applies PCA.",
        tags=_algorithm_profile(
            what_it_is="Morgan/ECFP fingerprints for SMILES-backed variables plus non-molecular features, followed by PCA compression.",
            best_for=["molecular search spaces", "ligand/base/solvent structure should matter"],
            avoid_when=["SMILES are unavailable", "RDKit is unavailable"],
            space_support="molecular categorical + general mixed spaces",
            data_regime="best in low-to-mid data once molecular similarity is informative",
            uncertainty_quality="depends on downstream surrogate",
            cost="low",
            interpretability="medium",
            dependencies=["rdkit"],
            implementation_status="conditional_phase1",
            fallback_to="one_hot",
            fallback_trigger="No RDKit or no valid SMILES fingerprints.",
            selection_hints=[
                "Prefer over one-hot when structurally similar reagents should share information.",
                "Check whether the problem spec exposes smiles_map for the relevant variables.",
            ],
            chemistry_aware=True,
        ),
        factory=lambda search_space, params=None: FingerprintConcatEncoder(search_space, params),
    ),
    "molclr_concat": PoolEntry(
        key="molclr_concat",
        display_name="MolCLR Embedding + PCA + OHE",
        description="Uses MolCLR embeddings for molecular categorical variables, concatenates other features, then applies PCA.",
        tags=_algorithm_profile(
            what_it_is="MolCLR vectors for SMILES-backed variables plus non-molecular features, followed by PCA compression.",
            best_for=["molecular search spaces", "when pretrained molecular representations are preferred over fixed fingerprints"],
            avoid_when=["MolCLR runtime is unavailable", "SMILES metadata is incomplete"],
            space_support="molecular categorical + general mixed spaces",
            data_regime="best in low-to-mid data once pretrained molecular similarity is informative",
            uncertainty_quality="depends on downstream surrogate",
            cost="medium",
            interpretability="low-to-medium",
            dependencies=["molclr"],
            implementation_status="conditional_phase1",
            fallback_to="one_hot",
            fallback_trigger="No MolCLR runtime or missing molecular embeddings for categories.",
            selection_hints=[
                "Prefer when you want a pretrained molecular representation instead of handcrafted descriptors.",
                "Check that the runtime environment actually exposes a compatible `molclr` module.",
            ],
            chemistry_aware=True,
        ),
        factory=lambda search_space, params=None: MolCLRConcatEncoder(search_space, params),
    ),
    "chemberta_concat": PoolEntry(
        key="chemberta_concat",
        display_name="ChemBERTa Embedding + PCA + OHE",
        description="Uses ChemBERTa embeddings for molecular categorical variables, concatenates other features, then applies PCA.",
        tags=_algorithm_profile(
            what_it_is="ChemBERTa transformer embeddings for SMILES-backed variables plus non-molecular features, followed by PCA compression.",
            best_for=["molecular search spaces", "when pretrained SMILES language-model representations are preferred"],
            avoid_when=["no local ChemBERTa snapshot is available", "SMILES metadata is incomplete"],
            space_support="molecular categorical + general mixed spaces",
            data_regime="best in low-to-mid data once pretrained semantic similarity is informative",
            uncertainty_quality="depends on downstream surrogate",
            cost="medium",
            interpretability="low-to-medium",
            dependencies=["chemberta"],
            implementation_status="conditional_phase1",
            fallback_to="one_hot",
            fallback_trigger="No local ChemBERTa snapshot or missing molecular embeddings for categories.",
            selection_hints=[
                "Prefer when you want a pretrained SMILES-language representation instead of graph encoders.",
                "Keep the model fully offline by pointing at a local cached snapshot.",
            ],
            chemistry_aware=True,
        ),
        factory=lambda search_space, params=None: ChemBERTaConcatEncoder(search_space, params),
    ),
    "physicochemical_descriptors": PoolEntry(
        key="physicochemical_descriptors",
        display_name="Physicochemical Descriptors",
        description="Uses RDKit descriptors for molecular variables, one-hot otherwise.",
        tags=_algorithm_profile(
            what_it_is="15-dimensional descriptor vector using MW, LogP, MR, TPSA, ring topology, hetero-atom counts, and related physicochemical features.",
            best_for=["problems needing interpretable continuous molecular features", "descriptor-friendly GP modeling"],
            avoid_when=["RDKit unavailable", "SMILES absent or noisy"],
            space_support="molecular categorical + continuous",
            data_regime="low-to-mid data",
            uncertainty_quality="depends on downstream surrogate",
            cost="low",
            interpretability="high",
            dependencies=["rdkit"],
            implementation_status="conditional_phase1",
            fallback_to="one_hot",
            fallback_trigger="No RDKit descriptor path or insufficient SMILES metadata.",
            selection_hints=[
                "Prefer when interpretability matters more than expressive power.",
                "Pairs well with GP kernels that assume smoother continuous structure.",
            ],
            chemistry_aware=True,
        ),
        factory=lambda search_space, params=None: PhysicochemicalDescriptorEncoder(search_space, params),
    ),
    "physical_features": PoolEntry(
        key="physical_features",
        display_name="Physical Features",
        description="AutoBO fixed embedding entry that prefers physicochemical descriptors and falls back safely.",
        tags=_algorithm_profile(
            what_it_is="Fixed AutoBO embedding entry that resolves to physicochemical descriptors when molecular metadata exists.",
            best_for=["AutoBO default embedding", "molecular benchmarks with descriptor-friendly kernels"],
            avoid_when=["descriptor semantics are critical but no molecular metadata exists"],
            space_support="molecular categorical + general mixed spaces",
            data_regime="low-to-mid data",
            uncertainty_quality="depends on downstream surrogate",
            cost="low",
            interpretability="high",
            dependencies=["rdkit_optional"],
            implementation_status="adaptive_phase2",
            fallback_to="one_hot",
            fallback_trigger="No usable molecular descriptors for the current problem.",
            selection_hints=[
                "Use as the fixed embedding entry for AutoBO.",
                "Expect one-hot fallback on non-molecular process benchmarks.",
            ],
            chemistry_aware=True,
        ),
        factory=lambda search_space, params=None: _create_physical_features_encoder(search_space, params),
    ),
    "llm_embedding": PoolEntry(
        key="llm_embedding",
        display_name="LLM Text Embedding",
        description="Semantic text embedding of the full reaction condition description.",
        tags=_algorithm_profile(
            what_it_is="Embeds a natural-language rendering of the candidate using a pretrained text embedding model.",
            best_for=["heterogeneous textual metadata", "problems with rich semantic annotations"],
            avoid_when=["external API access unavailable", "very low data without alignment model"],
            space_support="heterogeneous mixed spaces",
            data_regime="better once paired with a learned alignment model or enough labeled data exists",
            uncertainty_quality="weak unless aligned by a stronger surrogate",
            cost="medium-to-high",
            interpretability="low",
            dependencies=["network", "embedding_api"],
            implementation_status="label_only_fallback",
            fallback_to="one_hot",
            fallback_trigger="Embedding API disabled in Phase 1.",
            selection_hints=[
                "Do not treat semantic similarity as yield similarity without alignment.",
                "Only attractive when the problem has rich text beyond reagent identity.",
            ],
            chemistry_aware="indirect",
        ),
        factory=lambda search_space, params=None: GuardedFallbackEncoder(
            search_space,
            params,
            OneHotEncoder,
            "LLM embeddings are not enabled in Phase 1; falling back to one_hot.",
        ),
    ),
}


SURROGATE_POOL: dict[str, PoolEntry] = {
    "gp": PoolEntry(
        key="gp",
        display_name="Gaussian Process",
        description="Default surrogate family for low-data Bayesian optimization with calibrated uncertainty.",
        tags=_algorithm_profile(
            what_it_is="Gaussian Process regression with a separately selected kernel.",
            best_for=["sample-efficient optimization", "low-data regimes", "uncertainty-aware decision making"],
            avoid_when=["very high dimensions without structure", "very large datasets"],
            space_support="continuous or encoded mixed spaces",
            data_regime="excellent in 3-200 observations",
            uncertainty_quality="high",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to="exploration_shortlist",
            fallback_trigger="BoTorch GP fitting failure or unavailable runtime stack.",
            selection_hints=[
                "Default first choice unless dimensions or noise clearly argue otherwise.",
                "Kernel choice matters as much as the surrogate family choice.",
            ],
        ),
        factory=lambda params=None, kernel_key="matern52", kernel_params=None: BoTorchGPSurrogate(
            kernel_key,
            params,
            kernel_params,
        ),
    ),
    "rf": PoolEntry(
        key="rf",
        display_name="Random Forest",
        description="Random forest surrogate with empirical inter-tree uncertainty.",
        tags=_algorithm_profile(
            what_it_is="Random forest regressor with predictive spread estimated from individual tree outputs.",
            best_for=["rough non-linear response surfaces", "mixed encoded spaces"],
            avoid_when=["very small data (<10)", "strict probabilistic calibration is required"],
            space_support="continuous or encoded mixed spaces",
            data_regime="10+ observations preferred",
            uncertainty_quality="moderate",
            cost="low-to-medium",
            interpretability="medium",
            dependencies=["scikit-learn"],
            implementation_status="native_phase2",
            fallback_to="gp",
            fallback_trigger="scikit-learn unavailable or RF fitting fails.",
            selection_hints=["Use when GP smoothness assumptions appear too restrictive."],
        ),
        factory=lambda params=None: RandomForestSurrogate(params),
    ),
    "bnn": PoolEntry(
        key="bnn",
        display_name="Bayesian Neural Network",
        description="Practical Bayesian-style surrogate via MC dropout and Gaussian NLL.",
        tags=_algorithm_profile(
            what_it_is="Two-layer neural regressor with stochastic dropout inference and heteroscedastic uncertainty head.",
            best_for=["non-linear encoded spaces", "moderately complex response surfaces"],
            avoid_when=["very small data (<15)", "interpretability is critical"],
            space_support="continuous encoded spaces",
            data_regime="15+ observations preferred",
            uncertainty_quality="moderate",
            cost="moderate",
            interpretability="low",
            dependencies=["torch"],
            implementation_status="native_phase2",
            fallback_to="gp",
            fallback_trigger="PyTorch unavailable or BNN fitting fails.",
            selection_hints=["Use when GP under-fits richer nonlinear structure."],
        ),
        factory=lambda params=None: BNNSurrogate(params),
    ),
    "nn_dropout": PoolEntry(
        key="nn_dropout",
        display_name="NN + MC Dropout",
        description="Neural network with MC dropout uncertainty for flexible non-linear modeling.",
        tags=_algorithm_profile(
            what_it_is="Two-layer neural regressor with dropout-enabled stochastic inference.",
            best_for=["complex non-linear surfaces", "moderate-to-large encoded spaces"],
            avoid_when=["very small data (<20)", "well-calibrated sigma is critical"],
            space_support="continuous encoded spaces",
            data_regime="20+ observations preferred",
            uncertainty_quality="variable",
            cost="moderate-to-high",
            interpretability="low",
            dependencies=["torch"],
            implementation_status="native_phase2",
            fallback_to="gp",
            fallback_trigger="PyTorch unavailable or NN dropout fitting fails.",
            selection_hints=["Use when the response surface looks richer than a small BNN can capture."],
        ),
        factory=lambda params=None: NNDropoutSurrogate(params),
    ),
}


KERNEL_POOL: dict[str, PoolEntry] = {
    "matern52": PoolEntry(
        key="matern52",
        display_name="Matern-5/2",
        description="General-purpose smooth kernel and default BO baseline.",
        tags=_algorithm_profile(
            what_it_is="Matérn-5/2 kernel with ARD-like behavior in a BoTorch SingleTaskGP.",
            best_for=["general BO", "moderately smooth objectives"],
            avoid_when=["objective is clearly rougher than smooth", "you need explicit mixed-space structure"],
            space_support="continuous or encoded mixed spaces",
            data_regime="strong low-data default",
            uncertainty_quality="high",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=["Use as default when unsure.", "Safe first kernel for most campaigns."],
        ),
        factory=None,
    ),
    "matern32": PoolEntry(
        key="matern32",
        display_name="Matern-3/2",
        description="Rougher kernel than Matern-5/2 and more tolerant of non-smooth response surfaces.",
        tags=_algorithm_profile(
            what_it_is="Matérn-3/2 kernel for rougher objective assumptions.",
            best_for=["rougher surfaces", "less smooth chemistry response"],
            avoid_when=["you believe the objective is highly smooth"],
            space_support="continuous or encoded mixed spaces",
            data_regime="low data",
            uncertainty_quality="high",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=["Try when Matérn-5/2 appears over-smooth or overconfident."],
        ),
        factory=None,
    ),
    "rbf": PoolEntry(
        key="rbf",
        display_name="RBF / Squared Exponential",
        description="Strong smoothness prior for very smooth objectives.",
        tags=_algorithm_profile(
            what_it_is="Squared exponential kernel imposing a strong smoothness prior.",
            best_for=["very smooth objectives", "continuous process tuning"],
            avoid_when=["abrupt categorical effects dominate"],
            space_support="continuous or encoded mixed spaces",
            data_regime="low data with smooth behavior",
            uncertainty_quality="high",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=["Use sparingly; it is often too smooth for categorical-heavy chemistry search."],
        ),
        factory=None,
    ),
    "smkbo": PoolEntry(
        key="smkbo",
        display_name="SMKBO Spectral Mixture",
        description="Additive Gaussian spectral mixture plus heavy-tailed Cauchy spectral mixture kernel.",
        tags=_algorithm_profile(
            what_it_is="A multi-scale spectral kernel that combines standard and heavy-tailed mixture components.",
            best_for=["multi-scale response surfaces", "quasi-periodic structure", "embedding spaces with heterogeneous smoothness"],
            avoid_when=["very low data", "simple monotone trends where Matern is enough"],
            space_support="continuous or encoded mixed spaces",
            data_regime="best once a few informative observations exist",
            uncertainty_quality="high when fit is stable",
            cost="high",
            interpretability="low-to-medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="custom_phase1",
            fallback_to="matern52",
            fallback_trigger="Optimization becomes unstable or the richer spectral prior is unnecessary.",
            selection_hints=[
                "Try when Matérn kernels underfit oscillatory or multi-scale structure.",
                "More expressive, but usually needs more data than the default kernels.",
            ],
        ),
        factory=None,
    ),
    "smk": PoolEntry(
        key="smk",
        display_name="Spectral Mixture Alias",
        description="Compatibility alias that resolves to the repository's existing smkbo kernel implementation.",
        tags=_algorithm_profile(
            what_it_is="Compatibility alias for the existing SMKBO spectral mixture kernel.",
            best_for=["AutoBO GP-SMK compatibility"],
            avoid_when=["you need to distinguish between multiple spectral kernel implementations"],
            space_support="continuous or encoded mixed spaces",
            data_regime="best once a few informative observations exist",
            uncertainty_quality="high when fit is stable",
            cost="high",
            interpretability="low-to-medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="compat_phase2",
            fallback_to="matern52",
            fallback_trigger="Alias resolves to smkbo and follows its fallback behavior.",
            selection_hints=["Use only as a compatibility key; runtime resolves it to smkbo."],
        ),
        factory=None,
    ),
    "sum_kernel": PoolEntry(
        key="sum_kernel",
        display_name="Additive Mixed Kernel",
        description="Approximate additive kernel for partially separable mixed spaces.",
        tags=_algorithm_profile(
            what_it_is="Approximate additive composition for mixed-space effects.",
            best_for=["mixed spaces with semi-independent effects"],
            avoid_when=["interactions dominate the objective"],
            space_support="encoded mixed spaces",
            data_regime="mid data preferred",
            uncertainty_quality="good",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to="matern52",
            fallback_trigger="Fit is unstable or main-effects structure is not supported by the data.",
            selection_hints=["Choose when you expect main effects to dominate interactions."],
        ),
        factory=None,
    ),
    "product_kernel": PoolEntry(
        key="product_kernel",
        display_name="Product Mixed Kernel",
        description="Approximate interaction-heavy mixed kernel.",
        tags=_algorithm_profile(
            what_it_is="Approximate multiplicative composition to express stronger interactions.",
            best_for=["mixed spaces with strong interactions"],
            avoid_when=["data is too sparse to identify interactions"],
            space_support="encoded mixed spaces",
            data_regime="mid data preferred",
            uncertainty_quality="good",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to="matern52",
            fallback_trigger="Fit is unstable or interaction structure is unsupported by the data.",
            selection_hints=["Choose only when you have a concrete interaction hypothesis."],
        ),
        factory=None,
    ),
    "mixed_sum_product": PoolEntry(
        key="mixed_sum_product",
        display_name="Mixed Sum-Product Kernel",
        description="Approximate balanced mixed kernel for uncertain interaction structure.",
        tags=_algorithm_profile(
            what_it_is="Weighted blend of additive and multiplicative mixed-space behavior.",
            best_for=["mixed categorical-continuous spaces", "uncertain interaction structure"],
            avoid_when=["problem is purely continuous or purely categorical and simple"],
            space_support="encoded mixed spaces",
            data_regime="mid data preferred",
            uncertainty_quality="good",
            cost="moderate",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="native_phase1",
            fallback_to="matern52",
            fallback_trigger="Fit is unstable or the mixed interaction prior is unnecessary.",
            selection_hints=[
                "Recommended mixed-space kernel when the problem has both main effects and interactions.",
                "A good default for encoded chemistry search spaces if GP is chosen.",
            ],
        ),
        factory=None,
    ),
}


AF_POOL: dict[str, PoolEntry] = {
    "log_ei": PoolEntry(
        key="log_ei",
        display_name="Log Expected Improvement",
        description="Numerically stable default improvement-based acquisition.",
        tags=_algorithm_profile(
            what_it_is="Expected Improvement transformed into log space for better numerical behavior.",
            best_for=["default single-objective BO", "stable low-data optimization"],
            avoid_when=["you need explicit exploration control", "true multi-objective BO"],
            space_support="single-objective",
            data_regime="excellent default across low-to-mid data",
            uncertainty_quality="relies on surrogate uncertainty",
            cost="low",
            interpretability="high",
            dependencies=["botorch"],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=[
                "Default acquisition unless another strategy is clearly better justified.",
                "Prefer over classic EI in Phase 1.",
            ],
            batch_support=False,
        ),
        factory=lambda params=None: AcquisitionFunction("log_ei", params),
    ),
    "ucb": PoolEntry(
        key="ucb",
        display_name="Upper Confidence Bound",
        description="Explicit exploration-exploitation control via beta.",
        tags=_algorithm_profile(
            what_it_is="Optimistic score mean + beta * std.",
            best_for=["manual exploration control", "stagnating campaigns that need more exploration"],
            avoid_when=["you want a strong improvement-based default without tuning beta"],
            space_support="single-objective",
            data_regime="general",
            uncertainty_quality="relies on surrogate uncertainty",
            cost="low",
            interpretability="high",
            dependencies=["botorch"],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=[
                "Increase beta early; reduce it later.",
                "Useful when reflection says the campaign is over-exploiting.",
            ],
            batch_support=False,
        ),
        factory=lambda params=None: AcquisitionFunction("ucb", params),
    ),
    "ts": PoolEntry(
        key="ts",
        display_name="Thompson Sampling",
        description="Posterior-sampling acquisition that naturally promotes diversity.",
        tags=_algorithm_profile(
            what_it_is="Samples from a surrogate posterior proxy and optimizes the sample.",
            best_for=["diverse exploration", "simple batch diversity via repeated sampling"],
            avoid_when=["strict deterministic ranking is preferred"],
            space_support="single-objective",
            data_regime="general",
            uncertainty_quality="relies on surrogate uncertainty",
            cost="low",
            interpretability="medium",
            dependencies=["botorch"],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=["Prefer when shortlist diversity matters more than pure greedy improvement."],
            batch_support=True,
        ),
        factory=lambda params=None: AcquisitionFunction("ts", params),
    ),
    "qlog_ei": PoolEntry(
        key="qlog_ei",
        display_name="q-Log Expected Improvement",
        description="Batch LogEI label with Phase 1 pointwise fallback.",
        tags=_algorithm_profile(
            what_it_is="Batch LogEI-inspired policy backed by BoTorch posterior scoring.",
            best_for=["batch mode with q > 1"],
            avoid_when=["strict joint batch optimization is required"],
            space_support="single-objective batch",
            data_regime="general",
            uncertainty_quality="relies on surrogate uncertainty",
            cost="medium",
            interpretability="medium",
            dependencies=["torch", "gpytorch", "botorch"],
            implementation_status="pointwise_phase1",
            fallback_to="log_ei",
            fallback_trigger="Phase 1 ranks the pool pointwise instead of solving a joint q-batch acquisition problem.",
            selection_hints=["Use when you want a batch-aware intent while keeping the current pool-ranking workflow."],
            batch_support=True,
        ),
        factory=lambda params=None: AcquisitionFunction("qlog_ei", params),
    ),
}


def create_encoder(key: str, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    entry = EMBEDDING_POOL.get(key) or EMBEDDING_POOL["one_hot"]
    encoder = entry.factory(search_space, params or {})
    encoder.metadata.setdefault("selected_key", key)
    encoder.metadata.setdefault("resolved_key", entry.key)
    encoder.metadata.setdefault("runtime_mode", detect_runtime_capabilities()["runtime_mode"])
    return encoder


def create_surrogate(
    key: str,
    params: dict[str, Any] | None = None,
    kernel_key: str = "matern52",
    kernel_params: dict[str, Any] | None = None,
) -> BaseSurrogateModel:
    entry = SURROGATE_POOL.get(key) or SURROGATE_POOL["gp"]
    normalized_kernel_key = "smkbo" if str(kernel_key).strip().lower() == "smk" else str(kernel_key).strip().lower()
    if entry.key == "gp":
        model = entry.factory(params or {}, normalized_kernel_key or "matern52", kernel_params or {})
    else:
        model = entry.factory(params or {})
    model.metadata.setdefault("selected_key", key)
    model.metadata.setdefault("resolved_key", entry.key)
    model.metadata.setdefault("resolved_kernel", normalized_kernel_key if entry.key == "gp" else None)
    model.metadata.setdefault("runtime_mode", detect_runtime_capabilities()["runtime_mode"])
    return model


def create_acquisition(key: str, params: dict[str, Any] | None = None) -> AcquisitionFunction:
    entry = AF_POOL.get(key) or AF_POOL["log_ei"]
    acquisition = entry.factory(params or {})
    acquisition.metadata.setdefault("selected_key", key)
    acquisition.metadata.setdefault("resolved_key", entry.key)
    acquisition.metadata.setdefault("runtime_mode", detect_runtime_capabilities()["runtime_mode"])
    return acquisition


def candidate_to_key(candidate: dict[str, Any]) -> str:
    items = tuple(sorted((str(key), _stable_value(value)) for key, value in candidate.items()))
    return hashlib.sha256(repr(items).encode("utf-8")).hexdigest()


def candidate_distance(
    left: dict[str, Any],
    right: dict[str, Any],
    search_space: list[dict[str, Any]],
) -> float:
    """Mixed Hamming + normalized-continuous distance between two candidates."""
    distance = 0.0
    for variable in search_space:
        name = str(variable.get("name") or "")
        if variable.get("type") == "continuous":
            low, high = _continuous_bounds(variable)
            span = max(high - low, 1e-9)
            left_value = _safe_float_or_none(left.get(name))
            right_value = _safe_float_or_none(right.get(name))
            if left_value is None or right_value is None:
                continue
            distance += abs(left_value - right_value) / span
            continue
        distance += 0.0 if str(left.get(name, "")) == str(right.get(name, "")) else 1.0
    return distance


def build_doe_pool(
    variables: list[dict[str, Any]],
    *,
    pool_size: int = 60,
    seed: int = 0,
    observed_keys: set[str] | None = None,
    hard_constraints: list[dict[str, Any]] | None = None,
    candidate_pool: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    target_size = max(1, int(pool_size or 1))
    observed = set(observed_keys or set())
    constraints = list(hard_constraints or [])

    if candidate_pool is not None:
        raw_pool = [dict(candidate) for candidate in candidate_pool]
    else:
        source_pool_size = max(target_size * 4, 32)
        discrete_candidates = enumerate_discrete_candidates(variables, max_candidates=source_pool_size)
        if discrete_candidates:
            raw_pool = discrete_candidates
        else:
            raw_pool = hybrid_sample_candidates(variables, source_pool_size, seed=seed)

    feasible: list[dict[str, Any]] = []
    seen = set(observed)
    for candidate in raw_pool:
        key = candidate_to_key(candidate)
        if key in seen or _candidate_violates_constraints(candidate, constraints):
            continue
        seen.add(key)
        feasible.append(dict(candidate))

    if not feasible:
        return []

    limit = min(target_size, len(feasible))
    seeds = _coverage_seed_candidates(feasible, variables, limit=limit, seed=seed)
    selected_keys = {candidate_to_key(candidate) for candidate in seeds}
    remaining = [candidate for candidate in feasible if candidate_to_key(candidate) not in selected_keys]
    selected = _greedy_maximin_candidates(
        remaining,
        variables,
        limit=limit,
        seed=seed,
        initial_selected=seeds,
    )
    return selected[:limit]


def discrete_search_space_size(
    search_space: list[dict[str, Any]],
    max_candidates: int | None = None,
) -> int | None:
    total = 1
    for variable in search_space:
        if variable.get("type", "categorical") == "continuous":
            return None
        domain_size = len(_domain_labels(variable))
        if domain_size == 0:
            return 0
        total *= domain_size
        if max_candidates is not None and total > max_candidates:
            return total
    return total


def enumerate_discrete_candidates(
    search_space: list[dict[str, Any]],
    max_candidates: int | None = None,
) -> list[dict[str, Any]]:
    total = discrete_search_space_size(search_space, max_candidates=max_candidates)
    if total is None:
        return []
    if max_candidates is not None and total > max_candidates:
        return []

    names = []
    domains = []
    for variable in search_space:
        names.append(variable["name"])
        domains.append(_domain_labels(variable))
    candidates = []
    for values in itertools.product(*domains):
        candidates.append(dict(zip(names, values)))
    return candidates


def hybrid_sample_candidates(
    search_space: list[dict[str, Any]],
    num_samples: int,
    seed: int = 0,
) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    continuous_vars = [var for var in search_space if var.get("type") == "continuous"]
    categorical_vars = [var for var in search_space if var.get("type") != "continuous"]
    lhs_columns: dict[str, np.ndarray] = {}
    if continuous_vars:
        lhs = _latin_hypercube(len(continuous_vars), num_samples, rng)
        for idx, variable in enumerate(continuous_vars):
            low, high = _continuous_bounds(variable)
            lhs_columns[variable["name"]] = low + lhs[:, idx] * (high - low)

    candidates = []
    for row_idx in range(num_samples):
        candidate = {}
        for variable in categorical_vars:
            labels = _domain_labels(variable) or ["unknown"]
            candidate[variable["name"]] = str(rng.choice(labels))
        for variable in continuous_vars:
            low, high = _continuous_bounds(variable)
            value = float(lhs_columns[variable["name"]][row_idx])
            candidate[variable["name"]] = round(value if not (float(low).is_integer() and float(high).is_integer()) else round(value), 6)
        candidates.append(candidate)
    return candidates


def _candidate_violates_constraints(
    candidate: dict[str, Any],
    hard_constraints: list[dict[str, Any]],
) -> bool:
    return any(not constraint.get("check", lambda _: True)(candidate) for constraint in hard_constraints)


def _coverage_seed_candidates(
    pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    *,
    limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    categorical_variables = [variable for variable in variables if variable.get("type") != "continuous"]

    for variable in categorical_variables:
        if len(selected) >= limit:
            break
        name = str(variable.get("name") or "")
        value_counts = _categorical_value_counts(pool, name)
        prioritized_values = [
            value
            for value, _count in sorted(value_counts.items(), key=lambda item: (-item[1], item[0]))
        ][: min(len(_domain_labels(variable)), 4)]
        for value in prioritized_values:
            if len(selected) >= limit:
                break
            if any(str(candidate.get(name, "")) == value for candidate in selected):
                continue
            matching = [
                candidate
                for candidate in pool
                if str(candidate.get(name, "")) == value and candidate_to_key(candidate) not in selected_keys
            ]
            if not matching:
                continue
            chosen = _select_next_diverse_candidate(matching, variables, selected, seed)
            key = candidate_to_key(chosen)
            if key in selected_keys:
                continue
            selected.append(dict(chosen))
            selected_keys.add(key)
    return selected


def _categorical_value_counts(pool: list[dict[str, Any]], variable_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in pool:
        value = str(candidate.get(variable_name, ""))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _greedy_maximin_candidates(
    pool: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    *,
    limit: int,
    seed: int,
    initial_selected: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    selected = [dict(candidate) for candidate in (initial_selected or [])]
    selected_keys = {candidate_to_key(candidate) for candidate in selected}
    remaining = [dict(candidate) for candidate in pool if candidate_to_key(candidate) not in selected_keys]

    while remaining and len(selected) < limit:
        chosen = _select_next_diverse_candidate(remaining, variables, selected, seed)
        chosen_key = candidate_to_key(chosen)
        selected.append(dict(chosen))
        remaining = [candidate for candidate in remaining if candidate_to_key(candidate) != chosen_key]
    return selected


def _select_next_diverse_candidate(
    candidates: list[dict[str, Any]],
    variables: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    seed: int,
) -> dict[str, Any]:
    best_candidate = dict(candidates[0])
    best_score = float("-inf")
    best_tie_breaker = _candidate_seed_tie_breaker(best_candidate, seed)

    for candidate in candidates:
        tie_breaker = _candidate_seed_tie_breaker(candidate, seed)
        if not selected:
            if tie_breaker < best_tie_breaker:
                best_candidate = dict(candidate)
                best_tie_breaker = tie_breaker
            continue
        score = min(candidate_distance(candidate, prior, variables) for prior in selected)
        if score > best_score or (math.isclose(score, best_score) and tie_breaker < best_tie_breaker):
            best_candidate = dict(candidate)
            best_score = score
            best_tie_breaker = tie_breaker
    return best_candidate


def _candidate_seed_tie_breaker(candidate: dict[str, Any], seed: int) -> str:
    return hashlib.sha256(f"{seed}:{candidate_to_key(candidate)}".encode("utf-8")).hexdigest()


def get_pool_summary(pool: dict[str, PoolEntry]) -> list[dict[str, Any]]:
    capabilities = detect_runtime_capabilities()
    summary = []
    for entry in pool.values():
        tags = dict(entry.tags)
        summary.append(
            {
                "key": entry.key,
                "name": entry.display_name,
                "description": entry.description,
                "tags": tags,
                "availability": _entry_availability(tags, capabilities),
            }
        )
    return summary


def get_embedding_options() -> list[dict[str, Any]]:
    return get_pool_summary(EMBEDDING_POOL)


def get_surrogate_options() -> list[dict[str, Any]]:
    return get_pool_summary(SURROGATE_POOL)


def get_kernel_options() -> list[dict[str, Any]]:
    return get_pool_summary(KERNEL_POOL)


def get_af_options() -> list[dict[str, Any]]:
    return get_pool_summary(AF_POOL)


def _entry_availability(tags: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    dependencies = tags.get("dependencies", [])
    available = True
    missing = []
    for dependency in dependencies:
        if dependency == "rdkit" and not capabilities["rdkit"]:
            available = False
            missing.append("rdkit")
        if dependency == "molclr" and not capabilities.get("molclr", False):
            available = False
            missing.append("molclr")
        if dependency == "chemberta" and not capabilities.get("chemberta", False):
            available = False
            missing.append("chemberta")
        if dependency in {"torch", "gpytorch", "botorch", "botorch_optional", "advanced_bo_optional"} and not capabilities["torch_stack"]:
            available = False
            missing.append(dependency)
        if dependency in {"network", "embedding_api"}:
            available = False
            missing.append(dependency)
        if dependency == "scikit-learn":
            try:
                import sklearn  # noqa: F401
            except Exception:
                available = False
                missing.append("scikit-learn")
        if dependency == "scipy":
            try:
                import scipy  # noqa: F401
            except Exception:
                available = False
                missing.append("scipy")
    return {
        "is_available": available,
        "runtime_mode": capabilities["runtime_mode"],
        "missing_dependencies": missing,
    }


def _gpytorch_kernel(
    kernel_name: str,
    dim: int,
    metadata: dict[str, Any],
    kernel_params: dict[str, Any] | None = None,
):
    if ScaleKernel is None or MaternKernel is None or RBFKernel is None or AdditiveKernel is None or ProductKernel is None:
        raise RuntimeError("BoTorch kernel dependencies are unavailable")
    kernel_params = kernel_params or {}
    kernel_name = "smkbo" if str(kernel_name).strip().lower() == "smk" else str(kernel_name).strip().lower()
    if kernel_name == "rbf":
        return ScaleKernel(RBFKernel(ard_num_dims=dim))
    if kernel_name == "matern32":
        return ScaleKernel(MaternKernel(nu=1.5, ard_num_dims=dim))
    if kernel_name == "smkbo":
        if WrappedSMK is None:
            raise RuntimeError("SMKBO kernel dependencies are unavailable")
        num_mixtures1 = max(0, int(kernel_params.get("num_mixtures1", 5)))
        num_mixtures2 = max(0, int(kernel_params.get("num_mixtures2", 4)))
        metadata.setdefault("notes", []).append(
            "smkbo uses an additive SpectralMixture + CauchyMixture kernel without ScaleKernel."
        )
        return WrappedSMK(
            ard_num_dims=dim,
            num_mixtures1=num_mixtures1,
            num_mixtures2=num_mixtures2,
        )
    if kernel_name == "sum_kernel":
        metadata.setdefault("notes", []).append("sum_kernel uses an additive Matern + RBF kernel in GPyTorch.")
        return ScaleKernel(
            AdditiveKernel(
                MaternKernel(nu=2.5, ard_num_dims=dim),
                RBFKernel(ard_num_dims=dim),
            )
        )
    if kernel_name == "product_kernel":
        metadata.setdefault("notes", []).append("product_kernel uses a multiplicative Matern x RBF kernel in GPyTorch.")
        return ScaleKernel(
            ProductKernel(
                MaternKernel(nu=2.5, ard_num_dims=dim),
                RBFKernel(ard_num_dims=dim),
            )
        )
    if kernel_name == "mixed_sum_product":
        metadata.setdefault("notes", []).append("mixed_sum_product blends additive and multiplicative GPyTorch kernels.")
        return ScaleKernel(
            AdditiveKernel(
                MaternKernel(nu=2.5, ard_num_dims=dim),
                RBFKernel(ard_num_dims=dim),
                ProductKernel(
                    MaternKernel(nu=1.5, ard_num_dims=dim),
                    RBFKernel(ard_num_dims=dim),
                ),
            )
        )
    return ScaleKernel(MaternKernel(nu=2.5, ard_num_dims=dim))


def _to_torch_matrix(X: np.ndarray) -> "torch.Tensor":
    if torch is None:
        raise RuntimeError("Torch is unavailable")
    array = np.asarray(X, dtype=float)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    return torch.as_tensor(array, dtype=torch.double)


def _to_torch_column(y: np.ndarray) -> "torch.Tensor":
    if torch is None:
        raise RuntimeError("Torch is unavailable")
    array = np.asarray(y, dtype=float).reshape(-1, 1)
    return torch.as_tensor(array, dtype=torch.double)


def _continuous_bounds(variable: dict[str, Any]) -> tuple[float, float]:
    domain = variable.get("domain", [0.0, 1.0])
    low = float(domain[0])
    high = float(domain[1])
    if math.isclose(low, high):
        high = low + 1.0
    return low, high


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_continuous(value: Any, low: float, high: float) -> float:
    if value is None:
        return 0.5
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        return 0.5
    return float(np.clip((value_f - low) / (high - low), 0.0, 1.0))


def _denormalize_continuous(value: float, low: float, high: float) -> float:
    raw = low + float(np.clip(value, 0.0, 1.0)) * (high - low)
    return round(raw, 6)


def _safe_index(labels: list[Any], value: Any) -> int:
    value_str = str(value)
    for idx, label in enumerate(labels):
        if str(label) == value_str:
            return idx
    return 0


def _numeric_domain_spec(variable: dict[str, Any]) -> dict[str, Any] | None:
    labels = _domain_labels(variable)
    if not labels:
        return None
    numeric_values: list[float] = []
    for label in labels:
        numeric_value = _safe_float_or_none(label)
        if numeric_value is None:
            return None
        numeric_values.append(float(numeric_value))
    low = min(numeric_values)
    high = max(numeric_values)
    if math.isclose(low, high):
        high = low + 1.0
    return {
        "labels": labels,
        "value_map": {label: value for label, value in zip(labels, numeric_values)},
        "low": low,
        "high": high,
    }


def _domain_labels(variable: dict[str, Any]) -> list[str]:
    labels = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            labels.append(str(entry.get("label") or entry.get("name") or entry.get("value") or entry))
        else:
            labels.append(str(entry))
    return labels


def _variable_smiles_map(variable: dict[str, Any]) -> dict[str, str]:
    smiles_map = {str(key): str(value) for key, value in variable.get("smiles_map", {}).items()}
    for entry in variable.get("domain", []):
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("name") or entry.get("value") or "")
        smiles = str(entry.get("smiles") or "")
        if label and smiles:
            smiles_map[label] = smiles
    return smiles_map


def _fingerprint_from_smiles(smiles: str, radius: int, n_bits: int) -> np.ndarray | None:
    if Chem is None or rdFingerprintGenerator is None or DataStructs is None:
        return None
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return None
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits)
    fp = generator.GetFingerprint(molecule)
    array = np.zeros((n_bits,), dtype=float)
    DataStructs.ConvertToNumpyArray(fp, array)
    return array


def _molclr_embedding_from_smiles(smiles: str) -> np.ndarray | None:
    if not callable(MOLCLR_EMBED):
        return None
    try:
        vector = MOLCLR_EMBED(str(smiles))
    except Exception:  # pragma: no cover
        return None
    if vector is None:
        return None
    array = np.asarray(vector, dtype=float).reshape(-1)
    if array.size == 0 or np.any(~np.isfinite(array)):
        return None
    return array


def _chemberta_embedding_from_smiles(smiles: str) -> np.ndarray | None:
    if not callable(CHEMBERTA_EMBED):
        return None
    try:
        vector = CHEMBERTA_EMBED(str(smiles))
    except Exception:  # pragma: no cover
        return None
    if vector is None:
        return None
    array = np.asarray(vector, dtype=float).reshape(-1)
    if array.size == 0 or np.any(~np.isfinite(array)):
        return None
    return array


def _descriptor_vector_from_smiles(smiles: str) -> np.ndarray | None:
    if Chem is None or Descriptors is None or Crippen is None or rdMolDescriptors is None or Lipinski is None:
        return None
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return None
    hetero_atoms = sum(1 for atom in molecule.GetAtoms() if atom.GetAtomicNum() not in {1, 6})
    chiral_centers = len(Chem.FindMolChiralCenters(molecule, includeUnassigned=True))
    vector = np.asarray(
        [
            Descriptors.MolWt(molecule) / 1000.0,
            Crippen.MolLogP(molecule) / 10.0,
            Crippen.MolMR(molecule) / 30.0,
            rdMolDescriptors.CalcTPSA(molecule) / 250.0,
            float(Lipinski.NumHDonors(molecule)) / 10.0,
            float(Lipinski.NumHAcceptors(molecule)) / 15.0,
            float(Lipinski.NumRotatableBonds(molecule)) / 20.0,
            float(rdMolDescriptors.CalcNumRings(molecule)) / 10.0,
            float(rdMolDescriptors.CalcNumAromaticRings(molecule)) / 8.0,
            float(rdMolDescriptors.CalcNumAliphaticRings(molecule)) / 8.0,
            float(molecule.GetNumHeavyAtoms()) / 80.0,
            float(hetero_atoms) / 20.0,
            float(rdMolDescriptors.CalcFractionCSP3(molecule)),
            float(rdMolDescriptors.CalcNumAmideBonds(molecule)) / 10.0,
            float(chiral_centers) / 10.0,
        ],
        dtype=float,
    )
    return np.clip(vector, 0.0, 1.0)


def _latin_hypercube(dim: int, n: int, rng: np.random.Generator) -> np.ndarray:
    if dim <= 0 or n <= 0:
        return np.zeros((n, dim), dtype=float)
    result = np.zeros((n, dim), dtype=float)
    for column in range(dim):
        perm = rng.permutation(n)
        result[:, column] = (perm + rng.random(n)) / n
    return result


def _stable_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 8)
    return value
