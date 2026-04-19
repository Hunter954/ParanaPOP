from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse
from uuid import uuid4

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from flask import Response, current_app, send_file, send_from_directory
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

_ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif"}


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


def media_root() -> Path:
    path = Path(current_app.config["MEDIA_ROOT"]).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_prefix() -> str:
    return current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/")


def media_url_from_key(key: str) -> str:
    return f"{media_prefix()}/{key.lstrip('/')}"


def key_from_media_url(url: str) -> str | None:
    if not url:
        return None
    prefix = media_prefix() + "/"
    parsed = urlparse(url)
    target_path = parsed.path or url
    if not target_path.startswith(prefix):
        return None
    rel = target_path[len(prefix) :].lstrip("/")
    return rel or None


def local_path_from_url(url: str) -> Path | None:
    key = key_from_media_url(url)
    if not key:
        return None
    return media_root() / key


def _file_ext(filename: str) -> str:
    name = secure_filename(filename or "")
    _, ext = os.path.splitext(name)
    return ext.lower()


def _guess_extension(filename: str = "", content_type: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in _ALLOWED_IMAGE_EXTS:
        return ".jpg" if ext == ".jpeg" else ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in {".jpe", ".jpeg"}:
        return ".jpg"
    if guessed in _ALLOWED_IMAGE_EXTS:
        return guessed
    return ".bin"


def _content_type_for_name(filename: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def _r2_client():
    cfg = current_app.config
    return boto3.client(
        "s3",
        endpoint_url=cfg["R2_ENDPOINT"].strip(),
        aws_access_key_id=cfg["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"].strip(),
        region_name=cfg.get("R2_REGION", "auto").strip(),
        config=BotoConfig(signature_version="s3v4"),
    )


def save_file_storage(file_storage: FileStorage | None, subdir: str = "general") -> str:
    if not file_storage or not getattr(file_storage, "filename", ""):
        return ""

    ext = _file_ext(file_storage.filename) or ".bin"
    key = f"{subdir.strip('/')}/{datetime.utcnow().strftime('%Y/%m/%d')}/{uuid4().hex}{ext}".lstrip("/")
    content_type = getattr(file_storage, "mimetype", "") or _content_type_for_name(file_storage.filename)

    if using_r2():
        extra_args = {"ContentType": content_type} if content_type else None
        _r2_client().upload_fileobj(
            Fileobj=file_storage,
            Bucket=current_app.config["R2_BUCKET"].strip(),
            Key=key,
            ExtraArgs=extra_args or {},
        )
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass
        return media_url_from_key(key)

    folder = media_root() / subdir / datetime.utcnow().strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    full_path = folder / f"{uuid4().hex}{ext}"
    file_storage.save(full_path)
    return media_url_from_key(full_path.relative_to(media_root()).as_posix())


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
        return media_url_from_key(key)

    target_dir = media_root() / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid4().hex}{ext}"
    target_path.write_bytes(content)
    return media_url_from_key(target_path.relative_to(media_root()).as_posix())


def delete_media(url: str) -> None:
    key = key_from_media_url(url)
    if not key:
        return

    if using_r2():
        try:
            _r2_client().delete_object(Bucket=current_app.config["R2_BUCKET"].strip(), Key=key)
        except Exception:
            pass
        return

    path = media_root() / key
    if path.exists() and path.is_file():
        try:
            path.unlink()
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


def list_media_files(limit: int | None = None) -> list[MediaItem]:
    if using_r2():
        items: list[MediaItem] = []
        client = _r2_client()
        continuation_token = None
        while True:
            params = {
                "Bucket": current_app.config["R2_BUCKET"].strip(),
                "MaxKeys": 1000,
            }
            if continuation_token:
                params["ContinuationToken"] = continuation_token
            resp = client.list_objects_v2(**params)
            for obj in resp.get("Contents", []):
                key = obj.get("Key") or ""
                if not key or key.endswith("/"):
                    continue
                items.append(
                    MediaItem(
                        key=key,
                        name=Path(key).name,
                        url=media_url_from_key(key),
                        size_kb=max(1, round((obj.get("Size") or 0) / 1024)),
                        updated_at=obj.get("LastModified"),
                    )
                )
            if not resp.get("IsTruncated"):
                break
            continuation_token = resp.get("NextContinuationToken")
        items.sort(key=lambda item: item.updated_at or datetime.min, reverse=True)
        return items[:limit] if limit else items

    root = media_root()
    items = [
        MediaItem(
            key=p.relative_to(root).as_posix(),
            name=p.name,
            url=media_url_from_key(p.relative_to(root).as_posix()),
            size_kb=max(1, round(p.stat().st_size / 1024)),
            updated_at=datetime.fromtimestamp(p.stat().st_mtime),
        )
        for p in root.rglob("*")
        if p.is_file()
    ]
    items.sort(key=lambda item: item.updated_at or datetime.min, reverse=True)
    return items[:limit] if limit else items


def open_media_bytes(url_or_key: str) -> tuple[bytes, str, str]:
    key = key_from_media_url(url_or_key) or (url_or_key or "").lstrip("/")
    if not key:
        raise FileNotFoundError("Arquivo não encontrado")

    if using_r2():
        try:
            obj = _r2_client().get_object(Bucket=current_app.config["R2_BUCKET"].strip(), Key=key)
        except ClientError as exc:
            raise FileNotFoundError(key) from exc
        body = obj["Body"].read()
        content_type = obj.get("ContentType") or _content_type_for_name(key)
        return body, content_type, Path(key).name

    path = media_root() / key
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(key)
    return path.read_bytes(), _content_type_for_name(path.name), path.name


def send_media(filename: str) -> Response:
    key = (filename or "").lstrip("/")
    if using_r2():
        body, content_type, name = open_media_bytes(key)
        return send_file(BytesIO(body), mimetype=content_type, download_name=name)
    return send_from_directory(media_root(), key)


def send_media_download(key: str, download_name: str | None = None) -> Response:
    body, content_type, name = open_media_bytes(key)
    return send_file(
        BytesIO(body),
        mimetype=content_type,
        as_attachment=True,
        download_name=download_name or name,
    )
