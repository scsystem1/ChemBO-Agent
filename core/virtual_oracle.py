"""
Virtual experiment oracles backed by fitted surrogate models.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

try:  # pragma: no cover - optional dependency
    from autogluon.tabular import TabularPredictor
except Exception:  # pragma: no cover
    TabularPredictor = None


@dataclass(frozen=True)
class ResolvedVirtualOracleSpec:
    backend: str
    train_csv_path: str
    feature_columns: tuple[str, ...]
    target_column: str
    categorical_columns: tuple[str, ...]
    continuous_columns: tuple[str, ...]
    model_dir: str
    presets: str | None
    time_limit: int | None
    eval_metric: str | None
    excluded_model_types: tuple[str, ...]
    derived_columns: tuple["DerivedColumnSpec", ...]


@dataclass(frozen=True)
class DerivedColumnSpec:
    name: str
    kind: str
    inputs: tuple[str, ...]
    delimiter: str | None = None
    scale: float | None = None


_PREDICTOR_CACHE: dict[str, TabularPredictor] = {}


class AutoGluonVirtualOracle:
    """Use an AutoGluon regressor as a virtual chemistry-space oracle."""

    def __init__(self, problem_spec: dict[str, Any], settings: Any | None = None):
        self._spec = _resolve_virtual_oracle_spec(problem_spec, settings)
        self._predictor = _load_or_train_predictor(self._spec)

    @classmethod
    def from_problem_spec(
        cls,
        problem_spec: dict[str, Any],
        settings: Any | None = None,
    ) -> "AutoGluonVirtualOracle | None":
        spec = problem_spec.get("virtual_oracle")
        if not isinstance(spec, dict):
            return None
        backend = str(spec.get("backend") or "").strip().lower()
        if backend not in {"autogluon", "autogluon_tabular"}:
            return None
        return cls(problem_spec, settings=settings)

    def lookup(self, candidate: dict[str, Any]) -> dict[str, Any]:
        frame = _prediction_frame_from_candidates([candidate], self._spec)
        prediction = float(self._predictor.predict(frame).iloc[0])
        row = frame.iloc[0]
        normalized_candidate: dict[str, Any] = {}
        for column in self._spec.feature_columns:
            if column in self._spec.continuous_columns:
                normalized_candidate[column] = float(row[column])
            else:
                normalized_candidate[column] = str(row[column]).strip()
        return {
            "candidate": normalized_candidate,
            "result": prediction,
            "metadata": {
                "virtual_oracle_backend": self._spec.backend,
                "virtual_oracle_model_dir": self._spec.model_dir,
                "virtual_oracle_train_csv_path": self._spec.train_csv_path,
                "virtual_oracle_target_column": self._spec.target_column,
            },
        }


def _resolve_virtual_oracle_spec(
    problem_spec: dict[str, Any],
    settings: Any | None,
) -> ResolvedVirtualOracleSpec:
    spec = problem_spec.get("virtual_oracle")
    if not isinstance(spec, dict):
        raise ValueError("Problem spec does not contain a valid virtual_oracle configuration.")

    train_csv_path = str(Path(spec.get("train_csv_path") or "").expanduser().resolve())
    feature_columns = tuple(str(item) for item in (spec.get("feature_columns") or []))
    target_column = str(spec.get("target_column") or "").strip()
    categorical_columns = tuple(str(item) for item in (spec.get("categorical_columns") or []))
    continuous_columns = tuple(str(item) for item in (spec.get("continuous_columns") or []))
    if not train_csv_path or not feature_columns or not target_column:
        raise ValueError(
            "virtual_oracle requires train_csv_path, feature_columns, and target_column."
        )

    if not categorical_columns and not continuous_columns:
        categorical, continuous = _infer_column_types(problem_spec, feature_columns)
        categorical_columns = tuple(categorical)
        continuous_columns = tuple(continuous)

    output_root = Path(getattr(settings, "output_dir", "./outputs")).expanduser().resolve()
    model_dir = str(
        _default_model_dir(
            output_root=output_root,
            train_csv_path=train_csv_path,
            feature_columns=feature_columns,
            target_column=target_column,
            categorical_columns=categorical_columns,
            continuous_columns=continuous_columns,
            presets=str(spec.get("presets") or "").strip() or None,
            time_limit=_coerce_int(spec.get("time_limit"), default=None),
            excluded_model_types=tuple(
                str(item)
                for item in (
                    spec.get("excluded_model_types")
                    or ["GBM", "CAT", "XGB", "FASTAI", "RF", "XT"]
                )
            ),
            derived_columns=_parse_derived_columns(spec.get("derived_columns") or []),
        )
    )
    explicit_model_dir = str(spec.get("model_dir") or "").strip()
    if explicit_model_dir:
        model_dir = str(Path(explicit_model_dir).expanduser().resolve())

    return ResolvedVirtualOracleSpec(
        backend="autogluon_tabular",
        train_csv_path=train_csv_path,
        feature_columns=feature_columns,
        target_column=target_column,
        categorical_columns=categorical_columns,
        continuous_columns=continuous_columns,
        model_dir=model_dir,
        presets=str(spec.get("presets") or "").strip() or None,
        time_limit=_coerce_int(spec.get("time_limit"), default=None),
        eval_metric=str(spec.get("eval_metric") or "").strip() or None,
        excluded_model_types=tuple(
            str(item)
            for item in (
                spec.get("excluded_model_types")
                or ["GBM", "CAT", "XGB", "FASTAI", "RF", "XT"]
            )
        ),
        derived_columns=_parse_derived_columns(spec.get("derived_columns") or []),
    )


def _infer_column_types(
    problem_spec: dict[str, Any],
    feature_columns: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    variables = {
        str(item.get("name")): item
        for item in (problem_spec.get("variables") or [])
        if isinstance(item, dict) and item.get("name")
    }
    categorical: list[str] = []
    continuous: list[str] = []
    for column in feature_columns:
        variable = variables.get(column, {})
        if str(variable.get("type") or "").strip().lower() == "continuous":
            continuous.append(column)
        else:
            categorical.append(column)
    return categorical, continuous


def _default_model_dir(
    *,
    output_root: Path,
    train_csv_path: str,
    feature_columns: tuple[str, ...],
    target_column: str,
    categorical_columns: tuple[str, ...],
    continuous_columns: tuple[str, ...],
    presets: str | None,
    time_limit: int | None,
    excluded_model_types: tuple[str, ...],
    derived_columns: tuple[DerivedColumnSpec, ...],
) -> Path:
    fingerprint = json.dumps(
        {
            "train_csv_path": train_csv_path,
            "feature_columns": list(feature_columns),
            "target_column": target_column,
            "categorical_columns": list(categorical_columns),
            "continuous_columns": list(continuous_columns),
            "presets": presets,
            "time_limit": time_limit,
            "excluded_model_types": list(excluded_model_types),
            "derived_columns": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "inputs": list(item.inputs),
                    "delimiter": item.delimiter,
                    "scale": item.scale,
                }
                for item in derived_columns
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    return output_root / "virtual_oracles" / f"autogluon_{digest}"


def _load_or_train_predictor(spec: ResolvedVirtualOracleSpec) -> TabularPredictor:
    if TabularPredictor is None:  # pragma: no cover
        raise ImportError(
            "AutoGluon is not installed. Install `autogluon.tabular` to use virtual_oracle_auto mode."
        )

    cache_key = spec.model_dir
    cached = _PREDICTOR_CACHE.get(cache_key)
    if cached is not None:
        return cached

    model_path = Path(spec.model_dir)
    predictor: TabularPredictor | None = None
    if (model_path / "predictor.pkl").exists():
        try:
            predictor = TabularPredictor.load(str(model_path))
        except Exception:
            shutil.rmtree(model_path, ignore_errors=True)
            predictor = None
    if predictor is None:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        train_frame = _load_training_frame(spec)
        predictor = TabularPredictor(
            label=spec.target_column,
            path=str(model_path),
            problem_type="regression",
            eval_metric=spec.eval_metric or "root_mean_squared_error",
        ).fit(
            train_data=train_frame,
            presets=spec.presets or "medium_quality",
            time_limit=spec.time_limit,
            excluded_model_types=list(spec.excluded_model_types) or None,
            verbosity=0,
        )

    _PREDICTOR_CACHE[cache_key] = predictor
    return predictor


def _load_training_frame(spec: ResolvedVirtualOracleSpec) -> pd.DataFrame:
    frame = _apply_derived_columns(pd.read_csv(spec.train_csv_path), spec.derived_columns)
    required = set(spec.feature_columns) | {spec.target_column}
    missing = sorted(column for column in required if column not in frame.columns)
    if missing:
        raise ValueError(
            f"Virtual oracle training set is missing required columns: {missing}"
        )

    cleaned = frame[list(spec.feature_columns) + [spec.target_column]].copy()
    for column in spec.categorical_columns:
        cleaned[column] = cleaned[column].astype(str).str.strip()
    for column in spec.continuous_columns:
        cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")
    cleaned[spec.target_column] = pd.to_numeric(cleaned[spec.target_column], errors="coerce")

    if cleaned.isna().any().any():
        bad_columns = [column for column in cleaned.columns if cleaned[column].isna().any()]
        raise ValueError(
            f"Virtual oracle training data contains non-numeric or missing values in columns: {bad_columns}"
        )
    return cleaned


def _prediction_frame_from_candidates(
    candidates: list[dict[str, Any]],
    spec: ResolvedVirtualOracleSpec,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = [dict(candidate) for candidate in candidates]
    frame = _apply_derived_columns(pd.DataFrame(rows), spec.derived_columns)
    missing = sorted(column for column in spec.feature_columns if column not in frame.columns)
    if missing:
        raise ValueError(
            f"Virtual oracle candidate is missing required feature columns after derivation: {missing}"
        )
    frame = frame[list(spec.feature_columns)].copy()
    for column in spec.categorical_columns:
        frame[column] = frame[column].astype(str).str.strip()
    for column in spec.continuous_columns:
        try:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        except Exception as exc:
            raise ValueError(
                f"Virtual oracle candidate column '{column}' must be numeric."
            ) from exc
    return frame


def _parse_derived_columns(raw_items: list[Any]) -> tuple[DerivedColumnSpec, ...]:
    derived: list[DerivedColumnSpec] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        kind = str(item.get("type") or item.get("kind") or "").strip().lower()
        inputs = tuple(str(value).strip() for value in (item.get("inputs") or []) if str(value).strip())
        if not name or not kind or not inputs:
            continue
        delimiter = str(item.get("delimiter") or "|")
        scale = item.get("scale")
        derived.append(
            DerivedColumnSpec(
                name=name,
                kind=kind,
                inputs=inputs,
                delimiter=delimiter,
                scale=float(scale) if scale is not None and scale != "" else None,
            )
        )
    return tuple(derived)


def _apply_derived_columns(
    frame: pd.DataFrame,
    derived_columns: tuple[DerivedColumnSpec, ...],
) -> pd.DataFrame:
    if not derived_columns:
        return frame

    enriched = frame.copy()
    for spec in derived_columns:
        if spec.kind == "join_strings":
            if all(column in enriched.columns for column in spec.inputs):
                series = enriched[spec.inputs[0]].astype(str).str.strip()
                for column in spec.inputs[1:]:
                    series = series + (spec.delimiter or "|") + enriched[column].astype(str).str.strip()
                enriched[spec.name] = series
            elif spec.name not in enriched.columns:
                raise ValueError(
                    f"Derived column '{spec.name}' requires source columns {list(spec.inputs)}."
                )
        elif spec.kind == "reciprocal_scaled_sum":
            if all(column in enriched.columns for column in spec.inputs):
                total = pd.Series(0.0, index=enriched.index, dtype=float)
                for column in spec.inputs:
                    total = total + pd.to_numeric(enriched[column], errors="coerce")
                if total.isna().any() or (total == 0).any():
                    raise ValueError(
                        f"Derived column '{spec.name}' encountered invalid numeric inputs in {list(spec.inputs)}."
                    )
                enriched[spec.name] = float(spec.scale or 1.0) / total
            elif spec.name not in enriched.columns:
                raise ValueError(
                    f"Derived column '{spec.name}' requires source columns {list(spec.inputs)}."
                )
        else:
            raise ValueError(f"Unsupported derived column type: {spec.kind}")
    return enriched


def _coerce_int(value: Any, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
