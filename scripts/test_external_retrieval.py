"""
Manual harness for the external retrieval connectors.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import Settings
from knowledge.connectors import (
    LocalRAGConnector,
    PubChemConnector,
    SemanticScholarConnector,
    WebSearchConnector,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _build_connector(name: str, settings: Settings):
    if name == "semantic_scholar":
        return SemanticScholarConnector(api_key=settings.semantic_scholar_api_key)
    if name == "web":
        return WebSearchConnector(
            api_key=settings.tavily_api_key,
            include_domains=list(settings.web_search_domains or []) or None,
        )
    if name == "pubchem":
        return PubChemConnector()
    if name == "local_rag":
        return LocalRAGConnector(settings=settings)
    raise ValueError(f"Unsupported connector '{name}'")


def _print_chunks(name: str, chunks) -> None:
    print(f"\n=== {name} ({len(chunks)} result(s)) ===")
    for index, chunk in enumerate(chunks, start=1):
        print(f"{index}. {chunk.short_source}")
        print(f"   score={getattr(chunk, 'relevance_score', 0.0):.3f}")
        snippet = str(chunk.content or "").replace("\n", " ")
        print(f"   {snippet[:500]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query external knowledge connectors for ChemBO.")
    parser.add_argument(
        "--connector",
        default="semantic_scholar",
        choices=["semantic_scholar", "web", "pubchem", "local_rag", "all"],
        help="Connector to query.",
    )
    parser.add_argument("--query", default="", help="Search query or compound name.")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum results per connector.")
    parser.add_argument("--year-range", default="", help="Optional Semantic Scholar year range, e.g. 2018-2025.")
    parser.add_argument("--topic", default="general", help="Web-search topic hint.")
    args = parser.parse_args()

    if not args.query:
        raise SystemExit("--query is required")

    settings = Settings()
    connector_names = (
        ["semantic_scholar", "web", "pubchem", "local_rag"]
        if args.connector == "all"
        else [args.connector]
    )

    for name in connector_names:
        connector = _build_connector(name, settings)
        if not connector.is_available() and name in {"web", "local_rag"}:
            print(f"\n=== {name} ===")
            print("Connector is not currently available; skipping.")
            continue
        if name == "semantic_scholar":
            chunks = connector.search(args.query, max_results=args.top_k, year_range=args.year_range)
        elif name == "web":
            chunks = connector.search(args.query, max_results=args.top_k, topic=args.topic)
        elif name == "local_rag":
            chunks = connector.search(args.query, top_k=args.top_k)
        else:
            chunks = connector.search(args.query)
        _print_chunks(name, chunks)


if __name__ == "__main__":
    main()

