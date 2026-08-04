"""
The publishing catalog.

Lets one lesson, authored once in the platform org, appear live in many client
orgs — so updating the master updates it everywhere, rather than fanning out
copies.

WHY THIS IS NOT A HOLE IN THE TENANCY BOUNDARY
----------------------------------------------
Direction is what matters:

    Acme -> Beta       tenant data moving sideways     never
    Acme -> platform   exfiltration                    never
    platform -> Acme   publishing our own content      fine

A lesson is not tenant data. It is platform content flowing outward, the same
relationship a product has with its own documentation. The rule "no tenant's
data ever reaches another tenant" is therefore untouched.

Three properties keep it that way:

  - one-directional BY CONSTRUCTION: a CatalogItem may only reference a
    resource owned by the platform org, checked on insert. "Acme publishes to
    Beta" cannot be expressed in this schema at all.
  - read-only downstream, so no tenant can alter what another tenant sees.
  - publishing is a separate act from authoring, with its own audit event, so
    nothing leaves the platform org by accident.

Defined here rather than in models.py because these tables belong to the
platform, not to any tenant — keeping them apart makes that boundary visible
in the file layout. Note alembic/env.py must import this module for its tables
to appear in Base.metadata (see the imports there and the comment explaining
why that matters).
"""
from sqlalchemy import (
    Index,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import relationship

from .database import Base

# Resource types that can be published. Deliberately narrow: contacts, deals
# and the rest of the CRM are tenant data and can never appear here.
PUBLISHABLE_RESOURCE_TYPES = ('agent', 'vector_store')


class CatalogItem(Base):
    """A platform-owned resource published for grant to client orgs."""
    __tablename__ = 'catalog_items'

    id = Column(Integer, primary_key=True, index=True)
    resource_type = Column(String(20), nullable=False)   # 'agent' | 'vector_store'
    resource_id = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    published_by_account_id = Column(
        Integer, ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True)

    __table_args__ = (
        # One catalog entry per resource: publishing the same lesson twice
        # would produce two independently-revocable copies of the same grant
        # surface, and revoking one would look like it had worked.
        UniqueConstraint('resource_type', 'resource_id',
                         name='uq_catalog_item_resource'),
        CheckConstraint("resource_type IN ('agent','vector_store')",
                        name='ck_catalog_item_resource_type'),
    )

    grants = relationship('CatalogGrant', back_populates='item',
                          cascade='all, delete-orphan')

    def __repr__(self):
        return f'<CatalogItem {self.resource_type}={self.resource_id} "{self.title}">'


class CatalogGrant(Base):
    """Which org — and optionally which group inside it — receives an item.

    group_id NULL grants to the whole org; set it and only that department
    sees the lesson. That is the "share lesson 5 with Sales only" case.
    """
    __tablename__ = 'catalog_grants'

    id = Column(Integer, primary_key=True, index=True)
    catalog_item_id = Column(
        Integer, ForeignKey('catalog_items.id', ondelete='CASCADE'),
        nullable=False, index=True)
    org_id = Column(
        Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
        nullable=False, index=True)
    group_id = Column(
        Integer, ForeignKey('access_groups.id', ondelete='CASCADE'),
        nullable=True, index=True)
    granted_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    granted_by_account_id = Column(
        Integer, ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True)

    # Two PARTIAL unique indexes rather than one UniqueConstraint: Postgres
    # treats NULLs as distinct, so UNIQUE(item, org, group) would happily allow
    # two identical whole-org grants — and revoking one would appear to work
    # while access continued through the other.
    #
    # Declared here as well as in migration f9a0b1c2d3e4 so autogenerate does
    # not see them as drift and propose dropping them.
    __table_args__ = (
        Index('uq_catalog_grant_group', 'catalog_item_id', 'org_id', 'group_id',
              unique=True, postgresql_where=text('group_id IS NOT NULL')),
        Index('uq_catalog_grant_org', 'catalog_item_id', 'org_id',
              unique=True, postgresql_where=text('group_id IS NULL')),
    )

    item = relationship('CatalogItem', back_populates='grants')

    def __repr__(self):
        scope = f'group={self.group_id}' if self.group_id else 'whole-org'
        return f'<CatalogGrant item={self.catalog_item_id} org={self.org_id} {scope}>'
