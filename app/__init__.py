import threading, time
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


def _ensure_defaults():
    # cria slots padrão se não existirem
    defaults = [
        ("header_top", "Publicidade (Topo)"),
        ("lateral_1", "Publicidade (Lateral 1)"),
        ("lateral_2", "Publicidade (Lateral 2)"),
    ]
    for key, name in defaults:
        if not AdSlot.query.filter_by(key=key).first():
            db.session.add(AdSlot(key=key, name=name, html="", is_active=True))

    if not SiteSetting.query.filter_by(key="live_embed_html").first():
        db.session.add(SiteSetting(key="live_embed_html", value=""))

    db.session.commit()


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

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(site_bp)
    app.register_blueprint(admin_bp)

    with app.app_context():
        db.create_all()
        _ensure_defaults()

        # =========================================================
        # CRIA ADMIN AUTOMATICAMENTE (TEMPORÁRIO - SEM SHELL NO RAILWAY)
        # Depois que você conseguir logar, REMOVA este bloco e commite.
        # =========================================================
        admin_email = "admin@admin.com"
        admin_password = "senha123"

        u = User.query.filter_by(email=admin_email).first()
        if not u:
            u = User(email=admin_email, is_admin=True)
            u.set_password(admin_password)
            db.session.add(u)
            db.session.commit()
            print("ADMIN CRIADO AUTOMATICAMENTE:", admin_email)
        # =========================================================

    # auto sync (opcional)
    if app.config.get("AUTO_SYNC_INTERVAL", 0) and app.config["AUTO_SYNC_INTERVAL"] > 0:
        t = threading.Thread(target=_auto_sync_loop, args=(app,), daemon=True)
        t.start()

    return app
