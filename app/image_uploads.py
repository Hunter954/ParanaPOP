"""Bounded, single-pass compression for editorial image uploads."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryFile
import threading

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

# Bound decoded pixels across concurrent panel, bot and mirror requests.
_encode_lock = threading.Lock()
_RASTER_EXTENSIONS = {'.jpg', '.jpeg', '.jfif', '.png', '.webp', '.gif', '.avif', '.heic', '.heif'}
_OPTIONAL_CODECS = {'.avif', '.heic', '.heif'}


@contextmanager
def prepared_image(stream, filename, content_type, *, enabled=True):
    """Yield a seekable upload; callers retain ownership of the original stream."""
    ext = Path(filename or '').suffix.lower()
    raster = ext in _RASTER_EXTENSIONS or (
        (content_type or '').split(';')[0].strip() in
        {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif', 'image/heic', 'image/heif'}
    )
    if not enabled or not raster:
        stream.seek(0)
        yield stream, filename, content_type
        return

    stream.seek(0, 2)
    source_size = stream.tell()
    stream.seek(0)
    if source_size > current_app.config.get('IMAGE_MAX_BYTES', 15_000_000):
        raise ValueError('Imagem excede o limite de tamanho permitido')

    output = None
    selected = stream
    output_name, output_type = filename, content_type
    try:
        with _encode_lock:
            try:
                image = Image.open(stream)
            except UnidentifiedImageError as exc:
                # Pillow installations without optional HEIF/AVIF codecs must
                # retain the formats the panel already accepted.
                if ext not in _OPTIONAL_CODECS:
                    raise ValueError('Arquivo de imagem inválido ou formato não suportado') from exc
                image = None
            if image is not None:
                with image:
                    if image.width * image.height > current_app.config.get('IMAGE_MAX_PIXELS', 24_000_000):
                        raise ValueError('Imagem excede o limite de pixels permitido')
                    if not getattr(image, 'is_animated', False):
                        max_edge = current_app.config.get('IMAGE_WEB_MAX_EDGE', 1280)
                        original_size = image.size
                        # JPEG decoder downsampling avoids allocating the full
                        # original before resizing (e.g. a 24 MP phone photo).
                        image.draft('RGB', (max_edge, max_edge))
                        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                        with ImageOps.exif_transpose(image) as oriented:
                            mode = 'RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB'
                            with oriented.convert(mode) as converted:
                                # Do not propagate EXIF, ICC profiles or other
                                # bulky metadata into the public web image.
                                converted.info.clear()
                                output = TemporaryFile(mode='w+b')
                                converted.save(output, 'WEBP', quality=current_app.config.get('IMAGE_WEB_QUALITY', 68), method=3)
                        encoded_size = output.tell()
                        if encoded_size < source_size or max(original_size) > max_edge:
                            output.seek(0)
                            selected = output
                            output_name = f'{Path(filename or "image").stem}.webp'
                            output_type = 'image/webp'
        # Release decoded images and the CPU lock before any network upload.
        stream.seek(0)
        yield selected, output_name, output_type
    finally:
        if output is not None:
            output.close()
