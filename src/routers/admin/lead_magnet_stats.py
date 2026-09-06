"""
Which lead magnets perform best. Super admin only.

One grouped query over lead_magnet_signups, merged with the code registry in
both directions: a registered magnet with no signups yet shows 0 (so a page
that is not converting is visible, not absent), and a retired slug that still
has rows shows with title None (so history is not lost when a magnet is
removed from the registry).

Aggregates only. The emails themselves stay in the database; an export would
be a separate, deliberate endpoint.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, Request
from sqlalchemy import func as sa_func

from src.config import lead_magnets_registry as registry
from src.db.lead_magnet_signups import LeadMagnetSignup
from src.deps import db_dependency, super_admin_dependency
from src.rate_limit import limiter

from .models import LeadMagnetStatRow, LeadMagnetStatsResponse

router = APIRouter()


@router.get("/lead-magnets/stats", response_model=LeadMagnetStatsResponse)
@limiter.limit("30/minute")
async def lead_magnet_stats(
    db: db_dependency,
    super_admin: super_admin_dependency,
    request: Request,
    since: Optional[datetime] = Query(default=None, description="Count signups at or after this time"),
    until: Optional[datetime] = Query(default=None, description="Count signups at or before this time"),
):
    query = db.query(
        LeadMagnetSignup.slug,
        sa_func.count(LeadMagnetSignup.id),
        sa_func.count(sa_func.distinct(LeadMagnetSignup.email)),
        sa_func.min(LeadMagnetSignup.created_at),
        sa_func.max(LeadMagnetSignup.created_at),
    )
    if since is not None:
        query = query.filter(LeadMagnetSignup.created_at >= since)
    if until is not None:
        query = query.filter(LeadMagnetSignup.created_at <= until)

    counted = {
        slug: (signups, uniques, first, last)
        for slug, signups, uniques, first, last in query.group_by(LeadMagnetSignup.slug).all()
    }

    rows = []
    # Registry order first, then anything in the table the registry no longer knows.
    for slug in registry.all_slugs() + sorted(set(counted) - set(registry.all_slugs())):
        magnet = registry.get(slug)
        signups, uniques, first, last = counted.get(slug, (0, 0, None, None))
        rows.append(LeadMagnetStatRow(
            slug=slug,
            title=magnet.title if magnet else None,
            signups=signups,
            unique_emails=uniques,
            first_signup_at=first,
            last_signup_at=last,
        ))
    rows.sort(key=lambda r: r.signups, reverse=True)

    return LeadMagnetStatsResponse(since=since, until=until, rows=rows)
