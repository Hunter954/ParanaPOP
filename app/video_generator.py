from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, ImageDraw

from .art_generator import (
    VARIANT_SPECS,
    ArtGeneratorError,
    _asset_path,
    _build_text_layout,
    _draw_rounded_rectangle,
    _fit_single_line_text,
    _line_metrics,
    _load_font,
    _normalize_text,
    _text_bbox,
)
from .storage import key_from_media_url, save_bytes


class VideoGeneratorError(Exception):
    pass


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


def _probe_duration(input_path: Path) -> float:
    """Retorna a duração real do arquivo para impedir que a imagem em loop prolongue o vídeo."""
    ffprobe = _media_binary("FFPROBE_BINARY", "ffprobe")
    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        detail = (completed.stderr or "erro desconhecido").strip()[-500:]
        raise VideoGeneratorError(f"Não foi possível identificar a duração do vídeo: {detail}")
    try:
        duration = float((completed.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise VideoGeneratorError("O vídeo recebido não possui uma duração válida.") from exc
    if duration <= 0:
        raise VideoGeneratorError("O vídeo recebido possui duração inválida.")
    return duration


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

    logo_path = _asset_path("paranapop.png")
    if logo_path.exists():
        with Image.open(logo_path) as logo_img:
            logo = logo_img.convert("RGBA")
        max_logo_width = int(width * 0.22)
        scale = min(max_logo_width / logo.width, 1)
        logo = logo.resize(
            (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
            Image.Resampling.LANCZOS,
        )
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
            pad_y = int(category_payload["pad_y"])
            cat_height = int(category_payload["height"])
            x1 = pad_left
            y1 = current_y
            x2 = x1 + int(category_payload["text_width"]) + pad_x * 2
            y2 = y1 + cat_height
            _draw_rounded_rectangle(draw, (x1, y1, x2, y2), radius=12, fill=yellow)
            draw.text(
                (x1 + pad_x - cat_left, y1 + pad_y - cat_top),
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


def generate_paranapop_stories_video(
    *, video_content: bytes, title: str, category_text: str, filename_hint: str = "video.mp4"
) -> dict[str, str]:
    if not video_content:
        raise VideoGeneratorError("O vídeo recebido está vazio.")
    if not _normalize_text(title):
        raise VideoGeneratorError("O título é obrigatório.")

    ffmpeg = _ffmpeg_binary()
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
        input_duration = _probe_duration(input_path)
        output_duration = min(input_duration, float(max_seconds))
        generate_stories_overlay(title=title, category_text=category_text).save(overlay_path, "PNG")

        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1[base];"
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
