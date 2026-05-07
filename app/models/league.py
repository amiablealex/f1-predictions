"""League and league membership models."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class League(db.Model):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    creator = relationship("User", back_populates="leagues_created", foreign_keys=[created_by_id])
    memberships = relationship(
        "LeagueMembership",
        back_populates="league",
        cascade="all, delete-orphan",
    )

    @staticmethod
    def generate_invite_code() -> str:
        """Generate a random invite code using the configured alphabet."""
        alphabet = current_app.config["INVITE_CODE_ALPHABET"]
        length = current_app.config["INVITE_CODE_LENGTH"]
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<League {self.name!r} ({self.invite_code})>"


class LeagueMembership(db.Model):
    __tablename__ = "league_memberships"
    __table_args__ = (
        UniqueConstraint("league_id", "user_id", name="uq_league_membership"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    league = relationship("League", back_populates="memberships")
    user = relationship("User", back_populates="league_memberships")
