"""Пути, лимиты и константы ClipBanner Bot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FPS = 30  # частота кадров всех готовых роликов

VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv"})  # что можно прислать боту файлом
MIN_CACHE_BYTES = 100 * 1024  # файл кеша меньше этого — остаток оборванной загрузки
BANNER_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
BANNER_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".m4v"})
BANNER_EXTS = BANNER_IMAGE_EXTS | BANNER_VIDEO_EXTS  # всё, что можно выбрать баннером
BANNER_SIZE = (920, 360)  # размер всплывающих баннеров-пресетов, px
STRIP_SIZE = (1080, 300)  # размер баннеров-полос для постоянного баннера, px

# Лимиты Telegram Bot API. Локальный Bot API server снимает их, но обычный бот живёт с ними.
TELEGRAM_MAX_UPLOAD_MB = 20  # столько можно прислать боту обычным ботом через облачный API
TELEGRAM_MAX_DOWNLOAD_MB = 50  # столько бот может отдать пользователю
TELEGRAM_CAPTION_LIMIT = 1024  # символов в подписи к видео

DEFAULT_MAX_HEIGHT = 1080  # потолок качества скачивания с YouTube
REENCODE_HEIGHT = 1280  # если клип не влезает в лимит, перекодируем в 720x1280
REENCODE_CRF = 26
REENCODE_AUDIO_BITRATE = "128k"

# Главный рекламный баннер проекта: приходит вместе с ботом, копируется в папку баннеров.
DEFAULT_BANNER_FILE = "TGrader-banner-7s.mp4"
DEFAULT_BANNER_LABEL = "TGrader (7 с, со звуком)"


@dataclass(frozen=True)
class PresetSpec:
    """Готовый рекламный баннер: рисуется Pillow при первом запуске."""

    id: str
    file: str  # имя файла в папке баннеров
    label: str  # как называется в интерфейсе
    brand: str
    subtitle: str
    cta: str  # призыв к действию на плашке
    tag: str  # промо-тег в углу
    colors: tuple[str, str]  # градиент карточки: левый верх → правый низ
    accent: str  # цвет плашки CTA

    @property
    def strip_file(self) -> str:
        """Имя файла широкой полосы того же бренда (для постоянного баннера в поле кадра)."""
        return f"strip_{self.id}.png"


DEFAULT_PRESET = PresetSpec(
    id="tgrader",
    file="preset_tgrader.png",
    label="TGrader",
    brand="TGRADER",
    subtitle="Оценка и апгрейд Telegram-активов",
    cta="Забирай бонус по ссылке в профиле",
    tag="БОНУС",
    colors=("#1e3a8a", "#7c3aed"),
    accent="#38bdf8",
)

PRESETS = (
    DEFAULT_PRESET,
    PresetSpec(
        id="rubup",
        file="preset_rubup.png",
        label="RubUp",
        brand="RUBUP",
        subtitle="Быстрый буст и задания каждый день",
        cta="Переходи в бота",
        tag="ХИТ",
        colors=("#065f46", "#0891b2"),
        accent="#facc15",
    ),
    PresetSpec(
        id="musor_drop",
        file="preset_musor_drop.png",
        label="Musor Drop",
        brand="MUSOR DROP",
        subtitle="Ежедневные дропы и раздачи",
        cta="Забирай свой дроп прямо сейчас",
        tag="DROP",
        colors=("#ea580c", "#be123c"),
        accent="#a3e635",
    ),
)

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
    assets_dir: Path = ASSETS_DIR

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
            assets_dir=ASSETS_DIR,
        )

    @property
    def workspace_dir(self) -> Path:
        return self.banners_dir.parent

    @property
    def asset_banners_dir(self) -> Path:
        """Баннеры, которые поставляются вместе с ботом (в git), — отсюда копируются в рабочую папку."""
        return self.assets_dir / "banners"

    def ensure_dirs(self) -> None:
        for directory in (
            self.banners_dir,
            self.sfx_dir,
            self.outputs_dir,
            self.cache_dir,
            self.uploads_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
