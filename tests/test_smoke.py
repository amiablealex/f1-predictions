"""Phase 1 smoke test: app factory builds, models register, app responds."""


def test_app_boots(client):
    response = client.get("/", follow_redirects=False)
    # Anonymous root redirects to /auth/login
    assert response.status_code in (301, 302)
    assert "/auth/login" in response.headers.get("Location", "")


def test_login_stub_renders(client):
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert b"F1 Predictions" in response.data


def test_models_importable():
    """Confirm the models package re-exports correctly."""
    from app.models import (
        Driver,
        DnfCountPrediction,
        FastestLapPrediction,
        League,
        LeagueMembership,
        PasswordResetToken,
        PoleTimePrediction,
        PredictionScore,
        Round,
        RoundDriver,
        RoundScoringConfig,
        Session,
        SessionResult,
        Top3QualiPrediction,
        Top3SprintPrediction,
        Top10Prediction,
        User,
    )
    # If imports succeed, the test passes.
    assert User.__tablename__ == "users"
    assert Round.__tablename__ == "rounds"
