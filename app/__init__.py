import os
import threading, time
from pathlib import Path
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from flask import Flask
from flask_login import LoginManager
from dotenv import load_dotenv

from .config import Config
from .models import db, User, AdSlot, SiteSetting
from .routes import site_bp
from .admin import admin_bp
from .wp_client import WPClient
from .sync import sync_categories, sync_posts

login_manager = LoginManager()
login_manager.login_view = "admin.login"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def _ensure_schema_updates():
    inspector = inspect(db.engine)

    if inspector.has_table("user"):
        user_columns = {col["name"] for col in inspector.get_columns("user")}
        user_statements = []
        if "is_active" not in user_columns:
            user_statements.append('ALTER TABLE "user" ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE')
        if "created_at" not in user_columns:
            user_statements.append('ALTER TABLE "user" ADD COLUMN created_at TIMESTAMP')
        if "updated_at" not in user_columns:
            user_statements.append('ALTER TABLE "user" ADD COLUMN updated_at TIMESTAMP')
        if user_statements:
            with db.engine.begin() as conn:
                for stmt in user_statements:
                    conn.execute(text(stmt))
                conn.execute(text('UPDATE "user" SET is_active = TRUE WHERE is_active IS NULL'))
                conn.execute(text('UPDATE "user" SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL'))
                conn.execute(text('UPDATE "user" SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL'))

    if inspector.has_table("guide_listing"):
        guide_columns = {col["name"] for col in inspector.get_columns("guide_listing")}
        guide_statements = []
        if "source_provider" not in guide_columns:
            guide_statements.append('ALTER TABLE guide_listing ADD COLUMN source_provider VARCHAR(60)')
        if "source_ref" not in guide_columns:
            guide_statements.append('ALTER TABLE guide_listing ADD COLUMN source_ref VARCHAR(190)')
        if "source_query" not in guide_columns:
            guide_statements.append('ALTER TABLE guide_listing ADD COLUMN source_query VARCHAR(220)')
        if "maps_url" not in guide_columns:
            guide_statements.append('ALTER TABLE guide_listing ADD COLUMN maps_url VARCHAR(1000)')
        if "last_imported_at" not in guide_columns:
            guide_statements.append('ALTER TABLE guide_listing ADD COLUMN last_imported_at TIMESTAMP')
        if guide_statements:
            with db.engine.begin() as conn:
                for stmt in guide_statements:
                    conn.execute(text(stmt))


def _commit_or_rollback() -> bool:
    try:
        db.session.commit()
        return True
    except IntegrityError:
        # Handles startup race conditions when multiple workers/threads boot together.
        db.session.rollback()
        return False


def _ensure_defaults():
    defaults = [
        ("header_top", "Publicidade (Topo - faixa)"),
        ("home_top", "Publicidade (Home - faixa no meio)"),
        ("home_mid", "Publicidade (Final da matéria)"),
        ("home_bottom", "Publicidade (Home - faixa inferior)"),
        ("sidebar_1", "Publicidade (Sidebar 1)"),
        ("sidebar_2", "Publicidade (Sidebar 2)"),
    ]
    for key, name in defaults:
        if not AdSlot.query.filter_by(key=key).first():
            db.session.add(AdSlot(key=key, name=name, html="", is_active=True))
            _commit_or_rollback()

    for key, value in [
        ("live_embed_html", ""),
        ("logo_url", ""),
        ("site_name", os.getenv("SITE_NAME", "News")),
        ("favicon_url", ""),
        ("default_share_image", ""),
        ("site_tagline", "Portal de notícias do Oeste do Paraná"),
        ("default_meta_description", "Últimas notícias, política, cidade, esportes e tudo que movimenta o Oeste do Paraná."),
        ("facebook_app_id", ""),
        ("google_site_verification", ""),
        ("google_analytics_id", ""),
        ("contact_email", ""),
        ("contact_phone", ""),
        ("instagram_url", ""),
        ("facebook_url", ""),
        ("youtube_url", ""),
        ("x_url", ""),
        ("site_keywords", "notícias, Paraná, Foz do Iguaçu, portal de notícias, atualidades"),
        ("top_menu_category_ids", "[]"),
        ("home_calendar_enabled", "0"),
        ("home_calendar_date", ""),
        ("hub_enabled", "0"),
        ("hub_site_key", ""),
        ("hub_receive_token", ""),
        ("hub_auto_push", "1"),
        ("hub_remote_sites_json", "[]"),
        ("whatsapp_enabled", "0"),
        ("whatsapp_auto_send", "1"),
        ("whatsapp_service_url", ""),
        ("whatsapp_default_group_id", ""),
        ("whatsapp_default_group_name", ""),
        ("whatsapp_send_feed", "1"),
        ("whatsapp_send_stories", "1"),
        ("whatsapp_send_facebook", "1"),
        ("whatsapp_caption_template", ""),
        ("whatsapp_sent_post_ids_json", "[]"),
        ("instagram_bot_enabled", "0"),
        ("instagram_bot_auto_send", "0"),
        ("instagram_bot_service_url", ""),
        ("instagram_bot_service_token", ""),
        ("instagram_bot_send_feed", "1"),
        ("instagram_bot_send_story", "0"),
        ("instagram_bot_caption_template", ""),
        ("instagram_bot_sent_post_ids_json", "[]"),
    ]:
        if not SiteSetting.query.filter_by(key=key).first():
            db.session.add(SiteSetting(key=key, value=value))
            _commit_or_rollback()


def _bootstrap_database(app: Flask) -> bool:
    """Run lightweight startup DB setup without killing the web process.

    Railway can start the web service while the Postgres private endpoint is
    still becoming reachable. Previously, db.create_all() ran during app import
    and any temporary DB timeout killed the Gunicorn worker, causing Cloudflare
    502. This function retries briefly and, by default, lets the app boot even
    if Postgres is still unavailable.
    """
    retries = max(1, int(app.config.get("DB_BOOTSTRAP_RETRIES", 5)))
    delay = float(app.config.get("DB_BOOTSTRAP_RETRY_DELAY", 3))
    required = bool(app.config.get("DB_BOOTSTRAP_REQUIRED", False))

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            db.create_all()
            _ensure_schema_updates()
            _ensure_defaults()

            admin_email = (os.getenv("ADMIN_EMAIL") or "admin@admin.com").strip().lower()
            admin_password = os.getenv("ADMIN_PASSWORD") or "senha123"

            u = User.query.filter_by(email=admin_email).first()
            if not u:
                u = User(email=admin_email, is_admin=True, is_active=True)
                u.set_password(admin_password)
                db.session.add(u)
            else:
                u.is_admin = True
                u.is_active = True
                if not u.password_hash:
                    u.set_password(admin_password)
            if not _commit_or_rollback():
                u = User.query.filter_by(email=admin_email).first()
                if u:
                    u.is_admin = True
                    u.is_active = True
                    if not u.password_hash:
                        u.set_password(admin_password)
                    _commit_or_rollback()
            print("ADMIN OK:", admin_email)
            return True
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            last_error = exc
            print(f"DB BOOTSTRAP WARNING: attempt {attempt}/{retries} failed: {exc}")
            if attempt < retries:
                time.sleep(delay)

    message = f"DB BOOTSTRAP FAILED after {retries} attempt(s): {last_error}"
    if required:
        raise RuntimeError(message) from last_error
    print(message)
    print("Continuing web boot. Fix DATABASE_URL/Postgres connectivity in Railway if pages still fail.")
    return False


def _auto_sync_loop(app: Flask):
    with app.app_context():
        client = WPClient(app.config["WP_BASE_URL"])
        while True:
            try:
                sync_categories(client)
                sync_posts(client, max_pages=50, per_page=app.config["WP_PER_PAGE"])
            except Exception:
                pass
            time.sleep(app.config["AUTO_SYNC_INTERVAL"])


def create_app():
    load_dotenv()
    app = Flask(__name__)
    app.config.from_object(Config)

    Path(app.config["MEDIA_ROOT"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(site_bp)
    app.register_blueprint(admin_bp)

    from datetime import datetime
    app.jinja_env.globals["now"] = datetime.now

    with app.app_context():
        _bootstrap_database(app)

    if app.config.get("AUTO_SYNC_INTERVAL", 0) and app.config["AUTO_SYNC_INTERVAL"] > 0:
        t = threading.Thread(target=_auto_sync_loop, args=(app,), daemon=True)
        t.start()

    return app
