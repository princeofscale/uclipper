"""Баннеры проекта и звук их появления: копирование баннеров, описание, выдача по id и SFX.

Баннеров два (см. `app.config.BANNERS`): старый `TGrader` и новый `TgRader v2`. Оба лежат в образе
рядом с ботом и при первом запуске копируются в рабочую папку, откуда их берут задачи и экран «Баннеры».
"""
from __future__ import annotations

import logging
import math
import shutil
import struct
import uuid
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import ASSETS_DIR, BANNER_EXTS, BANNERS, BannerSpec
from app.services.video_processor import MediaError, probe_media

log = logging.getLogger(__name__)

SFX_NAME = "pop.wav"
SFX_RATE = 44100
SFX_SECONDS = 0.25


class BannerError(ValueError):
    """Ошибка работы с баннером. Текст — по-русски, его увидит пользователь."""


@dataclass(frozen=True)
class BannerInfo:
    id: str  # имя файла в папке баннеров — им баннер выбирается в задаче
    name: str  # как показывать в интерфейсе
    hint: str  # чем этот баннер отличается от другого
    path: Path  # сам файл баннера
    width: int
    height: int
    duration: float  # секунды
    has_audio: bool


# ---------------------------------------------------------------------------
# Файлы баннеров
# ---------------------------------------------------------------------------


def _copy_if_missing(banners_dir: Path, asset: Path, name: str) -> Path | None:
    """Копирует файл баннера в рабочую папку, если его там нет. Чужой (заменённый) файл не трогает."""
    target = banners_dir / name
    if target.is_file() and target.stat().st_size > 0:
        return target
    if not asset.is_file() or asset.stat().st_size == 0:
        log.warning("Баннер %s не найден в %s — бот запустится без него.", name, asset.parent)
        return None
    banners_dir.mkdir(parents=True, exist_ok=True)
    staging = banners_dir / f".{uuid.uuid4().hex}{target.suffix}"
    shutil.copyfile(asset, staging)
    staging.replace(target)
    return target


def ensure_banners(banners_dir: Path, assets_dir: Path | None = None) -> list[Path]:
    """Кладёт все баннеры проекта в папку баннеров и возвращает те файлы, что там в итоге лежат."""
    source = assets_dir if assets_dir is not None else ASSETS_DIR / "banners"
    ready: list[Path] = []
    for spec in BANNERS:
        path = _copy_if_missing(banners_dir, source / spec.file, spec.file)
        if path is not None:
            ready.append(path)
    return ready


@lru_cache(maxsize=8)
def _describe_cached(path_text: str, mtime_ns: int, size: int, name: str, hint: str) -> BannerInfo | None:
    """Описание баннера по ffprobe. `mtime_ns` и `size` — часть ключа кеша: заменили файл — описание обновится."""
    path = Path(path_text)
    try:
        media = probe_media(path)
    except MediaError:
        return None
    return BannerInfo(
        id=path.name,
        name=name,
        hint=hint,
        path=path,
        width=media.width,
        height=media.height,
        duration=round(media.duration, 3),
        has_audio=media.has_audio,
    )


def _describe(path: Path, spec: BannerSpec) -> BannerInfo | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _describe_cached(str(path), stat.st_mtime_ns, stat.st_size, spec.name, spec.hint)


def catalog_banners(banners_dir: Path) -> list[BannerInfo]:
    """Баннеры проекта, которые сейчас лежат в папке: в порядке каталога, с параметрами из ffprobe.

    Описание берётся из кеша: бот спрашивает его на каждом экране, а ffprobe файла, который не меняется, дорог.
    """
    described = (_describe(banners_dir / spec.file, spec) for spec in BANNERS)
    return [banner for banner in described if banner is not None]


def default_banner(banners_dir: Path) -> BannerInfo | None:
    """Главный баннер проекта (первый в каталоге) или `None`, если файла нет или он не читается как видео."""
    banners = catalog_banners(banners_dir)
    return banners[0] if banners else None


def find_banner(banner_id: str | None, banners_dir: Path) -> BannerInfo | None:
    """Баннер по id (имени файла) или `None`, если такого в каталоге нет."""
    if not banner_id:
        return None
    return next((banner for banner in catalog_banners(banners_dir) if banner.id == banner_id), None)


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
