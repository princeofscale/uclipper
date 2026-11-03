"""Баннеры: пресеты (Pillow), звук появления (SFX), загрузка и выдача баннеров."""
from __future__ import annotations

import io
import logging
import math
import re
import shutil
import struct
import subprocess
import uuid
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)

from app.config import (
    BANNER_EXTS,
    BANNER_SIZE,
    BANNER_VIDEO_EXTS,
    DEFAULT_BANNER_FILE,
    DEFAULT_BANNER_LABEL,
    PRESETS,
    STRIP_SIZE,
    PresetSpec,
)
from app.services.video_processor import MediaError, probe_media

log = logging.getLogger(__name__)

MAX_BANNER_BYTES = 10 * 1024 * 1024  # картинка
MAX_BANNER_VIDEO_BYTES = 40 * 1024 * 1024  # видео-баннер
MAX_BANNER_VIDEO_SECONDS = 20
STRIP_MIN_ASPECT = 2.6  # шире этого — «полоса» для постоянного баннера, уже — карточка для всплывающего
POSTERS_DIR = "posters"
MAX_BANNER_WIDTH = 1600
SUPERSAMPLE = 2  # пресеты рисуются в 2× и уменьшаются — гладкие края и углы

SFX_NAME = "pop.wav"
SFX_RATE = 44100
SFX_SECONDS = 0.25

# Жирные шрифты с кириллицей: macOS → Linux → Windows. Берётся первый найденный.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
)

_CUSTOM_NAME = re.compile(r"^custom_(?P<slug>.+)_[0-9a-f]{6}$")


class BannerError(ValueError):
    """Ошибка работы с баннером. Текст — по-русски, его увидит пользователь."""


@dataclass(frozen=True)
class BannerInfo:
    id: str  # имя файла в папке баннеров — им баннер выбирается в задаче
    name: str  # как показывать в интерфейсе
    kind: Literal["default", "preset", "custom"]
    path: Path  # сам файл баннера
    width: int
    height: int
    animated: bool  # GIF или видео
    media: Literal["image", "gif", "video"] = "image"
    duration: float | None = None  # секунды: длина видео или одного круга GIF
    has_audio: bool = False
    role: Literal["popup", "strip"] = "popup"  # подсказка интерфейсу: карточка или широкая полоса
    poster: Path | None = None  # кадр-превью видео-баннера (jpg на диске)


# ---------------------------------------------------------------------------
# Пресеты
# ---------------------------------------------------------------------------


def _find_font_path() -> str | None:
    return next((candidate for candidate in FONT_CANDIDATES if Path(candidate).is_file()), None)


def _bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _find_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: float, size: int, min_size: int = 16):
    """Жирный шрифт наибольшего размера (не больше `size`), при котором `text` влезает в `max_width`."""
    font = _bold_font(size)
    while size > min_size and draw.textlength(text, font=font) > max_width:
        size -= 2
        font = _bold_font(size)
    return font


def _gradient_card(size: tuple[int, int], colors: tuple[str, str]) -> Image.Image:
    """Непрозрачная прямоугольная заливка с диагональным градиентом."""
    horizontal = Image.linear_gradient("L").rotate(90, expand=True).resize(size)
    vertical = Image.linear_gradient("L").resize(size)
    mask = ImageChops.add(horizontal, vertical, scale=2.0)
    return Image.composite(Image.new("RGBA", size, colors[1]), Image.new("RGBA", size, colors[0]), mask)


def render_preset(spec: PresetSpec) -> Image.Image:
    """Рисует баннер-пресет: градиентная карточка, тег, бренд, подзаголовок и плашка CTA."""
    k = SUPERSAMPLE
    width, height = BANNER_SIZE[0] * k, BANNER_SIZE[1] * k
    card = _gradient_card((width, height), spec.colors)

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    white = (255, 255, 255, 255)
    ink = (15, 23, 42, 255)
    pad = 40 * k
    max_text = width - 2 * pad

    # декоративные круги справа
    draw.ellipse([width * 0.60, -height * 0.40, width * 1.12, height * 0.62], fill=(255, 255, 255, 30))
    draw.ellipse([width * 0.72, height * 0.40, width * 1.20, height * 1.30], fill=(255, 255, 255, 22))

    # промо-тег
    tag_font = _fit_font(draw, spec.tag, 200 * k, 28 * k)
    tag_width = draw.textlength(spec.tag, font=tag_font) + 36 * k
    draw.rounded_rectangle(
        [pad, 32 * k, pad + tag_width, 84 * k],
        radius=26 * k,
        fill=(255, 255, 255, 60),
        outline=(255, 255, 255, 140),
        width=2 * k,
    )
    draw.text((pad + 18 * k, 58 * k), spec.tag, font=tag_font, fill=white, anchor="lm")

    # бренд и подзаголовок
    draw.text((pad, 150 * k), spec.brand, font=_fit_font(draw, spec.brand, max_text, 104 * k), fill=white, anchor="lm")
    draw.text(
        (pad, 238 * k),
        spec.subtitle,
        font=_fit_font(draw, spec.subtitle, max_text, 38 * k),
        fill=(255, 255, 255, 235),
        anchor="lm",
    )

    # плашка призыва к действию со стрелкой
    inner, arrow = 30 * k, 34 * k
    cta_font = _fit_font(draw, spec.cta, max_text - 2 * inner - arrow, 32 * k)
    cta_width = draw.textlength(spec.cta, font=cta_font) + 2 * inner + arrow
    mid = 302 * k
    draw.rounded_rectangle([pad, 274 * k, pad + cta_width, 330 * k], radius=28 * k, fill=spec.accent)
    draw.text((pad + inner, mid), spec.cta, font=cta_font, fill=ink, anchor="lm")
    arrow_x = pad + cta_width - inner - 14 * k
    draw.polygon([(arrow_x, mid - 10 * k), (arrow_x + 14 * k, mid), (arrow_x, mid + 10 * k)], fill=ink)

    card = Image.alpha_composite(card, layer)
    rounded = Image.new("L", (width, height), 0)
    ImageDraw.Draw(rounded).rounded_rectangle([0, 0, width - 1, height - 1], radius=44 * k, fill=255)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(card, (0, 0), rounded)
    return canvas.resize(BANNER_SIZE, Image.Resampling.LANCZOS)


def ensure_default_banner(banners_dir: Path, asset: Path | None = None) -> Path | None:
    """Кладёт главный видео-баннер проекта (TGrader, 7.5 с, со звуком) в папку баннеров.

    Берётся из `assets/banners/`; если файла там нет — ничего не делает и возвращает None.
    Уже существующий файл не перезаписывается: пользователь мог заменить его своим.
    """
    if asset is None:
        from app.config import ASSETS_DIR

        asset = ASSETS_DIR / "banners" / DEFAULT_BANNER_FILE
    target = banners_dir / DEFAULT_BANNER_FILE
    if target.is_file() and target.stat().st_size > 0:
        return target
    if not asset.is_file() or asset.stat().st_size == 0:
        log.warning("Главный баннер %s не найден в %s — бот запустится без него.", DEFAULT_BANNER_FILE, asset.parent)
        return None
    banners_dir.mkdir(parents=True, exist_ok=True)
    staging = banners_dir / f".default_{uuid.uuid4().hex}.mp4"
    shutil.copyfile(asset, staging)
    staging.replace(target)
    return target


def ensure_presets(banners_dir: Path) -> list[Path]:
    """Создаёт недостающие пресеты. Уже существующие файлы не трогает — их можно заменить своими."""
    banners_dir.mkdir(parents=True, exist_ok=True)
    if _find_font_path() is None:
        log.warning("Не найден системный шрифт с кириллицей — тексты пресетов нарисованы запасным шрифтом.")
    paths = []
    for spec in PRESETS:
        path = banners_dir / spec.file
        if not path.is_file() or path.stat().st_size == 0:
            render_preset(spec).save(path, "PNG", optimize=True)
        paths.append(path)
    return paths


def render_strip(spec: PresetSpec) -> Image.Image:
    """Широкая полоса 1080×300 для постоянного баннера: тот же бренд, что у карточки, в формате верхнего поля."""
    k = SUPERSAMPLE
    width, height = STRIP_SIZE[0] * k, STRIP_SIZE[1] * k
    card = _gradient_card((width, height), spec.colors)

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    white = (255, 255, 255, 255)
    ink = (15, 23, 42, 255)
    pad = 48 * k
    max_text = width - 2 * pad

    draw.ellipse([width * 0.66, -height * 0.9, width * 1.12, height * 0.95], fill=(255, 255, 255, 30))
    draw.ellipse([width * 0.80, height * 0.25, width * 1.25, height * 1.65], fill=(255, 255, 255, 22))

    tag_font = _fit_font(draw, spec.tag, 200 * k, 24 * k)
    tag_width = draw.textlength(spec.tag, font=tag_font) + 32 * k
    draw.rounded_rectangle(
        [pad, 26 * k, pad + tag_width, 70 * k], radius=22 * k, fill=(255, 255, 255, 60), outline=(255, 255, 255, 140), width=2 * k
    )
    draw.text((pad + 16 * k, 48 * k), spec.tag, font=tag_font, fill=white, anchor="lm")

    draw.text((pad, 128 * k), spec.brand, font=_fit_font(draw, spec.brand, max_text, 92 * k), fill=white, anchor="lm")
    draw.text(
        (pad, 192 * k), spec.subtitle, font=_fit_font(draw, spec.subtitle, max_text, 34 * k), fill=(255, 255, 255, 235), anchor="lm"
    )

    inner, arrow = 28 * k, 32 * k
    cta_font = _fit_font(draw, spec.cta, max_text - 2 * inner - arrow, 30 * k)
    cta_width = draw.textlength(spec.cta, font=cta_font) + 2 * inner + arrow
    top, bottom = 222 * k, 272 * k
    mid = (top + bottom) // 2
    draw.rounded_rectangle([pad, top, pad + cta_width, bottom], radius=25 * k, fill=spec.accent)
    draw.text((pad + inner, mid), spec.cta, font=cta_font, fill=ink, anchor="lm")
    arrow_x = pad + cta_width - inner - 14 * k
    draw.polygon([(arrow_x, mid - 9 * k), (arrow_x + 13 * k, mid), (arrow_x, mid + 9 * k)], fill=ink)

    card = Image.alpha_composite(card, layer)
    rounded = Image.new("L", (width, height), 0)
    ImageDraw.Draw(rounded).rounded_rectangle([0, 0, width - 1, height - 1], radius=40 * k, fill=255)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(card, (0, 0), rounded)
    return canvas.resize(STRIP_SIZE, Image.Resampling.LANCZOS)


def ensure_strip_presets(banners_dir: Path) -> list[Path]:
    """Создаёт недостающие полосы-пресеты (`strip_<бренд>.png`). Существующие файлы не трогает."""
    banners_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for spec in PRESETS:
        path = banners_dir / spec.strip_file
        if not path.is_file() or path.stat().st_size == 0:
            render_strip(spec).save(path, "PNG", optimize=True)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# SFX
# ---------------------------------------------------------------------------


def ensure_sfx(sfx_dir: Path) -> Path:
    """Создаёт `pop.wav` — 0.25 с, затухающая синусоида 520 → 880 Гц, 44100 Гц, стерео."""
    sfx_dir.mkdir(parents=True, exist_ok=True)
    path = sfx_dir / SFX_NAME
    if path.is_file() and path.stat().st_size > 0:
        return path

    count = int(SFX_SECONDS * SFX_RATE)
    fade = int(0.01 * SFX_RATE)  # 10 мс затухания в конце — без щелчка
    phase = 0.0
    frames = bytearray()
    for i in range(count):
        t = i / SFX_RATE
        frequency = 520 + (880 - 520) * (t / SFX_SECONDS)
        phase += 2 * math.pi * frequency / SFX_RATE
        envelope = min(1.0, t / 0.005) * math.exp(-14 * t) * min(1.0, (count - 1 - i) / fade)
        sample = int(32767 * 0.5 * envelope * math.sin(phase))
        frames += struct.pack("<hh", sample, sample)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SFX_RATE)
        wav.writeframes(bytes(frames))
    return path


# ---------------------------------------------------------------------------
# Список, загрузка, выдача
# ---------------------------------------------------------------------------


def _display_name(path: Path) -> str:
    match = _CUSTOM_NAME.match(path.stem)
    return (match.group("slug") if match else path.stem).replace("_", " ")


def ensure_poster(video: Path, duration: float | None = None) -> Path | None:
    """Кадр-превью видео-баннера в `posters/<имя>.jpg` (создаётся один раз). `None`, если кадр достать не удалось."""
    target = video.parent / POSTERS_DIR / f"{video.stem}.jpg"
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    seek = min(2.0, (duration or 2.0) * 0.3)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{seek:.2f}", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(target),
    ]  # fmt: skip
    try:
        done = subprocess.run(command, capture_output=True)
    except FileNotFoundError:
        done = None
    if done is None or done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return None
    return target


def _gif_loop_seconds(image: Image.Image) -> float:
    total = 0
    for index in range(getattr(image, "n_frames", 1)):
        image.seek(index)
        total += image.info.get("duration") or 100  # браузеры и ffmpeg показывают кадр «0 мс» как 100 мс
    image.seek(0)
    return total / 1000


def _describe(path: Path) -> BannerInfo | None:
    preset = next((spec for spec in PRESETS if path.name in (spec.file, spec.strip_file)), None)
    is_default = path.name == DEFAULT_BANNER_FILE
    if is_default:
        name = DEFAULT_BANNER_LABEL
    elif preset is None:
        name = _display_name(path)
    else:
        name = preset.label if path.name == preset.file else f"{preset.label} (полоса)"
    fields: dict = {}
    if path.suffix.lower() in BANNER_VIDEO_EXTS:
        try:
            media = probe_media(path)
        except MediaError:
            return None
        poster = ensure_poster(path, media.duration)
        width, height = media.width, media.height
        fields = {
            "animated": True,
            "media": "video",
            "duration": round(media.duration, 3),
            "has_audio": media.has_audio,
            "poster": poster,
        }
    else:
        try:
            with Image.open(path) as image:
                width, height = image.size
                is_gif = path.suffix.lower() == ".gif" and getattr(image, "n_frames", 1) > 1
                if is_gif:
                    fields = {"animated": True, "media": "gif", "duration": round(_gif_loop_seconds(image), 3)}
        except (OSError, UnidentifiedImageError, SyntaxError, ValueError):
            return None
    return BannerInfo(
        id=path.name,
        name=name,
        kind="default" if is_default else ("preset" if preset else "custom"),
        path=path,
        width=width,
        height=height,
        role="strip" if width / max(height, 1) >= STRIP_MIN_ASPECT else "popup",
        **({"animated": False} | fields),
    )


def list_banners(banners_dir: Path) -> list[BannerInfo]:
    """Сначала главный баннер проекта, потом пресеты (карточки, затем полосы), затем свои — новые сверху."""
    if not banners_dir.is_dir():
        return []
    order = {DEFAULT_BANNER_FILE: -1}
    order |= {spec.file: index for index, spec in enumerate(PRESETS)}
    order |= {spec.strip_file: len(PRESETS) + index for index, spec in enumerate(PRESETS)}
    files = [
        path
        for path in banners_dir.iterdir()
        if path.is_file() and path.suffix.lower() in BANNER_EXTS and not path.name.startswith(".")
    ]
    files.sort(key=lambda path: (order.get(path.name, len(order)), -path.stat().st_mtime, path.name))
    return [info for info in (_describe(path) for path in files) if info is not None]


@lru_cache(maxsize=4)
def _describe_cached(path_text: str, mtime_ns: int, size: int) -> BannerInfo | None:
    return _describe(Path(path_text))


def default_banner(banners_dir: Path) -> BannerInfo | None:
    """Главный баннер проекта (TGrader) или `None`, если файла нет.

    Описание берётся из кеша: бот спрашивает его на каждом экране, а ffprobe файла, который не меняется, дорог.
    """
    path = banners_dir / DEFAULT_BANNER_FILE
    try:
        stat = path.stat()
    except OSError:
        return None
    return _describe_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _slug(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")[:24] or "banner"


def _rounded_alpha(image: Image.Image) -> Image.Image:
    """Альфа-канал со скруглёнными углами (маска рисуется в 4× для гладкого края)."""
    scale = 4
    width, height = image.size
    radius = round(min(width, height) * 0.12) * scale
    big = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, width * scale - 1, height * scale - 1], radius=radius, fill=255)
    mask = big.resize((width, height), Image.Resampling.LANCZOS)
    return ImageChops.multiply(image.getchannel("A"), mask)


def _normalize(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGBA")
    if image.width > MAX_BANNER_WIDTH:
        height = max(1, round(image.height * MAX_BANNER_WIDTH / image.width))
        image = image.resize((MAX_BANNER_WIDTH, height), Image.Resampling.LANCZOS)
    image.putalpha(_rounded_alpha(image))
    return image


def _save_uploaded_video(filename: str, extension: str, data: bytes, banners_dir: Path) -> BannerInfo:
    """Видео-баннер: сохраняется как есть (без перекодирования), если ffprobe его читает и он не длиннее лимита."""
    if len(data) > MAX_BANNER_VIDEO_BYTES:
        raise BannerError("Файл больше 40 МБ — выберите видео поменьше.")
    banners_dir.mkdir(parents=True, exist_ok=True)
    staging = banners_dir / f".upload_{uuid.uuid4().hex}{extension}"  # точка в начале: список баннеров его не покажет
    staging.write_bytes(data)
    try:
        try:
            media = probe_media(staging)
        except MediaError as error:
            raise BannerError("Не удалось прочитать видео-баннер: файл повреждён, без видеодорожки или это не видео.") from error
        if media.duration > MAX_BANNER_VIDEO_SECONDS:
            raise BannerError(
                f"Видео-баннер длиннее {MAX_BANNER_VIDEO_SECONDS} с ({media.duration:.1f} с) — обрежьте его или возьмите ролик покороче."
            )
        target = banners_dir / f"custom_{_slug(Path(filename).stem)}_{uuid.uuid4().hex[:6]}{extension}"
        staging.rename(target)
    finally:
        staging.unlink(missing_ok=True)
    info = _describe(target)
    if info is None:  # не должно случаться: только что проверили
        target.unlink(missing_ok=True)
        raise BannerError("Не удалось сохранить баннер.")
    return info


def save_uploaded_banner(filename: str, data: bytes, banners_dir: Path) -> BannerInfo:
    """Проверяет присланный файл и сохраняет его как свой баннер.

    Статичные картинки превращаются в PNG со скруглёнными углами (не шире 1600 px); анимированный GIF и
    видео (.mp4, .mov, .webm, .m4v до 20 с) сохраняются как есть. Неподходящий файл → `BannerError`,
    на диске ничего не остаётся.
    """
    extension = Path(filename).suffix.lower()
    if extension not in BANNER_EXTS:
        raise BannerError("Баннер — это картинка (.png, .jpg, .jpeg, .webp, .gif) или видео (.mp4, .mov, .webm, .m4v).")
    if not data:
        raise BannerError("Файл пустой.")
    if extension in BANNER_VIDEO_EXTS:
        return _save_uploaded_video(filename, extension, data, banners_dir)
    if len(data) > MAX_BANNER_BYTES:
        raise BannerError("Файл больше 10 МБ — выберите картинку поменьше.")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            animated = image.format == "GIF" and getattr(image, "n_frames", 1) > 1
            normalized = None if animated else _normalize(image)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as error:
        raise BannerError("Не удалось прочитать картинку: файл повреждён или это не изображение.") from error

    banners_dir.mkdir(parents=True, exist_ok=True)
    stem = f"custom_{_slug(Path(filename).stem)}_{uuid.uuid4().hex[:6]}"
    if animated:
        target = banners_dir / f"{stem}.gif"
        target.write_bytes(data)
    else:
        target = banners_dir / f"{stem}.png"
        normalized.save(target, "PNG", optimize=True)
    info = _describe(target)
    if info is None:  # не должно случаться: только что сохранили
        target.unlink(missing_ok=True)
        raise BannerError("Не удалось сохранить баннер.")
    return info


def resolve_banner(banner_id: str, banners_dir: Path) -> Path:
    """Путь к баннеру по id. Принимает только имя файла из папки баннеров — никаких `../`."""
    safe = (
        bool(banner_id)
        and banner_id == Path(banner_id).name
        and not banner_id.startswith(".")
        and "\\" not in banner_id
        and "\x00" not in banner_id
    )
    path = banners_dir / banner_id if safe else None
    if path is None or path.suffix.lower() not in BANNER_EXTS or not path.is_file():
        raise BannerError(f"Баннер «{banner_id}» не найден.")
    return path
