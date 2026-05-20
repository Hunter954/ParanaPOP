import os


def _database_url() -> str:
    """Return a SQLAlchemy-compatible database URL.

    Some hosts expose postgres:// while SQLAlchemy expects postgresql://.
    Railway normally uses postgresql://, but this keeps deploys safer.
    """
    url = os.getenv("DATABASE_URL", "sqlite:///dev.db")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    WP_BASE_URL = os.getenv("WP_BASE_URL", "https://paranapop.com.br").rstrip("/")
    WP_PER_PAGE = int(os.getenv("WP_PER_PAGE", "20"))

    AUTO_SYNC_INTERVAL = int(os.getenv("AUTO_SYNC_INTERVAL", "0"))

    SITE_NAME = os.getenv("SITE_NAME", "News")
    LIVE_EMBED_TITLE = os.getenv("LIVE_EMBED_TITLE", "AO VIVO")

    MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/data/uploads")
    MEDIA_URL_PREFIX = os.getenv("MEDIA_URL_PREFIX", "/media")
    R2_BUCKET = (os.getenv("R2_BUCKET") or "").strip()
    R2_ENDPOINT = (os.getenv("R2_ENDPOINT") or "").strip()
    R2_REGION = (os.getenv("R2_REGION") or "auto").strip()
    R2_ACCESS_KEY_ID = (os.getenv("R2_ACCESS_KEY_ID") or "").strip()
    R2_SECRET_ACCESS_KEY = (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip()
    R2_PUBLIC_BASE_URL = (os.getenv("R2_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    R2_LOCAL_DIR = (os.getenv("R2_LOCAL_DIR") or MEDIA_ROOT).strip()
    USE_R2 = bool(R2_BUCKET and R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(32 * 1024 * 1024)))
