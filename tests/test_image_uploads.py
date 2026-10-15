import base64
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app import storage, sync
from app.admin import _save_bot_image
from app.models import MediaAsset


@pytest.fixture(scope='module')
def large_photo():
    with Image.effect_noise((2800, 1800), 60).convert('RGB') as image:
        output = BytesIO()
        image.save(output, 'JPEG', quality=95, exif=Image.Exif())
        return output.getvalue()


@pytest.mark.parametrize('entry', ['panel', 'bot-multipart', 'bot-legacy', 'mirror'])
def test_all_editorial_entries_send_compressed_bytes_and_metadata_to_r2(app, monkeypatch, large_photo, entry):
    uploaded = {}
    def capture(Fileobj, Bucket, Key, ExtraArgs, Config):
        uploaded.update(key=Key, body=Fileobj.read(), mime=ExtraArgs['ContentType'])
        assert ExtraArgs['CacheControl'] == 'public, max-age=31536000, immutable'
    remote = Mock()
    remote.upload_fileobj.side_effect = capture
    monkeypatch.setattr(storage, '_r2_client', lambda: remote)
    app.config.update(R2_BUCKET='bucket', R2_ENDPOINT='https://r2.example', R2_ACCESS_KEY_ID='test',
                      R2_SECRET_ACCESS_KEY='test', R2_PUBLIC_BASE_URL='https://images.example')
    with app.test_request_context('/upload', method='POST', data=(
        {'image': (BytesIO(large_photo), 'phone.jpg')} if entry == 'bot-multipart' else {})):
        if entry == 'panel':
            url = storage.save_file_storage(FileStorage(stream=BytesIO(large_photo), filename='phone.jpg', content_type='image/jpeg'))
        elif entry.startswith('bot'):
            url = _save_bot_image({'image_base64': base64.b64encode(large_photo).decode()}, 'bot')
        else:
            @contextmanager
            def download(_url):
                with BytesIO(large_photo) as stream:
                    stream.media_type = 'image/jpeg'
                    yield stream
            monkeypatch.setattr(sync, 'download_image_temp', download)
            url = sync.download_external_image('https://source.example/phone.jpg')
        assert url == 'https://images.example/' + uploaded['key']
        assert url.endswith('.webp')
        assert uploaded['mime'] == 'image/webp'
        assert len(uploaded['body']) < len(large_photo) * .3
        with Image.open(BytesIO(uploaded['body'])) as image:
            assert image.format == 'WEBP'
            assert image.size == (1280, 823)
        assert MediaAsset.query.one().size_bytes == len(uploaded['body'])


def test_alpha_orientation_metadata_and_size_do_not_regress(app):
    with app.app_context():
        with Image.new('RGBA', (1800, 900), (255, 0, 0, 90)) as image:
            source = BytesIO()
            image.save(source, 'PNG')
        url = storage.save_stream(source, 'test', 'transparent.png', 'image/png')
        with Image.open(storage.local_path_from_url(url)) as image:
            assert image.size == (1280, 640)
            assert image.mode == 'RGBA'
            assert image.getpixel((10, 10))[3] == 90
        with Image.new('RGB', (1800, 900), 'red') as image:
            source = BytesIO()
            exif = Image.Exif()
            exif[274] = 6
            exif[270] = 'Private camera metadata'
            image.save(source, 'JPEG', exif=exif)
        url = storage.save_stream(source, 'test', 'rotated.jpg', 'image/jpeg')
        with Image.open(storage.local_path_from_url(url)) as image:
            assert image.size == (640, 1280)
            assert not image.getexif()


def test_animation_svg_video_and_generator_output_are_preserved(app):
    with app.app_context():
        source = BytesIO()
        with Image.new('RGB', (40, 30), 'red') as first, Image.new('RGB', (40, 30), 'blue') as second:
            first.save(source, 'GIF', save_all=True, append_images=[second], duration=100, loop=0)
        original = source.getvalue()
        url = storage.save_stream(source, 'test', 'animated.gif', 'image/gif')
        assert storage.local_path_from_url(url).read_bytes() == original
        for name, mime, body in [('logo.svg', 'image/svg+xml', b'<svg/>'), ('video.mp4', 'video/mp4', b'video')]:
            url = storage.save_stream(BytesIO(body), 'test', name, mime)
            assert storage.local_path_from_url(url).read_bytes() == body
        source = BytesIO()
        with Image.new('RGB', (1080, 1920), 'blue') as image:
            image.save(source, 'PNG')
        url = storage.save_stream(source, 'gerador/generated', 'story.png', 'image/png', optimize_image=False)
        assert storage.local_path_from_url(url).read_bytes() == source.getvalue()
        assert not source.closed


def test_limits_and_corrupt_images_reject_before_storage(app, large_photo):
    with app.app_context():
        app.config['IMAGE_MAX_BYTES'] = 10
        with pytest.raises(ValueError, match='tamanho'):
            storage.save_stream(BytesIO(large_photo), 'test', 'large.jpg', 'image/jpeg')
        app.config.update(IMAGE_MAX_BYTES=15_000_000, IMAGE_MAX_PIXELS=100)
        with pytest.raises(ValueError, match='pixels'):
            storage.save_file_storage(FileStorage(stream=BytesIO(large_photo), filename='large.jpg'))
        with pytest.raises(ValueError, match='inválido'):
            storage.save_stream(BytesIO(b'invalid'), 'test', 'bad.jpg', 'image/jpeg')
        assert MediaAsset.query.count() == 0


def test_already_small_webp_never_grows_and_mime_does_not_depend_on_system_database(app, monkeypatch):
    with app.app_context():
        source = BytesIO()
        with Image.new('RGB', (100, 100), 'red') as image:
            image.save(source, 'WEBP', quality=20)
        original = source.getvalue()
        url = storage.save_stream(source, 'test', 'small.webp', 'image/webp')
        assert storage.local_path_from_url(url).stat().st_size <= len(original)
        monkeypatch.setattr(storage.mimetypes, 'guess_type', lambda _: (None, None))
        assert storage._content_type_for_name('photo.webp') == 'image/webp'


def test_external_images_are_not_misidentified_as_existing_r2_objects(app):
    with app.app_context():
        app.config['R2_PUBLIC_BASE_URL'] = 'https://images.example'
        assert storage.key_from_media_url('https://source.example/photo.jpg') is None
        assert storage.key_from_media_url('https://images.example/photo.jpg') == 'photo.jpg'
        assert storage.key_from_media_url('/media/photo.jpg') == 'photo.jpg'


def test_real_boto_transfer_accepts_compressed_temporary_stream(app, monkeypatch, large_photo):
    import boto3
    from botocore.stub import Stubber
    remote = boto3.client('s3', region_name='auto', aws_access_key_id='test', aws_secret_access_key='test')
    monkeypatch.setattr(storage, '_r2_client', lambda: remote)
    monkeypatch.setattr(storage, 'using_r2', lambda: True)
    app.config.update(R2_BUCKET='bucket')
    with app.app_context(), Stubber(remote) as stub:
        stub.add_response('put_object', {'ETag': '"test"'})
        url = storage.save_file_storage(FileStorage(stream=BytesIO(large_photo), filename='phone.jpg', content_type='image/jpeg'))
        assert url.endswith('.webp')
        assert not storage.local_path_from_url(url).exists()
        stub.assert_no_pending_responses()
