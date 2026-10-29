import base64
import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException

from app.models import db, Post, PushSubscription, PushDelivery, SiteSetting
from app.pwa import dispatch_news, _ensure_identity

HEADERS = {'X-PWA-Request': '1', 'Origin': 'http://localhost'}


def subscription(endpoint='https://fcm.googleapis.com/fcm/send/test'):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda val: base64.urlsafe_b64encode(val).rstrip(b'=').decode()
    return {'endpoint': endpoint, 'keys': {'p256dh': encode(key), 'auth': encode(b'0123456789abcdef')}}


def test_install_assets_and_identity_survive_restart(app, client):
    response = client.get('/manifest.webmanifest')
    assert response.mimetype == 'application/manifest+json'
    assert response.json['display'] == 'standalone'
    for item in response.json['icons']:
        assert client.get(item['src']).status_code == 200
    worker = client.get('/sw.js')
    assert worker.status_code == 200
    assert worker.headers['Service-Worker-Allowed'] == '/'
    assert 'no-store' in worker.headers['Cache-Control']
    assert client.get('/static/pwa/offline.html').status_code == 200
    assert client.get('/static/pwa/apple-touch-icon.png').status_code == 200
    response = client.get('/api/pwa/config')
    assert list(response.json) == ['publicKey']
    public_key = response.json['publicKey']
    with app.app_context():
        _ensure_identity()
    assert client.get('/api/pwa/config').json['publicKey'] == public_key
    page = client.get('/')
    assert page.status_code == 200
    assert b'id="pwa-open"' in page.data and b'rel="manifest"' in page.data
    assert b'id="pwa-open"' not in client.get('/admin/login').data


def test_opt_in_idempotency_and_authenticated_opt_out(app, client):
    value = subscription()
    for _ in range(2):
        assert client.post('/api/pwa/subscribe', json=value, headers=HEADERS).status_code == 200
    with app.app_context():
        assert PushSubscription.query.count() == 1
    changed = {**value, 'keys': {**value['keys'], 'auth': base64.urlsafe_b64encode(b'fedcba9876543210').decode()}}
    assert client.post('/api/pwa/unsubscribe', json=changed, headers=HEADERS).status_code == 403
    assert client.post('/api/pwa/subscribe', json=changed, headers=HEADERS).status_code == 403
    assert client.post('/api/pwa/unsubscribe', json=value, headers=HEADERS).status_code == 200
    assert client.post('/api/pwa/unsubscribe', json=value, headers=HEADERS).status_code == 200
    with app.app_context():
        assert PushSubscription.query.count() == 0


@pytest.mark.parametrize('endpoint', ['http://fcm.googleapis.com/push', 'https://127.0.0.1/push',
    'https://fcm.googleapis.com.evil.test/push', 'https://fcm.googleapis.com:123/push',
    'https://x:y@fcm.googleapis.com/push', 'https://evil.test/push'])
def test_rejects_arbitrary_push_destinations(client, endpoint):
    assert client.post('/api/pwa/subscribe', json=subscription(endpoint), headers=HEADERS).status_code == 400


def test_rejects_foreign_origin_bad_keys_and_large_payload(client):
    value = subscription()
    assert client.post('/api/pwa/subscribe', json=value).status_code == 400
    assert client.post('/api/pwa/subscribe', json=value, headers={**HEADERS, 'Origin': 'https://evil.test'}).status_code == 403
    bad = {**value, 'keys': {'auth': 'bad', 'p256dh': 'bad'}}
    assert client.post('/api/pwa/subscribe', json=bad, headers=HEADERS).status_code == 400
    assert client.post('/api/pwa/subscribe', json={**value, 'large': 'x'*17000}, headers=HEADERS).status_code == 413


def test_same_origin_https_behind_tls_proxy(client):
    assert client.post('/api/pwa/subscribe', json=subscription(), base_url='http://paranapop.com.br',
        headers={**HEADERS, 'Origin': 'https://paranapop.com.br'}).status_code == 200


def prepare_news():
    now = datetime.utcnow()
    identity = SiteSetting.query.filter_by(key='pwa_identity_v1').one()
    data = json.loads(identity.value)
    data['enabled_since'] = (now - timedelta(hours=2)).isoformat()
    identity.value = json.dumps(data)
    value = subscription()
    db.session.add(PushSubscription(endpoint_hash=hashlib.sha256(value['endpoint'].encode()).hexdigest(),
        endpoint=value['endpoint'], p256dh=value['keys']['p256dh'], auth=value['keys']['auth'],
        created_at=now - timedelta(hours=1)))
    for slug, at in [('new', now - timedelta(minutes=1)), ('old', now - timedelta(hours=3)),
                     ('before-consent', now - timedelta(minutes=90)), ('draft', None),
                     ('scheduled', now + timedelta(hours=1))]:
        db.session.add(Post(title=slug, slug=slug, source='local', published_at=at))
    db.session.commit()


def test_only_new_due_news_once_and_scheduled_news_later(app, monkeypatch):
    send = Mock()
    monkeypatch.setattr('app.pwa.webpush', send)
    with app.app_context():
        prepare_news()
        assert dispatch_news() == 1
        assert json.loads(send.call_args.kwargs['data'])['url'] == '/p/new'
        assert dispatch_news() == 0
        assert send.call_count == 1
        scheduled = Post.query.filter_by(slug='scheduled').one()
        scheduled.published_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        assert dispatch_news() == 1
        assert json.loads(send.call_args.kwargs['data'])['url'] == '/p/scheduled'
        # Editing an article must not alert again.
        scheduled.title = 'Updated title'
        db.session.commit()
        assert dispatch_news() == 0
        assert PushDelivery.query.count() == 2


def test_transport_failure_retries_durably_and_stops_after_three(app, monkeypatch):
    send = Mock(side_effect=WebPushException('temporary'))
    monkeypatch.setattr('app.pwa.webpush', send)
    with app.app_context():
        prepare_news()
        assert dispatch_news() == 0
        assert dispatch_news() == 0  # Backoff prevents immediate retry.
        assert send.call_count == 1
        for _ in range(3):
            delivery = PushDelivery.query.one()
            delivery.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
            assert dispatch_news() == 0
        assert send.call_count == 3
        assert PushDelivery.query.one().sent_at is None


def test_expired_subscription_is_removed_without_blocking_batch(app, monkeypatch):
    response = Mock(status_code=410)
    send = Mock(side_effect=WebPushException('expired', response=response))
    monkeypatch.setattr('app.pwa.webpush', send)
    with app.app_context():
        prepare_news()
        db.session.add(Post(title='Second', slug='second', published_at=datetime.utcnow()))
        db.session.commit()
        assert dispatch_news() == 0
        assert PushSubscription.query.count() == 0
        assert PushDelivery.query.count() == 0
        assert send.call_count == 1


def test_deleting_post_does_not_block_foreign_key_cleanup(app, monkeypatch):
    from sqlalchemy import text
    monkeypatch.setattr('app.pwa.webpush', Mock())
    with app.app_context():
        prepare_news()
        dispatch_news()
        db.session.execute(text('PRAGMA foreign_keys=ON'))
        db.session.delete(Post.query.filter_by(slug='new').one())
        db.session.commit()
        assert PushDelivery.query.count() == 0
