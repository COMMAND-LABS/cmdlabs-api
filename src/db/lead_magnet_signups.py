from sqlalchemy import Column, Integer, String, DateTime, func, Index
from sqlalchemy.dialects.postgresql import JSONB

from .database import Base


class LeadMagnetSignup(Base):
    """One request for a lead magnet from the public /resources pages.

    Public, account-less rows, like Feedback. APPEND-ONLY: there is no unique
    constraint on (slug, email) on purpose. A repeat request re-sends the email
    (people lose emails), the visitor sees the same response either way (so the
    endpoint never confirms a prior signup), and the admin stats count
    DISTINCT email for uniques. That is simpler than an upsert with a counter
    and loses nothing.

    `slug` is a key into src/config/lead_magnets_registry.py, not a foreign
    key — a magnet retired from the registry keeps its history here.
    """
    __tablename__ = 'lead_magnet_signups'

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), nullable=False, index=True)
    # Stored canonical: stripped and lowercased by the request validator.
    email = Column(String(320), nullable=False, index=True)
    # Where the visitor came from: utm_* keys, referrer, landing_path. Data
    # only — never rendered into the email.
    attribution = Column(JSONB, nullable=True)
    # From the request header, truncated; never from the body.
    user_agent = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        # The stats query groups by slug over a time window.
        Index('ix_lead_magnet_signups_slug_created_at', 'slug', 'created_at'),
    )

    def __repr__(self):
        return f'<LeadMagnetSignup {self.id} slug={self.slug}>'
