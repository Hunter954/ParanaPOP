from datetime import datetime, timedelta

from app.models import AnalyticsSession, User, db


def test_online_requires_admin(app):
    client = app.test_client()
    assert client.get('/admin/api/online').status_code == 302
    with app.app_context():
        user = User(email='reader@example.com', is_admin=False)
        user.set_password('test')
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    response = client.get('/admin/api/online')
    assert response.status_code == 302
    assert response.headers['Location'] == '/'


def test_online_unique_visitors_and_expiry(app, client):
    with app.app_context():
        now = datetime.utcnow()
        for sid, visitor, age in [('a', 'same', 5), ('b', 'same', 10),
                                  ('c', 'other', 15), ('d', 'gone', 100)]:
            db.session.add(AnalyticsSession(session_id=sid, visitor_id=visitor,
                                           updated_at=now - timedelta(seconds=age)))
        db.session.commit()
    response = client.get('/admin/api/online')
    assert response.status_code == 200
    assert response.json == {'online': 2, 'window_seconds': 90}
    assert 'no-store' in response.headers['Cache-Control']


def test_heartbeat_restores_online_without_extra_pageview(app, client):
    with app.app_context():
        db.session.add(AnalyticsSession(session_id='reader', visitor_id='visitor',
                       updated_at=datetime.utcnow() - timedelta(minutes=5), pageviews=1))
        db.session.commit()
    assert client.get('/admin/api/online').json['online'] == 0
    response = app.test_client().post('/analytics/collect', json={
        'event': 'heartbeat', 'session_id': 'reader', 'visitor_id': 'visitor',
        'duration_seconds': 45, 'page_path': '/',
    })
    assert response.status_code == 200
    assert client.get('/admin/api/online').json['online'] == 1
    with app.app_context():
        assert AnalyticsSession.query.filter_by(session_id='reader').one().pageviews == 1


def test_dashboard_has_live_counter(client):
    response = client.get('/admin/')
    assert response.status_code == 200
    assert b'data-online-url="/admin/api/online"' in response.data
    assert b'online-dashboard.js' in response.data
