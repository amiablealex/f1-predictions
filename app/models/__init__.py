"""Model package.

All ORM model classes are defined under this package. Importing the package
ensures all models are registered with SQLAlchemy's metadata, which is what
Flask-Migrate needs to autogenerate migrations.
"""
from app.models.driver import Driver, RoundDriver
from app.models.league import League, LeagueMembership
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PoleTimePrediction,
    PredictionScore,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.result import SessionResult
from app.models.round import Round, RoundScoringConfig, Session
from app.models.user import User, PasswordResetToken

__all__ = [
    "Driver",
    "RoundDriver",
    "League",
    "LeagueMembership",
    "Round",
    "RoundScoringConfig",
    "Session",
    "SessionResult",
    "User",
    "PasswordResetToken",
    "Top10Prediction",
    "Top3QualiPrediction",
    "Top3SprintPrediction",
    "PoleTimePrediction",
    "FastestLapPrediction",
    "DnfCountPrediction",
    "PredictionScore",
]
