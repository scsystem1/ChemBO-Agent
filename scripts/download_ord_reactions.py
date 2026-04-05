#!/usr/bin/env python3
"""
Download and prepare ORD reaction subsets for local knowledge use.

This script targets the current ORD web API used by:
https://open-reaction-database.org

It does not attempt a full ORD dump. Instead, it downloads curated reaction
subsets for specific chemistry families and stores them under:
Local_Knowledge/ord_data/
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from google.protobuf.json_format import MessageToDict
from ord_schema.proto import reaction_pb2

BASE_URL = "https://open-reaction-database.org"
DATASETS_URL = f"{BASE_URL}/api/datasets"
SUBMIT_QUERY_URL = f"{BASE_URL}/api/submit_query"
FETCH_QUERY_URL = f"{BASE_URL}/api/fetch_query_result"

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "Local_Knowledge" / "ord_data"

COMMON_BASE_KEYWORDS = (
    "base",
    "carbonate",
    "phosphate",
    "hydroxide",
    "alkoxide",
    "tert-butoxide",
    "t-butoxide",
    "acetate",
    "dbu",
    "dipea",
    "hunig",
    "lutidine",
    "tmg",
    "btmg",
    "cs2co3",
    "k3po4",
    "k2co3",
    "naotbu",
    "kotbu",
    "koac",
)

CATALYST_KEYWORDS = (
    "catalyst",
    "precatalyst",
    "palladium",
    "nickel",
    "copper",
    "pd(",
    "ni(",
    "cu(",
    "[pd]",
    "[ni]",
    "[cu]",
    "tris(dibenzylideneacetone)dipalladium",
)

LIGAND_KEYWORDS = (
    "ligand",
    "phos",
    "binap",
    "dppf",
    "dtbpf",
    "bipyridine",
    "phenanthroline",
    "xantphos",
    "sphos",
    "xphos",
    "ruphos",
    "brettphos",
)

TARGETS = [
    {
        "key": "buchwald_hartwig",
        "filename": "buchwald_hartwig_reactions.json",
        "title": "Buchwald-Hartwig C-N cross-coupling",
        "notes": (
            "Exact ORD dataset matches identified from the live dataset catalog."
        ),
        "sources": [
            {
                "dataset_id": "ord_dataset-00005539a1e04c809a9a78647bea649c",
                "relation": "exact",
                "reason": "AstraZeneca ELN Buchwald-Hartwig dataset",
            },
            {
                "dataset_id": "ord_dataset-cbcc4048add7468e850b6ec42549c70d",
                "relation": "exact",
                "reason": "Pd-catalyzed Buchwald-Hartwig screen",
            },
        ],
    },
    {
        "key": "suzuki_miyaura",
        "filename": "suzuki_miyaura_reactions.json",
        "title": "Suzuki-Miyaura C-C cross-coupling",
        "notes": (
            "Includes both Pd and Ni Suzuki datasets because the user requested "
            "Pd/Ni-dominant cross-coupling coverage."
        ),
        "sources": [
            {
                "dataset_id": "ord_dataset-2038a3c967db4a32a8fbce288437e929",
                "relation": "exact",
                "reason": "Reizman Suzuki subset",
            },
            {
                "dataset_id": "ord_dataset-2be30f5d8dcd471aa6ad410bdee05902",
                "relation": "exact",
                "reason": "Reizman Suzuki subset",
            },
            {
                "dataset_id": "ord_dataset-31989f1b2b9d4885b1dd2d9982da4517",
                "relation": "exact",
                "reason": "Reizman Suzuki subset",
            },
            {
                "dataset_id": "ord_dataset-3b5db90e337942ea886b8f5bc5e3aa72",
                "relation": "exact",
                "reason": "Ni-catalyzed Suzuki-Miyaura dataset",
            },
            {
                "dataset_id": "ord_dataset-3b8a2ef300e145468579027f206a3ac8",
                "relation": "exact",
                "reason": "Merck/Cernak miniaturized Suzuki library",
            },
            {
                "dataset_id": "ord_dataset-68cb8b4b2b384e3d85b5b1efae58b203",
                "relation": "exact",
                "reason": "5760-reaction Suzuki HTE dataset",
            },
            {
                "dataset_id": "ord_dataset-bc349c19b4384756a2aee8aa525b6c2a",
                "relation": "exact",
                "reason": "Reizman Suzuki subset",
            },
            {
                "dataset_id": "ord_dataset-eeba974d3c284aed86d1c1d442260a1e",
                "relation": "exact",
                "reason": "Pd-catalyzed Suzuki HTE dataset",
            },
        ],
    },
    {
        "key": "negishi",
        "filename": "negishi_reactions.json",
        "title": "Negishi cross-coupling",
        "notes": (
            "No direct curated ORD dataset match was found in the live dataset "
            "catalog on 2026-04-05."
        ),
        "sources": [],
    },
    {
        "key": "stille",
        "filename": "stille_reactions.json",
        "title": "Stille cross-coupling",
        "notes": (
            "No direct curated ORD dataset match was found in the live dataset "
            "catalog on 2026-04-05."
        ),
        "sources": [],
    },
    {
        "key": "ullmann_type",
        "filename": "ullmann_type_reactions.json",
        "title": "Ullmann-type Cu-catalyzed C-N coupling",
        "notes": (
            "No explicit Ullmann dataset name was found. The downloaded records "
            "come from the closest curated Cu-mediated C-N coupling dataset "
            "(Chan-Lam) as a related fallback."
        ),
        "sources": [
            {
                "dataset_id": "ord_dataset-5c9a10329a8a48968d18879a48bb8ab2",
                "relation": "related_fallback",
                "reason": "Closest Cu-mediated C-N coupling dataset in ORD (Chan-Lam)",
            }
        ],
    },
    {
        "key": "direct_arylation",
        "filename": "direct_arylation_reactions.json",
        "title": "Pd-catalyzed direct arylation / C-H arylation",
        "notes": "Exact curated ORD match from the live dataset catalog.",
        "sources": [
            {
                "dataset_id": "ord_dataset-675eddcaa6674ce3ae61e79bbc1e1c08",
                "relation": "exact",
                "reason": "Pd-catalyzed imidazole C-H arylation dataset",
            }
        ],
    },
    {
        "key": "ch_activation_cc",
        "filename": "ch_activation_cc_reactions.json",
        "title": "Pd-catalyzed C-H activation / C-C bond formation",
        "notes": (
            "Uses curated C-H activation datasets found in the live ORD catalog."
        ),
        "sources": [
            {
                "dataset_id": "ord_dataset-5415f83da68e4797856ddd330dda5c61",
                "relation": "exact",
                "reason": "Oxazole C-H activation optimization campaign",
            },
            {
                "dataset_id": "ord_dataset-675eddcaa6674ce3ae61e79bbc1e1c08",
                "relation": "exact",
                "reason": "Pd-catalyzed imidazole C-H arylation dataset",
            },
        ],
    },
    {
        "key": "mitsunobu",
        "filename": "mitsunobu_reactions.json",
        "title": "Mitsunobu reactions",
        "notes": (
            "No direct curated ORD dataset match was found in the live dataset "
            "catalog on 2026-04-05."
        ),
        "sources": [],
    },
    {
        "key": "deoxyfluorination",
        "filename": "deoxyfluorination_reactions.json",
        "title": "Deoxyfluorination",
        "notes": "Exact curated ORD match from the live dataset catalog.",
        "sources": [
            {
                "dataset_id": "ord_dataset-fc83743b978f4deea7d6856deacbfe53",
                "relation": "exact",
                "reason": "Deoxyfluorination screen",
            }
        ],
    },
    {
        "key": "photoredox_ni_dual_catalysis",
        "filename": "photoredox_ni_dual_catalysis_reactions.json",
        "title": "Photoredox / Ni dual catalysis",
        "notes": (
            "No exact photoredox/Ni dual-catalysis dataset was found in the live "
            "ORD catalog. The downloaded records are photoredox-related fallback "
            "data only."
        ),
        "sources": [
            {
                "dataset_id": "ord_dataset-c5b00523487a4211a194160edf45e9ab",
                "relation": "related_fallback",
                "reason": "Photoredox-related HTE dataset without nickel dual catalysis",
            }
        ],
    },
    {
        "key": "scr",
        "filename": "scr_reactions.json",
        "title": "Selective catalytic reduction (SCR)",
        "notes": (
            "No direct curated ORD dataset match was found in the live dataset "
            "catalog on 2026-04-05."
        ),
        "sources": [],
    },
    {
        "key": "ocm",
        "filename": "ocm_reactions.json",
        "title": "Oxidative coupling of methane (OCM)",
        "notes": (
            "No direct curated ORD dataset match was found in the live dataset "
            "catalog on 2026-04-05."
        ),
        "sources": [],
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 120,
) -> Any:
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_dataset_catalog(session: requests.Session) -> list[dict[str, Any]]:
    items = request_json(session, DATASETS_URL, timeout=120)
    if not isinstance(items, list):
        raise ValueError("Unexpected ORD dataset catalog response")
    return items


def fetch_search_results(
    session: requests.Session,
    *,
    dataset_id: str,
    limit: int,
    poll_seconds: float = 1.0,
    timeout_seconds: float = 300.0,
) -> list[dict[str, Any]]:
    task_id = request_json(
        session,
        SUBMIT_QUERY_URL,
        params={"dataset_id": dataset_id, "limit": limit},
        timeout=120,
    )
    if not isinstance(task_id, str):
        raise ValueError(f"Unexpected task id response for {dataset_id}: {task_id!r}")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = session.get(
            FETCH_QUERY_URL,
            params={"task_id": task_id},
            timeout=120,
        )
        if response.status_code == 202:
            time.sleep(poll_seconds)
            continue
        if response.status_code == 200:
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(
                    f"Unexpected search payload for {dataset_id}: {type(payload)!r}"
                )
            return payload
        if response.status_code == 404:
            raise RuntimeError(f"ORD task {task_id} for {dataset_id} no longer exists")
        if response.status_code >= 500:
            raise RuntimeError(
                f"ORD task {task_id} for {dataset_id} failed with status "
                f"{response.status_code}"
            )
        response.raise_for_status()

    raise TimeoutError(f"Timed out waiting for ORD task {task_id} for {dataset_id}")


def first_identifier_value(
    identifiers: list[dict[str, Any]], preferred_types: tuple[str, ...]
) -> str:
    for identifier_type in preferred_types:
        for identifier in identifiers:
            if identifier.get("type") == identifier_type and identifier.get("value"):
                return str(identifier["value"])
    return ""


def format_amount(amount: dict[str, Any]) -> str:
    if not amount:
        return ""
    ordered_fields = (
        "moles",
        "mass",
        "volume",
        "equivalents",
        "percentage",
        "concentration",
        "amount",
    )
    for field in ordered_fields:
        value = amount.get(field)
        if isinstance(value, dict) and value.get("value") is not None:
            units = value.get("units", "")
            return f"{value['value']} {units}".strip()
    return ""


def normalize_text(value: str) -> str:
    return " ".join(str(value).strip().split())


def looks_like_base(label: str, name: str) -> bool:
    haystack = f"{label} {name}".lower()
    return any(keyword in haystack for keyword in COMMON_BASE_KEYWORDS)


def looks_like_catalyst(label: str, name: str) -> bool:
    haystack = f"{label} {name}".lower()
    return any(keyword in haystack for keyword in CATALYST_KEYWORDS)


def looks_like_ligand(label: str, name: str) -> bool:
    haystack = f"{label} {name}".lower()
    return any(keyword in haystack for keyword in LIGAND_KEYWORDS)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def extract_conditions(conditions: dict[str, Any]) -> dict[str, Any]:
    temperature = conditions.get("temperature", {})
    pressure = conditions.get("pressure", {})
    stirring = conditions.get("stirring", {})

    result = {
        "temperature": "",
        "pressure": "",
        "atmosphere": "",
        "stirring": "",
    }

    if temperature.get("setpoint", {}).get("value") is not None:
        temp_value = temperature["setpoint"]["value"]
        temp_units = temperature["setpoint"].get("units", "")
        result["temperature"] = f"{temp_value} {temp_units}".strip()
    elif temperature.get("control", {}).get("type"):
        result["temperature"] = str(temperature["control"]["type"]).lower()

    if pressure.get("control", {}).get("type"):
        result["pressure"] = str(pressure["control"]["type"]).lower()
    if pressure.get("atmosphere", {}).get("type"):
        result["atmosphere"] = str(pressure["atmosphere"]["type"]).lower()

    stirring_rate = stirring.get("rate", {})
    if stirring_rate.get("rpm") is not None:
        result["stirring"] = f"{stirring_rate['rpm']} rpm"
    elif stirring.get("type"):
        result["stirring"] = str(stirring["type"]).lower()

    return result


def extract_outcomes(outcomes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    collected: list[dict[str, Any]] = []
    yield_strings: list[str] = []

    for outcome in outcomes:
        reaction_time = ""
        if outcome.get("reaction_time", {}).get("value") is not None:
            rt = outcome["reaction_time"]
            reaction_time = f"{rt['value']} {rt.get('units', '')}".strip()
        for product in outcome.get("products", []):
            product_identifiers = product.get("identifiers", [])
            product_name = first_identifier_value(
                product_identifiers, ("NAME", "SMILES", "INCHI")
            )
            product_smiles = first_identifier_value(product_identifiers, ("SMILES",))
            desired = bool(product.get("is_desired_product"))
            for measurement in product.get("measurements", []):
                measurement_type = measurement.get("type", "")
                entry = {
                    "product_name": product_name,
                    "product_smiles": product_smiles,
                    "measurement_type": measurement_type,
                    "reaction_time": reaction_time,
                    "is_desired_product": desired,
                    "value": None,
                    "units": "",
                    "analysis_key": measurement.get("analysis_key", ""),
                }
                if measurement.get("percentage", {}).get("value") is not None:
                    entry["value"] = measurement["percentage"]["value"]
                    entry["units"] = "PERCENT"
                elif measurement.get("amount", {}).get("value") is not None:
                    entry["value"] = measurement["amount"]["value"]
                    entry["units"] = measurement["amount"].get("units", "")

                if measurement_type in {"YIELD", "CONVERSION"} and entry["value"] is not None:
                    yield_strings.append(
                        f"{measurement_type.lower()} {entry['value']}%"
                        + (f" for {product_name}" if product_name else "")
                    )
                collected.append(entry)

    return collected, dedupe(yield_strings)


def build_text_chunk(record: dict[str, Any]) -> str:
    parts: list[str] = []
    title = record.get("reaction_type") or record.get("category_title") or "Reaction"
    dataset_name = record.get("dataset_name") or record.get("dataset_id")
    reaction_smiles = record.get("reaction_smiles")
    if reaction_smiles:
        parts.append(f"{title} with reaction SMILES {reaction_smiles}.")
    else:
        parts.append(f"{title} from dataset {dataset_name}.")

    if record.get("catalysts"):
        parts.append(f"Catalyst: {', '.join(record['catalysts'])}.")
    if record.get("ligands"):
        parts.append(f"Ligand: {', '.join(record['ligands'])}.")
    if record.get("solvents"):
        parts.append(f"Solvent: {', '.join(record['solvents'])}.")
    if record.get("bases"):
        parts.append(f"Base: {', '.join(record['bases'])}.")

    conditions = record.get("conditions", {})
    condition_parts: list[str] = []
    if conditions.get("temperature"):
        condition_parts.append(f"temperature {conditions['temperature']}")
    if record.get("reaction_times"):
        condition_parts.append(f"time {', '.join(record['reaction_times'])}")
    if condition_parts:
        parts.append("Conditions: " + "; ".join(condition_parts) + ".")

    if record.get("yield_summary"):
        parts.append("Outcomes: " + "; ".join(record["yield_summary"]) + ".")

    doi = record.get("doi")
    if doi:
        parts.append(f"DOI: {doi}.")

    parts.append(f"[ORD: {record['reaction_id']}]")
    return " ".join(parts)


def parse_record(
    raw_record: dict[str, Any],
    *,
    dataset_meta: dict[str, Any],
    relation: str,
    category_key: str,
    category_title: str,
) -> dict[str, Any]:
    reaction = reaction_pb2.Reaction()
    reaction.ParseFromString(base64.b64decode(raw_record["proto"]))
    reaction_dict = MessageToDict(reaction, preserving_proto_field_name=True)

    return {
        "category_key": category_key,
        "category_title": category_title,
        "dataset_id": raw_record.get("dataset_id", ""),
        "dataset_name": dataset_meta.get("name", ""),
        "dataset_description": dataset_meta.get("description", ""),
        "dataset_num_reactions": dataset_meta.get("num_reactions"),
        "source_relation": relation,
        "reaction_id": reaction_dict.get("reaction_id", raw_record.get("reaction_id", "")),
        "reaction": reaction_dict,
    }


def catalog_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["dataset_id"]: item for item in items if item.get("dataset_id")}


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    relation_counts = Counter(record.get("source_relation", "unknown") for record in records)
    dataset_counts = Counter(record.get("dataset_id", "") for record in records)
    return {
        "record_count": len(records),
        "relation_counts": dict(relation_counts),
        "dataset_record_counts": dict(dataset_counts),
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def process_target(
    session: requests.Session,
    *,
    target: dict[str, Any],
    catalog_by_id: dict[str, dict[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    matched_datasets: list[dict[str, Any]] = []
    missing_dataset_ids: list[str] = []

    for source in target["sources"]:
        dataset_id = source["dataset_id"]
        dataset_meta = catalog_by_id.get(dataset_id)
        if not dataset_meta:
            missing_dataset_ids.append(dataset_id)
            continue

        matched_datasets.append(
            {
                "dataset_id": dataset_id,
                "name": dataset_meta.get("name", ""),
                "description": dataset_meta.get("description", ""),
                "num_reactions": dataset_meta.get("num_reactions"),
                "relation": source["relation"],
                "reason": source["reason"],
            }
        )

        limit = int(dataset_meta.get("num_reactions") or 100)
        raw_results = fetch_search_results(session, dataset_id=dataset_id, limit=limit)
        for raw_record in raw_results:
            reaction_id = raw_record.get("reaction_id")
            if not reaction_id or reaction_id in seen_ids:
                continue
            seen_ids.add(reaction_id)
            records.append(
                parse_record(
                    raw_record,
                    dataset_meta=dataset_meta,
                    relation=source["relation"],
                    category_key=target["key"],
                    category_title=target["title"],
                )
            )
        time.sleep(0.25)

    payload = {
        "generated_at_utc": utc_now(),
        "ord_base_url": BASE_URL,
        "category_key": target["key"],
        "category_title": target["title"],
        "notes": target["notes"],
        "matched_datasets": matched_datasets,
        "missing_dataset_ids": missing_dataset_ids,
        "summary": summarize_records(records),
        "records": records,
    }
    write_json(output_dir / target["filename"], payload)

    return {
        "category_key": target["key"],
        "filename": target["filename"],
        "record_count": len(records),
        "matched_dataset_count": len(matched_datasets),
        "missing_dataset_ids": missing_dataset_ids,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": "chembo_agent_ord_downloader/1.0"})

    print("Fetching ORD dataset catalog...", file=sys.stderr)
    catalog = fetch_dataset_catalog(session)
    catalog_by_id = catalog_map(catalog)
    write_json(output_dir / "ord_dataset_catalog.json", catalog)

    manifest = {
        "generated_at_utc": utc_now(),
        "ord_base_url": BASE_URL,
        "target_count": len(TARGETS),
        "targets": [],
    }

    for target in TARGETS:
        print(f"Processing {target['key']}...", file=sys.stderr)
        manifest["targets"].append(
            process_target(
                session,
                target=target,
                catalog_by_id=catalog_by_id,
                output_dir=output_dir,
            )
        )

    write_json(output_dir / "download_manifest.json", manifest)
    print("Finished ORD download and preparation.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
