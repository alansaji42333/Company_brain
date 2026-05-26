import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def cmd_ingest(args):
    from app.drive_ingest import ingest_drive_folder
    from app.chunking import chunk_documents
    from app.vectorstore import add_chunks

    folder_id = args.folder_id
    logger.info("Starting ingestion from Drive folder: %s", folder_id or "(from .env)")

    documents = ingest_drive_folder(folder_id)
    logger.info("Extracted %d document(s)", len(documents))

    chunks = chunk_documents(documents)
    logger.info("Created %d chunk(s)", len(chunks))

    add_chunks(chunks)
    logger.info("Ingestion complete — %d chunk(s) stored", len(chunks))


def cmd_ingest_slack(args):
    from app.slack_ingest import ingest_slack
    from app.vectorstore import add_chunks

    logger.info("Starting Slack ingestion...")
    chunks = ingest_slack()
    logger.info("Created %d Slack chunk(s)", len(chunks))

    add_chunks(chunks)
    logger.info("Slack ingestion complete — %d chunk(s) stored", len(chunks))


def cmd_synthesize(args):
    from app.skill_synthesis import synthesize

    logger.info("Starting skill synthesis...")
    summary = synthesize()
    logger.info(
        "Synthesis complete — %d batch(es) processed, %d new skill(s), %d skipped duplicate(s)",
        summary["batches_processed"],
        summary["new_skills"],
        summary["skipped_duplicates"],
    )


def cmd_ask(args):
    from app.rag import answer_question

    question = args.question
    if not question:
        logger.error("Please provide a question.")
        sys.exit(1)

    result = answer_question(question)
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

    ingest_parser = subparsers.add_parser("ingest", help="Ingest documents from Google Drive")
    ingest_parser.add_argument("--folder-id", help="Override the folder ID from .env")
    ingest_parser.set_defaults(func=cmd_ingest)

    ingest_slack_parser = subparsers.add_parser("ingest-slack", help="Ingest messages from Slack")
    ingest_slack_parser.set_defaults(func=cmd_ingest_slack)

    synthesize_parser = subparsers.add_parser("synthesize", help="Run skill synthesis on ingested content")
    synthesize_parser.set_defaults(func=cmd_synthesize)

    ask_parser = subparsers.add_parser("ask", help="Ask a question about your documents")
    ask_parser.add_argument("question", nargs="?", help="Your question")
    ask_parser.set_defaults(func=cmd_ask)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
