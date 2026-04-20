"""
Manual harness for inspecting ChemBO knowledge augmentation on a structured problem.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import Settings
from core.problem_loader import load_problem_file, problem_preview
from knowledge import augmentation_pipeline as pipeline
from knowledge.llm_adapter import RAGLLMAdapter
from knowledge.knowledge_card import format_cards_for_context
from knowledge.knowledge_state import build_coverage_report, build_derived_targets, build_node_digests


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and inspect the ChemBO knowledge state for a structured problem."
    )
    parser.add_argument(
        "--problem-file",
        default=str(ROOT_DIR / "examples" / "ocm_problem.yaml"),
        help="Structured YAML/JSON problem file. Defaults to examples/ocm_problem.yaml.",
    )
    parser.add_argument(
        "--settings-yaml",
        default="",
        help="Optional settings YAML. If omitted, uses Settings().",
    )
    parser.add_argument(
        "--show-evidence",
        type=int,
        default=6,
        help="How many evidence records to print.",
    )
    parser.add_argument(
        "--show-priors",
        type=int,
        default=8,
        help="How many served priors to print.",
    )
    parser.add_argument(
        "--show-cards",
        type=int,
        default=6,
        help="How many compatibility/summary cards to print.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to dump the full knowledge augmentation payload as JSON.",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print the compatibility kb_context block at the end.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable realtime stage logs and use the compact one-shot builder.",
    )
    return parser


def _load_settings(path: str) -> Settings:
    if path:
        return Settings.from_yaml(path)
    return Settings()


def _print_header(problem_spec: dict[str, Any], problem_file: str) -> None:
    reaction = problem_spec.get("reaction", {}) if isinstance(problem_spec.get("reaction"), dict) else {}
    retrieval = problem_spec.get("retrieval", {}) if isinstance(problem_spec.get("retrieval"), dict) else {}
    print("=== Knowledge Build Summary ===")
    print(f"problem_file: {Path(problem_file).resolve()}")
    print(f"reaction_family: {reaction.get('family') or problem_spec.get('reaction_type') or ''}")
    print(f"preview: {problem_preview(problem_spec)}")
    print(f"variables: {len(problem_spec.get('variables', []) or [])}")
    print(f"retrieval_goals: {', '.join(retrieval.get('goals', []) or []) or 'n/a'}")
    print(f"prefer_sources: {', '.join(retrieval.get('prefer_sources', []) or []) or 'n/a'}")
    print("")


def _log_stage(stage: str, message: str, *, started_at: float | None = None) -> None:
    prefix = f"[knowledge-build] {stage}"
    if started_at is None:
        print(f"{prefix}: {message}", flush=True)
        return
    elapsed = time.perf_counter() - started_at
    print(f"{prefix}: {message} ({elapsed:.2f}s)", flush=True)


def _print_top_level_stats(
    knowledge_state: dict[str, Any],
    artifacts: dict[str, Any],
    kb_priors: dict[str, Any],
    cards: list[dict[str, Any]],
) -> None:
    coverage = knowledge_state.get("coverage_report", {}) if isinstance(knowledge_state.get("coverage_report"), dict) else {}
    facets = coverage.get("facets", {}) if isinstance(coverage.get("facets"), dict) else {}
    print("=== Top-Level Stats ===")
    print(f"knowledge_profile: {knowledge_state.get('knowledge_profile', '')}")
    print(f"derived_targets: {len(knowledge_state.get('derived_targets', []) or [])}")
    print(f"queries: {len(artifacts.get('queries', []) or [])}")
    print(f"evidence_records: {len(knowledge_state.get('evidence_records', []) or [])}")
    print(f"served_priors: {len(knowledge_state.get('served_priors', []) or [])}")
    print(f"knowledge_cards: {len(cards)}")
    print(f"coverage_gaps: {len(coverage.get('coverage_gaps', []) or [])}")
    print(f"warm_start_bias_variables: {len((kb_priors.get('warm_start_bias', {}) or {}).keys())}")
    print(f"facets_covered: {len(facets)}")
    print("")


def _print_source_health(source_health: list[dict[str, Any]]) -> None:
    print("=== Source Health ===")
    if not source_health:
        print("No source health records.")
        print("")
        return
    for item in source_health:
        source = str(item.get("source") or "")
        query_id = str(item.get("query_id") or "")
        facet = str(item.get("facet") or "")
        status = str(item.get("status") or "")
        result_count = int(item.get("result_count", 0) or 0)
        latency_ms = float(item.get("latency_ms", 0.0) or 0.0)
        message = str(item.get("message") or "").strip()
        print(
            f"- {query_id} | {source} | facet={facet} | status={status} | "
            f"results={result_count} | latency_ms={latency_ms:.1f}"
        )
        if message:
            print(f"  note: {message}")
    print("")


def _print_coverage_report(coverage_report: dict[str, Any]) -> None:
    print("=== Coverage Report ===")
    facets = coverage_report.get("facets", {}) if isinstance(coverage_report.get("facets"), dict) else {}
    if not facets:
        print("No coverage report available.")
        print("")
        return
    for facet, payload in facets.items():
        payload = payload if isinstance(payload, dict) else {}
        print(
            f"- {facet}: status={payload.get('status')} "
            f"target={payload.get('target_evidence', 0)} "
            f"analogous={payload.get('analogous_evidence', 0)} "
            f"general={payload.get('general_evidence', 0)} "
            f"served_priors={payload.get('served_priors', 0)}"
        )
        note = str(payload.get("note") or "").strip()
        if note:
            print(f"  note: {note}")
    gaps = coverage_report.get("coverage_gaps", []) if isinstance(coverage_report.get("coverage_gaps"), list) else []
    if gaps:
        print("coverage_gaps:")
        for gap in gaps:
            print(
                f"- facet={gap.get('facet')} status={gap.get('status')} note={str(gap.get('note') or '').strip()}"
            )
    print("")


def _print_served_priors(served_priors: list[dict[str, Any]], limit: int) -> None:
    print("=== Served Priors ===")
    if not served_priors:
        print("No served priors.")
        print("")
        return
    for prior in served_priors[: max(limit, 0)]:
        payload = prior.get("payload", {}) if isinstance(prior.get("payload"), dict) else {}
        print(
            f"- {prior.get('prior_id')} | type={prior.get('prior_type')} | scope={prior.get('scope')} | "
            f"confidence={float(prior.get('confidence', 0.0) or 0.0):.2f} | "
            f"support={prior.get('support_count', 0)} | targets={prior.get('targets', [])}"
        )
        print(f"  summary: {prior.get('summary') or payload.get('summary') or ''}")
        if payload:
            interesting_payload = {
                key: value
                for key, value in payload.items()
                if key in {
                    "preferred_values",
                    "value_scores",
                    "avoided_values",
                    "allowed_values",
                    "preferred_combination",
                    "risky_values_by_target",
                }
            }
            if interesting_payload:
                print(f"  payload: {json.dumps(interesting_payload, ensure_ascii=False)}")
    print("")


def _print_cards(cards: list[dict[str, Any]], limit: int) -> None:
    print("=== Knowledge Cards ===")
    if not cards:
        print("No cards generated.")
        print("")
        return
    for card in cards[: max(limit, 0)]:
        print(
            f"- [{card.get('category')}] {card.get('title')} | scope={card.get('scope')} | "
            f"confidence={card.get('confidence')} | vars={card.get('variables_affected', [])}"
        )
        print(f"  claim: {card.get('claim')}")
    print("")


def _print_evidence_records(evidence_records: list[dict[str, Any]], limit: int) -> None:
    print("=== Evidence Records ===")
    if not evidence_records:
        print("No evidence records.")
        print("")
        return
    for record in evidence_records[: max(limit, 0)]:
        print(
            f"- {record.get('evidence_id')} | facet={record.get('facet')} | scope={record.get('scope')} | "
            f"source={record.get('source_type')} | family={record.get('evidence_family') or 'n/a'} | "
            f"support={float(record.get('support_strength', 0.0) or 0.0):.2f}"
        )
        print(f"  vars: {record.get('variables', [])}")
        print(f"  citation: {record.get('citation')}")
        print(f"  snippet: {str(record.get('snippet') or '').strip()[:280]}")
    print("")


def _maybe_dump_json(
    json_out: str,
    *,
    problem_spec: dict[str, Any],
    knowledge_state: dict[str, Any],
    cards: list[dict[str, Any]],
    artifacts: dict[str, Any],
    kb_context: str,
    kb_priors: dict[str, Any],
) -> None:
    if not json_out:
        return
    payload = {
        "problem_spec": problem_spec,
        "knowledge_state": knowledge_state,
        "knowledge_cards": cards,
        "retrieval_artifacts": artifacts,
        "kb_context": kb_context,
        "kb_priors": kb_priors,
    }
    out_path = Path(json_out).expanduser().resolve()
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"json_written: {out_path}")
    print("")


def _run_knowledge_augmentation_with_progress(
    problem_spec: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], str, dict[str, Any]]:
    overall_started = time.perf_counter()
    llm_adapter = RAGLLMAdapter(settings=settings)
    family = pipeline._reaction_family(problem_spec)
    profile = pipeline.infer_knowledge_profile(family)

    _log_stage("bootstrap", f"family={family or 'unknown'} profile={profile} llm_available={llm_adapter.available}")

    started = time.perf_counter()
    queries, query_notes = pipeline.generate_retrieval_queries(problem_spec, settings, llm_adapter=llm_adapter)
    _log_stage(
        "query_planning",
        f"queries={len(queries)} notes={len(query_notes)}",
        started_at=started,
    )

    started = time.perf_counter()
    retrieved_chunks, retrieval_meta = pipeline.execute_multi_source(queries, problem_spec, settings)
    _log_stage(
        "retrieval",
        (
            f"retrieved_chunks={len(retrieved_chunks)} "
            f"failures={len(retrieval_meta.get('retrieval_failures', []) or [])} "
            f"sources={len(retrieval_meta.get('source_health', []) or [])}"
        ),
        started_at=started,
    )

    started = time.perf_counter()
    deduplicated_chunks = pipeline.deduplicate_chunks(retrieved_chunks)
    _log_stage(
        "deduplicate",
        f"deduplicated_chunks={len(deduplicated_chunks)}",
        started_at=started,
    )

    started = time.perf_counter()
    filtered_chunks, filter_summary = pipeline.filter_and_sanitize(deduplicated_chunks, problem_spec, settings)
    _log_stage(
        "sanitize",
        (
            f"usable={len(filtered_chunks)} blocked={int(filter_summary.get('blocked', 0) or 0)} "
            f"discarded={int(filter_summary.get('discarded', 0) or 0)}"
        ),
        started_at=started,
    )

    started = time.perf_counter()
    snippets, snippet_notes = pipeline.build_evidence_snippets(filtered_chunks, queries, settings, llm_adapter=llm_adapter)
    _log_stage(
        "snippet_build",
        f"snippets={len(snippets)} notes={len(snippet_notes)}",
        started_at=started,
    )

    started = time.perf_counter()
    evidence_records = pipeline.build_evidence_records(snippets, problem_spec)
    _log_stage(
        "evidence_records",
        f"records={len(evidence_records)}",
        started_at=started,
    )

    started = time.perf_counter()
    served_priors, serving_notes = pipeline.serve_knowledge_priors(evidence_records, problem_spec)
    _log_stage(
        "prior_serving",
        f"served_priors={len(served_priors)} notes={len(serving_notes)}",
        started_at=started,
    )

    llm_cards: list[Any] = []
    card_notes: list[str] = []
    if llm_adapter.available:
        started = time.perf_counter()
        llm_cards, card_notes = pipeline.condense_and_build_cards(
            snippets,
            filtered_chunks,
            problem_spec,
            settings,
            llm_adapter=llm_adapter,
        )
        _log_stage(
            "llm_card_synthesis",
            f"llm_cards={len(llm_cards)} notes={len(card_notes)}",
            started_at=started,
        )
    else:
        card_notes.append("Knowledge-card synthesis fallback: LLM unavailable")
        _log_stage("llm_card_synthesis", "skipped because LLM is unavailable")

    started = time.perf_counter()
    compatibility_cards = pipeline.build_compatibility_cards(
        problem_spec=problem_spec,
        evidence_records=evidence_records,
        served_priors=served_priors,
    )
    merged_cards = pipeline.merge_knowledge_cards(llm_cards, compatibility_cards)
    card_payloads = [card.to_dict() for card in merged_cards]
    _log_stage(
        "compatibility_cards",
        f"compatibility_cards={len(compatibility_cards)} merged_cards={len(card_payloads)}",
        started_at=started,
    )

    source_health = retrieval_meta.get("source_health", [])
    started = time.perf_counter()
    coverage_report = build_coverage_report(
        target_family=family,
        profile=profile,
        required_facets=pipeline.required_facets_for_profile(profile),
        evidence_records=[item.to_dict() for item in evidence_records],
        served_priors=[item.to_dict() for item in served_priors],
        source_health=source_health,
    )
    _log_stage(
        "coverage",
        f"coverage_gaps={len(coverage_report.get('coverage_gaps', []) or [])}",
        started_at=started,
    )

    knowledge_state = {
        "target_family": family,
        "knowledge_profile": profile,
        "derived_targets": build_derived_targets(problem_spec),
        "source_health": source_health,
        "coverage_report": coverage_report,
        "evidence_records": [item.to_dict() for item in evidence_records],
        "served_priors": [item.to_dict() for item in served_priors],
        "knowledge_digests": build_node_digests(
            evidence_records=[item.to_dict() for item in evidence_records],
            served_priors=[item.to_dict() for item in served_priors],
        ),
    }
    artifacts = pipeline.knowledge_state_to_retrieval_artifacts(
        knowledge_state=knowledge_state,
        queries=queries,
        retrieval_meta=retrieval_meta,
        query_notes=query_notes,
        filter_summary=filter_summary,
        snippet_count=len(snippets),
        card_payloads=card_payloads,
        card_notes=snippet_notes + card_notes + serving_notes,
        retrieved_total=len(retrieved_chunks),
        deduplicated_total=len(deduplicated_chunks),
        usable_after_filter=len(filtered_chunks),
    )
    kb_context = format_cards_for_context(card_payloads)
    kb_priors = pipeline.served_priors_to_legacy_cache(
        served_priors=served_priors,
        variables=problem_spec.get("variables", []),
        coverage_report=coverage_report,
    )
    _log_stage("complete", f"done cards={len(card_payloads)} priors={len(served_priors)}", started_at=overall_started)
    return knowledge_state, card_payloads, artifacts, kb_context, kb_priors


def main() -> None:
    args = _build_parser().parse_args()
    settings = _load_settings(args.settings_yaml)
    problem_input = load_problem_file(args.problem_file)
    if not isinstance(problem_input, dict):
        raise SystemExit("--problem-file must point to a structured YAML/JSON ChemBO problem spec.")

    _print_header(problem_input, args.problem_file)
    if args.no_progress:
        knowledge_state, cards, artifacts, kb_context, kb_priors = pipeline.run_knowledge_augmentation(problem_input, settings)
    else:
        knowledge_state, cards, artifacts, kb_context, kb_priors = _run_knowledge_augmentation_with_progress(
            problem_input,
            settings,
        )
    source_health = knowledge_state.get("source_health", []) if isinstance(knowledge_state.get("source_health"), list) else []
    coverage_report = knowledge_state.get("coverage_report", {}) if isinstance(knowledge_state.get("coverage_report"), dict) else {}
    served_priors = knowledge_state.get("served_priors", []) if isinstance(knowledge_state.get("served_priors"), list) else []
    evidence_records = knowledge_state.get("evidence_records", []) if isinstance(knowledge_state.get("evidence_records"), list) else []

    _print_top_level_stats(knowledge_state, artifacts, kb_priors, cards)
    _print_source_health(source_health)
    _print_coverage_report(coverage_report)
    _print_served_priors(served_priors, args.show_priors)
    _print_cards(cards, args.show_cards)
    _print_evidence_records(evidence_records, args.show_evidence)
    _maybe_dump_json(
        args.json_out,
        problem_spec=problem_input,
        knowledge_state=knowledge_state,
        cards=cards,
        artifacts=artifacts,
        kb_context=kb_context,
        kb_priors=kb_priors,
    )

    if args.show_context:
        print("=== KB Context ===")
        print(kb_context or "[empty]")


if __name__ == "__main__":
    main()
