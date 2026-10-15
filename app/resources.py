"""Bound expensive work before parsing large request bodies."""
import threading
from contextlib import contextmanager
from functools import wraps
from flask import g, jsonify, request

_generation_lock = threading.RLock()


class GenerationBusy(RuntimeError):
    pass


@contextmanager
def generation_slot():
    if not _generation_lock.acquire(blocking=False):
        raise GenerationBusy('Já há uma geração em andamento. Aguarde e tente novamente.')
    try:
        yield
    finally:
        _generation_lock.release()


def bounded_generation(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with generation_slot():
            return fn(*args, **kwargs)
    return wrapped


def install_resource_guards(app):
    @app.before_request
    def admit_generation():
        endpoint = request.endpoint or ''
        heavy = endpoint.startswith('admin.bot_generate_') or endpoint == 'admin.bot_publish_api' or endpoint == 'site.gerador_pop'
        if request.method == 'POST' and heavy:
            if not _generation_lock.acquire(blocking=False):
                return jsonify(ok=False, message='Já há uma geração em andamento. Aguarde e tente novamente.'), 429, {'Retry-After': '10'}
            g.generation_admitted = True

    @app.teardown_request
    def release_generation(_error):
        if g.pop('generation_admitted', False):
            _generation_lock.release()

    @app.after_request
    def media_capability(response):
        if (request.endpoint or '').startswith('admin.bot_'):
            response.headers['X-Media-Upload'] = 'multipart'
        return response


def memory_snapshot():
    import gc
    import resource
    from pathlib import Path
    rss = None
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                rss = int(line.split()[1]) * 1024
    except OSError:
        pass
    return {'rss_bytes': rss, 'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            'gc_counts': gc.get_count()}


def log_database_resources(app):
    """Read-only diagnostic: no settings, history or extensions are changed."""
    from sqlalchemy import text
    from .models import db
    if db.engine.dialect.name != 'postgresql':
        return
    try:
        with db.engine.connect() as conn:
            settings = [dict(row) for row in conn.execute(text(
                "SELECT name, setting, unit FROM pg_settings WHERE name IN "
                "('shared_buffers','work_mem','maintenance_work_mem','max_connections')")).mappings()]
            connections = [dict(row) for row in conn.execute(text(
                "SELECT coalesce(state,'unknown') AS state,count(*) AS count FROM pg_stat_activity "
                "WHERE datname=current_database() GROUP BY state")).mappings()]
        app.logger.warning('DB_RESOURCE_SNAPSHOT %s', {'settings': settings, 'connections': connections})
    except Exception:
        app.logger.warning('DB_RESOURCE_SNAPSHOT unavailable')
