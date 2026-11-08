"""Regression tests for the org-level last-admin guard.

Bug C: set_member_role / remove_member skipped the guard when the actor was
the target, letting a sole admin demote or remove themselves and leave the
org without any admin. The guard must apply ALWAYS.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, Membership
from app.orgs import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    add_member,
    count_admins,
    create_org,
    remove_member,
    set_member_role,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- sole admin: guard must apply even to self-demotion / self-removal -----

async def test_sole_admin_cannot_demote_self(db):
    org = await create_org(db, name="A", slug="a", owner_user_id="u1")
    await db.commit()

    with pytest.raises(ValueError, match="last admin"):
        await set_member_role(db, org["id"], "u1", ROLE_MEMBER, actor_user_id="u1")
    admin_count = await count_admins(db, org["id"])
    assert admin_count == 1


async def test_sole_admin_cannot_remove_self(db):
    org = await create_org(db, name="B", slug="b", owner_user_id="u1")
    await db.commit()

    with pytest.raises(ValueError, match="last admin"):
        await remove_member(db, org["id"], "u1", actor_user_id="u1")
    admin_count = await count_admins(db, org["id"])
    assert admin_count == 1


async def test_last_admin_cannot_be_demoted_by_anyone(db):
    org = await create_org(db, name="C", slug="c", owner_user_id="u1")
    await add_member(db, org["id"], "u2", ROLE_MEMBER)
    await db.commit()

    # u2 is the acting user (not an admin in the DB, but the guard must not
    # care who the actor is — it protects the TARGET).
    with pytest.raises(ValueError, match="last admin"):
        await set_member_role(db, org["id"], "u1", ROLE_MEMBER, actor_user_id="u2")
    with pytest.raises(ValueError, match="last admin"):
        await remove_member(db, org["id"], "u1", actor_user_id="u2")


# --- two admins: one admin demoting/removing themselves is allowed ---------

async def test_second_admin_can_demote_self(db):
    org = await create_org(db, name="D", slug="d", owner_user_id="u1")
    await add_member(db, org["id"], "u2", ROLE_ADMIN)
    await db.commit()
    assert await count_admins(db, org["id"]) == 2

    membership = await set_member_role(db, org["id"], "u2", ROLE_MEMBER, actor_user_id="u2")
    await db.commit()
    assert membership.role == ROLE_MEMBER
    assert await count_admins(db, org["id"]) == 1


async def test_second_admin_can_remove_self(db):
    org = await create_org(db, name="E", slug="e", owner_user_id="u1")
    await add_member(db, org["id"], "u2", ROLE_ADMIN)
    await db.commit()

    await remove_member(db, org["id"], "u2", actor_user_id="u2")
    await db.commit()
    members = await db.execute(select(Membership).where(Membership.org_id == org["id"]))
    # Only u1 remains, still an admin.
    rows = [m.user_id for m in members.scalars().all()]
    assert rows == ["u1"]
    assert await count_admins(db, org["id"]) == 1
