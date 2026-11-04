"""Пути, лимиты и константы TgRader Clipper."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FPS = 30  # частота кадров всех готовых роликов

VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv"})  # что можно прислать боту файлом
MIN_CACHE_BYTES = 100 * 1024  # файл кеша меньше этого — остаток оборванной загрузки
MAX_SOURCE_SECONDS = 4 * 3600  # ролик длиннее не берём: на общем сервере он съест диск и часы рендера
BANNER_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
BANNER_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".m4v"})
BANNER_EXTS = BANNER_IMAGE_EXTS | BANNER_VIDEO_EXTS  # всё, что можно выбрать баннером

# Лимиты Telegram Bot API. Локальный Bot API server снимает их, но обычный бот живёт с ними.
TELEGRAM_MAX_UPLOAD_MB = 20  # столько можно прислать боту обычным ботом через облачный API
TELEGRAM_MAX_DOWNLOAD_MB = 50  # столько бот может отдать пользователю
TELEGRAM_CAPTION_LIMIT = 1024  # символов в подписи к видео

REENCODE_HEIGHT = 1280  # если клип не влезает в лимит, перекодируем в 720x1280
REENCODE_CRF = 26
REENCODE_AUDIO_BITRATE = "128k"

# Главный рекламный баннер проекта: приходит вместе с ботом, копируется в папку баннеров.
DEFAULT_BANNER_FILE = "TGrader-banner-7s.mp4"
DEFAULT_BANNER_LABEL = "TGrader (7 с, со звуком)"

PACKAGE_DIR = Path(__file__).resolve().parent  # .../app
PROJECT_DIR = PACKAGE_DIR.parent
ASSETS_DIR = PROJECT_DIR / "assets"


@dataclass(frozen=True)
class Settings:
    """Рабочие папки приложения.

    `default()` — настоящая раскладка проекта; `for_root(root)` переносит все записываемые папки
    под `root` (так тесты не трогают настоящий `workspace/`).
    """

    base_dir: Path
    banners_dir: Path
    sfx_dir: Path
    outputs_dir: Path
    cache_dir: Path
    uploads_dir: Path

    @classmethod
    def default(cls) -> "Settings":
        workspace = PROJECT_DIR / "workspace"
        return cls(
            base_dir=PROJECT_DIR,
            banners_dir=workspace / "banners",
            sfx_dir=workspace / "sfx",
            outputs_dir=workspace / "outputs",
            cache_dir=workspace / "cache",
            uploads_dir=workspace / "uploads",
        )

    @classmethod
    def for_root(cls, root: Path) -> "Settings":
        return cls(
            base_dir=root,
            banners_dir=root / "banners",
            sfx_dir=root / "sfx",
            outputs_dir=root / "outputs",
            cache_dir=root / "cache",
            uploads_dir=root / "uploads",
        )

    @property
    def workspace_dir(self) -> Path:
        return self.banners_dir.parent

    def ensure_dirs(self) -> None:
        for directory in (
            self.banners_dir,
            self.sfx_dir,
            self.outputs_dir,
            self.cache_dir,
            self.uploads_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
