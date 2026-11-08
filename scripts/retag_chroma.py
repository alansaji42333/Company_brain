#!/usr/bin/env python
"""Backfill `org_id` metadata on legacy Chroma chunks (pre-organization era).

Legacy vectors carry `user_id` metadata and no `org_id`. Since migration 0003
each user owns a personal org (`org_{md5(user_id)[:12]}`), so every legacy
vector maps cleanly onto that personal org.

    python -m scripts.retag_chroma            # dry run, prints a summary
    python -m scripts.retag_chroma --apply    # writes the metadata

Note: ChromaDB ids cannot be renamed, so legacy ids stay `user_doc_chunk`
while new writes use `org_doc_chunk`. This is harmless — ids are only used
for upsert/delete — and re-ingesting an org gives clean org-prefixed ids.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from app.config import COLLECTION_NAME, COLLECTION_SKILLS
from app.orgs import personal_org_id
from app.vectorstore import _get_collection


def retag(collection_name: str, apply: bool) -> dict:
    col = _get_collection(collection_name)
    total = col.count()
    stats = {"collection": collection_name, "total": total, "tagged": 0, "skipped": 0, "unmapped": 0}

    if total == 0:
        return stats

    result = col.get(include=["metadatas"])
    ids = result.get("ids") or []
    metadatas = result.get("metadatas") or []

    updates = {}
    for vector_id, metadata in zip(ids, metadatas):
        metadata = metadata or {}
        if metadata.get("org_id"):
            stats["skipped"] += 1
            continue
        user_id = metadata.get("user_id") or ""
        if not user_id:
            stats["unmapped"] += 1
            continue
        new_meta = dict(metadata)
        new_meta["org_id"] = personal_org_id(user_id)
        updates[vector_id] = new_meta
        stats["tagged"] += 1

    if updates and apply:
        col.update(ids=list(updates.keys()), metadatas=list(updates.values()))
        logger.info("Updated metadata for %d vector(s) in %s", len(updates), collection_name)
    elif updates:
        logger.info("Dry run: would update %d vector(s) in %s", len(updates), collection_name)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill org_id metadata in ChromaDB")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()

    for name in (COLLECTION_NAME, COLLECTION_SKILLS):
        stats = retag(name, apply=args.apply)
        print(
            f"{stats['collection']}: {stats['total']} vector(s) — "
            f"{stats['tagged']} tagged, {stats['skipped']} already tagged, "
            f"{stats['unmapped']} unmappable (no user_id)"
        )

    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
