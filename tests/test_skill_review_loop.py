"""Regression tests for the skill review loop (approve/reject/edit embedding).

Hermetic: add_chunks/delete_by_ids are mocked with recorders; SKILLS_DIR is
redirected to a tmp dir. No Chroma, no network.

Covers:
  Bug A — approve and reject must address the SAME stored vector id (a
          mismatch leaves rejected skills retrievable).
  Bug B — editing an approved skill demotes it to draft and deletes its
          vector; re-approving embeds the new text.
"""

import pytest


@pytest.fixture
def skills_env(monkeypatch, tmp_path):
    from app import skill_store

    monkeypatch.setattr(skill_store, "SKILLS_DIR", str(tmp_path / "skills"))

    added = []    # list of (collection, ids)
    deleted = []  # list of (collection, ids)

    def fake_add_chunks(chunks, collection=None):
        added.append((collection, [chunk["doc_id"] for chunk in chunks], list(chunks)))

    def fake_delete_by_ids(ids, collection=None):
        deleted.append((collection, list(ids)))

    monkeypatch.setattr(skill_store, "add_chunks", fake_add_chunks)
    monkeypatch.setattr(skill_store, "delete_by_ids", fake_delete_by_ids)
    return skill_store, added, deleted


def test_reject_deletes_exactly_the_vector_approve_embedded(skills_env):
    skill_store, added, deleted = skills_env

    skill_id = skill_store.create_skill("org_tenant1", "Naming Things", "summary", ["step 1"], [])
    skill_store.approve_skill("org_tenant1", skill_id)
    skill_store.reject_skill("org_tenant1", skill_id)

    assert len(added) == 1
    assert len(deleted) == 1

    # The stored Chroma id derives from (org_id, doc_id, chunk_index); derive
    # it through the same shared helper the app code uses — never by hand.
    from app.vectorstore import stored_chunk_id

    approved_doc_id = added[0][1][0]
    expected_stored_id = stored_chunk_id("org_tenant1", approved_doc_id, 0)

    assert deleted[0][1] == [expected_stored_id], (
        "approve and reject must address the exact same stored vector id, "
        "otherwise rejected skills stay retrievable"
    )


def test_editing_approved_skill_demotes_to_draft_and_deletes_vector(skills_env):
    skill_store, _added, deleted = skills_env

    from app.vectorstore import stored_chunk_id

    skill_id = skill_store.create_skill("org_tenant1", "Deploy Steps", "summary", ["step 1"], [])
    skill_store.approve_skill("org_tenant1", skill_id)
    assert skill_store.get_skill("org_tenant1", skill_id)["status"] == "approved"

    deleted.clear()

    skill_store.save_skill("org_tenant1", skill_id, "NEW BODY CONTENT")

    skill = skill_store.get_skill("org_tenant1", skill_id)
    assert skill["status"] == "draft"
    assert skill["content"] == "NEW BODY CONTENT"
    assert len(deleted) == 1
    assert deleted[0][1] == [stored_chunk_id("org_tenant1", f"skill_org_tenant1_{skill_id}", 0)]


def test_reapproving_edited_skill_embeds_new_text(skills_env):
    skill_store, added, _deleted = skills_env

    skill_id = skill_store.create_skill("org_tenant1", "Run Report", "old summary", ["old"], [])
    skill_store.approve_skill("org_tenant1", skill_id)
    skill_store.save_skill("org_tenant1", skill_id, "This is the NEW patched text")
    skill_store.approve_skill("org_tenant1", skill_id)

    # Two approve calls → two embeddings; the second must carry the new text.
    assert len(added) == 2
    new_chunks = added[1][2]
    assert len(new_chunks) == 1
    assert "NEW patched text" in new_chunks[0]["text"]
    assert "old summary" not in new_chunks[0]["text"]
