"""Схемы запросов, карточек видео и состояния задач нарезки."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class FrameMode(StrEnum):
    BLUR_PAD = "blur_pad"  # холст 9:16: видео целиком по центру, сверху и снизу размытый фон
    CENTER_CROP = "center_crop"  # холст 9:16 целиком занят картинкой: кроп по центру или по лицу
    ORIGINAL = "original"  # 16:9, 1920x1080


class SlicingMode(StrEnum):
    SEQUENTIAL = "sequential"  # подряд друг за другом
    EVENLY_SPACED = "evenly_spaced"  # равномерно по всему видео
    HIGHLIGHTS = "highlights"  # из каждой части ролика — самый громкий и живой момент (по звуку)


class CutMode(StrEnum):
    """Как пользователь настраивает нарезку: всё сам или доверяет автоподбору."""

    AUTO = "auto"
    MANUAL = "manual"


class Goal(StrEnum):
    """Куда пойдут клипы: от этого зависят их число и длина в автоподборе."""

    TIKTOK = "tiktok"
    SHORTS = "shorts"
    UNIVERSAL = "universal"


class BannerMode(StrEnum):
    FREEZE = "freeze"  # остановка видео и звука + затемнение
    OVERLAY = "overlay"  # баннер поверх идущего видео


class BannerPosition(StrEnum):
    CENTER = "center"
    TOP = "top"
    BOTTOM = "bottom"


class SourceType(StrEnum):
    LINK = "link"  # ссылка на ролик: YouTube, RuTube, VK Видео и другие площадки
    UPLOAD = "upload"  # видеофайл, присланный боту


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class ClipSettings(BaseModel):
    """Настройки, которые пользователь крутит в боте (без источника — их можно сохранить и переиспользовать)."""

    num_clips: int = Field(5, ge=1, le=50)
    clip_duration: float = Field(30.0, ge=2.0, le=180.0)  # чистая длительность, без пауз
    start_offset: float = Field(0.0, ge=0.0)  # вручную: сколько секунд отрезать в начале
    end_offset: float = Field(0.0, ge=0.0)  # вручную: сколько секунд отрезать в конце
    auto_trim: bool = False  # True: начало и конец ищет программа, ручные смещения игнорируются
    slicing_mode: SlicingMode = SlicingMode.EVENLY_SPACED
    frame_mode: FrameMode = FrameMode.BLUR_PAD

    banner_id: str | None = None  # имя файла баннера из папки баннеров
    banner_count: int = Field(2, ge=0, le=5)
    banner_duration: float = Field(2.5, ge=1.0, le=20.0)  # у видео-баннера длительность берётся из самого файла
    banner_mode: BannerMode = BannerMode.FREEZE
    banner_position: BannerPosition = BannerPosition.CENTER
    banner_scale: int = Field(85, ge=50, le=95)  # % ширины кадра
    banner_sound: bool = True  # звук видео-баннера (если он есть в файле)
    enable_sfx: bool = False

    max_height: int = Field(1080, ge=144, le=2160)  # потолок качества при скачивании


class VideoInfo(BaseModel):
    """Сведения о ролике для карточки в боте."""

    url: str
    title: str
    duration: float
    platform: str | None = None  # id площадки из `app.services.platforms`
    uploader: str | None = None
    thumbnail: str | None = None


class Draft(BaseModel):
    """Черновик задачи: выбранный источник + текущие настройки (живёт в FSM-состоянии диалога)."""

    source_type: SourceType = SourceType.LINK
    url: str | None = None
    upload_id: str | None = None
    platform: str | None = None  # id площадки ролика (`None` — присланный файл)
    title: str = "Видео"
    uploader: str | None = None
    duration: float = 0.0
    thumbnail: str | None = None
    settings: ClipSettings = Field(default_factory=ClipSettings)
    mode: CutMode | None = None  # None: человек ещё не выбрал, как нарезать
    goal: Goal = Goal.TIKTOK  # цель автоподбора

    @classmethod
    def from_video(cls, info: VideoInfo, settings: ClipSettings) -> "Draft":
        """Черновик из карточки ролика по ссылке."""
        return cls(
            source_type=SourceType.LINK,
            url=info.url,
            platform=info.platform,
            title=info.title,
            uploader=info.uploader,
            duration=info.duration,
            thumbnail=info.thumbnail,
            settings=settings,
        )

    @classmethod
    def from_upload(cls, upload_id: str, title: str, duration: float, settings: ClipSettings) -> "Draft":
        """Черновик из присланного файла."""
        return cls(source_type=SourceType.UPLOAD, upload_id=upload_id, title=title, duration=duration, settings=settings)

    def to_request(self) -> "JobRequest":
        """Превращает черновик в запрос для менеджера задач.

        Название, площадка и ключ источника едут вместе с задачей: по ним пишется история нарезок, чтобы потом
        можно было показать «что резали» и предложить «нарезать иначе» без повторного поиска ролика.
        """
        from app.services.platforms import identify  # локальный импорт: сервисы сами импортируют модели

        source = identify(self.url or "") if self.source_type is SourceType.LINK else None
        return JobRequest(
            source_type=self.source_type,
            url=self.url,
            upload_id=self.upload_id,
            title=self.title,
            platform=self.platform,
            source_key=source.key if source is not None else (self.upload_id or ""),
            mode=self.mode.value if self.mode else "",
            **self.settings.model_dump(),
        )


class JobRequest(ClipSettings):
    """Параметры одной задачи нарезки: источник + настройки (все поля настроек — унаследованы)."""

    source_type: SourceType = SourceType.LINK
    url: str | None = None
    upload_id: str | None = None
    title: str = ""  # название ролика: нужно истории и итогам, на саму нарезку не влияет
    platform: str | None = None  # id площадки из `app.services.platforms`
    source_key: str = ""  # ключ источника в кеше (`platforms.Source.key`) или id загрузки
    mode: str = ""  # «auto» или «manual»: чем человек резал (для истории)

    @model_validator(mode="after")
    def _check_source_and_banner(self) -> "JobRequest":
        if self.source_type is SourceType.LINK and not (self.url and self.url.strip()):
            raise ValueError("Укажите ссылку на ролик.")
        if self.source_type is SourceType.UPLOAD and not self.upload_id:
            raise ValueError("Сначала пришлите видеофайл.")
        if self.banner_count > 0 and not self.banner_id:
            raise ValueError("Выберите баннер или поставьте 0 показов на клип.")
        return self


class ClipResult(BaseModel):
    """Готовый вертикальный ролик."""

    index: int  # с 1
    filename: str  # имя файла внутри папки задачи
    source_start: float  # где клип начинается в исходном видео, с
    source_end: float
    duration: float  # длительность готового файла (с учётом пауз под баннер)
    banner_points: list[float] = Field(default_factory=list)  # секунды КЛИПА, где видео встаёт под баннер
    banner_windows: list[tuple[float, float]] = Field(default_factory=list)  # окна показа на таймлайне ролика
    render_seconds: float = 0.0  # сколько секунд занял рендер этого клипа (для истории и поиска деградации)


class TrimInfo(BaseModel):
    """Какой отрезок источника взят в работу и что из него убрано."""

    auto: bool  # границы нашла программа (иначе заданы вручную)
    start: float  # с какой секунды источника берутся клипы
    end: float  # по какую
    head: str | None = None  # что убрано в начале (по-русски)
    tail: str | None = None  # и в конце


class JobState(BaseModel):
    """Состояние задачи: его показывает сообщение-прогресс в боте."""

    id: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0  # 0..100
    stage: str = "В очереди"
    log: list[str] = Field(default_factory=list)
    clips: list[ClipResult] = Field(default_factory=list)
    total_clips: int = 0  # сколько клипов решено сделать (известно после расчёта плана; 0 — ещё считаем)
    trim: TrimInfo | None = None  # None, если начало и конец не обрезались
    archive_name: str | None = None  # имя ZIP-архива в папке задачи
    error: str | None = None
