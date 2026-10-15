"""Resumable bounded metadata reconciliation, never part of an HTTP listing."""
import hashlib
import threading
import time
from datetime import datetime
from pathlib import Path
from sqlalchemy import text
from .models import db, MediaIndexState


def media_scope():
    from flask import current_app
    from .storage import using_r2
    value = (current_app.config.get('R2_ENDPOINT', '') + '/' + current_app.config.get('R2_BUCKET', '')) if using_r2() else current_app.config['MEDIA_ROOT']
    return hashlib.sha256(value.encode()).hexdigest()


def record_asset(key, size, updated=None):
    # Independent transaction: indexing must not commit pending editorial changes.
    with db.engine.begin() as conn:
        conn.execute(text('INSERT INTO media_asset (scope, key, size_bytes, updated_at) '
                          'VALUES (:scope, :key, :size, :updated) '
                          'ON CONFLICT (scope, key) DO UPDATE SET '
                          'size_bytes=excluded.size_bytes, updated_at=excluded.updated_at'),
                     {'scope': media_scope(), 'key': key, 'size': size,
                      'updated': (updated.replace(tzinfo=None) if updated else datetime.utcnow())})


def remove_asset(key):
    with db.engine.begin() as conn:
        conn.execute(text('DELETE FROM media_asset WHERE scope=:scope AND key=:key'),
                     {'scope': media_scope(), 'key': key})


def reconcile_media(app, refresh=False):
    from .storage import using_r2, _r2_client, media_root
    with app.app_context():
        scope = media_scope()
        state = db.session.get(MediaIndexState, scope)
        if state and state.complete and not refresh:
            return
        token = state.cursor if state and not refresh else None
        if using_r2():
            while True:
                params = {'Bucket': app.config['R2_BUCKET'], 'MaxKeys': 500}
                if token:
                    params['ContinuationToken'] = token
                resp = _r2_client().list_objects_v2(**params)
                rows = [{'scope': scope, 'key': obj['Key'], 'size': obj.get('Size', 0),
                         'updated': obj['LastModified'].replace(tzinfo=None)}
                        for obj in resp.get('Contents', []) if not obj['Key'].endswith('/')]
                token = resp.get('NextContinuationToken') if resp.get('IsTruncated') else None
                with db.engine.begin() as conn:
                    if rows:
                        # A concurrent upload owns its newer metadata.
                        conn.execute(text('INSERT INTO media_asset (scope,key,size_bytes,updated_at) '
                            'VALUES (:scope,:key,:size,:updated) ON CONFLICT (scope,key) DO NOTHING'), rows)
                    conn.execute(text('INSERT INTO media_index_state (scope,cursor,complete) VALUES (:scope,:cursor,:done) '
                        'ON CONFLICT (scope) DO UPDATE SET cursor=excluded.cursor,complete=excluded.complete'),
                        {'scope': scope, 'cursor': token, 'done': token is None})
                db.session.remove()
                if not token:
                    break
                time.sleep(.05)
        else:
            for path in media_root().rglob('*'):
                if path.is_file():
                    stat = path.stat()
                    record_asset(path.relative_to(media_root()).as_posix(), stat.st_size,
                                 datetime.fromtimestamp(stat.st_mtime))
            with db.engine.begin() as conn:
                conn.execute(text('INSERT INTO media_index_state (scope,cursor,complete) VALUES (:scope,NULL,:done) '
                    'ON CONFLICT (scope) DO UPDATE SET complete=excluded.complete'), {'scope': scope, 'done': True})


def start_media_index(app):
    def run():
        try:
            reconcile_media(app)
        except Exception:
            app.logger.exception('Media metadata reconciliation interrupted; resume on next startup or index-media command')
    threading.Thread(target=run, name='media-index', daemon=True).start()
