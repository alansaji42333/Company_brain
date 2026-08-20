import logging
from datetime import datetime, timezone
from typing import Any
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.google_auth import get_credentials

logger = logging.getLogger(__name__)

Document = dict[str, Any]


def get_drive_service():
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)


def list_files_in_folder(service, folder_id: str) -> list[dict]:
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        results = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, webViewLink)",
            pageToken=page_token,
        ).execute()
        files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return files


def export_google_doc(service, file_id: str, mime_type: str) -> str | None:
    export_mime = "text/plain"
    try:
        response = service.files().export(fileId=file_id, mimeType=export_mime).execute()
        if isinstance(response, bytes):
            return response.decode("utf-8")
        return response
    except HttpError as e:
        logger.warning("Failed to export file %s: %s", file_id, e)
        return None


def download_file(service, file_id: str) -> bytes | None:
    try:
        request = service.files().get_media(fileId=file_id)
        return request.execute()
    except HttpError as e:
        logger.warning("Failed to download file %s: %s", file_id, e)
        return None


SUPPORTED_MIMETYPES = {
    "application/vnd.google-apps.document": "google_doc",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
}


def extract_text(file_info: dict, service) -> Document | None:
    file_id = file_info["id"]
    name = file_info["name"]
    mime_type = file_info["mimeType"]
    source_url = file_info.get("webViewLink", "")

    file_type = SUPPORTED_MIMETYPES.get(mime_type)

    if file_type == "google_doc":
        text = export_google_doc(service, file_id, mime_type)
        if text is None:
            return None
    elif file_type == "pdf":
        raw = download_file(service, file_id)
        if raw is None:
            return None
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif file_type in ("txt", "md"):
        raw = download_file(service, file_id)
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="replace")
    else:
        logger.info("Skipping unsupported file: %s (%s)", name, mime_type)
        return None

    return {
        "id": file_id,
        "name": name,
        "mime_type": mime_type,
        "text": text,
        "source_url": source_url,
        "source_type": "drive",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


def ingest_drive_folder(folder_id: str | None = None, user_id: str = "") -> list[Document]:
    from app.config import GOOGLE_DRIVE_FOLDER_ID
    folder_id = folder_id or GOOGLE_DRIVE_FOLDER_ID
    if not folder_id:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID must be set in .env or passed as argument")

    service = get_drive_service()
    files = list_files_in_folder(service, folder_id)
    logger.info("Found %d file(s) in Drive folder", len(files))

    documents = []
    for f in files:
        doc = extract_text(f, service)
        if doc is not None:
            doc["user_id"] = user_id
            documents.append(doc)

    logger.info("Successfully extracted %d document(s)", len(documents))
    return documents
