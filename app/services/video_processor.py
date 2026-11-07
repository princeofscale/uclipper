"""Движок нарезки: планирование окон, граф FFmpeg и рендер клипов.

Один проход FFmpeg на клип: быстрый seek `-ss/-t` перед `-i` + один `filter_complex`
(split → trim → tpad/apad → concat → drawbox → overlay). Ни одного временного файла.
"""
from __future__ import annotations

import json
import math
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal

from app.config import BANNER_VIDEO_EXTS, FPS, TELEGRAM_MAX_DOWNLOAD_MB
from app.services.capacity import ffmpeg_threads
from app.models import BannerMode, BannerPosition, FrameMode, SlicingMode
from app.services.face_center import detect_face_center_x

PREVIEW_SIZE = (540, 960)  # черновик: вчетверо меньше пикселей, чем 1080×1920
MIN_CLIP_SECONDS = 1.0
SAMPLE_RATE = 44100
READ_MARGIN = 0.5  # источник читается на полсекунды дольше клипа: точный конец задаёт trim в графе
PORTRAIT = (1080, 1920)  # 9:16 — blur_pad и center_crop
LANDSCAPE = (1920, 1080)  # режим original
FRAME_SIZE = {FrameMode.BLUR_PAD: PORTRAIT, FrameMode.CENTER_CROP: PORTRAIT, FrameMode.ORIGINAL: LANDSCAPE}
DIM_COLOR = "black@0.45"  # затемнение замороженного кадра на 45%
MAX_VIDEO_PAUSE = 20.0  # пауза под видео-баннер не длиннее этого, даже если сам файл длиннее
MIN_VIDEO_PAUSE = 1.0
BANNER_Y = {  # верх баннера; доли высоты кадра дают те же 180 / 240 px на 1920 и разумные отступы на 1080
    BannerPosition.CENTER: "(H-h)/2",
    BannerPosition.TOP: "H*0.09375",
    BannerPosition.BOTTOM: "H-h-H*0.125",
}


def snap(seconds: float) -> float:
    """Привязывает время к сетке кадров 1/FPS."""
    return round(seconds * FPS) / FPS


# ---------------------------------------------------------------------------
# Окна клипов в исходном видео
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipSchedule:
    starts: list[float]  # начало каждого клипа в источнике, с
    clip_duration: float  # итоговая «чистая» длительность клипа, с
    slicing_mode: SlicingMode  # режим, который реально применён
    warnings: list[str]  # что пришлось подкорректировать (по-русски)


def compute_clip_schedule(
    source_duration: float,
    start_offset: float,
    num_clips: int,
    clip_duration: float,
    slicing_mode: SlicingMode,
    end_offset: float = 0.0,
) -> ClipSchedule:
    """Раскладывает `num_clips` клипов по `clip_duration` с в видео длиной `source_duration`.

    Клипы берутся только из отрезка `[start_offset, source_duration − end_offset]`. Не падает на «неудобных» параметрах: слишком длинный клип укорачивается, а `sequential`,
    которому не хватает видео, переключается на `evenly_spaced` — обо всём пишется предупреждение.
    """
    usable_end = source_duration - end_offset
    available = usable_end - start_offset
    if available < MIN_CLIP_SECONDS:
        raise ValueError(
            f"Смещение от начала {start_offset:g} с и от конца {end_offset:g} с оставляют меньше "
            f"{MIN_CLIP_SECONDS:g} с видео (длина источника {source_duration:.1f} с) — уменьшите смещения."
        )

    warnings: list[str] = []
    frames = round(clip_duration * FPS)
    max_frames = math.floor(available * FPS + 1e-9)
    if frames > max_frames:
        frames = max_frames
        warnings.append(
            f"Клип {clip_duration:g} с не помещается в оставшиеся {available:.1f} с видео — "
            f"длительность уменьшена до {frames / FPS:.1f} с."
        )
    duration = frames / FPS

    mode = slicing_mode
    if mode is SlicingMode.SEQUENTIAL and num_clips * duration > available + 1e-9:
        warnings.append(
            f"{num_clips} клипов по {duration:g} с подряд не помещаются в {available:.1f} с видео — "
            "клипы распределены равномерно по ролику."
        )
        mode = SlicingMode.EVENLY_SPACED

    if mode is SlicingMode.SEQUENTIAL:
        starts = [start_offset + m * duration for m in range(num_clips)]
    elif num_clips == 1:
        starts = [start_offset]
    else:
        step = (available - duration) / (num_clips - 1)
        starts = [min(start_offset + m * step, usable_end - duration) for m in range(num_clips)]
        if step < duration - 1e-9:
            warnings.append(
                f"Клипы перекрываются: шаг между началами {step:.1f} с меньше длительности клипа {duration:g} с."
            )
    return ClipSchedule(starts=starts, clip_duration=duration, slicing_mode=mode, warnings=warnings)


# ---------------------------------------------------------------------------
# Таймлайн баннеров внутри одного клипа
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BannerTimeline:
    clip_duration: float  # T — чистая длительность, привязанная к сетке кадров
    banner_duration: float  # D — длительность одной паузы/показа, привязанная к сетке кадров
    banner_mode: BannerMode
    cuts: tuple[float, ...]  # K+2 точек разреза клипа: 0 … T
    freeze_points: tuple[float, ...]  # K точек остановки по времени КЛИПА (p_1 … p_K)
    windows: tuple[tuple[float, float], ...]  # окна показа баннера на ВЫХОДНОМ таймлайне
    total_duration: float  # T + K·D (freeze) или T (overlay)


def plan_banner_timeline(
    clip_duration: float,
    banner_count: int,
    banner_duration: float,
    banner_mode: BannerMode,
) -> BannerTimeline:
    """Считает точки разреза и окна показа баннера по формулам из спеки.

    p_i = i·T/(K+1);  S_i = p_{i+1} + i·D;  E_i = S_i + D.  Все времена привязаны к сетке кадров,
    чтобы окно затемнения совпадало с замороженными кадрами с точностью до кадра.
    """
    clip = snap(clip_duration)
    pause = snap(banner_duration)
    segments = banner_count + 1
    cuts = tuple(snap(i * clip / segments) for i in range(segments)) + (clip,)

    freeze = banner_mode is BannerMode.FREEZE
    windows: list[tuple[float, float]] = []
    for i in range(banner_count):
        start = cuts[i + 1]
        if freeze:
            start += i * pause  # все предыдущие паузы сдвигают таймлайн
            windows.append((start, start + pause))
        else:
            windows.append((start, min(start + pause, clip)))

    total = clip + banner_count * pause if freeze else clip
    return BannerTimeline(
        clip_duration=clip,
        banner_duration=pause,
        banner_mode=banner_mode,
        cuts=cuts,
        freeze_points=cuts[1:-1],
        windows=tuple(windows),
        total_duration=total,
    )


# ---------------------------------------------------------------------------
# Чтение медиа
# ---------------------------------------------------------------------------


class MediaError(RuntimeError):
    """Ошибка чтения или обработки видео. Текст — по-русски, его увидит пользователь."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe_media(path: Path) -> MediaInfo:
    """Длительность, размер и наличие звука по `ffprobe`."""
    command = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise MediaError("Не найден ffprobe — установите FFmpeg и добавьте его в PATH.") from error
    if result.returncode != 0:
        raise MediaError("Не удалось прочитать видеофайл: он повреждён или это не видео.")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise MediaError("Не удалось прочитать видеофайл: ffprobe вернул непонятный ответ.") from error

    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("В файле нет видеодорожки.")
    try:
        duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise MediaError("Не удалось определить длительность видео.")
    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        has_audio=audio is not None,
    )


@dataclass(frozen=True)
class BannerMedia:
    """Что за файл баннера: картинка (одна на всё время), GIF (петля на каждое окно) или видео (играет от начала в каждом окне)."""

    kind: Literal["image", "gif", "video"] = "image"
    duration: float | None = None  # у видео — его длина
    has_audio: bool = False


def probe_banner(path: Path) -> BannerMedia:
    """Определяет вид баннера по расширению; видео дополнительно читается ffprobe (длина и звук)."""
    suffix = path.suffix.lower()
    if suffix in BANNER_VIDEO_EXTS:
        info = probe_media(path)
        return BannerMedia("video", info.duration, info.has_audio)
    return BannerMedia("gif" if suffix == ".gif" else "image")


# ---------------------------------------------------------------------------
# Граф FFmpeg
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderOptions:
    """Всё, что влияет на вид одного клипа (значения по умолчанию — как в `JobRequest`)."""

    frame_mode: FrameMode = FrameMode.BLUR_PAD
    banner_mode: BannerMode = BannerMode.FREEZE
    banner_count: int = 2
    banner_duration: float = 2.5
    banner_position: BannerPosition = BannerPosition.CENTER
    banner_scale: int = 85  # % короткой стороны кадра (в вертикальных режимах это ширина)
    banner_sound: bool = True  # слышен ли звук видео-баннера
    enable_sfx: bool = False
    preview: bool = False  # черновик: кадр уменьшается до 540×960 и рендер идёт на ultrafast
    subtitles: Path | None = None  # готовый .ass для этого клипа; вшивается до пауз и баннера


# У анаморфных роликов и записей старого ТВ пиксель не квадратный (SAR ≠ 1:1): на экране кадр шире (или уже), чем
# хранится в файле. Остальные фильтры SAR не учитывают, и такой ролик получался бы сплюснутым. Поэтому кадр сначала
# растягивается до настоящих пропорций (размер ни в какую сторону не уменьшается), а дальше все пиксели квадратные.
# У обычных роликов SAR 1:1: размеры остаются прежними (в том числе нечётные), и фильтр ничего не делает.
SQUARE_PIXELS = "scale=w='if(gt(sar,1),trunc(iw*sar/2)*2,iw)':h='if(lt(sar,1),trunc(ih/sar/2)*2,ih)',setsar=1"


def _base_video_chain(frame_mode: FrameMode, face_cx: float | None) -> str:
    """Приводит кадр к целевому формату → метка `[v_base]` (30 fps, yuv420p, SAR 1)."""
    width, height = FRAME_SIZE[frame_mode]
    if frame_mode is FrameMode.CENTER_CROP:
        # Картинка на весь кадр; окно кропа по умолчанию по центру, а при найденном лице — вокруг него
        # (но не дальше границ картинки).
        x = "(iw-ow)/2" if face_cx is None else f"'min(max({face_cx:.4f}*iw-ow/2,0),iw-ow)'"
        return (
            f"[0:v]fps={FPS},{SQUARE_PIXELS},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:x={x}:y=(ih-oh)/2,format=yuv420p,setsar=1[v_base]"
        )
    if frame_mode is FrameMode.ORIGINAL:
        return (
            f"[0:v]fps={FPS},{SQUARE_PIXELS},scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,setsar=1[v_base]"
        )
    # Blur-Pad. Фон считается на кадре 270×480 и растягивается: блюр радиуса 25 на 1080×1920 визуально тот же
    # (PSNR 41.8 dB), а рендер в ~3 раза быстрее.
    return (
        f"[0:v]fps={FPS},{SQUARE_PIXELS},split=2[bg][fg];"
        f"[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:5,"
        f"eq=brightness=-0.15,scale={width}:{height}:flags=bicubic[bg_blur];"
        f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg_scaled];"
        f"[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,format=yuv420p,setsar=1[v_base]"
    )


def _read_seconds(timeline: BannerTimeline) -> float:
    """Сколько секунд источника читает FFmpeg (`-t` на входе)."""
    return timeline.clip_duration + READ_MARGIN


def _audio_prep_chain(has_audio: bool, read_seconds: float) -> str:
    """Звук источника в единый формат → метка `[a_prep]`; без дорожки — тишина (`anullsrc`).

    `apad=whole_dur` дописывает тишину, если звук кончается раньше видео, — иначе `concat` теряет
    последние сегменты клипа.
    """
    layout = "aformat=sample_fmts=fltp:channel_layouts=stereo"
    if has_audio:
        return f"[0:a]aresample={SAMPLE_RATE},{layout},apad=whole_dur={read_seconds:.3f}[a_prep]"
    return f"anullsrc=r={SAMPLE_RATE}:cl=stereo:d={read_seconds:.3f},{layout}[a_prep]"


def _freeze_segments(timeline: BannerTimeline, source: str = "v_base") -> list[str]:
    """Режет клип на K+1 сегментов; первые K заканчиваются стоп-кадром и тишиной → `[v_cat]`, `[a_cat]`.

    Видео режется по номерам кадров (точки разреза уже на сетке 1/FPS), `tpad` клонирует последний кадр
    ровно на D·FPS кадров, `apad` добавляет D секунд тишины.
    """
    pauses = len(timeline.freeze_points)
    segments = pauses + 1
    pause_frames = round(timeline.banner_duration * FPS)
    pause_seconds = f"{timeline.banner_duration:.6f}"
    chains = [
        f"[{source}]split={segments}" + "".join(f"[vs{i}]" for i in range(segments)),
        f"[a_prep]asplit={segments}" + "".join(f"[as{i}]" for i in range(segments)),
    ]
    for i in range(segments):
        first, last = round(timeline.cuts[i] * FPS), round(timeline.cuts[i + 1] * FPS)
        video = f"[vs{i}]trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS"
        audio = f"[as{i}]atrim=start={timeline.cuts[i]:.6f}:end={timeline.cuts[i + 1]:.6f},asetpts=PTS-STARTPTS"
        if i < pauses:
            video += f",tpad=stop_mode=clone:stop={pause_frames}"
            audio += f",apad=pad_dur={pause_seconds}"
        chains += [f"{video}[vf{i}]", f"{audio}[af{i}]"]
    chains.append("".join(f"[vf{i}][af{i}]" for i in range(segments)) + f"concat=n={segments}:v=1:a=1[v_joined][a_cat]")
    # `tpad` в ffmpeg 7.1 (он стоит в образе бота) выдаёт все клоны стоп-кадра с ОДНОЙ меткой времени, а `overlay`
    # подбирает кадр баннера именно по метке — и на всё окно показа брал бы первый кадр: баннер «замирал», звук шёл.
    # Кадры идут ровно через 1/FPS без пропусков, поэтому метки пересчитываются по номеру кадра — так верно везде.
    chains.append(f"[v_joined]setpts=N/({FPS}*TB)[v_cat]")
    return chains


def _enable_windows(timeline: BannerTimeline) -> str:
    """Выражение `enable`: «внутри любого окна показа». Границы — посреди кадра, чтобы кадр на стыке не попал в чужое окно."""
    half_frame = 0.5 / FPS
    return "+".join(f"between(t,{start - half_frame:.4f},{end - half_frame:.4f})" for start, end in timeline.windows)


def _dim_chain(timeline: BannerTimeline, video: str) -> list[str]:
    """Затемняет замороженный кадр на время показа → метка `[v_dim]`."""
    return [f"[{video}]drawbox=x=0:y=0:w=iw:h=ih:color={DIM_COLOR}:t=fill:enable='{_enable_windows(timeline)}'[v_dim]"]


def _popup_overlay(timeline: BannerTimeline, opts: RenderOptions, banner_idx: int, video: str, *, animated: bool) -> list[str]:
    """Кладёт всплывающий баннер только внутри окон показа → метка `[v_out]`.

    Картинка — один вход на все окна. Анимированный баннер (GIF, видео) — по входу на каждое окно: его кадры
    сдвигаются на начало окна, так что каждый показ идёт с первого кадра, а после окна наложение просто пропускает ролик.
    """
    # Ширина баннера — доля короткой стороны кадра: в вертикальных режимах это ширина кадра (как в спеке),
    # а в горизонтальном original баннер не разрастается на 85% от 1920.
    banner_width = int(round(min(FRAME_SIZE[opts.frame_mode]) * opts.banner_scale / 100)) // 2 * 2
    position = f"x=(W-w)/2:y={BANNER_Y[opts.banner_position]}"
    windows = timeline.windows
    if not animated:
        return [
            f"[{banner_idx}:v]scale={banner_width}:-2,format=rgba,setsar=1[banner]",
            f"[{video}][banner]overlay={position}:enable='{_enable_windows(timeline)}'[v_out]",
        ]

    chains: list[str] = []
    for i, (start, end) in enumerate(windows):
        chains.append(
            f"[{banner_idx + i}:v]fps={FPS},trim=duration={end - start:.6f},setpts=PTS-STARTPTS+{start:.6f}/TB,"
            f"scale={banner_width}:-2,format=rgba,setsar=1[banner{i}]"
        )
    half_frame = 0.5 / FPS
    for i, (start, end) in enumerate(windows):
        target = "v_out" if i == len(windows) - 1 else f"v_with{i}"
        window = f"between(t,{start - half_frame:.4f},{end - half_frame:.4f})"
        chains.append(f"[{video}][banner{i}]overlay={position}:eof_action=pass:enable='{window}'[{target}]")
        video = target
    return chains


def _audio_mix(timeline: BannerTimeline, audio: str, *, sfx_idx: int | None, banner_audio_idx: int | None) -> list[str]:
    """Накладывает на звук ролика «хлопок» и/или звук видео-баннера в начале каждого окна → метка `[a_out]`.

    Без того и другого звук проходит как есть.
    """
    windows = timeline.windows
    count = len(windows)
    chains: list[str] = []
    extras: list[str] = []
    if sfx_idx is not None:
        chains.append(
            f"[{sfx_idx}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,asplit={count}"
            + "".join(f"[pop{i}]" for i in range(count))
        )
        for i, (start, _) in enumerate(windows):
            delay_ms = round(start * 1000)
            chains.append(f"[pop{i}]adelay={delay_ms}|{delay_ms}[delayed{i}]")
            extras.append(f"delayed{i}")
    if banner_audio_idx is not None:
        for i, (start, end) in enumerate(windows):
            delay_ms = round(start * 1000)
            chains.append(
                f"[{banner_audio_idx + i}:a]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"atrim=duration={end - start:.6f},asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}[banner_sound{i}]"
            )
            extras.append(f"banner_sound{i}")
    if not extras:
        return [f"[{audio}]anull[a_out]"]
    # normalize=0: иначе amix делит громкость на число входов и заглушает исходный звук
    mix_inputs = f"[{audio}]" + "".join(f"[{label}]" for label in extras)
    chains.append(f"{mix_inputs}amix=inputs={len(extras) + 1}:duration=first:dropout_transition=0:normalize=0[a_out]")
    return chains


def _ass_path(path: Path) -> str:
    """Путь для `filter_complex`: двоеточие и обратный слэш ломают разбор, их экранируем.

    В бою файл всегда лежит в папке задачи, и сюда попадает простое имя — но полагаться на это нельзя.
    """
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")


@lru_cache(maxsize=1)
def has_libass() -> bool:
    """Есть ли в этой сборке FFmpeg фильтр `ass` (libass). В контейнере бота есть, в локальной сборке — нет.

    Проверяется один раз за процесс: если фильтра нет, субтитры не вшиваются вообще, иначе рендер падал бы.
    """
    try:
        done = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return " ass " in (done.stdout or "")


def _preview_scaled(graph: str) -> str:
    """Добавляет в конец графа уменьшение до `PREVIEW_SIZE`. Вся геометрия (баннер, паузы, затемнение)
    считается как для боевого клипа — уменьшается только итоговый кадр, поэтому черновик показывает
    ровно то, что выйдет в 1080p.
    """
    width, height = PREVIEW_SIZE
    return f"{graph};[v_out]scale={width}:{height}:flags=bilinear[v_preview]"


def build_filter_graph(
    timeline: BannerTimeline,
    opts: RenderOptions,
    *,
    has_audio: bool = True,
    banner_idx: int | None = None,
    sfx_idx: int | None = None,
    face_cx: float | None = None,
    banner: BannerMedia | None = None,
) -> str:
    """Весь `filter_complex` одного клипа. Выходные метки: `[v_out]` и `[a_out]`.

    Входы: `[0:…]` — источник; с `banner_idx` — баннер (у GIF и видео по одному входу на окно показа подряд);
    `[sfx_idx:a]` — звук появления баннера. Слои: ролик → затемнение (только при остановке) → баннер.
    """
    media = banner or BannerMedia()
    chains = [_base_video_chain(opts.frame_mode, face_cx), _audio_prep_chain(has_audio, _read_seconds(timeline))]
    video, audio = "v_base", "a_prep"
    if opts.subtitles is not None:  # субтитры вшиваются до пауз: на паузе кадр замирает вместе с текстом
        chains.append(f"[v_base]ass={_ass_path(opts.subtitles)}[v_subs]")
        video = "v_subs"
    shown = bool(timeline.windows)
    if shown:
        if banner_idx is None:
            raise ValueError("banner_idx обязателен, когда есть окна показа баннера")
        if timeline.banner_mode is BannerMode.FREEZE:  # в overlay видео и звук идут без остановок и без затемнения
            chains += _freeze_segments(timeline, video)
            chains += _dim_chain(timeline, "v_cat")
            video, audio = "v_dim", "a_cat"
    if not shown:
        chains += [f"[{video}]null[v_out]", f"[{audio}]anull[a_out]"]
        return ";".join(chains)

    chains += _popup_overlay(timeline, opts, banner_idx, video, animated=media.kind != "image")
    own_sound = media.kind == "video" and media.has_audio and opts.banner_sound
    chains += _audio_mix(timeline, audio, sfx_idx=sfx_idx, banner_audio_idx=banner_idx if own_sound else None)
    return ";".join(chains)


def _bitrate_cap(total_seconds: float) -> int | None:
    """Потолок битрейта, чтобы клип влез в лимит Telegram без второго перекодирования.

    Клип тяжелее 50 МБ бот сжимает отдельным проходом (`delivery.shrink_clip`) — это ещё один полный
    энкод и потеря качества. Дешевле сразу ограничить битрейт: для клипа 59 с лимит не срабатывает
    вообще (там и так меньше), а для длинного 200-секундного — встаёт на ~2 Мбит/с и клип уходит как есть.
    """
    if total_seconds <= 0:
        return None
    budget = TELEGRAM_MAX_DOWNLOAD_MB * 1024 * 1024 * 8 / total_seconds * 0.9  # 10% запаса на контейнер
    return int(budget) if budget < 8_000_000 else None  # до 8 Мбит/с CRF и так справляется


def build_ffmpeg_command(
    source: Path,
    output: Path,
    clip_start: float,
    timeline: BannerTimeline,
    opts: RenderOptions,
    *,
    has_audio: bool = True,
    banner: Path | None = None,
    banner_media: BannerMedia | None = None,
    sfx: Path | None = None,
    face_cx: float | None = None,
    threads: int | None = None,
) -> list[str]:
    """Команда FFmpeg: быстрый seek `-ss/-t` перед `-i`, один `filter_complex`, H.264 + AAC.

    Порядок входов: источник, баннер (у GIF/видео — по входу на окно), звук SFX.
    `threads` — сколько потоков разрешено процессору контейнера (`None` — ffmpeg решает сам).
    """
    # Черновик: ultrafast и crf 30 — вчетверо меньше пикселей и самый быстрый пресет. Боевой рендер:
    # superfast crf 24 (замер: то же качество по SSIM, на четверть быстрее veryfast).
    preview_encoder = ["-preset", "ultrafast", "-crf", "30"] if opts.preview else ["-preset", "superfast", "-crf", "24"]
    limit = ["-threads", str(threads)] if threads else []  # ключ перед `-i` относится к декодеру этого входа
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    if threads:
        command += ["-filter_threads", str(threads), "-filter_complex_threads", str(threads)]
    command += [*limit, "-ss", f"{clip_start:.3f}", "-t", f"{_read_seconds(timeline):.3f}", "-i", str(source)]
    media = banner_media or BannerMedia()
    next_input = 1
    banner_idx = sfx_idx = None
    if timeline.windows:
        if banner is not None and banner_media is None:
            media = probe_banner(banner)
        banner_idx = next_input
        if media.kind == "image":
            command += [*limit, "-i", str(banner)]
            next_input += 1
        else:  # GIF и видео: свой вход на каждое окно — так каждый показ идёт с первого кадра
            for start, end in timeline.windows:
                if media.kind == "gif":  # петля ровно на длину окна
                    command += ["-stream_loop", "-1", "-t", f"{end - start:.3f}"]
                command += [*limit, "-i", str(banner)]
            next_input += len(timeline.windows)
    if timeline.windows and sfx is not None:
        sfx_idx = next_input
        command += ["-i", str(sfx)]
    graph = build_filter_graph(
        timeline, opts, has_audio=has_audio, banner_idx=banner_idx, sfx_idx=sfx_idx, face_cx=face_cx,
        banner=media,
    )  # fmt: skip
    video_label = "[v_out]"
    if opts.preview:  # черновик: тот же граф, но итоговый кадр вчетверо меньше
        graph = _preview_scaled(graph)
        video_label = "[v_preview]"
    command += [
        "-filter_complex", graph, "-map", video_label, "-map", "[a_out]",
        *limit,
        # superfast вместо veryfast: на замере 1080p60 в 1 ядро x264 ест больше половины времени рендера, а superfast
        # на crf 24 даёт то же качество (SSIM ≈ 0,994) при файле на 15% больше и быстрее на четверть.
        # `-r` не задаём: частоту кадров уже выставил фильтр `fps` в начале графа, а второй пересчёт только
        # пересобирает кадры и добавляет дрожание на стыках сегментов.
        *preview_encoder, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        # Ключевой кадр каждые 2 секунды: платформа и плеер нормально перематывают и режут клип,
        # цена — 2–5% битрейта.
        "-g", str(FPS * 2), "-keyint_min", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", str(SAMPLE_RATE), "-ac", "2",
        "-movflags", "+faststart", "-t", f"{timeline.total_duration:.3f}",
    ]  # fmt: skip
    cap = _bitrate_cap(timeline.total_duration)
    if cap:  # длинный клип: ограничиваем битрейт, чтобы он влез в лимит Telegram без второго энкода
        command += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
    command.append(str(output))
    return command


def _run_ffmpeg(command: list[str], total_seconds: float, on_progress: Callable[[float], None] | None) -> None:
    """Запускает FFmpeg, читает `-progress`, при ошибке бросает `MediaError` с хвостом stderr."""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError as error:
        raise MediaError("Не найден ffmpeg — установите FFmpeg и добавьте его в PATH.") from error

    errors: deque[str] = deque(maxlen=40)

    def drain_stderr() -> None:
        for line in process.stderr:
            errors.append(line.rstrip())

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()
    try:
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if on_progress and key in ("out_time_us", "out_time_ms") and value.isdigit():
                on_progress(min(1.0, int(value) / 1_000_000 / total_seconds))
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
    reader.join(timeout=5)
    if code != 0:
        raise MediaError("FFmpeg завершился с ошибкой:\n" + "\n".join(errors))


def render_clip(
    source: Path,
    output: Path,
    clip_start: float,
    clip_duration: float,
    opts: RenderOptions,
    *,
    info: MediaInfo,
    banner: Path | None = None,
    sfx: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
    threads: int | None = None,
) -> BannerTimeline:
    """Рендерит один вертикальный клип за один проход FFmpeg и возвращает его таймлайн.

    `threads` — сколько потоков отдать ffmpeg этой задачи: когда нарезок несколько, менеджер делит ядра между ними.
    """
    wants_banner = opts.banner_count > 0
    if wants_banner and banner is None:
        raise MediaError("Для показа баннера нужен файл баннера — выберите баннер или поставьте 0 показов.")
    media = probe_banner(banner) if wants_banner and banner is not None else BannerMedia()
    pause = opts.banner_duration
    if media.kind == "video" and media.duration:  # видео-баннер задаёт паузу сам: ни кадры, ни звук не обрезаются
        pause = min(max(media.duration, MIN_VIDEO_PAUSE), MAX_VIDEO_PAUSE)
    timeline = plan_banner_timeline(clip_duration, opts.banner_count, pause, opts.banner_mode)
    use_sfx = opts.enable_sfx and bool(timeline.windows)
    if use_sfx and sfx is None:
        raise MediaError("Для звукового эффекта нужен файл звука — отключите SFX или создайте pop.wav.")
    face_cx = None
    if opts.frame_mode is FrameMode.CENTER_CROP:
        face_cx = detect_face_center_x(source, clip_start, timeline.clip_duration)
    command = build_ffmpeg_command(
        source, output, clip_start, timeline, opts,
        has_audio=info.has_audio, banner=banner, banner_media=media,
        sfx=sfx if use_sfx else None, face_cx=face_cx,
        threads=threads if threads is not None else ffmpeg_threads(),
    )  # fmt: skip
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_ffmpeg(command, timeline.total_duration, on_progress)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return timeline
