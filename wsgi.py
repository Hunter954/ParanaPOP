from flask.cli import with_appcontext
import click

from app import create_app
from app.models import db, User
from app.wp_client import WPClient
from app.sync import sync_categories, sync_posts

app = create_app()

@app.cli.command("init-db")
@with_appcontext
def init_db():
    db.create_all()
    click.echo("DB ok.")

@app.cli.command("create-admin")
@click.argument("email")
@click.argument("password")
@with_appcontext
def create_admin(email, password):
    email = email.strip().lower()
    u = User.query.filter_by(email=email).first()
    if not u:
        u = User(email=email, is_admin=True)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo("Admin criado.")
    else:
        u.set_password(password)
        db.session.commit()
        click.echo("Senha do admin atualizada.")

@app.cli.command("sync-wp")
@with_appcontext
def sync_wp():
    from flask import current_app
    client = WPClient(current_app.config["WP_BASE_URL"])
    sync_categories(client)
    sync_posts(client, max_pages=50, per_page=current_app.config["WP_PER_PAGE"])
    click.echo("Sync WP ok.")
