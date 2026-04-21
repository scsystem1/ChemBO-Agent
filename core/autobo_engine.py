"""
AutoBO Engine: adaptive surrogate selection with optional LLM-guided review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from langchain_core.messages import AIMessage

from core.autobo_prompts import (
    build_acquisition_selection_prompt,
    build_surrogate_plausibility_prompt,
)
from core.context_builder import ContextBuilder
from core.dataset_oracle import DatasetOracle
from knowledge.knowledge_state import knowledge_mode_for_node, score_candidate_with_priors
from memory.memory_manager import MemoryManager
from pools.component_pools import (
    BaseSurrogateModel,
    BoTorchGPSurrogate,
    candidate_to_key,
    create_acquisition,
    create_encoder,
    create_surrogate,
    detect_runtime_capabilities,
)
from tools.chembo_tools import (
    build_candidate_pool as build_bo_candidate_pool,
    build_diverse_fallback_candidates,
    build_shortlist_from_candidates as build_bo_shortlist_from_candidates,
    dataset_candidate_pool_from_spec,
    dedupe_observations,
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


@dataclass
class LOOCVResult:
    model_id: str
    mu: np.ndarray
    sigma: np.ndarray
    y_true: np.ndarray


def get_eligible_surrogate_specs(
    all_specs: list[SurrogateSpec],
    n_obs: int,
    settings,
) -> list[SurrogateSpec]:
    return [spec for spec in all_specs if _surrogate_min_observations(spec, settings) <= int(n_obs)]


def _surrogate_min_observations(spec: SurrogateSpec, settings) -> int:
    if spec.model_id in {"bnn", "nn_dropout"}:
        return max(1, int(getattr(settings, "autobo_nn_min_obs", 30) or 30))
    return 8


def _gated_out_surrogate_reasons(
    all_specs: list[SurrogateSpec],
    n_obs: int,
    settings,
) -> dict[str, str]:
    gated: dict[str, str] = {}
    for spec in all_specs:
        min_obs = _surrogate_min_observations(spec, settings)
        if int(n_obs) < min_obs:
            gated[spec.model_id] = f"requires >= {min_obs} observations"
    return gated


def _create_surrogate_from_spec(spec: SurrogateSpec) -> BaseSurrogateModel:
    return create_surrogate(
        spec.surrogate_key,
        dict(spec.params),
        spec.kernel_key or "matern52",
        dict(spec.kernel_params),
    )


def bootstrap_autobo_state(
    *,
    state: dict[str, Any],
    problem_spec: dict[str, Any],
    settings,
    proposal_strategy: str,
) -> dict[str, Any]:
    embedding_payload = resolve_autobo_embedding(problem_spec, settings)
    autobo_state = _resolve_autobo_state(state.get("autobo_state", {}), settings)
    active_model_id = str(autobo_state.get("active_model") or getattr(settings, "autobo_initial_active", "gp_matern52"))
    bo_config = {
        "surrogate_model": "autobo_pool",
        "surrogate_params": {},
        "kernel_config": {
            "key": "autobo_adaptive",
            "params": {},
            "rationale": "Managed by the AutoBO surrogate controller.",
        },
        "acquisition_function": "qlog_ei",
        "af_params": {},
        "rationale": "AutoBO adaptive surrogate pool with qLogEI acquisition.",
        "confidence": 1.0,
        "config_version": len(state.get("config_history", [])) + 1,
        "validated": True,
        "selection_source": "autobo",
        "selection_diagnostics": {},
        "autobo_active_model": active_model_id,
    }
    effective_config = dict(state.get("effective_config", {}))
    effective_config.update(
        {
            "runtime_mode": detect_runtime_capabilities()["runtime_mode"],
            "proposal_strategy": proposal_strategy,
            "embedding_method": embedding_payload["embedding_config"]["method"],
            "requested_embedding_method": embedding_payload["embedding_config"]["requested_method"],
            "embedding_notes": embedding_payload["embedding_config"].get("encoder_notes", []),
            "embedding_fallback_reason": embedding_payload["embedding_config"].get("fallback_reason"),
            "surrogate_model": "autobo_pool",
            "kernel_config": bo_config["kernel_config"],
            "acquisition_function": "qlog_ei",
            "selection_source": "autobo",
            "autobo_active_model": active_model_id,
        }
    )
    message = AIMessage(
        content=(
            "Bootstrapped AutoBO runtime: "
            f"embedding={embedding_payload['embedding_config']['requested_method']}->"
            f"{embedding_payload['embedding_config']['method']} active={active_model_id}"
        )
    )
    return {
        "messages": [message],
        "embedding_config": embedding_payload["embedding_config"],
        "embedding_locked": True,
        "embedding_history": [
            {
                "configured_at_iteration": 0,
                "effective_from_iteration": 1,
                "mode": "initial",
                "embedding_config": embedding_payload["embedding_config"],
            }
        ],
        "bo_config": bo_config,
        "config_history": list(state.get("config_history", [])) + [bo_config],
        "effective_config": effective_config,
        "autobo_state": {**autobo_state, "active_model": active_model_id},
        "log_lines": [
            f"[autobo_bootstrap] embedding={embedding_payload['embedding_config']['requested_method']}->"
            f"{embedding_payload['embedding_config']['method']} active={active_model_id}"
        ],
    }


def resolve_autobo_embedding(problem_spec: dict[str, Any], settings) -> dict[str, Any]:
    search_space = problem_spec.get("variables", [])
    requested_method = str(
        getattr(settings, "fixed_embedding_method", "physical_features") or "physical_features"
    ).strip().lower()
    encoder_key = requested_method
    encoder = create_encoder(encoder_key, search_space, {})
    resolved_method = str(encoder.metadata.get("resolved_key") or encoder_key)
    fallback_reason = _embedding_fallback_reason(requested_method, resolved_method, encoder.metadata.get("notes", []))
    embedding_config = {
        "method": resolved_method,
        "resolved_method": resolved_method,
        "requested_method": requested_method,
        "resolver_key": encoder_key,
        "params": {},
        "rationale": "Rule-based AutoBO bootstrap with descriptor-first embedding resolution.",
        "dim": encoder.dim,
        "confidence": 1.0,
        "metadata": dict(encoder.metadata),
        "encoder_notes": list(encoder.metadata.get("notes", [])),
        "fallback_reason": fallback_reason,
    }
    return {"encoder": encoder, "embedding_config": embedding_config}


def _annotate_shortlist_with_knowledge(
    *,
    state: dict[str, Any],
    shortlist: list[dict[str, Any]],
    node_name: str,
) -> tuple[list[dict[str, Any]], str]:
    knowledge_state = state.get("knowledge_state", {}) if isinstance(state.get("knowledge_state"), dict) else {}
    coverage_report = knowledge_state.get("coverage_report", {}) if isinstance(knowledge_state.get("coverage_report"), dict) else {}
    served_priors = knowledge_state.get("served_priors", []) if isinstance(knowledge_state.get("served_priors"), list) else []
    knowledge_mode = knowledge_mode_for_node(coverage_report, served_priors, node_name=node_name)
    annotated: list[dict[str, Any]] = []
    for item in shortlist:
        candidate = dict(item.get("candidate", {}))
        prior_signal = score_candidate_with_priors(candidate, served_priors, node_name=node_name)
        raw_total = float((prior_signal.get("knowledge_score_breakdown", {}) or {}).get("total", 0.0) or 0.0)
        if knowledge_mode == "knowledge_guided":
            effective_total = raw_total
        elif knowledge_mode == "coverage_first":
            effective_total = min(raw_total, 0.0)
        else:
            effective_total = 0.0
        acquisition_value = _coerce_float(item.get("acquisition_value"), default=None)
        knowledge_adjusted_score = effective_total if acquisition_value is None else float(acquisition_value) + 0.1 * effective_total
        annotated.append(
            {
                **item,
                "applied_prior_ids": list(prior_signal.get("applied_prior_ids", [])),
                "knowledge_score_breakdown": dict(prior_signal.get("knowledge_score_breakdown", {})),
                "knowledge_mode": knowledge_mode,
                "knowledge_adjusted_score": round(float(knowledge_adjusted_score), 6),
            }
        )
    if knowledge_mode in {"knowledge_guided", "coverage_first"}:
        ranked = sorted(
            enumerate(annotated),
            key=lambda item: (
                -1.0 * float(item[1].get("knowledge_adjusted_score", 0.0) or 0.0),
                -1.0 * float(_coerce_float(item[1].get("acquisition_value"), default=0.0) or 0.0),
                int(item[1].get("autobo_rank", 9999) or 9999),
            )
        )
        for rank, (index, _) in enumerate(ranked, start=1):
            annotated[index]["knowledge_rank"] = rank
    else:
        for index, item in enumerate(annotated, start=1):
            item["knowledge_rank"] = index
    return annotated, knowledge_mode


def run_autobo_iteration(
    *,
    state: dict[str, Any],
    settings,
    llm,
    invoke_json_node,
) -> dict[str, Any]:
    autobo_state = _resolve_autobo_state(state.get("autobo_state", {}), settings)
    observations = list(state.get("observations", []))
    variables = state.get("problem_spec", {}).get("variables", [])
    direction = state.get("optimization_direction", "maximize")
    embedding_config = state.get("embedding_config", {})
    active_model_id = str(autobo_state.get("active_model") or getattr(settings, "autobo_initial_active", "gp_matern52"))
    shortlist_limit = max(
        int(getattr(settings, "autobo_acq_top_k", 8) or 8),
        int(getattr(settings, "shortlist_top_k", 5) or 5),
        int(getattr(settings, "batch_size", 1) or 1),
    )
    encoder = create_encoder(
        embedding_config.get("resolver_key") or embedding_config.get("requested_method") or embedding_config.get("method", "one_hot"),
        variables,
        embedding_config.get("params", {}),
    )
    deduped = dedupe_observations(observations)
    observed_keys = {
        candidate_to_key(item.get("candidate", {}))
        for item in deduped
        if item.get("candidate")
    }
    dataset_spec = state.get("problem_spec", {}).get("dataset", {})
    dataset_candidate_pool = dataset_candidate_pool_from_spec(dataset_spec)
    candidate_pool = build_bo_candidate_pool(
        variables,
        observed_keys=observed_keys,
        candidate_pool_size=max(256, shortlist_limit * 32),
        seed=int(state.get("iteration", 0)),
        hard_constraints=[],
        candidate_pool=dataset_candidate_pool,
    )
    if not candidate_pool:
        candidate_pool = build_diverse_fallback_candidates(
            variables,
            n_total=shortlist_limit,
            seed=int(state.get("iteration", 0)),
            hard_constraints=[],
            observed_keys=observed_keys,
            candidate_pool=dataset_candidate_pool,
        )

    if not deduped:
        fallback_shortlist = build_bo_shortlist_from_candidates(candidate_pool[:shortlist_limit], [])
        for index, item in enumerate(fallback_shortlist):
            item["autobo_rank"] = index + 1
        fallback_shortlist, knowledge_mode = _annotate_shortlist_with_knowledge(
            state=state,
            shortlist=fallback_shortlist,
            node_name="run_bo_iteration",
        )
        resolved_components = {
            "embedding_method": embedding_config.get("method"),
            "surrogate_model": active_model_id,
            "kernel_config": {"key": "autobo_adaptive"},
            "acquisition_function": "qlog_ei",
        }
        payload = {
            "status": "warm_start_fallback",
            "strategy": "autobo_adaptive",
            "shortlist": fallback_shortlist,
            "recommended_index": 0,
            "candidates": [item["candidate"] for item in fallback_shortlist],
            "predictions": [item["predicted_value"] for item in fallback_shortlist],
            "uncertainties": [item["uncertainty"] for item in fallback_shortlist],
            "acquisition_values": [item["acquisition_value"] for item in fallback_shortlist],
            "resolved_components": resolved_components,
            "metadata": {
                "proposal_strategy": "autobo_adaptive",
                "active_model": active_model_id,
                "fit_results": {},
                "trigger_reason": "no_observations",
                "switch_info": {"switched": False, "reason": "No observations available"},
                "candidate_pool_source": "dataset" if dataset_candidate_pool is not None else "search_space",
                "knowledge_mode": knowledge_mode,
            },
        }
        return {
            "messages": [
                AIMessage(
                    content=(
                        "AutoBO fallback: no observations available, using a deterministic shortlist "
                        f"({knowledge_mode})."
                    )
                )
            ],
            "proposal_shortlist": fallback_shortlist,
            "payload": payload,
            "effective_config": _effective_config_with_components(
                state,
                active_model_id=active_model_id,
                resolved_components=resolved_components,
                switch_info={"switched": False, "reason": "No observations available"},
                trigger_reason="no_observations",
            ),
            "bo_config": _bo_config_with_active_model(state.get("bo_config", {}), active_model_id),
            "autobo_state": autobo_state,
            "llm_usage": _empty_usage_delta(),
            "log_lines": [f"[run_bo_iteration] autobo active={active_model_id} switched=False shortlist={len(fallback_shortlist)}"],
        }

    y_obs = np.asarray([float(item["result"]) for item in deduped], dtype=float)
    X_obs = encoder.encode_batch([item.get("candidate", {}) for item in deduped])
    y_model = y_obs if direction != "minimize" else -1.0 * y_obs
    y_mean = float(np.mean(y_model))
    y_std = float(np.std(y_model)) or 1.0
    y_scaled = (y_model - y_mean) / y_std
    scored_observations = [{**item, "result": float(y_scaled[index])} for index, item in enumerate(deduped)]

    all_specs = surrogate_specs_from_ids(list(getattr(settings, "autobo_surrogate_pool", [])))
    spec_lookup = {spec.model_id: spec for spec in all_specs}
    eligible_specs = get_eligible_surrogate_specs(all_specs, len(deduped), settings)
    gated_out_models = _gated_out_surrogate_reasons(all_specs, len(deduped), settings)
    pool = SurrogatePool(eligible_specs)
    fit_results = pool.fit_all(X_obs, y_scaled)
    for model_id, reason in gated_out_models.items():
        fit_results.setdefault(
            model_id,
            {"success": False, "error": reason, "stage": "eligibility_gate"},
        )

    tracker = FitnessTracker(
        weights=dict(getattr(settings, "autobo_fitness_weights", {})),
        seq_start_n=min(int(getattr(settings, "autobo_seq_start_n", 8)), max(len(scored_observations) - 1, 0)),
        ci_level=float(getattr(settings, "autobo_cal_ci_level", 0.95)),
    )
    fitted_ids: list[str] = []
    for spec in eligible_specs:
        if not pool.fit_status.get(spec.model_id):
            continue
        try:
            tracker.latest_scores[spec.model_id] = tracker.compute_loocv_metrics(
                spec.model_id,
                spec,
                encoder,
                scored_observations,
                direction="maximize",
            )
            fitted_ids.append(spec.model_id)
        except Exception as exc:
            pool.fit_status[spec.model_id] = False
            pool.fit_errors[spec.model_id] = f"{type(exc).__name__}: {exc}"
            fit_results[spec.model_id] = {
                "success": False,
                "error": pool.fit_errors[spec.model_id],
                "stage": "loocv",
            }

    trigger_monitor = TriggerMonitor(vars(settings))
    should_trigger = False
    trigger_reason = "no_eligible_surrogate_for_switch"
    llm_usage = _empty_usage_delta()
    llm_scores: dict[str, float] = {}
    llm_plausibility_audits: list[dict[str, Any]] = []
    composite: dict[str, FitnessScores] = {}
    switched = False
    switch_info = {
        "switched": False,
        "from": autobo_state.get("active_model"),
        "to": active_model_id,
        "reason": "No eligible surrogate available for switching.",
    }
    if fitted_ids:
        should_trigger, trigger_reason = trigger_monitor.check_layer1(
            active_model_id=active_model_id,
            fitness_tracker=tracker,
            iteration=int(state.get("iteration", 0)),
            last_layer2_iter=int(autobo_state.get("last_layer2_iteration", 0)),
            performance_log=state.get("performance_log", []),
        )
        if should_trigger and bool(getattr(settings, "autobo_llm_plaus_enabled", True)):
            llm_scores, llm_plausibility_audits, llm_usage = _run_llm_plausibility_eval(
                state=state,
                pool=pool,
                encoder=encoder,
                observations=deduped,
                fitted_ids=fitted_ids,
                llm=llm,
                settings=settings,
                invoke_json_node=invoke_json_node,
            )

        composite = tracker.compute_composite(
            fitted_ids=fitted_ids,
            f_llm_scores=llm_scores,
            effective_llm_weight=float(autobo_state.get("effective_llm_weight", 0.30)),
        )
        active_model_id, switched, switch_reason = trigger_monitor.decide_switch(
            active_model_id,
            composite,
            int(state.get("iteration", 0)),
            int(autobo_state.get("hysteresis_until", 0)),
        )
        switch_info = {
            "switched": bool(switched),
            "from": autobo_state.get("active_model"),
            "to": active_model_id,
            "reason": switch_reason if switched else (trigger_reason or switch_reason or "No switch"),
        }

    shortlist_only_model_id: str | None = None
    active_model = pool.get_active_model(active_model_id)
    if active_model is None and fitted_ids:
        active_model_id = fitted_ids[0]
        active_model = pool.get_active_model(active_model_id)
        switch_info = {
            "switched": True,
            "from": autobo_state.get("active_model"),
            "to": active_model_id,
            "reason": "Fell back to the first successfully fitted surrogate.",
        }
        switched = True
    elif active_model is None:
        active_spec = spec_lookup.get(active_model_id)
        if active_spec is not None:
            try:
                active_model = _create_surrogate_from_spec(active_spec)
                active_model.fit(X_obs, y_scaled)
                shortlist_only_model_id = active_model_id
                fit_results[active_model_id] = {
                    "success": True,
                    "error": "",
                    "stage": "shortlist_only",
                }
                switch_info = {
                    "switched": False,
                    "from": autobo_state.get("active_model"),
                    "to": active_model_id,
                    "reason": "Active surrogate used only to generate a shortlist; no switch comparison was run.",
                }
            except Exception as exc:
                fit_results[active_model_id] = {
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "stage": "shortlist_only",
                }

    shortlist_raw = []
    acquisition_flow = AcquisitionFlow(
        top_k=shortlist_limit,
        prefilter_multiplier=int(getattr(settings, "autobo_shortlist_prefilter_multiplier", 10) or 10),
        hallucination_mode=str(getattr(settings, "autobo_shortlist_hallucination_mode", "kriging_believer")),
    )
    if active_model is not None:
        active_spec = spec_lookup.get(active_model_id)
        refit_model_factory = None
        if active_spec is not None:
            refit_model_factory = lambda spec=active_spec: _create_surrogate_from_spec(spec)
        shortlist_raw = acquisition_flow.propose_candidates(
            active_model=active_model,
            refit_model_factory=refit_model_factory,
            encoder=encoder,
            candidate_pool=candidate_pool,
            observations=deduped,
            direction=direction,
            seed=int(state.get("iteration", 0)),
        )

    if shortlist_raw:
        shortlist = [
            {
                "candidate": item["candidate"],
                "predicted_value": item["predicted_value"],
                "uncertainty": item["uncertainty"],
                "acquisition_value": item["acquisition_value"],
                "acquisition_value_raw": item.get("acquisition_value_raw"),
                "selection_step": item.get("selection_step"),
                "selection_mode": item.get("selection_mode"),
                "constraint_violations": [],
                "constraint_satisfied": True,
                "autobo_rank": item["rank"],
            }
            for item in shortlist_raw
        ]
        status = "shortlist_only_fallback" if shortlist_only_model_id else "success"
    else:
        shortlist = build_bo_shortlist_from_candidates(candidate_pool[:shortlist_limit], [])
        for index, item in enumerate(shortlist):
            item["autobo_rank"] = index + 1
        status = "fallback"
    shortlist, knowledge_mode = _annotate_shortlist_with_knowledge(
        state=state,
        shortlist=shortlist,
        node_name="run_bo_iteration",
    )

    calibration_entry = {
        "iteration": int(state.get("iteration", 0)),
        "active_model": active_model_id,
        "coverage": {
            model_id: _recent_calibration_coverage(tracker.cal_log.get(model_id, []))
            for model_id in fitted_ids
        },
        "trigger_reason": trigger_reason if (should_trigger or not fitted_ids) else "no_trigger",
    }
    fitness_entry = {
        model_id: {
            "f_seq": score.f_seq,
            "f_cal": score.f_cal,
            "f_rank": score.f_rank,
            "f_llm": score.f_llm,
            "composite": score.composite,
        }
        for model_id, score in composite.items()
    }
    fitness_log = dict(autobo_state.get("fitness_log", {}))
    fitness_log[str(int(state.get("iteration", 0)))] = fitness_entry
    switch_history = list(autobo_state.get("switch_history", []))
    if switched:
        switch_history.append(
            {
                "iteration": int(state.get("iteration", 0)),
                **switch_info,
                "scores": {model_id: score.composite for model_id, score in composite.items()},
            }
        )

    resolved_components = {
        "embedding_method": embedding_config.get("method"),
        "surrogate_model": active_model_id,
        "kernel_config": {"key": _autobo_kernel_key(active_model_id)},
        "acquisition_function": "qlog_ei",
    }
    payload = {
        "status": status,
        "strategy": "autobo_adaptive",
        "shortlist": shortlist,
        "recommended_index": 0,
        "candidates": [item["candidate"] for item in shortlist],
        "predictions": [item["predicted_value"] for item in shortlist],
        "uncertainties": [item["uncertainty"] for item in shortlist],
        "acquisition_values": [item["acquisition_value"] for item in shortlist],
        "resolved_components": resolved_components,
        "metadata": {
            "proposal_strategy": "autobo_adaptive",
            "active_model": active_model_id,
            "fit_results": fit_results,
            "trigger_reason": trigger_reason if (should_trigger or not fitted_ids) else "no_trigger",
            "switch_info": switch_info,
            "candidate_pool_source": "dataset" if dataset_candidate_pool is not None else "search_space",
            "knowledge_mode": knowledge_mode,
            "shortlist_prefilter_size": acquisition_flow.last_prefilter_size,
            "shortlist_hallucination_mode": acquisition_flow.hallucination_mode,
            "gated_out_models": gated_out_models,
            "shortlist_only_model": shortlist_only_model_id,
        },
    }
    next_autobo_state = {
        **autobo_state,
        "active_model": active_model_id,
        "fitness_log": _trim_autobo_mapping(fitness_log, limit=50),
        "calibration_log": _trim_autobo_list(list(autobo_state.get("calibration_log", [])) + [calibration_entry], limit=50),
        "switch_history": _trim_autobo_list(switch_history, limit=50),
        "last_layer2_iteration": int(state.get("iteration", 0)) if should_trigger else int(autobo_state.get("last_layer2_iteration", 0)),
        "hysteresis_until": (
            int(state.get("iteration", 0)) + int(getattr(settings, "autobo_hysteresis_cooldown", 3))
            if switched
            else int(autobo_state.get("hysteresis_until", 0))
        ),
        "llm_plaus_audit": _trim_autobo_list(
            list(autobo_state.get("llm_plaus_audit", [])) + llm_plausibility_audits,
            limit=50,
        ),
    }
    message = AIMessage(
        content=(
            f"AutoBO iter={state.get('iteration', 0)} active={active_model_id} "
            f"fitted={len(fitted_ids)} shortlist={len(shortlist)} knowledge_mode={knowledge_mode} {switch_info['reason']}"
        )
    )
    return {
        "messages": [message],
        "proposal_shortlist": shortlist,
        "payload": payload,
        "effective_config": _effective_config_with_components(
            state,
            active_model_id=active_model_id,
            resolved_components=resolved_components,
            switch_info=switch_info,
            trigger_reason=trigger_reason,
        ),
        "bo_config": _bo_config_with_active_model(state.get("bo_config", {}), active_model_id),
        "autobo_state": next_autobo_state,
        "llm_usage": llm_usage,
        "log_lines": [f"[run_bo_iteration] autobo active={active_model_id} switched={switched} shortlist={len(shortlist)}"],
    }


def select_autobo_candidate(
    *,
    state: dict[str, Any],
    settings,
    llm,
    invoke_json_node,
) -> dict[str, Any]:
    shortlist = list(state.get("proposal_shortlist", []))
    if not shortlist:
        return {
            "messages": [AIMessage(content="AutoBO shortlist is empty; no candidate could be selected.")],
            "proposal_selected": {
                "selected_index": 0,
                "override": False,
                "candidate": {},
                "rationale": {
                    "chemical_reasoning": "AutoBO shortlist was empty.",
                    "comparison_to_top1": "",
                    "selection_mode": "top1_follow",
                    "hypothesis_alignment": "",
                    "information_value": "",
                    "concerns": "",
                },
                "confidence": 0.0,
                "selection_source": "autobo_empty_shortlist",
                "selected_rank": 0,
                "top1_candidate": {},
                "applied_prior_ids": [],
                "knowledge_score_breakdown": {},
                "knowledge_mode": "knowledge_gap",
            },
            "current_proposal": {"candidates": [{}], "selected_index": 0},
            "llm_usage": _empty_usage_delta(),
            "log_lines": ["[select_candidate] autobo shortlist empty"],
        }

    if not bool(getattr(settings, "autobo_llm_acq_enabled", True)):
        selected_record = shortlist[0]
        candidate = selected_record.get("candidate", {})
        return {
            "messages": [AIMessage(content="AutoBO LLM acquisition disabled; using shortlist rank-1 raw acquisition candidate.")],
            "proposal_selected": {
                "selected_index": 0,
                "override": False,
                "candidate": candidate,
                "rationale": {
                    "chemical_reasoning": "Selected the highest-ranked AutoBO shortlist candidate.",
                    "comparison_to_top1": "Candidate #1 is accepted as the best current choice.",
                    "selection_mode": "top1_follow",
                    "hypothesis_alignment": "",
                    "information_value": "",
                    "concerns": "",
                },
                "confidence": 1.0,
                "selection_source": "autobo_top1",
                "autobo_qlogei_rank": 1,
                "selected_rank": 1,
                "top1_candidate": dict(shortlist[0].get("candidate", {})),
                "applied_prior_ids": list(selected_record.get("applied_prior_ids", [])),
                "knowledge_score_breakdown": dict(selected_record.get("knowledge_score_breakdown", {})),
                "knowledge_mode": str(selected_record.get("knowledge_mode") or ""),
            },
            "current_proposal": {
                "candidates": [candidate],
                "selected_index": 0,
                "applied_prior_ids": list(selected_record.get("applied_prior_ids", [])),
                "knowledge_score_breakdown": dict(selected_record.get("knowledge_score_breakdown", {})),
                "knowledge_mode": str(selected_record.get("knowledge_mode") or ""),
            },
            "llm_usage": _empty_usage_delta(),
            "log_lines": ["[select_candidate] autobo top1 fallback"],
        }

    memory_manager = MemoryManager.from_dict(state.get("memory", {}))
    context = ContextBuilder.for_autobo_acquisition_select(state, memory_manager)
    prompt = build_acquisition_selection_prompt(
        reaction_context=context.get("reaction_context", {}),
        top_observations=context.get("top_observations", []),
        bottom_observations=context.get("bottom_observations", []),
        candidates=[
            {
                "id": index + 1,
                "candidate": item.get("candidate", {}),
                "predicted_value": item.get("predicted_value"),
                "uncertainty": item.get("uncertainty"),
                "acquisition_value": item.get("acquisition_value"),
                "acquisition_value_raw": item.get("acquisition_value_raw"),
                "selection_step": item.get("selection_step"),
                "selection_mode": item.get("selection_mode"),
                "applied_prior_ids": item.get("applied_prior_ids", []),
                "knowledge_score_breakdown": item.get("knowledge_score_breakdown", {}),
                "knowledge_mode": item.get("knowledge_mode", ""),
            }
            for index, item in enumerate(shortlist[: int(getattr(settings, "autobo_acq_top_k", 8) or 8)])
        ],
        total_observations=int(context.get("total_observations", 0)),
        knowledge_cards=context.get("knowledge_guidance", []),
        memory_rules=context.get("memory_rules", []),
        active_hypotheses=context.get("active_hypotheses", []),
    )
    default = {
        "selected_id": 1,
        "reasoning": "Default to qLogEI top-1.",
        "comparison_to_top1": "Candidate #1 is accepted as the best current choice.",
        "selection_mode": "top1_follow",
    }
    parsed, messages, llm_usage = invoke_json_node(
        llm,
        state,
        prompt,
        default,
        node_name="select_candidate",
    )
    selected_id = _coerce_int(parsed.get("selected_id"), default=1)
    chosen_index = min(max(selected_id - 1, 0), len(shortlist) - 1)
    selected_record = shortlist[chosen_index]
    candidate = selected_record.get("candidate", {})
    outbound_messages = list(messages)
    raw_comparison_to_top1 = str(parsed.get("comparison_to_top1") or "")
    comparison_to_top1 = raw_comparison_to_top1 or default["comparison_to_top1"]
    selection_mode = str(parsed.get("selection_mode") or default["selection_mode"])
    if selected_id != 1 and len(raw_comparison_to_top1.strip()) < 20:
        comparison_to_top1 = (
            f"The LLM overrode shortlist top-1 and chose candidate #{selected_id}. "
            "Provide a more explicit comparison in future runs."
        )
    if selected_id != 1 and selection_mode == "top1_follow":
        selection_mode = "non_top1_override"

    oracle = DatasetOracle.from_problem_spec(state.get("problem_spec", {}))
    if oracle is not None:
        if oracle.candidate_exists(candidate):
            candidate = oracle.lookup(candidate)["candidate"]
            selected_record = dict(selected_record)
            selected_record["candidate"] = candidate
        else:
            fallback_selection = _first_dataset_backed_shortlist_record(shortlist, oracle, preferred_index=chosen_index)
            if fallback_selection is not None:
                chosen_index, selected_record = fallback_selection
                candidate = selected_record.get("candidate", {})
                outbound_messages.append(
                    AIMessage(
                        content=(
                            f"Replaced invalid AutoBO selection rank {selected_id} with dataset-backed shortlist index {chosen_index}."
                        )
                    )
                )

    proposal_selected = {
        "selected_index": chosen_index,
        "override": False,
        "candidate": candidate,
        "rationale": {
            "chemical_reasoning": str(parsed.get("reasoning") or default["reasoning"]),
            "comparison_to_top1": comparison_to_top1,
            "selection_mode": selection_mode,
            "hypothesis_alignment": "",
            "information_value": "",
            "concerns": "",
        },
        "confidence": 0.8,
        "selection_source": "autobo_llm_acquisition",
        "autobo_qlogei_rank": selected_id,
        "selected_rank": selected_id,
        "top1_candidate": dict(shortlist[0].get("candidate", {})),
        "applied_prior_ids": list(selected_record.get("applied_prior_ids", [])),
        "knowledge_score_breakdown": dict(selected_record.get("knowledge_score_breakdown", {})),
        "knowledge_mode": str(selected_record.get("knowledge_mode") or ""),
    }
    return {
        "messages": outbound_messages,
        "proposal_selected": proposal_selected,
        "current_proposal": {
            "candidates": [candidate],
            "selected_index": chosen_index,
            "applied_prior_ids": list(selected_record.get("applied_prior_ids", [])),
            "knowledge_score_breakdown": dict(selected_record.get("knowledge_score_breakdown", {})),
            "knowledge_mode": str(selected_record.get("knowledge_mode") or ""),
        },
        "llm_usage": llm_usage,
        "log_lines": [f"[select_candidate] autobo rank={selected_id} shortlist_index={chosen_index}"],
    }


def record_autobo_result(
    *,
    state: dict[str, Any],
    settings,
    selected: dict[str, Any],
    shortlist: list[dict[str, Any]],
    candidate: dict[str, Any],
    result_value: float,
) -> dict[str, Any]:
    autobo_state = _resolve_autobo_state(state.get("autobo_state", {}), settings)
    calibrator = ReverseCalibrator.from_dict({"plaus_records": autobo_state.get("llm_plaus_audit", [])})
    log_lines: list[str] = []
    calibrator.plaus_records = _resolve_pending_plausibility_records(
        calibrator.plaus_records,
        candidate,
        result_value,
    )
    effective_llm_weight = float(autobo_state.get("effective_llm_weight", 0.30))
    should_degrade, recommended_weight, degrade_reason = calibrator.should_degrade_llm_weight()
    if should_degrade:
        effective_llm_weight = min(effective_llm_weight, float(recommended_weight))
        log_lines.append(f"[autobo_llm_weight] degraded_to={effective_llm_weight:.2f} reason={degrade_reason}")

    return {
        "autobo_state": {
            **autobo_state,
            "llm_plaus_audit": _trim_autobo_list(calibrator.plaus_records, limit=50),
            "effective_llm_weight": effective_llm_weight,
        },
        "log_lines": log_lines,
    }


class SurrogatePool:
    """Fit and query a pool of surrogate models while isolating failures."""

    def __init__(self, specs: list[SurrogateSpec] | None = None):
        resolved_specs = DEFAULT_SURROGATE_SPECS if specs is None else specs
        self.specs = {spec.model_id: spec for spec in resolved_specs}
        self.models: dict[str, BaseSurrogateModel] = {}
        self.fit_status: dict[str, bool] = {}
        self.fit_errors: dict[str, str] = {}

    def fit_all(self, X: np.ndarray, y: np.ndarray) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for model_id, spec in self.specs.items():
            try:
                model = _create_surrogate_from_spec(spec)
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
        self.seq_start_n = max(0, int(seq_start_n))  # deprecated under full LOOCV mode
        self.ci_level = float(ci_level)
        self.z_score = _z_score_for_ci(self.ci_level)
        self.seq_log: dict[str, list[float]] = {}
        self.cal_log: dict[str, list[bool]] = {}
        self.latest_scores: dict[str, FitnessScores] = {}

    def compute_loocv_predictions(
        self,
        model_id: str,
        spec: SurrogateSpec,
        encoder,
        observations: list[dict[str, Any]],
    ) -> LOOCVResult:
        X_obs, y_obs = _observations_to_arrays(observations, encoder)
        n_obs = int(X_obs.shape[0])
        if n_obs < 2:
            return LOOCVResult(
                model_id=model_id,
                mu=np.zeros(n_obs, dtype=float),
                sigma=np.ones(n_obs, dtype=float),
                y_true=np.asarray(y_obs, dtype=float),
            )

        mu = np.zeros(n_obs, dtype=float)
        sigma = np.zeros(n_obs, dtype=float)
        for index in range(n_obs):
            keep_mask = np.ones(n_obs, dtype=bool)
            keep_mask[index] = False
            train_X = X_obs[keep_mask]
            train_y = np.asarray(y_obs, dtype=float)[keep_mask]
            if train_X.shape[0] == 0:
                raise RuntimeError(f"LOOCV for {model_id} requires at least one training point per fold.")
            model = _create_surrogate_from_spec(spec)
            model.fit(train_X, train_y)
            fold_mu, fold_sigma = model.predict(X_obs[index : index + 1])
            mu[index] = float(np.asarray(fold_mu, dtype=float)[0])
            sigma[index] = float(max(np.asarray(fold_sigma, dtype=float)[0], 1e-6))

        return LOOCVResult(
            model_id=model_id,
            mu=np.asarray(mu, dtype=float),
            sigma=np.asarray(sigma, dtype=float),
            y_true=np.asarray(y_obs, dtype=float),
        )

    def compute_loocv_metrics(
        self,
        model_id: str,
        spec: SurrogateSpec,
        encoder,
        observations: list[dict[str, Any]],
        direction: str = "maximize",
    ) -> FitnessScores:
        loocv = self.compute_loocv_predictions(model_id, spec, encoder, observations)
        sigma_safe = np.maximum(np.asarray(loocv.sigma, dtype=float), 1e-6)
        y_true = np.asarray(loocv.y_true, dtype=float)
        mu = np.asarray(loocv.mu, dtype=float)

        log_likelihood = -0.5 * np.log(2.0 * np.pi * sigma_safe**2) - 0.5 * ((y_true - mu) / sigma_safe) ** 2
        f_seq = float(np.mean(log_likelihood)) if len(log_likelihood) else 0.0
        self.seq_log.setdefault(model_id, []).append(f_seq)

        lower = mu - self.z_score * sigma_safe
        upper = mu + self.z_score * sigma_safe
        in_ci = (y_true >= lower) & (y_true <= upper)
        self.cal_log[model_id] = [bool(item) for item in in_ci.tolist()]
        coverage = float(np.mean(in_ci)) if len(in_ci) else 0.0
        f_cal = -abs(coverage - self.ci_level)

        if len(y_true) < 3:
            f_rank = 0.0
        else:
            if len(y_true) < 5:
                rank_indices = np.arange(len(y_true))
            elif direction == "minimize":
                rank_indices = np.argsort(y_true)[:5]
            else:
                rank_indices = np.argsort(y_true)[-5:][::-1]
            f_rank = _safe_spearman(mu[rank_indices], y_true[rank_indices]) if len(rank_indices) >= 3 else 0.0

        return FitnessScores(
            model_id=model_id,
            f_seq=f_seq,
            f_cal=float(f_cal),
            f_rank=float(f_rank),
        )

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
    def __init__(
        self,
        top_k: int = 8,
        prefilter_multiplier: int = 10,
        hallucination_mode: str = "kriging_believer",
    ):
        self.top_k = max(1, int(top_k))
        self.prefilter_multiplier = max(1, int(prefilter_multiplier))
        self.hallucination_mode = str(hallucination_mode or "kriging_believer").strip().lower()
        self.last_prefilter_size = 0

    def propose_candidates(
        self,
        active_model: BaseSurrogateModel,
        refit_model_factory: Callable[[], BaseSurrogateModel] | None,
        encoder,
        candidate_pool: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        direction: str = "maximize",
        seed: int = 0,
    ) -> list[dict[str, Any]]:
        if not candidate_pool:
            self.last_prefilter_size = 0
            return []

        try:
            scale_context = _build_observation_scale_context(observations, direction=direction)
            shortlist = _build_sequential_fantasized_shortlist(
                active_model=active_model,
                refit_model_factory=refit_model_factory,
                encoder=encoder,
                candidate_pool=candidate_pool,
                scale_context=scale_context,
                top_k=self.top_k,
                prefilter_multiplier=self.prefilter_multiplier,
                hallucination_mode=self.hallucination_mode,
                seed=seed,
            )
            self.last_prefilter_size = int(
                min(
                    len(candidate_pool),
                    max(self.top_k, self.prefilter_multiplier * self.top_k),
                )
            )
            for item in shortlist:
                item.pop("_predicted_value_scaled", None)
            return shortlist
        except Exception:
            rng = np.random.default_rng(seed)
            indices = list(rng.choice(len(candidate_pool), size=min(self.top_k, len(candidate_pool)), replace=False))
            self.last_prefilter_size = min(len(candidate_pool), max(self.top_k, self.prefilter_multiplier * self.top_k))
            return [
                {
                    "candidate": dict(candidate_pool[index]),
                    "predicted_value": None,
                    "uncertainty": None,
                    "acquisition_value": None,
                    "acquisition_value_raw": None,
                    "selection_step": rank + 1,
                    "selection_mode": "fallback_random",
                    "rank": rank + 1,
                }
                for rank, index in enumerate(indices)
            ]


def _build_observation_scale_context(
    observations: list[dict[str, Any]],
    *,
    direction: str,
) -> dict[str, Any]:
    valid = [dict(item) for item in observations if item.get("result") is not None]
    results = np.asarray([float(item["result"]) for item in valid], dtype=float)
    if direction == "minimize":
        y_model = -1.0 * results
    else:
        y_model = results
    y_mean = float(np.mean(y_model)) if len(y_model) else 0.0
    y_std = float(np.std(y_model)) or 1.0
    y_scaled = (y_model - y_mean) / y_std if len(y_model) else np.zeros(0, dtype=float)
    scaled_observations = [
        {
            **item,
            "result": float(y_scaled[index]),
        }
        for index, item in enumerate(valid)
    ]
    return {
        "observations_scaled": scaled_observations,
        "y_mean": y_mean,
        "y_std": y_std,
        "best_f_scaled": float(np.max(y_scaled)) if len(y_scaled) else 0.0,
        "direction": direction,
    }


def _score_candidate_pool(
    *,
    surrogate: BaseSurrogateModel,
    encoder,
    candidate_pool: list[dict[str, Any]],
    best_f_scaled: float,
    y_mean: float,
    y_std: float,
    direction: str,
    seed: int,
) -> dict[str, Any]:
    X_pool = encoder.encode_batch(candidate_pool)
    pred_mean_scaled, pred_std_scaled = surrogate.predict(X_pool)
    pred_mean_scaled = np.asarray(pred_mean_scaled, dtype=float)
    pred_std_scaled = np.maximum(np.asarray(pred_std_scaled, dtype=float), 1e-6)

    if isinstance(surrogate, BoTorchGPSurrogate) and surrogate.model is not None:
        try:
            acquisition = create_acquisition("qlog_ei", {})
            acq_values = acquisition.score(surrogate, X_pool, best_f_scaled, np.random.default_rng(seed))
        except Exception:
            acq_values = _analytic_ei(pred_mean_scaled, pred_std_scaled, best_f_scaled)
    else:
        acq_values = _analytic_ei(pred_mean_scaled, pred_std_scaled, best_f_scaled)

    pred_mean = pred_mean_scaled * float(y_std) + float(y_mean)
    pred_std = np.maximum(pred_std_scaled * float(y_std), 1e-6)
    if direction == "minimize":
        pred_mean = -1.0 * pred_mean

    return {
        "candidate_pool": [dict(candidate) for candidate in candidate_pool],
        "X_pool": X_pool,
        "pred_mean_scaled": pred_mean_scaled,
        "pred_std_scaled": pred_std_scaled,
        "pred_mean": np.asarray(pred_mean, dtype=float),
        "pred_std": np.asarray(pred_std, dtype=float),
        "acquisition": np.asarray(acq_values, dtype=float),
    }


def _build_hallucinated_observations(
    selected_records: list[dict[str, Any]],
    *,
    hallucination_mode: str,
) -> list[dict[str, Any]]:
    normalized_mode = str(hallucination_mode or "kriging_believer").strip().lower()
    if normalized_mode != "kriging_believer":
        raise ValueError(f"Unsupported hallucination mode: {hallucination_mode}")
    hallucinated: list[dict[str, Any]] = []
    for item in selected_records:
        hallucinated.append(
            {
                "candidate": dict(item.get("candidate", {})),
                "result": float(item.get("_predicted_value_scaled", 0.0) or 0.0),
            }
        )
    return hallucinated


def _fit_fantasized_model(
    *,
    refit_model_factory: Callable[[], BaseSurrogateModel],
    encoder,
    observations_scaled: list[dict[str, Any]],
) -> BaseSurrogateModel:
    X_train, y_train = _observations_to_arrays(observations_scaled, encoder)
    model = refit_model_factory()
    model.fit(X_train, y_train)
    return model


def _shortlist_record_from_scores(
    *,
    score_payload: dict[str, Any],
    candidate_index: int,
    selection_step: int,
    selection_mode: str,
    acquisition_value: float,
    acquisition_value_raw: float,
) -> dict[str, Any]:
    index = int(candidate_index)
    return {
        "candidate": dict(score_payload["candidate_pool"][index]),
        "predicted_value": float(score_payload["pred_mean"][index]),
        "uncertainty": float(score_payload["pred_std"][index]),
        "acquisition_value": float(acquisition_value),
        "acquisition_value_raw": float(acquisition_value_raw),
        "selection_step": int(selection_step),
        "selection_mode": str(selection_mode),
        "rank": int(selection_step),
        "_predicted_value_scaled": float(score_payload["pred_mean_scaled"][index]),
    }


def _build_sequential_fantasized_shortlist(
    *,
    active_model: BaseSurrogateModel,
    refit_model_factory: Callable[[], BaseSurrogateModel] | None,
    encoder,
    candidate_pool: list[dict[str, Any]],
    scale_context: dict[str, Any],
    top_k: int,
    prefilter_multiplier: int,
    hallucination_mode: str,
    seed: int,
) -> list[dict[str, Any]]:
    if not candidate_pool:
        return []

    top_k = max(1, int(top_k))
    prefilter_size = min(len(candidate_pool), max(top_k, int(prefilter_multiplier) * top_k))
    raw_scores = _score_candidate_pool(
        surrogate=active_model,
        encoder=encoder,
        candidate_pool=candidate_pool,
        best_f_scaled=float(scale_context.get("best_f_scaled", 0.0) or 0.0),
        y_mean=float(scale_context.get("y_mean", 0.0) or 0.0),
        y_std=float(scale_context.get("y_std", 1.0) or 1.0),
        direction=str(scale_context.get("direction") or "maximize"),
        seed=seed,
    )
    raw_acquisition = np.asarray(raw_scores["acquisition"], dtype=float)
    raw_order = np.argsort(raw_acquisition)[::-1]
    prefilter_indices = [int(index) for index in raw_order[:prefilter_size]]
    if not prefilter_indices:
        return []

    shortlist: list[dict[str, Any]] = []
    selected_global_indices: list[int] = []
    top1_index = int(prefilter_indices[0])
    shortlist.append(
        _shortlist_record_from_scores(
            score_payload=raw_scores,
            candidate_index=top1_index,
            selection_step=1,
            selection_mode="raw_top1",
            acquisition_value=float(raw_acquisition[top1_index]),
            acquisition_value_raw=float(raw_acquisition[top1_index]),
        )
    )
    selected_global_indices.append(top1_index)
    remaining_indices = [index for index in prefilter_indices if index != top1_index]

    while remaining_indices and len(shortlist) < top_k:
        fallback_index = max(remaining_indices, key=lambda index: float(raw_acquisition[int(index)]))
        conditioned_record: dict[str, Any] | None = None
        if refit_model_factory is not None:
            try:
                fantasized_model = _fit_fantasized_model(
                    refit_model_factory=refit_model_factory,
                    encoder=encoder,
                    observations_scaled=list(scale_context.get("observations_scaled", []))
                    + _build_hallucinated_observations(shortlist, hallucination_mode=hallucination_mode),
                )
                remaining_pool = [candidate_pool[index] for index in remaining_indices]
                conditioned_scores = _score_candidate_pool(
                    surrogate=fantasized_model,
                    encoder=encoder,
                    candidate_pool=remaining_pool,
                    best_f_scaled=float(scale_context.get("best_f_scaled", 0.0) or 0.0),
                    y_mean=float(scale_context.get("y_mean", 0.0) or 0.0),
                    y_std=float(scale_context.get("y_std", 1.0) or 1.0),
                    direction=str(scale_context.get("direction") or "maximize"),
                    seed=seed + len(shortlist),
                )
                local_best = int(np.argmax(np.asarray(conditioned_scores["acquisition"], dtype=float)))
                conditioned_record = _shortlist_record_from_scores(
                    score_payload=conditioned_scores,
                    candidate_index=local_best,
                    selection_step=len(shortlist) + 1,
                    selection_mode="fantasized_greedy",
                    acquisition_value=float(conditioned_scores["acquisition"][local_best]),
                    acquisition_value_raw=float(raw_acquisition[int(remaining_indices[local_best])]),
                )
                fallback_index = int(remaining_indices[local_best])
            except Exception:
                conditioned_record = None

        if conditioned_record is None:
            conditioned_record = _shortlist_record_from_scores(
                score_payload=raw_scores,
                candidate_index=fallback_index,
                selection_step=len(shortlist) + 1,
                selection_mode="fantasized_greedy",
                acquisition_value=float(raw_acquisition[fallback_index]),
                acquisition_value_raw=float(raw_acquisition[fallback_index]),
            )

        shortlist.append(conditioned_record)
        selected_global_indices.append(int(fallback_index))
        remaining_indices = [index for index in remaining_indices if int(index) != int(fallback_index)]

    return shortlist


class ReverseCalibrator:
    def __init__(self, window_size: int = 15, degrade_threshold: float = 0.2):
        self.window_size = int(window_size)
        self.degrade_threshold = float(degrade_threshold)
        self.plaus_records: list[dict[str, Any]] = []

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
        recent_plaus = [
            item
            for item in self.plaus_records[-self.window_size :]
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
        return {"plaus_records": self.plaus_records[-50:]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReverseCalibrator":
        instance = cls()
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


def _z_score_for_ci(ci_level: float) -> float:
    bounded = min(max(float(ci_level), 1e-3), 0.999)
    try:
        from scipy.stats import norm

        return float(norm.ppf(0.5 + bounded / 2.0))
    except Exception:
        return 1.96


def _embedding_fallback_reason(requested_method: str, resolved_method: str, notes: list[str]) -> str | None:
    if requested_method == resolved_method:
        return None
    for note in notes:
        if "fell back" in str(note).lower():
            return str(note)
    if notes:
        return str(notes[-1])
    return f"Requested {requested_method} but resolved to {resolved_method}."


def _resolve_autobo_state(autobo_state: dict[str, Any] | None, settings) -> dict[str, Any]:
    current = dict(autobo_state or {})
    return {
        "active_model": str(current.get("active_model") or getattr(settings, "autobo_initial_active", "gp_matern52")),
        "fitness_log": dict(current.get("fitness_log", {})),
        "calibration_log": list(current.get("calibration_log", [])),
        "switch_history": list(current.get("switch_history", [])),
        "last_layer2_iteration": int(current.get("last_layer2_iteration", 0)),
        "hysteresis_until": int(current.get("hysteresis_until", 0)),
        "llm_plaus_audit": list(current.get("llm_plaus_audit", [])),
        "effective_llm_weight": float(current.get("effective_llm_weight", 0.30)),
    }


def _effective_config_with_components(
    state: dict[str, Any],
    *,
    active_model_id: str,
    resolved_components: dict[str, Any],
    switch_info: dict[str, Any],
    trigger_reason: str,
) -> dict[str, Any]:
    effective_config = dict(state.get("effective_config", {}))
    effective_config.update(
        {
            "resolved_components": resolved_components,
            "surrogate_model": "autobo_pool",
            "kernel_config": {"key": "autobo_adaptive", "params": {}},
            "acquisition_function": "qlog_ei",
            "autobo_active_model": active_model_id,
            "selection_source": "autobo",
            "selection_diagnostics": {
                "switch_info": switch_info,
                "trigger_reason": trigger_reason,
            },
        }
    )
    return effective_config


def _bo_config_with_active_model(bo_config: dict[str, Any], active_model_id: str) -> dict[str, Any]:
    next_config = dict(bo_config or {})
    next_config["autobo_active_model"] = active_model_id
    return next_config


def _empty_usage_delta() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_calls": 0,
        "estimated": False,
    }


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if np.isfinite(value) else default
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _first_dataset_backed_shortlist_record(
    shortlist: list[dict[str, Any]],
    oracle: DatasetOracle,
    preferred_index: int = 0,
) -> tuple[int, dict[str, Any]] | None:
    preferred = shortlist[preferred_index:] + shortlist[:preferred_index]
    preferred_indices = list(range(preferred_index, len(shortlist))) + list(range(0, preferred_index))
    for index, item in zip(preferred_indices, preferred):
        candidate = item.get("candidate", {})
        if not isinstance(candidate, dict):
            continue
        if oracle.candidate_exists(candidate):
            normalized = dict(item)
            normalized["candidate"] = oracle.lookup(candidate)["candidate"]
            return index, normalized
    return None


def _run_llm_plausibility_eval(
    *,
    state: dict[str, Any],
    pool: SurrogatePool,
    encoder,
    observations: list[dict[str, Any]],
    fitted_ids: list[str],
    llm,
    settings,
    invoke_json_node,
) -> tuple[dict[str, float], list[dict[str, Any]], dict[str, Any]]:
    if len(fitted_ids) < 2:
        return {}, [], _empty_usage_delta()

    variables = state.get("problem_spec", {}).get("variables", [])
    observed_keys = {
        candidate_to_key(item.get("candidate", {}))
        for item in observations
        if item.get("candidate")
    }
    dataset_candidate_pool = dataset_candidate_pool_from_spec(state.get("problem_spec", {}).get("dataset", {}))
    candidate_pool = build_bo_candidate_pool(
        variables,
        observed_keys=observed_keys,
        candidate_pool_size=max(128, int(getattr(settings, "autobo_eval_points", 10) or 10) * 32),
        seed=int(state.get("iteration", 0)),
        hard_constraints=[],
        candidate_pool=dataset_candidate_pool,
    )
    if not candidate_pool:
        return {}, [], _empty_usage_delta()

    X_pool = encoder.encode_batch(candidate_pool)
    all_predictions = pool.predict_all(X_pool)
    if len(all_predictions) < 2:
        return {}, [], _empty_usage_delta()

    autobo_state = _resolve_autobo_state(state.get("autobo_state", {}), settings)
    active_model_id = str(autobo_state.get("active_model") or getattr(settings, "autobo_initial_active", "gp_matern52"))
    active_model = pool.get_active_model(active_model_id)
    top_acquisition_keys: set[str] = set()
    if active_model is not None:
        active_spec = pool.specs.get(active_model_id)
        refit_model_factory = None
        if active_spec is not None:
            refit_model_factory = lambda spec=active_spec: create_surrogate(
                spec.surrogate_key,
                dict(spec.params),
                spec.kernel_key or "matern52",
                dict(spec.kernel_params),
            )
        acquisition_shortlist = AcquisitionFlow(
            top_k=5,
            prefilter_multiplier=int(getattr(settings, "autobo_shortlist_prefilter_multiplier", 10) or 10),
            hallucination_mode=str(getattr(settings, "autobo_shortlist_hallucination_mode", "kriging_believer")),
        ).propose_candidates(
            active_model=active_model,
            refit_model_factory=refit_model_factory,
            encoder=encoder,
            candidate_pool=candidate_pool,
            observations=observations,
            direction=state.get("optimization_direction", "maximize"),
            seed=int(state.get("iteration", 0)),
        )
        top_acquisition_keys = {
            candidate_to_key(item.get("candidate", {}))
            for item in acquisition_shortlist
            if item.get("candidate")
        }

    model_means = np.stack([all_predictions[model_id][0] for model_id in all_predictions], axis=0)
    disagreement = np.max(model_means, axis=0) - np.min(model_means, axis=0)
    disagree_indices = np.argsort(disagreement)[::-1][:5]
    eval_indices = list(disagree_indices.astype(int))
    for index, candidate in enumerate(candidate_pool):
        if candidate_to_key(candidate) in top_acquisition_keys and index not in eval_indices:
            eval_indices.append(index)
        if len(eval_indices) >= int(getattr(settings, "autobo_eval_points", 10) or 10):
            break

    direction = state.get("optimization_direction", "maximize")
    observed_results = np.asarray([float(item["result"]) for item in observations if item.get("result") is not None], dtype=float)
    y_model = observed_results if direction != "minimize" else -1.0 * observed_results
    y_mean = float(np.mean(y_model)) if len(y_model) else 0.0
    y_std = float(np.std(y_model)) or 1.0
    anon_map = {model_id: chr(65 + index) for index, model_id in enumerate(fitted_ids[:6])}
    reverse_anon = {value: key for key, value in anon_map.items()}
    eval_points = []
    for point_offset, candidate_index in enumerate(eval_indices[: int(getattr(settings, "autobo_eval_points", 10) or 10)]):
        candidate = candidate_pool[int(candidate_index)]
        predictions = {}
        for model_id in fitted_ids:
            if model_id not in all_predictions or model_id not in anon_map:
                continue
            mean_scaled, sigma_scaled = all_predictions[model_id]
            mean_raw = float(mean_scaled[int(candidate_index)] * y_std + y_mean)
            if direction == "minimize":
                mean_raw = -1.0 * mean_raw
            sigma_raw = float(max(sigma_scaled[int(candidate_index)] * y_std, 1e-6))
            predictions[anon_map[model_id]] = {"mu": mean_raw, "sigma": sigma_raw}
        eval_points.append(
            {
                "point_id": f"P{point_offset + 1}",
                "candidate": candidate,
                "candidate_description": ", ".join(f"{key}={value}" for key, value in candidate.items()),
                "predictions": predictions,
            }
        )

    memory_manager = MemoryManager.from_dict(state.get("memory", {}))
    context = ContextBuilder.for_autobo_surrogate_eval(state, memory_manager)
    prompt = build_surrogate_plausibility_prompt(
        reaction_context=context.get("reaction_context", {}),
        top_observations=context.get("top_observations", []),
        bottom_observations=context.get("bottom_observations", []),
        eval_points=eval_points,
        knowledge_cards=context.get("knowledge_guidance", []),
        memory_rules=context.get("memory_rules", []),
    )
    parsed, _, usage = invoke_json_node(
        llm,
        state,
        prompt,
        {"evaluations": []},
        node_name="run_bo_iteration",
    )
    model_scores: dict[str, list[float]] = {model_id: [] for model_id in fitted_ids}
    audit_records: list[dict[str, Any]] = []
    point_lookup = {item["point_id"]: item for item in eval_points}
    for evaluation in parsed.get("evaluations", []):
        point_id = str(evaluation.get("point_id") or "")
        prediction_id = str(evaluation.get("prediction_id") or "")
        model_id = reverse_anon.get(prediction_id)
        point = point_lookup.get(point_id)
        if model_id is None or point is None:
            continue
        score = _coerce_float(evaluation.get("score"), default=3.0)
        model_scores.setdefault(model_id, []).append(score)
        prediction_payload = point.get("predictions", {}).get(prediction_id, {})
        audit_records.append(
            {
                "iteration": int(state.get("iteration", 0)),
                "point_id": point_id,
                "model_id": model_id,
                "candidate": dict(point.get("candidate", {})),
                "candidate_key": candidate_to_key(point.get("candidate", {})),
                "llm_score": score,
                "predicted_mu": prediction_payload.get("mu"),
                "predicted_sigma": prediction_payload.get("sigma"),
                "reasoning": str(evaluation.get("reasoning") or ""),
                "observed_y": None,
            }
        )

    scores = {
        model_id: float(np.mean(values)) if values else 3.0
        for model_id, values in model_scores.items()
    }
    return scores, audit_records, usage


def _trim_autobo_list(items: list[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    return list(items[-max(int(limit), 1) :])


def _trim_autobo_mapping(payload: dict[str, Any], limit: int = 50) -> dict[str, Any]:
    if len(payload) <= limit:
        return dict(payload)
    ordered_keys = sorted(payload.keys(), key=lambda key: int(key) if str(key).isdigit() else key)
    trimmed = ordered_keys[-max(int(limit), 1) :]
    return {key: payload[key] for key in trimmed}


def _recent_calibration_coverage(values: list[bool], window: int = 10) -> float | None:
    if not values:
        return None
    sample = values[-max(int(window), 1) :]
    return float(np.mean(sample)) if sample else None


def _autobo_kernel_key(active_model_id: str) -> str:
    if active_model_id == "gp_smk":
        return "smk"
    if active_model_id == "gp_matern32":
        return "matern32"
    return "matern52"


def _resolve_pending_plausibility_records(
    records: list[dict[str, Any]],
    candidate: dict[str, Any],
    observed_y: float,
) -> list[dict[str, Any]]:
    candidate_key = candidate_to_key(candidate or {})
    updated = []
    for record in records:
        item = dict(record)
        if item.get("candidate_key") == candidate_key and item.get("observed_y") is None:
            item["observed_y"] = float(observed_y)
        updated.append(item)
    return updated


def _coerce_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value) if np.isfinite(value) else default
    if isinstance(value, str):
        try:
            numeric = float(value.strip())
        except ValueError:
            return default
        return numeric if np.isfinite(numeric) else default
    return default
