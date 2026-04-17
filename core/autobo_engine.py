"""
AutoBO Engine: adaptive surrogate selection with optional LLM-guided review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pools.component_pools import (
    BaseSurrogateModel,
    BoTorchGPSurrogate,
    candidate_to_key,
    create_acquisition,
    create_surrogate,
)


@dataclass
class SurrogateSpec:
    model_id: str
    surrogate_key: str
    kernel_key: str | None
    params: dict[str, Any] = field(default_factory=dict)
    display_name: str = ""
    kernel_params: dict[str, Any] = field(default_factory=dict)


DEFAULT_SURROGATE_SPECS: list[SurrogateSpec] = [
    SurrogateSpec("gp_matern52", "gp", "matern52", {}, "GP-Matern-5/2-ARD"),
    SurrogateSpec("gp_smk", "gp", "smk", {}, "GP-SMK-ARD", kernel_params={"num_mixtures1": 4, "num_mixtures2": 3}),
    SurrogateSpec("gp_matern32", "gp", "matern32", {}, "GP-Matern-3/2-ARD"),
    SurrogateSpec("rf", "rf", None, {"n_estimators": 100, "min_samples_leaf": 2}, "RF-Quantile"),
    SurrogateSpec(
        "bnn",
        "bnn",
        None,
        {"hidden_sizes": [64, 64], "n_epochs": 200, "mc_samples": 50},
        "BNN-VI",
    ),
    SurrogateSpec(
        "nn_dropout",
        "nn_dropout",
        None,
        {"hidden_sizes": [128, 128], "dropout_rate": 0.15, "n_epochs": 300, "mc_samples": 100},
        "NN-MCDropout",
    ),
]


def surrogate_specs_from_ids(model_ids: list[str] | None = None) -> list[SurrogateSpec]:
    if not model_ids:
        return list(DEFAULT_SURROGATE_SPECS)
    spec_map = {spec.model_id: spec for spec in DEFAULT_SURROGATE_SPECS}
    return [spec_map[model_id] for model_id in model_ids if model_id in spec_map]


class SurrogatePool:
    """Fit and query a pool of surrogate models while isolating failures."""

    def __init__(self, specs: list[SurrogateSpec] | None = None):
        self.specs = {spec.model_id: spec for spec in (specs or DEFAULT_SURROGATE_SPECS)}
        self.models: dict[str, BaseSurrogateModel] = {}
        self.fit_status: dict[str, bool] = {}
        self.fit_errors: dict[str, str] = {}

    def fit_all(self, X: np.ndarray, y: np.ndarray) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for model_id, spec in self.specs.items():
            try:
                model = create_surrogate(
                    spec.surrogate_key,
                    spec.params,
                    spec.kernel_key or "matern52",
                    spec.kernel_params,
                )
                model.fit(X, y)
                self.models[model_id] = model
                self.fit_status[model_id] = True
                self.fit_errors[model_id] = ""
                results[model_id] = {"success": True, "error": ""}
            except Exception as exc:  # pragma: no cover - best effort isolation
                self.fit_status[model_id] = False
                self.fit_errors[model_id] = f"{type(exc).__name__}: {exc}"
                results[model_id] = {"success": False, "error": self.fit_errors[model_id]}
        return results

    def predict(self, model_id: str, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        model = self.models.get(model_id)
        if model is None:
            raise RuntimeError(f"Model '{model_id}' is not fitted.")
        return model.predict(X)

    def predict_all(self, X: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        outputs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for model_id, ok in self.fit_status.items():
            if not ok:
                continue
            model = self.models.get(model_id)
            if model is None:
                continue
            try:
                outputs[model_id] = model.predict(X)
            except Exception:  # pragma: no cover
                continue
        return outputs

    def get_fitted_ids(self) -> list[str]:
        return [model_id for model_id, ok in self.fit_status.items() if ok]

    def get_active_model(self, active_id: str) -> BaseSurrogateModel | None:
        if self.fit_status.get(active_id):
            return self.models.get(active_id)
        return None


@dataclass
class FitnessScores:
    model_id: str
    f_seq: float = 0.0
    f_cal: float = 0.0
    f_rank: float = 0.0
    f_llm: float = 0.0
    composite: float = 0.0


class FitnessTracker:
    def __init__(
        self,
        weights: dict[str, float] | None = None,
        seq_start_n: int = 8,
        ci_level: float = 0.95,
    ):
        self.weights = dict(weights or {"seq": 0.35, "cal": 0.20, "rank": 0.15, "llm": 0.30})
        self.seq_start_n = max(0, int(seq_start_n))
        self.ci_level = float(ci_level)
        self.z_score = 1.96
        self.seq_log: dict[str, list[float]] = {}
        self.cal_log: dict[str, list[bool]] = {}
        self.latest_scores: dict[str, FitnessScores] = {}

    def compute_f_seq_incremental(
        self,
        model_id: str,
        surrogate_pool: SurrogatePool,
        encoder,
        observations: list[dict[str, Any]],
    ) -> float:
        if len(observations) <= self.seq_start_n:
            return 0.0
        model = surrogate_pool.get_active_model(model_id)
        if model is None:
            return -1e6
        X_obs, y_obs = _observations_to_arrays(observations, encoder)
        if X_obs.shape[0] <= self.seq_start_n:
            return 0.0
        mu, sigma = model.predict(X_obs)
        sigma_safe = np.maximum(np.asarray(sigma, dtype=float), 1e-6)
        residual = (np.asarray(y_obs, dtype=float) - np.asarray(mu, dtype=float)) / sigma_safe
        log_likelihood = -0.5 * np.log(2 * np.pi * sigma_safe**2) - 0.5 * residual**2
        value = float(np.mean(log_likelihood[self.seq_start_n :]))
        self.seq_log.setdefault(model_id, []).append(value)
        return value

    def compute_f_cal(
        self,
        model_id: str,
        surrogate_pool: SurrogatePool,
        encoder,
        observations: list[dict[str, Any]],
    ) -> float:
        if len(observations) <= self.seq_start_n:
            return 0.0
        model = surrogate_pool.get_active_model(model_id)
        if model is None:
            return -1.0
        X_obs, y_obs = _observations_to_arrays(observations, encoder)
        mu, sigma = model.predict(X_obs)
        sigma_safe = np.maximum(np.asarray(sigma, dtype=float), 1e-6)
        lower = np.asarray(mu, dtype=float) - self.z_score * sigma_safe
        upper = np.asarray(mu, dtype=float) + self.z_score * sigma_safe
        in_ci = (np.asarray(y_obs, dtype=float) >= lower) & (np.asarray(y_obs, dtype=float) <= upper)
        self.cal_log.setdefault(model_id, []).extend(bool(item) for item in in_ci.tolist())
        coverage = float(np.mean(in_ci)) if len(in_ci) else 0.0
        return -abs(coverage - self.ci_level)

    def compute_f_rank(
        self,
        model_id: str,
        surrogate_pool: SurrogatePool,
        encoder,
        observations: list[dict[str, Any]],
        direction: str = "maximize",
        top_n: int = 5,
    ) -> float:
        if len(observations) < top_n:
            return 0.0
        model = surrogate_pool.get_active_model(model_id)
        if model is None:
            return 0.0
        X_obs, y_obs = _observations_to_arrays(observations, encoder)
        y_values = np.asarray(y_obs, dtype=float)
        if direction == "minimize":
            top_indices = np.argsort(y_values)[:top_n]
        else:
            top_indices = np.argsort(y_values)[-top_n:][::-1]
        mu_top, _ = model.predict(X_obs[top_indices])
        return _safe_spearman(np.asarray(mu_top, dtype=float), y_values[top_indices])

    def compute_composite(
        self,
        fitted_ids: list[str],
        f_llm_scores: dict[str, float] | None = None,
        effective_llm_weight: float = 0.30,
    ) -> dict[str, FitnessScores]:
        if not fitted_ids:
            return {}
        f_llm_scores = f_llm_scores or {}
        raw: dict[str, dict[str, float]] = {}
        for model_id in fitted_ids:
            latest = self.latest_scores.get(model_id, FitnessScores(model_id))
            raw[model_id] = {
                "seq": latest.f_seq,
                "cal": latest.f_cal,
                "rank": latest.f_rank,
                "llm": float(f_llm_scores.get(model_id, 0.0)),
            }

        normalized: dict[str, dict[str, float]] = {model_id: {} for model_id in fitted_ids}
        for signal in ("seq", "cal", "rank", "llm"):
            values = np.asarray([raw[model_id][signal] for model_id in fitted_ids], dtype=float)
            mean_val = float(np.mean(values))
            std_val = float(np.std(values)) or 1.0
            for model_id in fitted_ids:
                normalized[model_id][signal] = (raw[model_id][signal] - mean_val) / std_val

        weights = dict(self.weights)
        if not f_llm_scores:
            llm_weight = float(weights.get("llm", 0.30))
            residual = max(1.0 - llm_weight, 1e-6)
            for signal in ("seq", "cal", "rank"):
                weights[signal] = float(weights.get(signal, 0.0)) / residual
            weights["llm"] = 0.0
        else:
            weights["llm"] = float(effective_llm_weight)
            total = sum(float(value) for value in weights.values()) or 1.0
            weights = {key: float(value) / total for key, value in weights.items()}

        result: dict[str, FitnessScores] = {}
        for model_id in fitted_ids:
            z_values = normalized[model_id]
            composite = (
                weights.get("seq", 0.0) * z_values["seq"]
                + weights.get("cal", 0.0) * z_values["cal"]
                + weights.get("rank", 0.0) * z_values["rank"]
                + weights.get("llm", 0.0) * z_values["llm"]
            )
            result[model_id] = FitnessScores(
                model_id=model_id,
                f_seq=raw[model_id]["seq"],
                f_cal=raw[model_id]["cal"],
                f_rank=raw[model_id]["rank"],
                f_llm=raw[model_id]["llm"],
                composite=float(composite),
            )
        self.latest_scores = result
        return result


class TriggerMonitor:
    def __init__(self, settings_dict: dict[str, Any]):
        self.layer2_min_interval = int(settings_dict.get("autobo_layer2_min_interval", 8))
        self.hysteresis_cooldown = int(settings_dict.get("autobo_hysteresis_cooldown", 3))
        self.switch_threshold = float(settings_dict.get("autobo_switch_threshold", 0.5))
        self.seq_lead_threshold = float(settings_dict.get("autobo_seq_lead_threshold", 1.5))
        self.cal_lower = float(settings_dict.get("autobo_cal_lower_bound", 0.70))
        self.cal_upper = float(settings_dict.get("autobo_cal_upper_bound", 0.99))
        self.stagnation_window = int(settings_dict.get("autobo_stagnation_window", 3))

    def check_layer1(
        self,
        active_model_id: str,
        fitness_tracker: FitnessTracker,
        iteration: int,
        last_layer2_iter: int,
        performance_log: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        active_scores = fitness_tracker.latest_scores.get(active_model_id)
        if active_scores is None:
            return False, "Active model has no scores"

        for model_id, challenger in fitness_tracker.latest_scores.items():
            if model_id == active_model_id:
                continue
            if challenger.f_seq - active_scores.f_seq > self.seq_lead_threshold:
                return True, f"Challenger {model_id} leads in F_seq"

        cal_log = fitness_tracker.cal_log.get(active_model_id, [])
        if len(cal_log) >= 10:
            coverage = float(np.mean(cal_log[-10:]))
            if coverage < self.cal_lower or coverage > self.cal_upper:
                return True, f"Active model coverage={coverage:.3f} out of range"

        if len(performance_log) >= self.stagnation_window:
            recent = performance_log[-self.stagnation_window :]
            if all(not bool(item.get("improved", False)) for item in recent):
                return True, f"No improvement in last {self.stagnation_window} iterations"

        if int(iteration) - int(last_layer2_iter) >= self.layer2_min_interval:
            return True, f"Periodic refresh interval={self.layer2_min_interval}"

        return False, ""

    def decide_switch(
        self,
        active_model_id: str,
        composite_scores: dict[str, FitnessScores],
        iteration: int,
        hysteresis_until: int,
    ) -> tuple[str, bool, str]:
        if int(iteration) < int(hysteresis_until):
            return active_model_id, False, "In hysteresis cooldown"
        if not composite_scores:
            return active_model_id, False, "No composite scores available"

        ranked = sorted(composite_scores.values(), key=lambda item: item.composite, reverse=True)
        top_candidate = ranked[0]
        active_score = composite_scores.get(active_model_id)
        if active_score is None:
            return top_candidate.model_id, True, f"Active model {active_model_id} has no score"
        if top_candidate.model_id == active_model_id:
            return active_model_id, False, "Active model remains top-ranked"
        gap = float(top_candidate.composite - active_score.composite)
        if gap > self.switch_threshold:
            return top_candidate.model_id, True, (
                f"Switching from {active_model_id} to {top_candidate.model_id} with gap={gap:.3f}"
            )
        return active_model_id, False, f"Top gap {gap:.3f} below threshold"


class AcquisitionFlow:
    def __init__(self, top_k: int = 8):
        self.top_k = max(1, int(top_k))

    def propose_candidates(
        self,
        active_model: BaseSurrogateModel,
        encoder,
        candidate_pool: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        direction: str = "maximize",
        seed: int = 0,
    ) -> list[dict[str, Any]]:
        if not candidate_pool:
            return []
        X_pool = encoder.encode_batch(candidate_pool)
        results = np.asarray([float(item["result"]) for item in observations if item.get("result") is not None], dtype=float)
        if direction == "minimize":
            y_model = -1.0 * results
        else:
            y_model = results
        y_mean = float(np.mean(y_model)) if len(y_model) else 0.0
        y_std = float(np.std(y_model)) or 1.0
        best_f_scaled = float(np.max((y_model - y_mean) / y_std)) if len(y_model) else 0.0

        try:
            pred_mean_scaled, pred_std_scaled = active_model.predict(X_pool)
        except Exception:
            rng = np.random.default_rng(seed)
            indices = list(rng.choice(len(candidate_pool), size=min(self.top_k, len(candidate_pool)), replace=False))
            return [
                {
                    "candidate": candidate_pool[index],
                    "predicted_value": None,
                    "uncertainty": None,
                    "acquisition_value": None,
                    "rank": rank + 1,
                }
                for rank, index in enumerate(indices)
            ]

        pred_mean_scaled = np.asarray(pred_mean_scaled, dtype=float)
        pred_std_scaled = np.maximum(np.asarray(pred_std_scaled, dtype=float), 1e-6)

        if isinstance(active_model, BoTorchGPSurrogate) and active_model.model is not None:
            try:
                acquisition = create_acquisition("qlog_ei", {})
                acq_values = acquisition.score(active_model, X_pool, best_f_scaled, np.random.default_rng(seed))
            except Exception:
                acq_values = _analytic_ei(pred_mean_scaled, pred_std_scaled, best_f_scaled)
        else:
            acq_values = _analytic_ei(pred_mean_scaled, pred_std_scaled, best_f_scaled)

        pred_mean = pred_mean_scaled * y_std + y_mean
        pred_std = np.maximum(pred_std_scaled * y_std, 1e-6)
        if direction == "minimize":
            pred_mean = -1.0 * pred_mean

        top_indices = np.argsort(np.asarray(acq_values, dtype=float))[::-1][: self.top_k]
        shortlist = []
        for rank, index in enumerate(top_indices):
            shortlist.append(
                {
                    "candidate": dict(candidate_pool[int(index)]),
                    "predicted_value": float(pred_mean[int(index)]),
                    "uncertainty": float(pred_std[int(index)]),
                    "acquisition_value": float(acq_values[int(index)]),
                    "rank": rank + 1,
                }
            )
        return shortlist


class ReverseCalibrator:
    def __init__(self, window_size: int = 15, degrade_threshold: float = 0.2):
        self.window_size = int(window_size)
        self.degrade_threshold = float(degrade_threshold)
        self.acq_records: list[dict[str, Any]] = []
        self.plaus_records: list[dict[str, Any]] = []

    def record_acquisition_choice(
        self,
        llm_selected_rank: int,
        qlogei_top1_candidate: dict[str, Any],
        llm_selected_candidate: dict[str, Any],
        observed_y: float | None = None,
    ) -> None:
        self.acq_records.append(
            {
                "llm_rank": int(llm_selected_rank),
                "observed_y": observed_y,
                "candidate_key": candidate_to_key(llm_selected_candidate or {}),
                "top1_candidate_key": candidate_to_key(qlogei_top1_candidate or {}),
            }
        )

    def record_plausibility_eval(
        self,
        model_id: str,
        point_candidate: dict[str, Any],
        llm_score: float,
        observed_y: float | None = None,
        predicted_mu: float | None = None,
    ) -> None:
        self.plaus_records.append(
            {
                "model_id": model_id,
                "candidate_key": candidate_to_key(point_candidate or {}),
                "llm_score": float(llm_score),
                "observed_y": observed_y,
                "predicted_mu": predicted_mu,
            }
        )

    def should_degrade_llm_weight(self) -> tuple[bool, float, str]:
        recent_acq = [item for item in self.acq_records[-self.window_size :] if item.get("observed_y") is not None]
        if len(recent_acq) >= self.window_size:
            if all(int(item.get("llm_rank", 1)) != 1 for item in recent_acq[-5:]):
                return True, 0.10, "LLM acquisition selection has not added value recently"

        recent_plaus = [
            item
            for item in self.plaus_records[-20:]
            if item.get("observed_y") is not None and item.get("predicted_mu") is not None
        ]
        if len(recent_plaus) >= 10:
            llm_scores = np.asarray([float(item.get("llm_score", 0.0)) for item in recent_plaus], dtype=float)
            actual_errors = np.asarray(
                [abs(float(item.get("observed_y", 0.0)) - float(item.get("predicted_mu", 0.0))) for item in recent_plaus],
                dtype=float,
            )
            rho = _safe_spearman(llm_scores, -1.0 * actual_errors)
            if np.isfinite(rho) and rho < self.degrade_threshold:
                return True, 0.10, f"LLM plausibility correlation={rho:.3f} below threshold"

        return False, 0.30, "LLM signal appears calibrated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "acq_records": self.acq_records[-50:],
            "plaus_records": self.plaus_records[-50:],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReverseCalibrator":
        instance = cls()
        instance.acq_records = list(data.get("acq_records", []))
        instance.plaus_records = list(data.get("plaus_records", []))
        return instance


def _analytic_ei(mu: np.ndarray, sigma: np.ndarray, best_f: float) -> np.ndarray:
    from scipy.stats import norm

    mu = np.asarray(mu, dtype=float)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-8)
    z = (mu - float(best_f)) / sigma
    ei = sigma * (z * norm.cdf(z) + norm.pdf(z))
    return np.maximum(ei, 0.0)


def _observations_to_arrays(
    observations: list[dict[str, Any]],
    encoder,
) -> tuple[np.ndarray, np.ndarray]:
    candidates = [item.get("candidate", {}) for item in observations if item.get("result") is not None]
    y_values = np.asarray([float(item["result"]) for item in observations if item.get("result") is not None], dtype=float)
    return encoder.encode_batch(candidates), y_values


def _safe_spearman(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) <= 1 or len(right) <= 1:
        return 0.0
    try:
        from scipy.stats import spearmanr

        rho, _ = spearmanr(left, right)
        return float(rho) if np.isfinite(rho) else 0.0
    except Exception:
        return 0.0
