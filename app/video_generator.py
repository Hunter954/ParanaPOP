from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from flask import current_app
from PIL import Image, ImageDraw

from .art_generator import (
    VARIANT_SPECS,
    _asset_path,
    _build_text_layout,
    _draw_rounded_rectangle,
    _fit_single_line_text,
    _load_font,
    _normalize_text,
    _text_bbox,
)
from .storage import key_from_media_url, save_bytes


class VideoGeneratorError(Exception):
    pass


PARANAPOP_STORIES_SIZE = (1080, 1920)
TRIVOX_VERTICAL_SIZE = (1080, 1920)
TRIVOX_HORIZONTAL_SIZE = (1920, 1080)


def _media_binary(config_key: str, default: str) -> str:
    configured = (current_app.config.get(config_key) or default).strip()
    resolved = shutil.which(configured)
    if not resolved:
        raise VideoGeneratorError(
            f"{default} não está instalado no ambiente de processamento."
        )
    return resolved


def _ffmpeg_binary() -> str:
    return _media_binary("FFMPEG_BINARY", "ffmpeg")


def _probe_video_info(input_path: Path) -> dict[str, float | int]:
    """Obtém duração e dimensões reais do vídeo para orientar o layout e limitar a saída."""
    ffprobe = _media_binary("FFPROBE_BINARY", "ffprobe")
    command = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_tags=rotate:format=duration",
        "-of", "json",
        str(input_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        detail = (completed.stderr or "erro desconhecido").strip()[-500:]
        raise VideoGeneratorError(f"Não foi possível analisar o vídeo: {detail}")

    try:
        payload = json.loads(completed.stdout or "{}")
        stream = (payload.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        rotate = int((stream.get("tags") or {}).get("rotate") or 0)
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoGeneratorError("Não foi possível interpretar as informações do vídeo.") from exc

    if rotate in {90, 270, -90, -270}:
        width, height = height, width

    if duration <= 0:
        raise VideoGeneratorError("O vídeo recebido possui duração inválida.")
    if width <= 0 or height <= 0:
        raise VideoGeneratorError("O vídeo recebido possui dimensões inválidas.")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "is_vertical": height >= width,
    }


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        test = f"{current} {word}".strip()
        left, _top, right, _bottom = _text_bbox(font, test)
        if (right - left) <= max_width:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_multiline_title(
    text: str,
    *,
    max_width: int,
    max_lines: int,
    max_size: int,
    min_size: int,
) -> tuple[object, list[str]]:
    clean = _normalize_text(text)
    if not clean:
        return _load_font(min_size, bold=True), []

    for size in range(max_size, min_size - 1, -2):
        font = _load_font(size, bold=True)
        lines = _wrap_text(clean, font, max_width)
        if lines and len(lines) <= max_lines:
            return font, lines

    font = _load_font(min_size, bold=True)
    words = clean.split()
    lines = _wrap_text(clean, font, max_width)
    if len(lines) <= max_lines:
        return font, lines

    trimmed: list[str] = []
    idx = 0
    for _ in range(max_lines):
        if idx >= len(words):
            break
        current = words[idx]
        idx += 1
        while idx < len(words):
            test = f"{current} {words[idx]}"
            left, _top, right, _bottom = _text_bbox(font, test)
            if (right - left) <= max_width:
                current = test
                idx += 1
            else:
                break
        trimmed.append(current)
    if idx < len(words) and trimmed:
        last = trimmed[-1]
        while True:
            candidate = last.rstrip(" .,") + "..."
            left, _top, right, _bottom = _text_bbox(font, candidate)
            if (right - left) <= max_width or len(last) <= 8:
                trimmed[-1] = candidate
                break
            last = " ".join(last.split()[:-1]).strip()
            if not last:
                trimmed[-1] = candidate
                break
    return font, trimmed


def _load_logo(filename: str, *, max_width: int | None = None, max_height: int | None = None) -> Image.Image | None:
    path = _asset_path(filename)
    if not path.exists():
        return None
    with Image.open(path) as img:
        logo = img.convert("RGBA")
    if max_width or max_height:
        width_limit = max_width or logo.width
        height_limit = max_height or logo.height
        scale = min(width_limit / logo.width, height_limit / logo.height, 1)
        logo = logo.resize(
            (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return logo


def _draw_title_box(
    canvas: Image.Image,
    *,
    title: str,
    box_left: int,
    box_top: int,
    max_width: int,
    max_lines: int,
    align: str = "left",
    padding_x: int = 28,
    padding_y: int = 18,
    radius: int = 22,
    max_font_size: int = 66,
    min_font_size: int = 28,
) -> tuple[int, int, int, int] | None:
    draw = ImageDraw.Draw(canvas)
    text_width = max(120, max_width - padding_x * 2)
    font, lines = _fit_multiline_title(
        title,
        max_width=text_width,
        max_lines=max_lines,
        max_size=max_font_size,
        min_size=min_font_size,
    )
    if not lines:
        return None

    line_gap = max(8, int(font.size * 0.22))
    line_boxes = []
    widest = 0
    total_height = 0
    for line in lines:
        left, top, right, bottom = _text_bbox(font, line)
        line_width = right - left
        line_height = bottom - top
        widest = max(widest, line_width)
        total_height += line_height
        line_boxes.append((left, top, line_width, line_height))
    total_height += line_gap * (len(lines) - 1)

    box_width = min(max_width, widest + padding_x * 2)
    box_height = total_height + padding_y * 2
    box_right = box_left + box_width
    box_bottom = box_top + box_height

    _draw_rounded_rectangle(draw, (box_left, box_top, box_right, box_bottom), radius=radius, fill=(0, 0, 0, 235))

    current_y = box_top + padding_y
    for index, line in enumerate(lines):
        left, top, line_width, line_height = line_boxes[index]
        if align == "center":
            line_x = box_left + (box_width - line_width) / 2 - left
        else:
            line_x = box_left + padding_x - left
        draw.text((line_x, current_y - top), line, font=font, fill=(255, 255, 255, 255))
        current_y += line_height + line_gap

    return (box_left, box_top, box_right, box_bottom)


def generate_stories_overlay(title: str, category_text: str = "") -> Image.Image:
    """Cria somente a camada visual do Paraná Pop para sobrepor ao vídeo."""
    spec = VARIANT_SPECS["stories"]
    width, height = spec["width"], spec["height"]
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    fade_height = int(height * 0.38)
    fade_path = _asset_path("fadepop.png")
    if fade_path.exists():
        with Image.open(fade_path) as fade_img:
            fade = fade_img.convert("RGBA")
        scale = width / fade.width
        fade = fade.resize((width, max(1, int(fade.height * scale))), Image.Resampling.LANCZOS)
        fade_height = fade.height
        canvas.alpha_composite(fade, (0, height - fade.height))

    logo = _load_logo("paranapop.png", max_width=int(width * 0.22), max_height=int(height * 0.12))
    if logo:
        canvas.alpha_composite(logo, (64, 64))

    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    yellow = (255, 218, 0, 255)
    black = (0, 0, 0, 255)
    title_text = _normalize_text(title).upper()
    category = _normalize_text(category_text).upper()
    pad_left = 64
    pad_right = 64

    if title_text:
        layout = _build_text_layout(
            spec=spec,
            width=width,
            height=height,
            fade_height=fade_height,
            title_text=title_text,
            category=category,
            pad_left=pad_left,
            pad_right=pad_right,
        )
        block_bottom = int(layout["text_region_bottom"])
        current_y = block_bottom - int(layout["block_height"])

        category_payload = layout.get("category")
        if category_payload:
            cat_left, cat_top, _cat_right, _cat_bottom = category_payload["bbox"]
            pad_x = int(category_payload["pad_x"])
            cat_height = int(category_payload["height"])
            x1 = pad_left
            y1 = current_y
            x2 = x1 + int(category_payload["text_width"]) + pad_x * 2
            y2 = y1 + cat_height
            _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=12, fill=yellow)
            draw.text(
                (x1 + pad_x - cat_left, y1 + int(category_payload["pad_y"]) - cat_top),
                category,
                font=category_payload["font"],
                fill=black,
            )
            current_y = y2 + int(layout["badge_gap"])

        title_font = layout["title_font"]
        line_boxes = layout["line_boxes"]
        line_gap = int(layout["line_gap"])
        for index, line in enumerate(layout["lines"]):
            left, top, _right, _bottom, _line_width, line_height = line_boxes[index]
            draw.text((pad_left - left, current_y - top), line, font=title_font, fill=white)
            current_y += line_height + line_gap
    elif category:
        max_width = width - (pad_left + pad_right)
        tag_font, _size = _fit_single_line_text(category, max_size=22, max_width=max_width - 44, min_size=14)
        left, top, right, bottom = _text_bbox(tag_font, category)
        text_width = right - left
        text_height = bottom - top
        pad_x = 22
        pad_y = 10
        x1 = pad_left
        y2 = height - int(spec.get("bottom_gap", 78))
        y1 = y2 - text_height - pad_y * 2
        x2 = x1 + text_width + pad_x * 2
        _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=12, fill=yellow)
        draw.text((x1 + pad_x - left, y1 + pad_y - top), category, font=tag_font, fill=black)

    return canvas


def generate_trivox_vertical_overlay(title: str) -> Image.Image:
    width, height = TRIVOX_VERTICAL_SIZE
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    logo = _load_logo("trivox.png", max_width=int(width * 0.28), max_height=int(height * 0.10))
    if logo:
        x = (width - logo.width) // 2
        y = 52
        canvas.alpha_composite(logo, (x, y))

    box_left = 36
    max_box_width = int(width * 0.76)
    temp_canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bbox = _draw_title_box(
        temp_canvas,
        title=title,
        box_left=box_left,
        box_top=0,
        max_width=max_box_width,
        max_lines=3,
        align="left",
        padding_x=26,
        padding_y=18,
        radius=24,
        max_font_size=58,
        min_font_size=28,
    )
    if bbox:
        _, _, _, box_bottom = bbox
        box_height = box_bottom
        box_top = height - 150 - box_height
        _draw_title_box(
            canvas,
            title=title,
            box_left=box_left,
            box_top=box_top,
            max_width=max_box_width,
            max_lines=3,
            align="left",
            padding_x=26,
            padding_y=18,
            radius=24,
            max_font_size=58,
            min_font_size=28,
        )

    return canvas


def generate_trivox_horizontal_overlay(title: str) -> Image.Image:
    width, height = TRIVOX_HORIZONTAL_SIZE
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    logo = _load_logo("trivox.png", max_width=int(width * 0.20), max_height=int(height * 0.15))
    if logo:
        x = width - logo.width - 56
        y = 40
        canvas.alpha_composite(logo, (x, y))

    max_box_width = int(width * 0.82)
    start_left = (width - max_box_width) // 2
    temp_canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bbox = _draw_title_box(
        temp_canvas,
        title=title,
        box_left=start_left,
        box_top=0,
        max_width=max_box_width,
        max_lines=3,
        align="center",
        padding_x=34,
        padding_y=22,
        radius=26,
        max_font_size=72,
        min_font_size=30,
    )
    if bbox:
        _, _, _, box_bottom = bbox
        box_height = box_bottom
        box_top = height - 86 - box_height
        _draw_title_box(
            canvas,
            title=title,
            box_left=start_left,
            box_top=box_top,
            max_width=max_box_width,
            max_lines=3,
            align="center",
            padding_x=34,
            padding_y=22,
            radius=26,
            max_font_size=72,
            min_font_size=30,
        )

    return canvas


def _render_video_with_overlay(
    *,
    input_path: Path,
    output_path: Path,
    overlay_path: Path,
    output_size: tuple[int, int],
    output_duration: float,
) -> None:
    ffmpeg = _ffmpeg_binary()
    width, height = output_size
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1[base];"
        "[base][1:v]overlay=0:0:format=auto:shortest=1:eof_action=endall[v]"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(input_path),
        "-loop", "1",
        "-i", str(overlay_path),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",
        "-t", f"{output_duration:.3f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(120, int(output_duration * 3)),
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoGeneratorError("O processamento do vídeo excedeu o tempo permitido.") from exc
    if completed.returncode != 0 or not output_path.exists():
        detail = (completed.stderr or "erro desconhecido").strip()[-700:]
        raise VideoGeneratorError(f"Falha no FFmpeg: {detail}")


def generate_paranapop_stories_video(
    *, video_content: bytes, title: str, category_text: str, filename_hint: str = "video.mp4"
) -> dict[str, str]:
    if not video_content:
        raise VideoGeneratorError("O vídeo recebido está vazio.")
    if not _normalize_text(title):
        raise VideoGeneratorError("O título é obrigatório.")

    max_seconds = int(current_app.config.get("VIDEO_MAX_SECONDS", 180))

    with tempfile.TemporaryDirectory(prefix="paranapop-video-") as tmp:
        tmp_dir = Path(tmp)
        input_ext = Path(filename_hint or "video.mp4").suffix.lower()
        if input_ext not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            input_ext = ".mp4"
        input_path = tmp_dir / f"input{input_ext}"
        overlay_path = tmp_dir / "overlay.png"
        output_path = tmp_dir / "paranapop-stories.mp4"
        input_path.write_bytes(video_content)
        video_info = _probe_video_info(input_path)
        output_duration = min(float(video_info["duration"]), float(max_seconds))
        generate_stories_overlay(title=title, category_text=category_text).save(overlay_path, "PNG")
        _render_video_with_overlay(
            input_path=input_path,
            output_path=output_path,
            overlay_path=overlay_path,
            output_size=PARANAPOP_STORIES_SIZE,
            output_duration=output_duration,
        )
        output = output_path.read_bytes()

    filename = f"video-stories-{uuid.uuid4().hex}.mp4"
    url = save_bytes(output, folder="gerador/video-generated", filename_hint=filename, content_type="video/mp4")
    return {
        "key": "stories",
        "label": "Vídeo Stories / Reels",
        "size": "1080x1920",
        "url": url,
        "download_key": key_from_media_url(url) or f"gerador/video-generated/{filename}",
        "download_name": filename,
        "mimetype": "video/mp4",
    }


def generate_trivox_video(
    *, video_content: bytes, title: str, filename_hint: str = "video.mp4"
) -> dict[str, str]:
    if not video_content:
        raise VideoGeneratorError("O vídeo recebido está vazio.")
    if not _normalize_text(title):
        raise VideoGeneratorError("O título é obrigatório.")

    max_seconds = int(current_app.config.get("VIDEO_MAX_SECONDS", 180))

    with tempfile.TemporaryDirectory(prefix="trivox-video-") as tmp:
        tmp_dir = Path(tmp)
        input_ext = Path(filename_hint or "video.mp4").suffix.lower()
        if input_ext not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            input_ext = ".mp4"
        input_path = tmp_dir / f"input{input_ext}"
        overlay_path = tmp_dir / "overlay.png"
        output_path = tmp_dir / "trivox-video.mp4"
        input_path.write_bytes(video_content)
        video_info = _probe_video_info(input_path)
        output_duration = min(float(video_info["duration"]), float(max_seconds))

        if bool(video_info["is_vertical"]):
            overlay = generate_trivox_vertical_overlay(title=title)
            output_size = TRIVOX_VERTICAL_SIZE
            key = "stories"
            label = "Vídeo Stories"
            size_label = "1080x1920"
        else:
            overlay = generate_trivox_horizontal_overlay(title=title)
            output_size = TRIVOX_HORIZONTAL_SIZE
            key = "horizontal"
            label = "Vídeo Horizontal"
            size_label = "1920x1080"

        overlay.save(overlay_path, "PNG")
        _render_video_with_overlay(
            input_path=input_path,
            output_path=output_path,
            overlay_path=overlay_path,
            output_size=output_size,
            output_duration=output_duration,
        )
        output = output_path.read_bytes()

    filename = f"trivox-video-{key}-{uuid.uuid4().hex}.mp4"
    url = save_bytes(output, folder="gerador/video-generated", filename_hint=filename, content_type="video/mp4")
    return {
        "key": key,
        "label": label,
        "size": size_label,
        "url": url,
        "download_key": key_from_media_url(url) or f"gerador/video-generated/{filename}",
        "download_name": filename,
        "mimetype": "video/mp4",
    }
