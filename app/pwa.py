"""Installable reader and opt-in Web Push, shared across Gunicorn workers."""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urlsplit

import click
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Blueprint, abort, current_app, jsonify, request, send_from_directory, url_for
from pywebpush import WebPushException, webpush
from sqlalchemy import and_, exists, select, text
from sqlalchemy.exc import IntegrityError

from .models import db, Post, PushDelivery, PushSubscription, SiteSetting

pwa_bp = Blueprint('pwa', __name__)
_dispatch_lock = threading.Lock()
_IDENTITY_KEY = 'pwa_identity_v1'


def _encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _identity():
    return json.loads(SiteSetting.query.filter_by(key=_IDENTITY_KEY).one().value)


def _ensure_identity():
    if SiteSetting.query.filter_by(key=_IDENTITY_KEY).first():
        return
    key = ec.generate_private_key(ec.SECP256R1())
    value = dict(
        private=_encode(key.private_bytes(serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
        public=_encode(key.public_key().public_bytes(serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)),
        enabled_since=datetime.utcnow().isoformat(),
    )
    db.session.add(SiteSetting(key=_IDENTITY_KEY, value=json.dumps(value)))
    try:
        db.session.commit()
    except IntegrityError:  # First boot may have several Gunicorn workers.
        db.session.rollback()


@pwa_bp.get('/manifest.webmanifest')
def manifest():
    response = jsonify({
        'id': '/', 'name': 'Paraná Pop', 'short_name': 'Paraná Pop',
        'description': 'Notícias de Foz do Iguaçu e do Oeste do Paraná na palma da mão.',
        'lang': 'pt-BR', 'start_url': '/?origem=app', 'scope': '/',
        'display': 'standalone', 'background_color': '#02d4fd',
        'theme_color': '#02d4fd', 'categories': ['news'],
        'icons': [
            {'src': f'/static/pwa/icon-{size}.png', 'sizes': f'{size}x{size}',
             'type': 'image/png', 'purpose': 'any'} for size in (192, 512)
        ] + [{'src': '/static/pwa/icon-maskable-512.png', 'sizes': '512x512',
              'type': 'image/png', 'purpose': 'maskable'}],
        'shortcuts': [{'name': 'Últimas notícias', 'url': '/'}],
    })
    response.mimetype = 'application/manifest+json'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@pwa_bp.get('/sw.js')
def service_worker():
    response = send_from_directory(current_app.static_folder, 'pwa/sw.js', mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@pwa_bp.get('/api/pwa/config')
def public_config():
    response = jsonify(publicKey=_identity()['public'])
    response.headers['Cache-Control'] = 'no-store'
    return response


def _subscription_payload():
    # JSON + a custom header requires a CORS preflight for a foreign origin.
    if request.headers.get('X-PWA-Request') != '1' or not request.is_json:
        abort(400)
    origin = request.headers.get('Origin')
    if origin:
        parsed_origin = urlsplit(origin)
        local_http = parsed_origin.scheme == 'http' and parsed_origin.hostname in ('localhost', '127.0.0.1')
        if parsed_origin.netloc != request.host or not (parsed_origin.scheme == 'https' or local_http):
            abort(403)
    if (request.content_length or 0) > 16384 or len(request.get_data()) > 16384:
        abort(413)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    endpoint = payload.get('endpoint')
    keys = payload.get('keys')
    if not isinstance(endpoint, str) or len(endpoint) > 4096 or not isinstance(keys, dict):
        abort(400)
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ''
        allowed = (host == 'fcm.googleapis.com' or host == 'web.push.apple.com'
            or host.endswith('.push.apple.com') or host.endswith('.push.services.mozilla.com')
            or host.endswith('.notify.windows.com'))
        if not allowed or parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
            abort(400)
        public, auth = keys.get('p256dh', ''), keys.get('auth', '')
        if not isinstance(public, str) or not isinstance(auth, str) or len(public) > 180 or len(auth) > 80:
            abort(400)
        decode = lambda val: base64.b64decode(val + '=' * (-len(val) % 4), altchars=b'-_', validate=True)
        if len(decode(auth)) != 16:
            abort(400)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(public))
    except (ValueError, TypeError):
        abort(400)
    return endpoint, public, auth


@pwa_bp.post('/api/pwa/subscribe')
def subscribe():
    endpoint, public, auth = _subscription_payload()
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    existing = PushSubscription.query.filter_by(endpoint_hash=digest).first()
    if existing:
        if not secrets.compare_digest(existing.auth, auth) or existing.p256dh != public:
            abort(403)
    else:
        db.session.add(PushSubscription(endpoint_hash=digest, endpoint=endpoint, p256dh=public, auth=auth))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = PushSubscription.query.filter_by(endpoint_hash=digest).one()
            if not secrets.compare_digest(existing.auth, auth) or existing.p256dh != public:
                abort(403)
    return jsonify(ok=True), 200, {'Cache-Control': 'no-store'}


def _delete_subscription(subscription):
    PushDelivery.query.filter_by(subscription_id=subscription.id).delete()
    db.session.delete(subscription)


@pwa_bp.post('/api/pwa/unsubscribe')
def unsubscribe():
    endpoint, public, auth = _subscription_payload()
    existing = PushSubscription.query.filter_by(endpoint_hash=hashlib.sha256(endpoint.encode()).hexdigest()).first()
    if existing:
        if not secrets.compare_digest(existing.auth, auth) or existing.p256dh != public:
            abort(403)
        _delete_subscription(existing)
        db.session.commit()
    return jsonify(ok=True), 200, {'Cache-Control': 'no-store'}


def _dispatch_batch():
    now = datetime.utcnow()
    identity = _identity()
    # Never send old news or publications predating a reader's consent.
    cutoff = max(datetime.fromisoformat(identity['enabled_since']), now - timedelta(hours=24))
    eligible = and_(Post.published_at >= cutoff, Post.published_at <= now,
                    Post.published_at >= PushSubscription.created_at)
    seen = exists(select(PushDelivery.id).where(
        PushDelivery.post_id == Post.id, PushDelivery.subscription_id == PushSubscription.id))
    pairs = db.session.query(Post.id, PushSubscription.id).join(
        PushSubscription, eligible).filter(~seen).order_by(Post.published_at, Post.id, PushSubscription.id).limit(50).all()
    for post_id, subscription_id in pairs:
        db.session.add(PushDelivery(post_id=post_id, subscription_id=subscription_id, next_attempt_at=now))
    db.session.commit()
    pending = db.session.query(PushDelivery.id).join(Post).join(
        PushSubscription, PushSubscription.id == PushDelivery.subscription_id).filter(
        eligible, PushDelivery.sent_at.is_(None), PushDelivery.attempts < 3,
        PushDelivery.next_attempt_at <= now).order_by(PushDelivery.next_attempt_at, PushDelivery.id).limit(50).all()
    sent = 0
    for (delivery_id,) in pending:
        delivery = db.session.get(PushDelivery, delivery_id)
        if delivery is None:
            continue
        subscription = db.session.get(PushSubscription, delivery.subscription_id)
        post = db.session.get(Post, delivery.post_id)
        # Persist the attempt before network I/O, so a restart resumes later.
        delivery.attempts += 1
        delivery.next_attempt_at = datetime.utcnow() + timedelta(minutes=5 * delivery.attempts)
        db.session.commit()
        try:
            webpush(subscription_info={'endpoint': subscription.endpoint,
                    'keys': {'p256dh': subscription.p256dh, 'auth': subscription.auth}},
                data=json.dumps({'title': unescape(post.title)[:160],
                    'body': unescape(re.sub('<[^>]+>', '', post.excerpt or 'Confira a notícia completa no Paraná Pop.'))[:220],
                    'url': '/p/' + post.slug, 'tag': f'noticia-{post.id}'}, ensure_ascii=False),
                vapid_private_key=identity['private'],
                vapid_claims={'sub': 'https://paranapop.com.br'}, ttl=3600, timeout=8)
            delivery.sent_at = datetime.utcnow()
            sent += 1
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                _delete_subscription(subscription)
            else:
                current_app.logger.warning('PWA push failed: HTTP %s (delivery %s)', status, delivery.id)
        except Exception:
            # Do not log endpoints, keys, or library exceptions containing them.
            current_app.logger.warning('PWA push transport failed (delivery %s)', delivery.id)
        db.session.commit()
    # Receipts older than the delivery window are no longer useful.
    PushDelivery.query.filter(PushDelivery.next_attempt_at < now - timedelta(days=7)).delete()
    db.session.commit()
    return sent


def dispatch_news():
    if not _dispatch_lock.acquire(blocking=False):
        return 0
    connection = None
    locked = False
    try:
        if db.engine.dialect.name == 'postgresql':
            connection = db.engine.connect()
            locked = bool(connection.execute(text('SELECT pg_try_advisory_lock(746219083)')).scalar())
            if not locked:
                return 0
        return _dispatch_batch()
    finally:
        if connection is not None:
            try:
                if locked:
                    connection.execute(text('SELECT pg_advisory_unlock(746219083)'))
            finally:
                connection.close()
        _dispatch_lock.release()


def _worker(app):
    import time
    while True:
        time.sleep(30)
        with app.app_context():
            try:
                dispatch_news()
            except Exception:
                db.session.rollback()
                app.logger.warning('PWA dispatcher will retry on the next cycle')
            finally:
                db.session.remove()


def init_pwa(app, bootstrap_ok):
    app.register_blueprint(pwa_bp)
    if bootstrap_ok:
        with app.app_context():
            _ensure_identity()
    @app.cli.command('push-dispatch')
    def push_dispatch():
        """Run one bounded news delivery batch; also runs automatically."""
        click.echo(f'{dispatch_news()} notification(s) sent.')
    if bootstrap_ok and not app.config.get('TESTING') and os.getenv('PWA_PUSH_WORKER', '1') == '1':
        threading.Thread(target=_worker, args=(app,), daemon=True, name='pwa-news').start()
