import os
os.environ['MEDIA_INDEX_ON_START'] = '0'
import pytest
from app.config import Config
from app import create_app
from app.models import db, SiteSetting

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'SQLALCHEMY_DATABASE_URI', 'sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(Config, 'SQLALCHEMY_ENGINE_OPTIONS', {})
    monkeypatch.setattr(Config, 'MEDIA_ROOT', str(tmp_path / 'media'))
    monkeypatch.setattr(Config, 'TESTING', True, raising=False)
    for key in ['R2_BUCKET', 'R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_PUBLIC_BASE_URL']:
        monkeypatch.setattr(Config, key, '')
    application = create_app()
    application.config['WTF_CSRF_ENABLED'] = False
    with application.app_context():
        db.session.add(SiteSetting(key='whatsapp_bot_publish_token', value='test-token'))
        db.session.commit()
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()

@pytest.fixture
def client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = '1'
        session['_fresh'] = True
    return client
