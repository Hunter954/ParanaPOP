import base64
import io
import threading
from datetime import datetime, timedelta, date
from unittest.mock import Mock
from PIL import Image
from sqlalchemy import event
from app.models import db, AnalyticsSession, Post, MediaAsset
from app.admin import _analytics_stats
from app import storage


def photo_bytes():
    out = io.BytesIO()
    Image.new('RGB', (120, 80), 'red').save(out, 'JPEG')
    return out.getvalue()


def test_analytics_preserves_boundaries_distinct_users_and_empty_days(app):
    start = datetime(2026, 10, 1)
    with app.app_context():
        records = [
            (start-timedelta(seconds=1), 'old', 5, 15, True, True),
            (start, 'a', 2, 10, False, True),
            (start+timedelta(hours=1), 'a', 1, 20, True, False),
            (start+timedelta(days=1), 'b', 4, 60, False, True),
            (start+timedelta(days=1), '', 1, 0, True, True),
            (start+timedelta(days=3), 'outside', 8, 90, True, True),
        ]
        for idx, (at, visitor, views, duration, bounce, new) in enumerate(records):
            db.session.add(AnalyticsSession(session_id=str(idx), visitor_id=visitor,
                created_at=at, pageviews=views, duration_seconds=duration, is_bounce=bounce, is_new_user=new))
        db.session.commit()
        result = _analytics_stats(start_date=date(2026,10,1), end_date=date(2026,10,3))
        assert result['current'] == dict(sessions=4,pageviews=8,total_users=2,avg_duration=22,bounce_rate=50.,new_users=2)
        assert result['previous']['sessions'] == 1
        assert result['daily_series'][0]['total_users'] == 1
        assert result['daily_series'][1]['total_users'] == 1
        assert result['daily_series'][2]['sessions'] == 0


def test_post_list_is_paginated_and_does_not_load_article_bodies_or_analytics(app, client, monkeypatch):
    import app.admin as admin
    monkeypatch.setattr(admin, '_analytics_stats', Mock(side_effect=AssertionError('analytics loaded')))
    with app.app_context():
        db.session.add_all([Post(title=f'Title {i}', slug=f'post-{i}', source='local', content_html='x'*10000,
                                published_at=datetime(2026,1,1)+timedelta(days=i)) for i in range(85)])
        db.session.commit()
        statements=[]
        def collect(_conn,_cursor,statement,*_args): statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', collect)
        response=client.get('/admin/posts?page=2&q=Title&source=local')
        assert response.status_code == 200
        assert response.data.count(b'onsubmit="return confirm') == 40
        assert b'page=3' in response.data
        assert not any('post.content_html' in sql for sql in statements)
        assert not any('analytics_session' in sql.lower() for sql in statements)
        event.remove(db.engine, 'before_cursor_execute', collect)


def test_media_catalog_and_stream_upload_do_not_list_bucket(app, client, monkeypatch):
    with app.app_context():
        url=storage.save_stream(io.BytesIO(b'content'), 'test', 'file.mp4', 'video/mp4')
        assert url.endswith('.mp4')
        assert MediaAsset.query.count()==1
        monkeypatch.setattr(storage, '_r2_client', Mock(side_effect=AssertionError('remote listing')))
        assert storage.list_media_files(limit=8)[0].size_kb == 1
        assert client.get('/admin/media').status_code==200


def test_multipart_and_legacy_photo_requests_generate_only_selected_format(app, client):
    headers={'X-Bot-Token':'test-token'}
    response=client.post('/admin/api/whatsapp-bot/generate-photo', headers=headers,
        data={'title':'Título de teste','category':'Cidade','formats':'feed', 'image':(io.BytesIO(photo_bytes()),'test.jpg')})
    assert response.status_code==200, response.json
    assert [x['key'] for x in response.json['images']]==['feed']
    with app.app_context():
        assert MediaAsset.query.count()==2
    legacy=client.post('/admin/api/whatsapp-bot/generate-trivox-photo', headers=headers,
        json={'title':'Título de teste','image_base64':base64.b64encode(photo_bytes()).decode()})
    assert legacy.status_code==200, legacy.json
    assert client.options('/admin/api/whatsapp-bot/generate-photo').headers['X-Media-Upload']=='multipart'


def test_upload_limits_and_busy_reject_without_consuming_request_body(app, client):
    app.config['IMAGE_MAX_BYTES']=20
    response=client.post('/admin/api/whatsapp-bot/generate-photo', headers={'X-Bot-Token':'test-token'},
        data={'title':'Título','category':'Cidade','image':(io.BytesIO(photo_bytes()),'test.jpg')})
    assert response.status_code in (400,413)
    from app.resources import generation_slot
    ready=threading.Event();release=threading.Event()
    def occupy():
        with generation_slot():
            ready.set();release.wait(5)
    worker=threading.Thread(target=occupy);worker.start();ready.wait(5)
    try:
        response=client.post('/admin/api/whatsapp-bot/generate-video', data=b'not-json')
        assert response.status_code==429
        assert response.headers['Retry-After']=='10'
    finally:
        release.set();worker.join()


def test_private_r2_download_streams_range_and_closes_body(app, monkeypatch):
    class Body(io.BytesIO):
        reads=[]
        def read(self,size=-1):
            assert size==65536
            self.reads.append(size)
            return super().read(size)
    body=Body(b'part')
    fake=Mock();fake.get_object.return_value={'Body':body,'ContentLength':4,'ContentType':'video/mp4','ContentRange':'bytes 0-3/20'}
    monkeypatch.setattr(storage,'_r2_client',lambda:fake)
    monkeypatch.setattr(storage,'using_r2',lambda:True)
    with app.test_request_context('/media/video.mp4',headers={'Range':'bytes=0-3'}):
        response=storage.send_media('video.mp4')
        assert response.status_code==206
        assert response.get_data()==b'part'
        response.close()
        assert body.closed
        assert fake.get_object.call_args.kwargs['Range']=='bytes=0-3'


def test_art_reuses_requested_variants_within_one_job(app):
    from app.art_generator import generate_variants
    with app.app_context():
        options=dict(title='Notícia teste',image_source='',include_title=True,category_text='Cidade')
        first=generate_variants(**options,variants={'feed'})
        second=generate_variants(**options,variants={'feed','stories'})
        assert first[0]['url']==second[0]['url']
        assert MediaAsset.query.count()==2


def test_catalog_reconciliation_is_paginated_and_resumable(app, monkeypatch):
    from app.media_index import reconcile_media
    from app.models import MediaIndexState
    fake=Mock()
    fake.list_objects_v2.side_effect=[
        {'Contents':[{'Key':'a.jpg','Size':10,'LastModified':datetime(2026,1,1)}],
         'IsTruncated':True,'NextContinuationToken':'next-page'},
        RuntimeError('interrupted'),
    ]
    monkeypatch.setattr(storage,'using_r2',lambda:True)
    monkeypatch.setattr(storage,'_r2_client',lambda:fake)
    with app.app_context():
        app.config.update(R2_BUCKET='test',R2_ENDPOINT='https://r2.example')
        import pytest
        with pytest.raises(RuntimeError):reconcile_media(app)
        db.session.remove()
        assert MediaIndexState.query.one().cursor=='next-page'
        fake.list_objects_v2.side_effect=None
        fake.list_objects_v2.return_value={'Contents':[{'Key':'b.jpg','Size':20,'LastModified':datetime(2026,1,2)}],'IsTruncated':False}
        reconcile_media(app)
        assert fake.list_objects_v2.call_args.kwargs['ContinuationToken']=='next-page'
        assert [item.name for item in storage.list_media_files()]==['b.jpg','a.jpg']
        db.session.remove()
        assert MediaIndexState.query.one().complete


def test_real_ffmpeg_generates_both_brands_without_output_bytes_buffer(app, tmp_path):
    import subprocess,json
    from app.video_generator import generate_paranapop_stories_video,generate_trivox_video
    source=tmp_path/'input.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x240:rate=5',
                    '-t','0.4','-c:v','libx264','-threads','1','-y',str(source)],check=True)
    with app.app_context():
        for generate,extra in [(generate_paranapop_stories_video,{'category_text':'Cidade'}),(generate_trivox_video,{})]:
            with source.open('rb') as stream:
                result=generate(video_stream=stream,title='Vídeo de teste',**extra)
            target=storage.local_path_from_url(result['url'])
            probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(target)]))
            video=next(s for s in probe['streams'] if s['codec_type']=='video')
            assert (video['width'],video['height'])==(1080,1920)
            assert result['url'].endswith('.mp4')


def test_admin_pages_and_health_smoke(app,client):
    for route in ['/admin/','/admin/settings','/admin/users','/admin/posts/new','/admin/categories',
                  '/admin/insights','/admin/media','/admin/bot-publicar','/admin/whatsapp','/admin/api/resources','/health']:
        response=client.get(route)
        assert response.status_code==200,(route,response.status_code)
    assert client.get('/health').json['memory_optimized']
