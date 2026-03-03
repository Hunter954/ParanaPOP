from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

post_categories = db.Table(
    "post_categories",
    db.Column("post_id", db.Integer, db.ForeignKey("post.id"), primary_key=True),
    db.Column("category_id", db.Integer, db.ForeignKey("category.id"), primary_key=True),
)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(190), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=True)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        # ✅ CORRETO
        return check_password_hash(self.password_hash, pw)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wp_id = db.Column(db.Integer, unique=True, index=True, nullable=True)
    name = db.Column(db.String(190), nullable=False)
    slug = db.Column(db.String(190), unique=True, index=True, nullable=False)

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    wp_id = db.Column(db.Integer, unique=True, index=True, nullable=True)
    source = db.Column(db.String(20), default="wp")  # wp | local

    title = db.Column(db.String(500), nullable=False)
    slug = db.Column(db.String(220), unique=True, index=True, nullable=False)
    excerpt = db.Column(db.Text, nullable=True)
    content_html = db.Column(db.Text, nullable=True)

    featured_image = db.Column(db.String(800), nullable=True)
    author_name = db.Column(db.String(190), nullable=True)

    published_at = db.Column(db.DateTime, index=True, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)

    categories = db.relationship("Category", secondary=post_categories, lazy="joined")

class AdSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)  # ex: lateral_1, lateral_2, header_top
    name = db.Column(db.String(190), nullable=False)
    html = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

class SiteSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)

class PageView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=True)
    path = db.Column(db.String(800), nullable=False)
    ua = db.Column(db.String(400), nullable=True)
    ip = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

class AdClick(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slot_key = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
