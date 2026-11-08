import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _org_id(args) -> str:
    """Resolve --org-id, defaulting to the caller's personal org."""
    from app.orgs import personal_org_id
    return args.org_id or personal_org_id(args.user_id or "cli")


def cmd_ingest(args):
    from app.chunking import chunk_documents
    from app.drive_ingest import ingest_drive_folder
    from app.vectorstore import add_chunks

    user_id = args.user_id or "cli"
    org_id = _org_id(args)
    documents = ingest_drive_folder(args.folder_id, user_id=user_id, org_id=org_id)
    chunks = chunk_documents(documents, user_id=user_id, org_id=org_id)
    add_chunks(chunks)
    logger.info("Ingestion complete — %d chunk(s) stored for org %s", len(chunks), org_id)


def cmd_ingest_slack(args):
    from app.slack_ingest import ingest_slack
    from app.vectorstore import add_chunks

    user_id = args.user_id or "cli"
    org_id = _org_id(args)
    chunks = ingest_slack(user_id=user_id, org_id=org_id)
    add_chunks(chunks)
    logger.info("Slack ingestion complete — %d chunk(s) stored for org %s", len(chunks), org_id)


def cmd_synthesize(args):
    from app.skill_synthesis import synthesize

    org_id = _org_id(args)
    summary = synthesize(org_id=org_id)
    logger.info(
        "Synthesis complete — %d batch(es), %d new skill(s), %d skipped (org %s)",
        summary["batches_processed"], summary["new_skills"], summary["skipped_duplicates"], org_id,
    )


def cmd_ask(args):
    from app.rag import answer_question

    question = args.question
    if not question:
        logger.error("Please provide a question.")
        sys.exit(1)

    user_id = args.user_id or "cli"
    org_id = _org_id(args)
    result = answer_question(question, org_id=org_id, user_id=user_id)
    print(f"\nAnswer:\n{result['answer']}\n")
    if result["sources"]:
        print("Sources:")
        for s in result["sources"]:
            stype = s.get("type", "")
            label = f"({stype}) " if stype else ""
            url = f": {s['url']}" if s.get("url") else ""
            print(f"  - {label}{s['name']}{url}")


def main():
    parser = argparse.ArgumentParser(description="Company Brain — RAG over your company documents")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    def add_tenant_args(p):
        p.add_argument("--user-id", default="cli", help="User ID (acting user / provenance)")
        p.add_argument("--org-id", default=None, help="Organization ID (defaults to the personal org)")

    ingest_parser = subparsers.add_parser("ingest", help="Ingest documents from Google Drive")
    ingest_parser.add_argument("--folder-id", help="Override the folder ID from .env")
    add_tenant_args(ingest_parser)
    ingest_parser.set_defaults(func=cmd_ingest)

    ingest_slack_parser = subparsers.add_parser("ingest-slack", help="Ingest messages from Slack")
    add_tenant_args(ingest_slack_parser)
    ingest_slack_parser.set_defaults(func=cmd_ingest_slack)

    synthesize_parser = subparsers.add_parser("synthesize", help="Run skill synthesis on ingested content")
    add_tenant_args(synthesize_parser)
    synthesize_parser.set_defaults(func=cmd_synthesize)

    ask_parser = subparsers.add_parser("ask", help="Ask a question about your documents")
    ask_parser.add_argument("question", nargs="?", help="Your question")
    add_tenant_args(ask_parser)
    ask_parser.set_defaults(func=cmd_ask)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
