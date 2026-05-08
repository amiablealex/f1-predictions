"""Flask application factory."""
from __future__ import annotations

import logging

from flask import Flask, redirect, url_for
from flask_login import current_user

from app.config import get_config
from app.extensions import csrf, db, login_manager, migrate
from werkzeug.middleware.proxy_fix import ProxyFix

# Models are imported here so that `flask db migrate` discovers them via
# SQLAlchemy's metadata. Do not remove.
from app.models import (  # noqa: F401  pylint: disable=unused-import
    driver,
    league,
    prediction,
    result,
    round as round_model,
    user,
)


def create_app(config_class=None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    cfg = config_class or get_config()
    app.config.from_object(cfg)
    
    # Trust one layer of proxy headers (Railway's edge).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _configure_logging(app)
    _init_extensions(app)
    _register_blueprints(app)
    _register_user_loader()
    _register_jinja_helpers(app)

    return app


def _configure_logging(app: Flask) -> None:
    level = logging.DEBUG if app.config["DEBUG"] else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app.logger.setLevel(level)


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)


def _register_blueprints(app: Flask) -> None:
    from app.admin.routes import admin_bp
    from app.auth.routes import auth_bp
    from app.leaderboard.routes import leaderboard_bp
    from app.leagues.routes import leagues_bp
    from app.predictions.routes import predictions_bp
    from app.rounds.routes import rounds_bp
    from app.rules.routes import rules_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(predictions_bp)                          # /predictions
    app.register_blueprint(rounds_bp)                                # /round/...
    app.register_blueprint(leagues_bp, url_prefix="/leagues")
    app.register_blueprint(leaderboard_bp, url_prefix="/leaderboard")
    app.register_blueprint(rules_bp, url_prefix="/rules")
    app.register_blueprint(admin_bp, url_prefix="/admin")

    @app.route("/health")
    def health():
        from sqlalchemy import text
        try:
            db.session.execute(text("SELECT 1"))
            return {"status": "ok"}, 200
        except Exception:
            app.logger.exception("Health check failed")
            return {"status": "error"}, 500

    @app.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        # Authenticated landing → predictions if there's an open round,
        # otherwise the round view (which handles 'no rounds yet').
        from datetime import datetime, timezone
        from app.utils import get_current_round
        rd = get_current_round(app.config["F1_SEASON"])
        if rd is None:
            return redirect(url_for("rounds.current"))
        now = datetime.now(timezone.utc)
        if not rd.predictions_locked and rd.predictions_deadline and rd.predictions_deadline > now:
            return redirect(url_for("predictions.edit"))
        return redirect(url_for("rounds.view", season=rd.season, round_number=rd.round_number))


def _register_user_loader() -> None:
    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None


def _register_jinja_helpers(app: Flask) -> None:
    """Make commonly-used helpers available in templates."""
    from app.utils import (
        country_flag,
        deadline_phrase,
        driver_label,
        format_pole_time_ms,
        local_time,
        points_class,
        session_status_class,
        session_status_label,
    )

    @app.context_processor
    def inject_palette():
        return {"palette": app.config["PALETTE"]}

    app.jinja_env.filters["points_class"] = points_class
    app.jinja_env.filters["deadline_phrase"] = deadline_phrase
    app.jinja_env.filters["session_status_class"] = session_status_class
    app.jinja_env.filters["session_status_label"] = session_status_label
    app.jinja_env.filters["pole_time"] = format_pole_time_ms
    app.jinja_env.filters["country_flag"] = country_flag
    app.jinja_env.filters["driver_label"] = driver_label
    app.jinja_env.filters["local_time"] = local_time

    # Expose enum values as Jinja globals so templates can read them by
    # readable names without dotted attribute access on enum classes.
    from app.models.prediction import PredictionType
    from app.models.round import SessionType
    app.jinja_env.globals.update(
        SESSION_QUALI=SessionType.QUALIFYING,
        SESSION_RACE=SessionType.RACE,
        SESSION_SPRINT_QUALI=SessionType.SPRINT_QUALI,
        SESSION_SPRINT_RACE=SessionType.SPRINT_RACE,
        PT_RACE_TOP10=PredictionType.RACE_TOP10,
        PT_QUALI_TOP3=PredictionType.QUALI_TOP3,
        PT_SPRINT_TOP3=PredictionType.SPRINT_TOP3,
        PT_POLE_TIME=PredictionType.POLE_TIME,
        PT_FASTEST_LAP=PredictionType.FASTEST_LAP,
        PT_DNF_COUNT=PredictionType.DNF_COUNT,
    )
