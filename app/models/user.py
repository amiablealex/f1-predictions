"""User and password reset models."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


def _utcnow() -> datetime:
    """Return a timezone-aware UTC `datetime` for column defaults."""
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_contributor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    league_memberships = relationship(
        "LeagueMembership",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    leagues_created = relationship(
        "League",
        back_populates="creator",
        foreign_keys="League.created_by_id",
    )
    reset_tokens = relationship(
        "PasswordResetToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    # ----- password handling -----
    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username!r}>"


class PasswordResetToken(db.Model):
    """Single-use token for password reset emails."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user = relationship("User", back_populates="reset_tokens")

    @classmethod
    def issue(cls, user: User, ttl_hours: int) -> "PasswordResetToken":
        token = secrets.token_urlsafe(32)
        return cls(
            user_id=user.id,
            token=token,
            expires_at=_utcnow() + timedelta(hours=ttl_hours),
        )

    @property
    def is_valid(self) -> bool:
        if self.used_at is not None:
            return False
        return _utcnow() < self.expires_at

    def consume(self) -> None:
        self.used_at = _utcnow()
