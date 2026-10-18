from __future__ import annotations

import mimetypes
import os
import threading
import shutil
from functools import partial
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse
import re
from uuid import uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from flask import Response, current_app, send_file, send_from_directory, redirect, request
from sqlalchemy import desc
from werkzeug.http import dump_options_header
from .image_uploads import prepared_image
from .models import MediaAsset
from .media_index import media_scope, record_asset, remove_asset
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

_CLIENT_LOCK = threading.Lock()
_TRANSFER_CONFIG = TransferConfig(max_concurrency=1, use_threads=False, max_io_queue=2)

_ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".svg", ".avif", ".heic", ".heif"}


class _UploadStream:
    """boto may close its input; the caller owns the underlying upload."""
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        pass


@dataclass
class MediaItem:
    key: str
    name: str
    url: str
    size_kb: int
    updated_at: datetime | None = None


def using_r2() -> bool:
    cfg = current_app.config
    return bool(
        cfg.get("R2_BUCKET")
        and cfg.get("R2_ENDPOINT")
        and cfg.get("R2_ACCESS_KEY_ID")
        and cfg.get("R2_SECRET_ACCESS_KEY")
    )


def _reject_incomplete_r2() -> None:
    if current_app.config.get('R2_BUCKET') and not using_r2():
        raise RuntimeError('R2 configurado sem credenciais completas; upload local bloqueado')


def media_root() -> Path:
    path = Path(current_app.config["MEDIA_ROOT"]).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_prefix() -> str:
    return current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/")


def public_media_base() -> str:
    return (current_app.config.get("R2_PUBLIC_BASE_URL") or "").strip().rstrip("/")


def public_media_enabled() -> bool:
    return bool(using_r2() and public_media_base())


def public_media_url(key: str) -> str:
    key = (key or "").lstrip("/")
    base = public_media_base()
    if base:
        return f"{base}/{key}"
    return f"{media_prefix()}/{key}"


def media_url_from_key(key: str) -> str:
    key = (key or "").lstrip("/")
    if public_media_enabled():
        return public_media_url(key)
    return f"{media_prefix()}/{key}"


def key_from_media_url(url: str) -> str | None:
    if not url:
        return None
    value = (url or "").strip()
    parsed = urlparse(value)
    target_path = parsed.path or value
    prefix = media_prefix() + "/"
    public_prefix = (public_media_base() + "/") if public_media_base() else ""

    if public_prefix and value.startswith(public_prefix):
        rel = value[len(public_prefix):].lstrip("/")
        return rel or None
    if target_path.startswith(prefix):
        rel = target_path[len(prefix):].lstrip("/")
        return rel or None
    if (parsed.scheme and public_prefix
            and parsed.netloc.lower() == urlparse(public_media_base()).netloc.lower()
            and target_path.startswith(urlparse(public_media_base()).path + "/")):
        rel = target_path[len(urlparse(public_media_base()).path) + 1:].lstrip("/")
        return rel or None
    return None


def is_managed_media_url(url: str) -> bool:
    return key_from_media_url(url) is not None


def normalize_media_url(url: str) -> str:
    if not url:
        return ""
    key = key_from_media_url(url)
    if key:
        return media_url_from_key(key)
    return url


def rewrite_media_urls(value: str) -> str:
    if not value:
        return value
    base = public_media_base()
    if not base:
        return value
    prefix = media_prefix().rstrip("/")
    result = value.replace(f'="{prefix}/', f'="{base}/')
    result = result.replace(f"='{prefix}/", f"='{base}/")
    result = result.replace(f'("{prefix}/', f'("{base}/')
    result = result.replace(f"('{prefix}/", f"('{base}/")
    result = re.sub(r'(?<![A-Za-z0-9_\-])' + re.escape(prefix) + r'/', base + '/', result)
    return result


def local_path_from_url(url: str) -> Path | None:
    key = key_from_media_url(url)
    if not key:
        return None
    root = media_root()
    candidate = (root / key).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _file_ext(filename: str) -> str:
    name = secure_filename(filename or "")
    _, ext = os.path.splitext(name)
    return ext.lower()


def _guess_extension(filename: str = "", content_type: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in {".mp4", ".mov", ".webm", ".m4v"}:
        return ext
    if ext in _ALLOWED_IMAGE_EXTS:
        return ".jpg" if ext == ".jpeg" else ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in {".jpe", ".jpeg"}:
        return ".jpg"
    if guessed in _ALLOWED_IMAGE_EXTS | {".mp4", ".mov", ".webm", ".m4v"}:
        return guessed
    return ".bin"


def _content_type_for_name(filename: str, fallback: str = "application/octet-stream") -> str:
    if Path(filename).suffix.lower() == ".webp":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def _new_r2_client():
    cfg = current_app.config
    return boto3.client(
        "s3",
        endpoint_url=cfg["R2_ENDPOINT"].strip(),
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"].strip(),
        region_name=cfg.get("R2_REGION", "auto").strip(),
        config=BotoConfig(signature_version="s3v4"),
    )



def _r2_client():
    app = current_app._get_current_object()
    with _CLIENT_LOCK:
        if 'r2_client' not in app.extensions:
            app.extensions['r2_client'] = _new_r2_client()
        return app.extensions['r2_client']


def save_stream(stream: BinaryIO, folder: str, filename_hint='file', content_type='', *, optimize_image=True) -> str:
    with prepared_image(stream, filename_hint, content_type, enabled=optimize_image) as prepared:
        return _save_stream(prepared[0], folder, prepared[1], prepared[2])


def _save_stream(stream: BinaryIO, folder: str, filename_hint='file', content_type='') -> str:
    """Upload without reading the entire file into a Python bytes object."""
    ext = _guess_extension(filename_hint, content_type)
    key = f"{folder.strip('/')}/{uuid4().hex}{ext}"
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    if using_r2():
        _r2_client().upload_fileobj(_UploadStream(stream), current_app.config['R2_BUCKET'], key,
            ExtraArgs={'ContentType': content_type or _content_type_for_name(filename_hint),
                       'CacheControl': 'public, max-age=31536000, immutable'},
            Config=_TRANSFER_CONFIG)
    else:
        _reject_incomplete_r2()
        target = media_root() / key
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as output:
            shutil.copyfileobj(stream, output, length=64 * 1024)
    record_asset(key, size)
    return media_url_from_key(key)


def save_file_storage(file_storage: FileStorage | None, subdir: str = "general") -> str:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return ""
    # Preserve validation before interpreting the supplied filename/type.
    if _file_ext(file_storage.filename) not in _ALLOWED_IMAGE_EXTS:
        raise ValueError("Formato de imagem não permitido")
    with prepared_image(file_storage.stream, file_storage.filename, file_storage.mimetype) as prepared:
        upload = FileStorage(stream=prepared[0], filename=prepared[1], content_type=prepared[2])
        return _save_file_storage(upload, subdir)


def _save_file_storage(file_storage: FileStorage | None, subdir: str = "general") -> str:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return ""

    original_name = secure_filename(file_storage.filename or "")
    ext = _file_ext(original_name).lower()
    if not ext or ext not in _ALLOWED_IMAGE_EXTS:
        raise ValueError("Formato de imagem não permitido. Use JPG, JPEG, JFIF, PNG, WEBP, GIF, SVG, AVIF, HEIC ou HEIF.")

    key = f"{subdir.strip('/')}/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid4().hex}{ext}".lstrip("/")
    content_type = getattr(file_storage, "mimetype", "") or _content_type_for_name(original_name)

    # Always rewind before handing the stream to a storage backend. Some
    # validators inspect the upload first and leave the pointer away from zero.
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass

    if using_r2():
        extra_args = {"ContentType": content_type, "CacheControl": "public, max-age=31536000, immutable"}
        _r2_client().upload_fileobj(
            Fileobj=_UploadStream(file_storage.stream),
            Bucket=current_app.config["R2_BUCKET"].strip(),
            Key=key,
            ExtraArgs=extra_args,
            Config=_TRANSFER_CONFIG,
        )
        file_storage.stream.seek(0, 2)
        record_asset(key, file_storage.stream.tell())
        return media_url_from_key(key)

    # A bucket outage must fail the upload, not create a second local source.
    _reject_incomplete_r2()

    root = media_root()
    full_path = root / key
    full_path.parent.mkdir(parents=True, exist_ok=True)
    file_storage.save(full_path)
    record_asset(key, full_path.stat().st_size)
    return f"{media_prefix()}/{key}"


def save_bytes(content: bytes, folder: str, filename_hint: str = "file", content_type: str = "") -> str:
    if not content:
        return ""
    ext = _guess_extension(filename_hint, content_type)
    key = f"{folder.strip('/')}/{uuid4().hex}{ext}".lstrip("/")
    final_content_type = content_type or _content_type_for_name(filename_hint)

    if using_r2():
        extra_args = {"ContentType": final_content_type} if final_content_type else None
        _r2_client().put_object(
            Bucket=current_app.config["R2_BUCKET"].strip(),
            Key=key,
            Body=content,
            **(extra_args or {}),
        )
        record_asset(key, len(content))
        return media_url_from_key(key)

    _reject_incomplete_r2()
    target_dir = media_root() / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid4().hex}{ext}"
    target_path.write_bytes(content)
    record_asset(target_path.relative_to(media_root()).as_posix(), len(content))
    return media_url_from_key(target_path.relative_to(media_root()).as_posix())


def delete_media(url: str) -> None:
    key = key_from_media_url(url)
    if not key:
        return

    if using_r2():
        try:
            _r2_client().delete_object(Bucket=current_app.config["R2_BUCKET"].strip(), Key=key)
            remove_asset(key)
        except Exception:
            pass
        return

    path = media_root() / key
    if path.exists() and path.is_file():
        try:
            path.unlink()
            remove_asset(key)
        except Exception:
            pass


def media_exists(url: str) -> bool:
    key = key_from_media_url(url)
    if not key:
        return False

    if using_r2():
        try:
            _r2_client().head_object(Bucket=current_app.config["R2_BUCKET"].strip(), Key=key)
            return True
        except Exception:
            return False

    path = media_root() / key
    return path.exists() and path.is_file()


def list_media_files(limit: int = 48, offset: int = 0) -> list[MediaItem]:
    rows = MediaAsset.query.filter_by(scope=media_scope()).order_by(
        desc(MediaAsset.updated_at), desc(MediaAsset.id)).offset(max(0, offset)).limit(min(limit or 48, 200)).all()
    return [MediaItem(key=row.key, name=Path(row.key).name, url=media_url_from_key(row.key),
                      size_kb=max(1, round(row.size_bytes / 1024)), updated_at=row.updated_at)
            for row in rows]


def open_media_bytes(url_or_key: str) -> tuple[bytes, str, str]:
    key = key_from_media_url(url_or_key) or (url_or_key or "").lstrip("/")
    if not key:
        raise FileNotFoundError("Arquivo não encontrado")

    if using_r2():
        try:
            obj = _r2_client().get_object(Bucket=current_app.config["R2_BUCKET"].strip(), Key=key)
        except ClientError as exc:
            raise FileNotFoundError(key) from exc
        limit = current_app.config.get('IMAGE_MAX_BYTES', 15_000_000)
        with obj['Body'] as stream:
            body = stream.read(limit + 1)
        if len(body) > limit:
            raise ValueError('Imagem excede o limite permitido')
        content_type = obj.get("ContentType") or _content_type_for_name(key)
        return body, content_type, Path(key).name

    path = media_root() / key
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(key)
    with path.open('rb') as stream:
        body = stream.read(current_app.config.get('IMAGE_MAX_BYTES', 15_000_000) + 1)
    if len(body) > current_app.config.get('IMAGE_MAX_BYTES', 15_000_000):
        raise ValueError('Imagem excede o limite permitido')
    return body, _content_type_for_name(path.name), path.name


def _r2_response(key, download_name=None):
    params = {'Bucket': current_app.config['R2_BUCKET'], 'Key': key}
    if request.headers.get('Range'):
        params['Range'] = request.headers['Range']
    client = _r2_client()
    try:
        obj = client.get_object(**params)
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'InvalidRange':
            return Response(status=416, headers={'Accept-Ranges': 'bytes'})
        raise FileNotFoundError(key) from exc
    body = obj['Body']
    def chunks():
        try:
            yield from iter(partial(body.read, 64 * 1024), b'')
        finally:
            body.close()
    response = Response(chunks(), status=206 if obj.get('ContentRange') else 200,
                        mimetype=obj.get('ContentType') or _content_type_for_name(key))
    response.call_on_close(body.close)
    response.headers['Content-Length'] = str(obj['ContentLength'])
    response.headers['Accept-Ranges'] = 'bytes'
    if obj.get('ContentRange'):
        response.headers['Content-Range'] = obj['ContentRange']
    if obj.get('ETag'):
        response.headers['ETag'] = obj['ETag']
    if download_name:
        response.headers['Content-Disposition'] = dump_options_header('attachment', {'filename': download_name})
    return response


def send_media(filename: str) -> Response:
    key = (filename or '').lstrip('/')
    if public_media_enabled():
        return redirect(public_media_url(key), code=302)
    if using_r2():
        return _r2_response(key)
    return send_from_directory(media_root(), key)


def send_media_download(key: str, download_name: str | None = None) -> Response:
    key = (key or '').lstrip('/')
    name = download_name or Path(key).name
    if using_r2():
        return _r2_response(key, name)
    return send_from_directory(media_root(), key, as_attachment=True, download_name=name)
