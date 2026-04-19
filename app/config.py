import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///dev.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

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
    R2_LOCAL_DIR = (os.getenv("R2_LOCAL_DIR") or MEDIA_ROOT).strip()
    USE_R2 = bool(R2_BUCKET and R2_ENDPOINT and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY)
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(32 * 1024 * 1024)))
