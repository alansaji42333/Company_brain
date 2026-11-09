"""Organization (workspace) and membership service.

Organizations are the tenant boundary. Every user has an implicit "personal"
organization derived deterministically from their user_id
(id = ``org_<md5:12>``, slug = ``personal-<md5:12>``). This is exactly what the
0003 migration backfills for pre-existing rows, and it is what legacy tokens
(no ``org_id`` claim / header) resolve to — preserving HS256 dev mode.

Membership is always verified against the database: a JWT ``org_id`` claim or
``X-Org-Id`` header is only a hint, never trusted on its own.
"""
import hashlib
import logging
import re
import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, AuthError
from app.database import Membership, Organization

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_REVIEWER, ROLE_MEMBER)

# Roles allowed to review/approve/edit skills and run synthesis.
REVIEW_ROLES = (ROLE_ADMIN, ROLE_REVIEWER)


def _digest(user_id: str) -> str:
    return hashlib.md5(user_id.encode("utf-8")).hexdigest()[:12]


def personal_org_id(user_id: str) -> str:
    """Deterministic personal org id for a user (matches the 0003 backfill)."""
    return f"org_{_digest(user_id)}"


def personal_org_slug(user_id: str) -> str:
    return f"personal-{_digest(user_id)}"


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return slug[:60] or "workspace"


async def get_membership(db: AsyncSession, org_id: str, user_id: str) -> Membership | None:
    result = await db.execute(
        select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_organization(db: AsyncSession, org_id: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    return result.scalar_one_or_none()


async def get_organization_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def get_or_create_personal_org(db: AsyncSession, user_id: str) -> AuthContext:
    """Return the user's personal-org AuthContext, creating it if absent."""
    org_id = personal_org_id(user_id)
    membership = await get_membership(db, org_id, user_id)
    if membership is not None:
        return AuthContext(user_id=user_id, org_id=org_id, role=membership.role)

    db.add(Organization(
        id=org_id,
        name=f"{user_id}'s workspace",
        slug=personal_org_slug(user_id),
    ))
    db.add(Membership(org_id=org_id, user_id=user_id, role=ROLE_ADMIN))
    await db.flush()
    logger.info("Created personal org %s for %s", org_id, user_id)
    return AuthContext(user_id=user_id, org_id=org_id, role=ROLE_ADMIN)


async def resolve_auth_context(
    db: AsyncSession, user_id: str, org_candidate: str | None = None
) -> AuthContext:
    """Resolve the AuthContext for a verified user_id.

    If an org candidate (JWT `org_id` claim or X-Org-Id header) is supplied, a
    real membership is required — otherwise a 403 is raised. With no candidate,
    the user's implicit personal org is used (created lazily for new users).
    """
    if org_candidate:
        membership = await get_membership(db, org_candidate, user_id)
        if membership is None:
            raise AuthError(
                f"You are not a member of organization {org_candidate}", 403
            )
        return AuthContext(user_id=user_id, org_id=org_candidate, role=membership.role)
    return await get_or_create_personal_org(db, user_id)


# --- org CRUD -------------------------------------------------------------

async def create_org(
    db: AsyncSession, name: str, slug: str | None, owner_user_id: str
) -> dict:
    """Create an organization; the caller becomes its admin."""
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    if not slug or not slug.strip():
        slug = _slugify(name)
        if await get_organization_by_slug(db, slug):
            slug = f"{slug}-{org_id.split('_', 1)[1]}"
    else:
        slug = _slugify(slug)

    existing = await get_organization_by_slug(db, slug)
    if existing is not None:
        raise ValueError("An organization with this slug already exists")
    if await get_organization(db, org_id) is not None:
        raise ValueError("Organization id collision; retry")

    db.add(Organization(id=org_id, name=name, slug=slug))
    db.add(Membership(org_id=org_id, user_id=owner_user_id, role=ROLE_ADMIN))
    await db.flush()
    logger.info("Created org %s (slug=%s) owner=%s", org_id, slug, owner_user_id)
    return {"id": org_id, "name": name, "slug": slug}


async def list_all_org_ids(db: AsyncSession) -> list[str]:
    result = await db.execute(select(Organization.id))
    return list(result.scalars().all())


async def list_user_orgs(db: AsyncSession, user_id: str) -> list[dict]:
    """Orgs `user_id` is a member of (id/name/slug/role) — for the client's
    org switcher. Read-only; membership is by definition verified here."""
    result = await db.execute(
        select(Organization, Membership)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.name, Organization.id)
    )
    return [
        {"id": org.id, "name": org.name, "slug": org.slug, "role": membership.role}
        for org, membership in result.all()
    ]


# --- membership management -------------------------------------------------

async def list_members(db: AsyncSession, org_id: str) -> list[dict]:
    result = await db.execute(
        select(Membership)
        .where(Membership.org_id == org_id)
        .order_by(Membership.created_at)
    )
    return [
        {"user_id": m.user_id, "role": m.role, "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in result.scalars().all()
    ]


async def count_admins(db: AsyncSession, org_id: str) -> int:
    result = await db.execute(
        select(func.count()).select_from(Membership).where(
            Membership.org_id == org_id, Membership.role == ROLE_ADMIN
        )
    )
    return int(result.scalar_one() or 0)


async def add_member(
    db: AsyncSession, org_id: str, user_id: str, role: str = ROLE_MEMBER
) -> Membership:
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    existing = await get_membership(db, org_id, user_id)
    if existing is not None:
        raise ValueError("User is already a member of this organization")
    membership = Membership(org_id=org_id, user_id=user_id, role=role)
    db.add(membership)
    await db.flush()
    return membership


async def set_member_role(
    db: AsyncSession, org_id: str, user_id: str, role: str, actor_user_id: str
) -> Membership:
    """Set a member's role; protects against demoting/removing the last admin.

    The guard applies regardless of who the actor is — a sole admin cannot
    demote themselves either, because an org must never be left without an
    admin.
    """
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    membership = await get_membership(db, org_id, user_id)
    if membership is None:
        raise ValueError("User is not a member of this organization")

    if membership.role == ROLE_ADMIN and role != ROLE_ADMIN and await count_admins(db, org_id) <= 1:
        raise ValueError("Cannot demote the last admin of the organization")
    membership.role = role
    await db.flush()
    await db.refresh(membership)
    return membership


async def remove_member(
    db: AsyncSession, org_id: str, user_id: str, actor_user_id: str
) -> None:
    membership = await get_membership(db, org_id, user_id)
    if membership is None:
        raise ValueError("User is not a member of this organization")

    # The last admin cannot be removed — including self-removal (an org must
    # always keep at least one admin).
    if membership.role == ROLE_ADMIN and await count_admins(db, org_id) <= 1:
        raise ValueError("Cannot remove the last admin of the organization")

    await db.execute(
        sa_delete(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user_id
        )
    )
    await db.flush()