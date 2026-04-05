"""
Manual Local RAG query harness.
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
from knowledge.local_rag import LocalRAGStore, format_retrieval_result


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local chemistry RAG index with a reaction and description.")
    parser.add_argument("--reaction-text", required=True, help="Reaction text or reaction SMILES")
    parser.add_argument("--description", default="", help="Natural-language description of what to retrieve")
    parser.add_argument("--reaction-family", default="", help="Reaction family hint, e.g. DAR/BH/SUZUKI")
    parser.add_argument("--focus-term", action="append", default=[], help="Optional extra focus term; repeatable")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results to return")
    parser.add_argument("--build-index", action="store_true", help="Rebuild index from Local_Knowledge before querying")
    parser.add_argument("--data-dir", default="./Local_Knowledge", help="Local_Knowledge base directory")
    args = parser.parse_args()

    settings = Settings()
    store = LocalRAGStore(settings=settings)
    if args.build_index:
        store.build_index(data_dir=args.data_dir)

    result = store.search_reaction(
        reaction_text=args.reaction_text,
        description=args.description,
        reaction_family=args.reaction_family,
        focus_terms=args.focus_term,
        top_k=args.top_k,
    )
    print(format_retrieval_result(result))


if __name__ == "__main__":
    main()
