"""
Component pools and lightweight BO runtime implementations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import itertools
import io
import math
import os

import numpy as np

try:
    import torch
    from botorch.acquisition import LogExpectedImprovement, UpperConfidenceBound
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from botorch.models.transforms.outcome import Standardize
    from gpytorch.kernels import AdditiveKernel, MaternKernel, ProductKernel, RBFKernel, ScaleKernel
    from gpytorch.mlls import ExactMarginalLogLikelihood
    try:
        from .smk_kernels import WrappedSMK
    except ImportError:  # pragma: no cover
        from pools.smk_kernels import WrappedSMK
except ImportError:  # pragma: no cover
    torch = None
    LogExpectedImprovement = None
    UpperConfidenceBound = None
    fit_gpytorch_mll = None
    SingleTaskGP = None
    Standardize = None
    AdditiveKernel = None
    MaternKernel = None
    ProductKernel = None
    RBFKernel = None
    ScaleKernel = None
    ExactMarginalLogLikelihood = None
    WrappedSMK = None

def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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

@dataclass
class PoolEntry:
    key: str
    display_name: str
    description: str
    tags: dict[str, Any] = field(default_factory=dict)
    factory: Callable[..., Any] | None = None


def detect_runtime_capabilities() -> dict[str, Any]:
    rdkit_available = Chem is not None and rdFingerprintGenerator is not None and DataStructs is not None
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
    if not torch_stack_present:
        notes.append("BoTorch stack is unavailable in the current environment.")
    else:
        notes.append("BoTorch stack enabled.")
    return {
        "rdkit": rdkit_available,
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
            else:
                labels = spec["labels"]
                decoded[spec["name"]] = labels[int(np.argmax(chunk))] if labels else None
        return decoded


class OrdinalCategoricalEncoder(BaseEncoder):
    """Encodes each categorical variable as an arbitrary 1..N ordinal."""

    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.specs: list[dict[str, Any]] = []
        self.metadata.setdefault("notes", []).append(
            "Categorical labels are mapped to arbitrary ordinal numbers; distances between labels are not meaningful."
        )
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
            else:
                labels = _domain_labels(variable) or ["unknown"]
                self.specs.append(
                    {
                        "name": variable["name"],
                        "type": "categorical",
                        "labels": labels,
                        "slice": slice(offset, offset + 1),
                    }
                )
            offset += 1
        self._dim = offset

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            else:
                labels = spec["labels"]
                vector[spec["slice"]] = float(_safe_index(labels, value) + 1)
        return vector

    def decode(self, encoded: np.ndarray) -> dict[str, Any]:
        encoded = np.asarray(encoded, dtype=float).reshape(-1)
        decoded = {}
        for spec in self.specs:
            chunk = encoded[spec["slice"]]
            if spec["type"] == "continuous":
                decoded[spec["name"]] = _denormalize_continuous(float(chunk[0]), spec["low"], spec["high"])
            else:
                labels = spec["labels"]
                if not labels:
                    decoded[spec["name"]] = None
                    continue
                ordinal = int(round(float(chunk[0])))
                ordinal = min(max(ordinal, 1), len(labels))
                decoded[spec["name"]] = labels[ordinal - 1]
        return decoded

    def get_bounds(self) -> tuple[np.ndarray, np.ndarray]:
        lower = np.zeros(self.dim, dtype=float)
        upper = np.ones(self.dim, dtype=float)
        for spec in self.specs:
            if spec["type"] == "categorical":
                lower[spec["slice"]] = 1.0
                upper[spec["slice"]] = float(len(spec["labels"]))
        return lower, upper


class FingerprintConcatEncoder(BaseEncoder):
    def __init__(self, search_space: list[dict[str, Any]], params: dict[str, Any] | None = None):
        super().__init__(search_space, params)
        self.radius = int(self.params.get("radius", 2))
        self.n_bits = int(self.params.get("n_bits", 256))
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
        self._dim = offset

    def encode(self, candidate: dict[str, Any]) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=float)
        for spec in self.specs:
            value = candidate.get(spec["name"])
            if spec["type"] == "continuous":
                vector[spec["slice"]] = _normalize_continuous(value, spec["low"], spec["high"])
            elif spec["type"] == "fingerprint":
                vector[spec["slice"]] = spec["fp_map"].get(str(value), np.zeros(self.n_bits, dtype=float))
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

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        if not detect_runtime_capabilities()["torch_stack"]:
            raise RuntimeError("BoTorch stack is unavailable")
        if X.size == 0:
            raise RuntimeError("Cannot fit GP without training data")
        train_X = _to_torch_matrix(X)
        train_Y = _to_torch_column(y)
        noise_level = max(float(self.params.get("noise_level", 1e-4)), 1e-6)
        train_Yvar = torch.full_like(train_Y, noise_level)
        covar_module = _gpytorch_kernel(self.kernel_name, X.shape[1], self.metadata, self.kernel_params)
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
        self.model = SingleTaskGP(
            train_X=train_X,
            train_Y=train_Y,
            train_Yvar=train_Yvar,
            covar_module=covar_module,
            outcome_transform=Standardize(m=1),
        )
        mll = ExactMarginalLogLikelihood(self.model.likelihood, self.model)
        fit_gpytorch_mll(mll)
        self.model.eval()
        self.model.likelihood.eval()
        self.log_marginal_likelihood_ = None

    def predict(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.model is None:
            raise RuntimeError("GP model must be fit before prediction")
        posterior = self.model.posterior(_to_torch_matrix(X))
        mean = posterior.mean.squeeze(-1).detach().cpu().numpy()
        variance = posterior.variance.squeeze(-1).clamp_min(1e-12)
        std = variance.sqrt().detach().cpu().numpy()
        return np.asarray(mean, dtype=float), np.asarray(std, dtype=float)


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
    "ordinal_categorical": PoolEntry(
        key="ordinal_categorical",
        display_name="Ordinal Categories + Normalized Continuous",
        description="Maps each categorical label to an arbitrary integer 1..N and keeps continuous variables normalized.",
        tags=_algorithm_profile(
            what_it_is="Single-scalar ordinal encoding for each categorical variable, even when category order has no semantic meaning.",
            best_for=["ablation baselines", "very compact encodings", "sanity checks against one-hot"],
            avoid_when=["distance between categories should be meaningful", "surrogate is sensitive to arbitrary geometry"],
            space_support="categorical + continuous",
            data_regime="best treated as a lightweight baseline rather than a chemistry-aware default",
            uncertainty_quality="depends strongly on downstream surrogate and can be misleading",
            cost="negligible",
            interpretability="medium",
            dependencies=[],
            implementation_status="native_phase1",
            fallback_to=None,
            fallback_trigger=None,
            selection_hints=[
                "Use as an ablation or stress-test baseline, not as the default categorical representation.",
                "Remember that label order is arbitrary unless your domain is truly ordinal.",
            ],
            chemistry_aware=False,
        ),
        factory=lambda search_space, params=None: OrdinalCategoricalEncoder(search_space, params),
    ),
    "fingerprint_concat": PoolEntry(
        key="fingerprint_concat",
        display_name="Morgan/ECFP Fingerprint + OHE",
        description="Uses Morgan/ECFP fingerprints for molecular categorical variables and one-hot elsewhere.",
        tags=_algorithm_profile(
            what_it_is="Morgan/ECFP fingerprints for SMILES-backed variables plus one-hot and normalized continuous values.",
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
    "physicochemical_descriptors": PoolEntry(
        key="physicochemical_descriptors",
        display_name="Physicochemical Descriptors",
        description="Uses RDKit descriptors for molecular variables, one-hot otherwise.",
        tags=_algorithm_profile(
            what_it_is="Descriptor vector using RDKit physicochemical, topological, ring, and simple 3D-proxy features.",
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
    "hybrid_descriptor": PoolEntry(
        key="hybrid_descriptor",
        display_name="Hybrid Molecular Descriptor",
        description="Combines chemistry-aware molecular encodings with general encodings for non-molecular variables.",
        tags=_algorithm_profile(
            what_it_is="Uses chemistry-aware encodings for molecular variables and lightweight encodings elsewhere.",
            best_for=["mixed chemical process spaces", "default chemistry-aware mixed search spaces"],
            avoid_when=["problem contains no molecular metadata", "all chemistry-aware dependencies are unavailable"],
            space_support="mixed molecular categorical + non-molecular categorical + continuous",
            data_regime="low-to-mid data",
            uncertainty_quality="depends on downstream surrogate",
            cost="low-to-medium",
            interpretability="medium",
            dependencies=["rdkit_optional"],
            implementation_status="conditional_phase1",
            fallback_to="fingerprint_concat",
            fallback_trigger="Descriptor path unavailable; use fingerprint or one-hot delegate.",
            selection_hints=[
                "Prefer when the search space mixes chemistry-bearing variables and operational settings.",
                "Good default if you want chemistry-aware sharing without committing to learned embeddings.",
            ],
            chemistry_aware=True,
        ),
        factory=lambda search_space, params=None: HybridDescriptorEncoder(search_space, params),
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
    model = entry.factory(params or {}, kernel_key, kernel_params or {})
    model.metadata.setdefault("selected_key", key)
    model.metadata.setdefault("resolved_key", entry.key)
    model.metadata.setdefault("resolved_kernel", kernel_key if entry.key == "gp" else None)
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
        if dependency in {"torch", "gpytorch", "botorch", "botorch_optional", "advanced_bo_optional"} and not capabilities["torch_stack"]:
            available = False
            missing.append(dependency)
        if dependency in {"network", "embedding_api"}:
            available = False
            missing.append(dependency)
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
