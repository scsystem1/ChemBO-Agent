"""
Component Pools
================
Registries of available BO components and the lightweight runtime objects
used by the ChemBO Phase 1 demo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Callable
import hashlib
import itertools
import math
import random

import numpy as np

try:
    from rdkit import Chem
    from rdkit.Chem import AllChem, DataStructs
except ImportError:  # pragma: no cover - optional dependency
    Chem = None
    AllChem = None
    DataStructs = None

try:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, RBF, WhiteKernel
except ImportError:  # pragma: no cover - expected in very minimal envs
    RandomForestRegressor = None
    GaussianProcessRegressor = None
    ConstantKernel = None
    Matern = None
    RBF = None
    WhiteKernel = None


NDIST = NormalDist()


@dataclass
class PoolEntry:
    """A single component available in a pool."""

    key: str
    display_name: str
    description: str
    tags: dict[str, Any] = field(default_factory=dict)
    factory: Callable[..., Any] | None = None


class BaseEncoder:
    """Shared encoder interface used by the BO engine."""

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
    """One-hot encode categoricals and normalize continuous variables."""

    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.specs: list[dict[str, Any]] = []
        offset = 0
        for variable in search_space:
            var_type = variable.get("type", "categorical")
            if var_type == "continuous":
                domain = variable.get("domain", [0.0, 1.0])
                low = float(domain[0])
                high = float(domain[1])
                if math.isclose(low, high):
                    high = low + 1.0
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
            else:
                labels = _domain_labels(variable)
                if not labels:
                    labels = ["unknown"]
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
            else:
                labels = spec["labels"]
                idx = _safe_index(labels, value)
                vector[spec["slice"].start + idx] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        decoded: dict[str, Any] = {}
        for spec in self.specs:
            chunk = encoded[spec["slice"]]
            if spec["type"] == "continuous":
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            else:
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded


class FingerprintConcatEncoder(BaseEncoder):
    """Use Morgan fingerprints for categorical variables with SMILES metadata."""

    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.n_bits = int(self.params.get("n_bits", 256))
        self.radius = int(self.params.get("radius", 2))
        self.specs: list[dict[str, Any]] = []
        offset = 0
        has_fp = False
        for variable in search_space:
            var_type = variable.get("type", "categorical")
            if var_type == "continuous":
                domain = variable.get("domain", [0.0, 1.0])
                low = float(domain[0])
                high = float(domain[1])
                if math.isclose(low, high):
                    high = low + 1.0
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

            labels = _domain_labels(variable)
            smiles_map = _variable_smiles_map(variable)
            fp_map: dict[str, np.ndarray] = {}
            if Chem is not None and AllChem is not None:
                for label, smiles in smiles_map.items():
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
                if smiles_map and not fp_map:
                    self.metadata["notes"].append(
                        f"{variable['name']}: invalid or unavailable SMILES fingerprints; using one-hot fallback"
                    )
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels or ["unknown"],
                        "slice": slice(offset, offset + len(labels or ['unknown'])),
                    }
                )
                offset += len(labels or ["unknown"])

        if not has_fp:
            self.metadata["notes"].append("No valid molecular fingerprints found; encoder behaves like one-hot.")
        self._dim = offset

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "fingerprint":
                fp = spec["fp_map"].get(str(value))
                if fp is None:
                    fp = np.zeros(self.n_bits, dtype=float)
                vector[spec["slice"]] = fp
            else:
                labels = spec["labels"]
                idx = _safe_index(labels, value)
                vector[spec["slice"].start + idx] = 1.0
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        decoded: dict[str, Any] = {}
        for spec in self.specs:
            chunk = encoded[spec["slice"]]
            if spec["type"] == "continuous":
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            elif spec["type"] == "fingerprint":
                best_label = None
                best_distance = float("inf")
                for label, fp in spec["fp_map"].items():
                    distance = float(np.linalg.norm(chunk - fp))
                    if distance < best_distance:
                        best_distance = distance
                        best_label = label
                decoded[spec["name"]] = best_label
            else:
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded


class GuardedFallbackEncoder(BaseEncoder):
    """Expose advanced encoder keys while safely degrading to stable baselines."""

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


class BaseSurrogateModel:
    """Shared surrogate interface."""

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}
        self.metadata: dict[str, Any] = {"notes": []}

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        raise NotImplementedError

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    @property
    def botorch_model(self):
        return None


class GaussianProcessSurrogate(BaseSurrogateModel):
    """Sklearn-backed GP surrogate used for the Phase 1 demo."""

    def __init__(self, kernel_name: str, params: dict[str, Any] | None = None):
        super().__init__(params)
        self.kernel_name = kernel_name
        self.model = None
        self.log_marginal_likelihood_: float | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if GaussianProcessRegressor is None or ConstantKernel is None or WhiteKernel is None:
            raise RuntimeError("scikit-learn GaussianProcessRegressor is unavailable")

        noise_level = float(self.params.get("noise_level", 1e-5))
        if self.kernel_name == "rbf":
            base_kernel = RBF(length_scale=np.ones(X.shape[1]))
        else:
            nu = 2.5 if self.kernel_name != "mixture" else 1.5
            base_kernel = Matern(length_scale=np.ones(X.shape[1]), nu=nu)

        kernel = ConstantKernel(1.0, (1e-3, 1e3)) * base_kernel + WhiteKernel(
            noise_level=max(noise_level, 1e-6),
            noise_level_bounds=(1e-8, 1e1),
        )
        self.model = GaussianProcessRegressor(
            kernel=kernel,
            alpha=max(noise_level, 1e-8),
            normalize_y=True,
            n_restarts_optimizer=int(self.params.get("n_restarts_optimizer", 1)),
            random_state=int(self.params.get("random_state", 0)),
        )
        self.model.fit(X, y)
        self.log_marginal_likelihood_ = float(self.model.log_marginal_likelihood_value_)

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.model is None:
            raise RuntimeError("Surrogate model must be fit before prediction")
        mean, std = self.model.predict(X, return_std=True)
        return np.asarray(mean, dtype=float), np.maximum(np.asarray(std, dtype=float), 1e-9)


class RandomForestSurrogate(BaseSurrogateModel):
    """Random forest surrogate with empirical uncertainty from tree dispersion."""

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self.model = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if RandomForestRegressor is None:
            raise RuntimeError("scikit-learn RandomForestRegressor is unavailable")
        self.model = RandomForestRegressor(
            n_estimators=int(self.params.get("n_estimators", 200)),
            min_samples_leaf=int(self.params.get("min_samples_leaf", 1)),
            random_state=int(self.params.get("random_state", 0)),
        )
        self.model.fit(X, y)

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.model is None:
            raise RuntimeError("Surrogate model must be fit before prediction")
        tree_predictions = np.asarray([tree.predict(X) for tree in self.model.estimators_], dtype=float)
        mean = tree_predictions.mean(axis=0)
        std = np.maximum(tree_predictions.std(axis=0), 1e-6)
        return mean, std


class GuardedFallbackSurrogate(BaseSurrogateModel):
    """Fallback wrapper for advanced model keys."""

    def __init__(self, delegate: BaseSurrogateModel, reason: str):
        self._delegate = delegate
        self.params = delegate.params
        self.metadata = dict(delegate.metadata)
        self.metadata.setdefault("notes", []).append(reason)

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self._delegate.fit(X, y)

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self._delegate.predict(X)

    @property
    def botorch_model(self):
        return self._delegate.botorch_model


class AcquisitionFunction:
    """Pointwise acquisition scorer for candidate-pool optimization."""

    def __init__(self, key: str, params: dict[str, Any] | None = None):
        self.key = key
        self.params = params or {}
        self.metadata: dict[str, Any] = {"notes": []}

    def score(self, mean: np.ndarray, std: np.ndarray, best_f: float | None, rng: np.random.Generator) -> np.ndarray:
        mean = np.asarray(mean, dtype=float)
        std = np.maximum(np.asarray(std, dtype=float), 1e-9)
        if self.key in {"ei", "qei"}:
            reference = best_f if best_f is not None else float(np.max(mean))
            improvement = mean - reference
            z = improvement / std
            pdf = np.vectorize(NDIST.pdf)(z)
            cdf = np.vectorize(NDIST.cdf)(z)
            return improvement * cdf + std * pdf
        if self.key in {"ucb", "qucb"}:
            beta = float(self.params.get("beta", 0.2))
            return mean + beta * std
        if self.key == "ts":
            return rng.normal(loc=mean, scale=std)
        if self.key == "nehvi":
            self.metadata.setdefault("notes", []).append("NEHVI not implemented for Phase 1; using UCB fallback.")
            beta = float(self.params.get("beta", 0.4))
            return mean + beta * std
        return mean


def _normalize_continuous(value: Any, low: float, high: float) -> float:
    if value is None:
        return 0.5
    value_f = float(value)
    if math.isclose(low, high):
        return 0.5
    return float(np.clip((value_f - low) / (high - low), 0.0, 1.0))


def _denormalize_continuous(value: float, low: float, high: float) -> float:
    value = float(np.clip(value, 0.0, 1.0))
    raw = low + value * (high - low)
    if float(low).is_integer() and float(high).is_integer():
        return round(raw, 6)
    return round(raw, 6)


def _safe_index(labels: list[Any], value: Any) -> int:
    value_str = str(value)
    for idx, label in enumerate(labels):
        if str(label) == value_str:
            return idx
    return 0


def _domain_labels(variable: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for entry in variable.get("domain", []):
        if isinstance(entry, dict):
            labels.append(str(entry.get("label") or entry.get("name") or entry.get("value") or entry))
        else:
            labels.append(str(entry))
    return labels


def _variable_smiles_map(variable: dict[str, Any]) -> dict[str, str]:
    smiles_map = {str(k): str(v) for k, v in variable.get("smiles_map", {}).items()}
    for entry in variable.get("domain", []):
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("name") or entry.get("value") or "")
        smiles = entry.get("smiles")
        if label and smiles:
            smiles_map[label] = str(smiles)
    return smiles_map


def _fingerprint_from_smiles(smiles: str, radius: int, n_bits: int) -> np.ndarray | None:
    if Chem is None or AllChem is None or DataStructs is None:
        return None
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return None
    fp = AllChem.GetMorganFingerprintAsBitVect(molecule, radius=radius, nBits=n_bits)
    array = np.zeros((n_bits,), dtype=float)
    DataStructs.ConvertToNumpyArray(fp, array)
    return array


def _make_one_hot_encoder(search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    return OneHotEncoder(search_space, params)


def _make_fingerprint_encoder(search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    return FingerprintConcatEncoder(search_space, params)


def _make_llm_embedding_encoder(search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    return GuardedFallbackEncoder(
        search_space,
        params,
        OneHotEncoder,
        "LLM embedding path requires external API/network access; falling back to one-hot encoding.",
    )


def _make_chemberta_encoder(search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    return GuardedFallbackEncoder(
        search_space,
        params,
        FingerprintConcatEncoder,
        "ChemBERTa weights are not loaded in Phase 1; falling back to fingerprint_concat.",
    )


def _make_bayesbe_encoder(search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    return GuardedFallbackEncoder(
        search_space,
        params,
        FingerprintConcatEncoder,
        "BayBE encodings are unavailable in this environment; falling back to fingerprint_concat.",
    )


def _make_gp_matern52(params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    return GaussianProcessSurrogate("matern52", params)


def _make_gp_rbf(params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    return GaussianProcessSurrogate("rbf", params)


def _make_gp_mixture(params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    model = GaussianProcessSurrogate("mixture", params)
    model.metadata.setdefault("notes", []).append(
        "Mixture kernel is approximated with a Matérn GP over encoded mixed features."
    )
    return model


def _make_dkl(params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    return GuardedFallbackSurrogate(
        GaussianProcessSurrogate("matern52", params),
        "DKL is not enabled in Phase 1; falling back to gp_matern52.",
    )


def _make_random_forest(params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    return RandomForestSurrogate(params)


def _make_gp_tanimoto(params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    return GuardedFallbackSurrogate(
        GaussianProcessSurrogate("matern52", params),
        "Tanimoto GP is approximated with gp_matern52 on fingerprint features.",
    )


def _make_acquisition(key: str) -> Callable[[dict[str, Any] | None], AcquisitionFunction]:
    def factory(params: dict[str, Any] | None = None) -> AcquisitionFunction:
        return AcquisitionFunction(key, params)

    return factory


# ============================================================================
# EMBEDDING POOL
# ============================================================================

EMBEDDING_POOL: dict[str, PoolEntry] = {
    "one_hot": PoolEntry(
        key="one_hot",
        display_name="One-Hot Encoding",
        description=(
            "Simple one-hot encoding for categorical variables, identity for "
            "continuous. No learned representation. Baseline method."
        ),
        tags={
            "learned": False,
            "handles_categorical": True,
            "handles_continuous": True,
            "dimensionality": "scales_with_categories",
            "requires_pretraining": False,
            "cost": "low",
            "best_for": "small category counts, baseline comparison",
        },
        factory=_make_one_hot_encoder,
    ),
    "fingerprint_concat": PoolEntry(
        key="fingerprint_concat",
        display_name="Morgan Fingerprint + OHE",
        description=(
            "Molecular fingerprints (Morgan/ECFP4) for molecular categorical "
            "variables (ligands, solvents), concatenated with one-hot for "
            "non-molecular categoricals and raw continuous values. "
            "Chemistry-aware but fixed representation."
        ),
        tags={
            "learned": False,
            "handles_categorical": True,
            "handles_continuous": True,
            "chemistry_aware": True,
            "dimensionality": "medium (128-512 depending on FP radius)",
            "requires_pretraining": False,
            "cost": "low",
            "best_for": "when variables are actual molecules with SMILES",
        },
        factory=_make_fingerprint_encoder,
    ),
    "llm_embedding": PoolEntry(
        key="llm_embedding",
        display_name="LLM Text Embedding",
        description=(
            "Encode each reaction condition set as a natural language string, "
            "then embed via a pretrained LLM (text-embedding-3-large). "
            "Captures semantic similarity but is NOT aligned with yield prediction. "
            "Recommended to combine with DKL for alignment."
        ),
        tags={
            "learned": False,
            "handles_categorical": True,
            "handles_continuous": True,
            "chemistry_aware": True,
            "dimensionality": "high (3072 for text-embedding-3-large)",
            "requires_pretraining": False,
            "cost": "medium (API calls)",
            "alignment_with_target": "low — needs DKL",
            "best_for": "rich textual descriptions, heterogeneous variables",
        },
        factory=_make_llm_embedding_encoder,
    ),
    "chemberta": PoolEntry(
        key="chemberta",
        display_name="ChemBERTa Molecular Embedding",
        description=(
            "Domain-specific transformer embeddings from ChemBERTa. "
            "Pretrained on ~77M SMILES strings. Best for molecular variables. "
            "Fixed 768-dim representation."
        ),
        tags={
            "learned": True,
            "handles_categorical": True,
            "handles_continuous": False,
            "chemistry_aware": True,
            "dimensionality": "768",
            "requires_pretraining": False,
            "cost": "medium",
            "best_for": "molecular-heavy search spaces",
        },
        factory=_make_chemberta_encoder,
    ),
    "bayesbe_encoding": PoolEntry(
        key="bayesbe_encoding",
        display_name="BayBE Chemical Encoding",
        description=(
            "Industry-grade chemical encodings from BayBE (Merck KGaA). "
            "Includes RDKIT descriptors, MACCS keys, and custom substance "
            "encodings. Shown to reduce experiments by ≥50% vs one-hot."
        ),
        tags={
            "learned": False,
            "handles_categorical": True,
            "handles_continuous": True,
            "chemistry_aware": True,
            "dimensionality": "varies (50-200)",
            "requires_pretraining": False,
            "cost": "low",
            "best_for": "industrial chemical optimization with known substances",
        },
        factory=_make_bayesbe_encoder,
    ),
}


# ============================================================================
# SURROGATE MODEL POOL
# ============================================================================

SURROGATE_POOL: dict[str, PoolEntry] = {
    "gp_matern52": PoolEntry(
        key="gp_matern52",
        display_name="GP with Matérn-5/2 Kernel",
        description=(
            "Standard Gaussian Process with Matérn-5/2 kernel. The workhorse "
            "of BO. Provides well-calibrated uncertainty. Best for smooth "
            "objectives with ≤20 dimensions."
        ),
        tags={
            "model_type": "gp",
            "kernel": "matern52",
            "handles_categorical": False,
            "max_effective_dims": 20,
            "uncertainty_calibration": "excellent",
            "scalability": "O(n³) — up to ~2000 points",
            "requires_gpu": False,
            "best_for": "default choice, smooth low-dim problems",
        },
        factory=_make_gp_matern52,
    ),
    "gp_rbf": PoolEntry(
        key="gp_rbf",
        display_name="GP with RBF (SE) Kernel",
        description=(
            "GP with Radial Basis Function kernel. Assumes very smooth "
            "objective. Slightly more restrictive than Matérn-5/2."
        ),
        tags={
            "model_type": "gp",
            "kernel": "rbf",
            "handles_categorical": False,
            "max_effective_dims": 20,
            "uncertainty_calibration": "excellent",
            "scalability": "O(n³)",
            "requires_gpu": False,
            "best_for": "very smooth objectives",
        },
        factory=_make_gp_rbf,
    ),
    "gp_mixture_kernel": PoolEntry(
        key="gp_mixture_kernel",
        display_name="GP with Mixture Kernel (CatBO-style)",
        description=(
            "GP with a mixture kernel that handles categorical and continuous "
            "variables natively via separate kernel components combined "
            "additively or multiplicatively. Based on CASMOPOLITANMixed."
        ),
        tags={
            "model_type": "gp",
            "kernel": "mixture",
            "handles_categorical": True,
            "max_effective_dims": 30,
            "uncertainty_calibration": "good",
            "scalability": "O(n³)",
            "requires_gpu": False,
            "best_for": "mixed categorical-continuous problems (like DAR)",
        },
        factory=_make_gp_mixture,
    ),
    "dkl": PoolEntry(
        key="dkl",
        display_name="Deep Kernel Learning (DKL)",
        description=(
            "Neural network feature extractor + GP layer. Learns a "
            "task-aligned embedding jointly with the GP via marginal "
            "likelihood. Addresses the embedding-objective misalignment "
            "problem. Requires more data (~20+ points) to train the NN."
        ),
        tags={
            "model_type": "dkl",
            "kernel": "rbf_on_learned_features",
            "handles_categorical": True,
            "max_effective_dims": 100,
            "uncertainty_calibration": "good (with proper regularization)",
            "scalability": "O(n³) for GP part, NN part is fast",
            "requires_gpu": True,
            "min_data_for_training": 20,
            "best_for": "high-dim embeddings, misaligned representations",
        },
        factory=_make_dkl,
    ),
    "random_forest": PoolEntry(
        key="random_forest",
        display_name="Random Forest Surrogate",
        description=(
            "Ensemble of decision trees. Handles mixed types natively, "
            "scales well, but provides less calibrated uncertainty than GP. "
            "Robust to noisy data. Good when GP struggles."
        ),
        tags={
            "model_type": "random_forest",
            "handles_categorical": True,
            "max_effective_dims": 100,
            "uncertainty_calibration": "moderate",
            "scalability": "excellent",
            "requires_gpu": False,
            "best_for": "high-dim, noisy, or non-stationary objectives",
        },
        factory=_make_random_forest,
    ),
    "gp_tanimoto": PoolEntry(
        key="gp_tanimoto",
        display_name="GP with Tanimoto Kernel",
        description=(
            "GP using Tanimoto kernel on molecular fingerprints. "
            "Chemistry-standard similarity metric. Best when variables "
            "are molecules represented as fingerprints."
        ),
        tags={
            "model_type": "gp",
            "kernel": "tanimoto",
            "handles_categorical": True,
            "max_effective_dims": 2048,
            "uncertainty_calibration": "good",
            "requires_gpu": False,
            "best_for": "fingerprint-based molecular search spaces",
        },
        factory=_make_gp_tanimoto,
    ),
}


# ============================================================================
# ACQUISITION FUNCTION POOL
# ============================================================================

AF_POOL: dict[str, PoolEntry] = {
    "ei": PoolEntry(
        key="ei",
        display_name="Expected Improvement (EI)",
        description=(
            "Classic acquisition function balancing exploration and exploitation "
            "via improvement over the current best. Well-understood, robust."
        ),
        tags={
            "type": "improvement",
            "exploration_exploitation": "balanced",
            "batch_support": False,
            "multi_objective": False,
            "requires_best_f": True,
            "best_for": "default choice, moderate-budget campaigns",
        },
        factory=_make_acquisition("ei"),
    ),
    "ucb": PoolEntry(
        key="ucb",
        display_name="Upper Confidence Bound (UCB)",
        description=(
            "Optimistic acquisition function: μ + β·σ. Explicit exploration "
            "control via β parameter. Higher β = more exploration."
        ),
        tags={
            "type": "optimistic",
            "exploration_exploitation": "tunable (via beta)",
            "batch_support": False,
            "multi_objective": False,
            "hyperparameters": {"beta": "float, typically 0.1-2.0"},
            "best_for": "when explicit exploration control is needed",
        },
        factory=_make_acquisition("ucb"),
    ),
    "qei": PoolEntry(
        key="qei",
        display_name="q-Expected Improvement (qEI)",
        description=(
            "Batch version of EI. Selects q candidates jointly to maximize "
            "total expected improvement. Essential for parallel experiments."
        ),
        tags={
            "type": "improvement",
            "exploration_exploitation": "balanced",
            "batch_support": True,
            "multi_objective": False,
            "best_for": "batch experiments (q > 1)",
        },
        factory=_make_acquisition("qei"),
    ),
    "qucb": PoolEntry(
        key="qucb",
        display_name="q-Upper Confidence Bound (qUCB)",
        description="Batch version of UCB. Joint optimization of q candidates.",
        tags={
            "type": "optimistic",
            "exploration_exploitation": "tunable",
            "batch_support": True,
            "multi_objective": False,
            "best_for": "batch experiments with exploration control",
        },
        factory=_make_acquisition("qucb"),
    ),
    "ts": PoolEntry(
        key="ts",
        display_name="Thompson Sampling",
        description=(
            "Sample from the posterior and optimize the sample. Naturally "
            "diverse in batch mode. Computationally cheap per sample."
        ),
        tags={
            "type": "sampling",
            "exploration_exploitation": "automatically balanced",
            "batch_support": True,
            "multi_objective": False,
            "best_for": "high exploration, early-stage optimization",
        },
        factory=_make_acquisition("ts"),
    ),
    "nehvi": PoolEntry(
        key="nehvi",
        display_name="Noisy Expected Hypervolume Improvement (NEHVI)",
        description=(
            "Multi-objective acquisition function. Maximizes expected "
            "improvement in Pareto hypervolume. For ≥2 objectives."
        ),
        tags={
            "type": "hypervolume",
            "exploration_exploitation": "balanced",
            "batch_support": True,
            "multi_objective": True,
            "best_for": "multi-objective optimization (yield + selectivity)",
        },
        factory=_make_acquisition("nehvi"),
    ),
}


def create_encoder(key: str, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None) -> BaseEncoder:
    entry = EMBEDDING_POOL.get(key) or EMBEDDING_POOL["one_hot"]
    encoder = entry.factory(search_space, params or {})
    encoder.metadata.setdefault("selected_key", key)
    encoder.metadata.setdefault("resolved_key", entry.key)
    return encoder


def create_surrogate(key: str, params: dict[str, Any] | None = None) -> BaseSurrogateModel:
    entry = SURROGATE_POOL.get(key) or SURROGATE_POOL["gp_matern52"]
    model = entry.factory(params or {})
    model.metadata.setdefault("selected_key", key)
    model.metadata.setdefault("resolved_key", entry.key)
    return model


def create_acquisition(key: str, params: dict[str, Any] | None = None) -> AcquisitionFunction:
    entry = AF_POOL.get(key) or AF_POOL["ei"]
    acq = entry.factory(params or {})
    acq.metadata.setdefault("selected_key", key)
    acq.metadata.setdefault("resolved_key", entry.key)
    return acq


def candidate_to_key(candidate: dict[str, Any]) -> str:
    items = tuple(sorted((str(k), _stable_value(v)) for k, v in candidate.items()))
    return hashlib.sha256(repr(items).encode("utf-8")).hexdigest()


def enumerate_discrete_candidates(search_space: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categorical_names: list[str] = []
    domains: list[list[Any]] = []
    for variable in search_space:
        if variable.get("type", "categorical") == "continuous":
            return []
        categorical_names.append(variable["name"])
        labels = _domain_labels(variable)
        domains.append(labels)
    candidates: list[dict[str, Any]] = []
    for values in itertools.product(*domains):
        candidates.append(dict(zip(categorical_names, values)))
    return candidates


def hybrid_sample_candidates(
    search_space: list[dict[str, Any]],
    num_samples: int,
    seed: int = 0,
) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    continuous_vars = [variable for variable in search_space if variable.get("type") == "continuous"]
    categorical_vars = [variable for variable in search_space if variable.get("type") != "continuous"]
    lhs_columns: dict[str, np.ndarray] = {}
    if continuous_vars:
        lhs = _latin_hypercube(len(continuous_vars), num_samples, rng)
        for idx, variable in enumerate(continuous_vars):
            low = float(variable["domain"][0])
            high = float(variable["domain"][1])
            lhs_columns[variable["name"]] = low + lhs[:, idx] * (high - low)

    candidates: list[dict[str, Any]] = []
    for row_idx in range(num_samples):
        candidate: dict[str, Any] = {}
        for variable in categorical_vars:
            labels = _domain_labels(variable)
            candidate[variable["name"]] = str(rng.choice(labels)) if labels else "unknown"
        for variable in continuous_vars:
            value = float(lhs_columns[variable["name"]][row_idx])
            if float(variable["domain"][0]).is_integer() and float(variable["domain"][1]).is_integer():
                candidate[variable["name"]] = round(value)
            else:
                candidate[variable["name"]] = round(value, 6)
        candidates.append(candidate)
    return candidates


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


def get_pool_summary(pool: dict[str, PoolEntry]) -> list[dict]:
    return [
        {
            "key": entry.key,
            "name": entry.display_name,
            "description": entry.description,
            "tags": entry.tags,
        }
        for entry in pool.values()
    ]


def get_embedding_options() -> list[dict]:
    return get_pool_summary(EMBEDDING_POOL)


def get_surrogate_options() -> list[dict]:
    return get_pool_summary(SURROGATE_POOL)


def get_af_options() -> list[dict]:
    return get_pool_summary(AF_POOL)
