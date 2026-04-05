"""
CLI entrypoint for building the local RAG index.
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
from knowledge.local_rag import LocalRAGStore


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def build_index(
    data_dir: str | None = None,
    ord_dir: str | None = None,
    reviews_dir: str | None = None,
    textbooks_dir: str | None = None,
    supplementary_dir: str | None = None,
    clear_existing: bool = False,
) -> dict[str, int]:
    settings = Settings()
    store = LocalRAGStore(settings=settings)
    stats = store.build_index(
        data_dir=data_dir,
        ord_dir=ord_dir,
        reviews_dir=reviews_dir,
        textbooks_dir=textbooks_dir,
        supplementary_dir=supplementary_dir,
        clear_existing=clear_existing,
    )
    logger.info("Index statistics: %s", stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local chemistry RAG index.")
    parser.add_argument("--data-dir", help="Base Local_Knowledge directory")
    parser.add_argument("--ord-dir", help="Override ORD JSON directory")
    parser.add_argument("--reviews-dir", help="Override review PDF directory")
    parser.add_argument("--textbooks-dir", help="Override textbook PDF directory")
    parser.add_argument("--supplementary-dir", help="Override supplementary directory")
    parser.add_argument("--clear", action="store_true", help="Clear existing collections before ingesting")
    args = parser.parse_args()

    build_index(
        data_dir=args.data_dir,
        ord_dir=args.ord_dir,
        reviews_dir=args.reviews_dir,
        textbooks_dir=args.textbooks_dir,
        supplementary_dir=args.supplementary_dir,
        clear_existing=args.clear,
    )


if __name__ == "__main__":
    main()
