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
    defaults = [
        ("header_top", "Publicidade (Topo - faixa)"),
        ("home_top", "Publicidade (Home - faixa superior)"),
        ("home_mid", "Publicidade (Home - faixa meio)"),
        ("home_bottom", "Publicidade (Home - faixa inferior)"),
        ("sidebar_1", "Publicidade (Sidebar 1)"),
        ("sidebar_2", "Publicidade (Sidebar 2)"),
    ]
    for key, name in defaults:
        if not AdSlot.query.filter_by(key=key).first():
            db.session.add(AdSlot(key=key, name=name, html="", is_active=True))

    if not SiteSetting.query.filter_by(key="live_embed_html").first():
        db.session.add(SiteSetting(key="live_embed_html", value=""))

    # Logo do site (URL)
    if not SiteSetting.query.filter_by(key="logo_url").first():
        db.session.add(SiteSetting(key="logo_url", value=""))

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

    # utilitário para templates (data/hora)
    from datetime import datetime
    app.jinja_env.globals["now"] = datetime.now

    with app.app_context():
        db.create_all()
        _ensure_defaults()

        # ✅ ADMIN AUTOMÁTICO (IDEMPOTENTE)
        # - se não existir: cria
        # - se existir: garante is_admin e reseta senha
        # Depois que logar, REMOVA este bloco e commite.
        admin_email = "admin@admin.com"
        admin_password = "senha123"

        u = User.query.filter_by(email=admin_email).first()
        if not u:
            u = User(email=admin_email, is_admin=True)
            db.session.add(u)

        u.is_admin = True
        u.set_password(admin_password)
        db.session.commit()
        print("ADMIN OK:", admin_email)

    if app.config.get("AUTO_SYNC_INTERVAL", 0) and app.config["AUTO_SYNC_INTERVAL"] > 0:
        t = threading.Thread(target=_auto_sync_loop, args=(app,), daemon=True)
        t.start()

    return app
