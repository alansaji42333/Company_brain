#!/usr/bin/env python
"""Move skill files from the old per-user layout to the per-org layout.

Before the organization migration, skills lived in `data/skills/{user_id}/`.
They now live in `data/skills/{org_id}/`, where a user's personal org is
`org_{md5(user_id)[:12]}`.

    python -m scripts.migrate_skills           # dry run
    python -m scripts.migrate_skills --apply   # move the directories

The target directory is derived from each source directory name, so a
directory that is already an org id (`org_*` / `personal-*` slug form) is
skipped. Moves are skipped too if the destination already exists.
"""
import argparse
import logging
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from app.config import SKILLS_DIR  # noqa: E402
from app.orgs import personal_org_id  # noqa: E402


def migrate(apply: bool) -> None:
    if not os.path.isdir(SKILLS_DIR):
        print(f"No skills directory at {SKILLS_DIR}; nothing to do.")
        return

    for name in sorted(os.listdir(SKILLS_DIR)):
        src = os.path.join(SKILLS_DIR, name)
        if not os.path.isdir(src):
            continue
        if name.startswith("org_"):
            print(f"{name}: already an org directory, skipped")
            continue

        dst = os.path.join(SKILLS_DIR, personal_org_id(name))
        if os.path.exists(dst):
            print(f"{name}: destination {os.path.basename(dst)} already exists, skipped")
            continue

        if apply:
            shutil.move(src, dst)
            print(f"{name}: moved -> {os.path.basename(dst)}")
        else:
            print(f"{name}: would move -> {os.path.basename(dst)}")

    if not apply:
        print("\nDry run only. Re-run with --apply to move the directories.")


def main():
    parser = argparse.ArgumentParser(description="Migrate skill directories to the org layout")
    parser.add_argument("--apply", action="store_true", help="Move the directories (default: dry run)")
    args = parser.parse_args()
    migrate(apply=args.apply)


if __name__ == "__main__":
    main()
