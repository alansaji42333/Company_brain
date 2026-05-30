from app.chunking import chunk_document, chunk_documents, token_count


def test_token_count():
    assert token_count("hello world") > 0


def test_chunk_document_basic():
    doc = {"id": "doc1", "name": "Test", "text": "This is a test document. " * 100}
    chunks = chunk_document(doc, user_id="user1")
    assert len(chunks) > 0
    assert all(c["doc_id"] == "doc1" for c in chunks)
    assert all(c["user_id"] == "user1" for c in chunks)
    assert all("chunk_index" in c for c in chunks)


def test_chunk_document_empty():
    doc = {"id": "doc1", "name": "Empty", "text": ""}
    chunks = chunk_document(doc)
    assert chunks == []


def test_chunk_documents():
    docs = [
        {"id": "d1", "name": "A", "text": "Hello world. " * 50},
        {"id": "d2", "name": "B", "text": "Another doc. " * 50},
    ]
    all_chunks = chunk_documents(docs, user_id="u1")
    assert len(all_chunks) > 0
    assert all(c["user_id"] == "u1" for c in all_chunks)