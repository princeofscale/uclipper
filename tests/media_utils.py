"""Помощники тестов: синтетические видео и измерения готовых роликов (ffmpeg/ffprobe + Pillow).

Синтетика «кодирует время»: яркость кадра растёт линейно (luma = 255·t/длина), а звук — чирп,
частота которого растёт со временем (200 + 200·t Гц). По яркости и частоте в любой точке готового ролика
можно определить, из какой секунды источника взят этот кусок — так проверяется, что контент после паузы
продолжается ровно с того места, где остановился.
"""
from __future__ import annotations

import json
import re
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat


def run_ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def make_video(
    path: Path,
    *,
    size: str = "1280x720",
    seconds: int = 20,
    audio: bool = True,
    fps: int = 30,
    vcodec: str = "libx264",
    audio_seconds: int | None = None,
    video_filter: str | None = None,
) -> Path:
    """Синтетическое видео через `ffmpeg -f lavfi`: растущая яркость + (опционально) чирп.

    `audio_seconds` делает звук короче видео; `video_filter` заменяет картинку (по умолчанию — яркость от времени);
    `vcodec="ffv1"` нужен для нечётных размеров, которые x264 не кодирует.
    """
    picture = video_filter or f"geq=lum='255*T/{seconds}':cb=128:cr=128"
    args = ["-f", "lavfi", "-i", f"color=c=black:s={size}:r={fps}:d={seconds},format=yuv420p,{picture}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"aevalsrc='0.3*sin(2*PI*(200+100*t)*t)':s=44100:d={audio_seconds or seconds}"]
    args += ["-c:v", vcodec, "-pix_fmt", "yuv420p"]
    if vcodec == "libx264":
        args += ["-preset", "ultrafast"]
    if audio:
        args += ["-c:a", "aac"] + ([] if audio_seconds else ["-shortest"])
    else:
        args += ["-an"]
    run_ffmpeg(*args, str(path))
    return path


def make_gif(path: Path, colors: list[tuple[int, int, int]], *, frame_ms: int = 500, size: tuple[int, int] = (920, 360)) -> Path:
    """Зацикленный GIF из однотонных кадров — по цвету пикселя видно, какой кадр показан."""
    frames = [Image.new("RGB", size, color) for color in colors]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=frame_ms, loop=0)
    return path


def make_color_video(
    path: Path,
    segments: list[tuple[str, float]],
    *,
    size: str = "320x180",
    tone_hz: int | None = None,
) -> Path:
    """Видео-баннер из однотонных кусков `(цвет ffmpeg, секунды)` подряд; `tone_hz` — звук-синус на всю длину.

    По цвету пикселя видно, какой кусок баннера показан в данный момент, а по частоте звука — что звук баннера
    слышен именно там, где должен.
    """
    total = sum(seconds for _, seconds in segments)
    args: list[str] = []
    for color, seconds in segments:
        args += ["-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d={seconds}"]
    if tone_hz:
        args += ["-f", "lavfi", "-i", f"sine=f={tone_hz}:d={total}:r=44100"]
    graph = "".join(f"[{i}:v]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=0,format=yuv420p[v]"
    args += ["-filter_complex", graph, "-map", "[v]"]
    if tone_hz:
        args += ["-map", f"{len(segments)}:a", "-c:a", "aac", "-ac", "2"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    run_ffmpeg(*args)
    return path


def make_episode(
    path: Path,
    *,
    head_black: float = 0,
    head_silent: float = 0,
    content: float = 20,
    dark_scene: float = 0,
    credits: float = 0,
    tail_black: float = 0,
    audio: bool = True,
    size: str = "320x180",
) -> Path:
    """Синтетическая «серия»: чёрный старт → картинка без звука → сюжет → (тёмная сцена) → титры → чёрный хвост; всё склеено в один mp4.

    Сюжет — яркая движущаяся тестовая картинка с тоном 440 Гц; чёрные куски — чёрный кадр и тишина; тёмная сцена —
    тёмно-серый шум без текста; титры — белые строки на чёрном, ползущие вверх, под ровный «музыкальный» тон.
    Ключевые кадры раз в 3 с, как у роликов YouTube.
    """
    width, height = (int(part) for part in size.split("x"))
    scratch = path.parent / f"{path.stem}_parts"
    scratch.mkdir(exist_ok=True)
    inputs: list[str] = []
    segments: list[str] = []  # метки видео/звука каждого куска по порядку
    count = 0

    def add(video_source: list[str], video_filter: str, tone: str | None, seconds: float) -> None:
        nonlocal count
        inputs.extend(video_source)
        index = count
        count += 1
        segments.append(f"[{index}:v]{video_filter},fps=30,scale={width}:{height},setsar=1,format=yuv420p[v{index}]")
        if audio:
            audio_source = f"sine=f={tone}:d={seconds}:r=44100" if tone else f"anullsrc=r=44100:cl=stereo:d={seconds}"
            inputs.extend(["-f", "lavfi", "-i", audio_source])
            count += 1
            segments.append(f"[{index + 1}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]")

    def lavfi(source: str) -> list[str]:
        return ["-f", "lavfi", "-i", source]

    if head_black:
        add(lavfi(f"color=c=black:s={size}:r=30:d={head_black}"), "null", None, head_black)
    if head_silent:  # картинка уже идёт, а звука ещё нет
        add(lavfi(f"testsrc2=s={size}:r=30:d={head_silent}"), "null", None, head_silent)
    if content:
        add(lavfi(f"testsrc2=s={size}:r=30:d={content}"), "null", "440", content)
    if dark_scene:
        add(lavfi(f"color=c=0x202020:s={size}:r=30:d={dark_scene}"), "noise=alls=12:allf=t", "330", dark_scene)
    if credits:
        sheet = scratch / "credits.png"
        text_image = Image.new("RGB", (width, height + int(credits * 40)), (0, 0, 0))
        draw = ImageDraw.Draw(text_image)
        font = ImageFont.load_default(size=max(10, height // 12))
        for line in range(int((text_image.height) / (height / 9))):
            draw.text((width // 8, line * height // 9), f"ACTOR {line:02d}   AS   ROLE {line:02d}", fill=(240, 240, 240), font=font)
        text_image.save(sheet)
        scroll = f"crop={width}:{height}:0:'min(t*40,ih-{height})'"
        add(["-loop", "1", "-framerate", "30", "-t", f"{credits}", "-i", str(sheet)], scroll, "220", credits)
    if tail_black:
        add(lavfi(f"color=c=black:s={size}:r=30:d={tail_black}"), "null", None, tail_black)

    step = 2 if audio else 1
    order = "".join(f"[v{i}]" + (f"[a{i}]" if audio else "") for i in range(0, count, step))
    pieces = count // step
    graph = ";".join(segments) + f";{order}concat=n={pieces}:v=1:a={1 if audio else 0}" + ("[v][a]" if audio else "[v]")
    args = [*inputs, "-filter_complex", graph, "-map", "[v]"] + (["-map", "[a]", "-c:a", "aac"] if audio else ["-an"])
    run_ffmpeg(*args, "-c:v", "libx264", "-preset", "ultrafast", "-g", "90", "-pix_fmt", "yuv420p", str(path))
    return path


def probe(path: Path) -> SimpleNamespace:
    data = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return SimpleNamespace(
        width=video["width"],
        height=video["height"],
        vcodec=video["codec_name"],
        acodec=audio["codec_name"] if audio else None,
        duration=float(data["format"]["duration"]),
        vduration=float(video["duration"]) if "duration" in video else None,
        aduration=float(audio["duration"]) if audio and "duration" in audio else None,
    )


def count_frames(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(out.strip())


def frame_png(video: Path, t: float, out: Path) -> Path:
    run_ffmpeg("-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out))
    return out


def rgb_at(video: Path, t: float, xy: tuple[int, int], scratch: Path) -> tuple[int, int, int]:
    with Image.open(frame_png(video, t, scratch / f"rgb_{video.stem}_{t:.3f}.png")) as image:
        return image.convert("RGB").getpixel(xy)


def luma_at(video: Path, t: float, xy: tuple[int, int], scratch: Path) -> int:
    with Image.open(frame_png(video, t, scratch / f"luma_{video.stem}_{t:.3f}.png")) as image:
        return image.convert("L").getpixel(xy)


def bright_span(video: Path, t: float, y: int, scratch: Path, threshold: int = 200) -> tuple[int, int] | None:
    """Крайние x ярких (> threshold) пикселей строки `y` кадра в момент t; None, если ярких пикселей нет."""
    with Image.open(frame_png(video, t, scratch / f"span_{video.stem}_{t:.3f}.png")) as image:
        row = image.convert("L").crop((0, y, image.width, y + 1))
        xs = [x for x in range(image.width) if row.getpixel((x, 0)) > threshold]
    return (xs[0], xs[-1]) if xs else None


def bright_rows(video: Path, t: float, x: int, scratch: Path, threshold: int = 200) -> tuple[int, int] | None:
    """Крайние y ярких (> threshold) пикселей столбца `x` кадра в момент t; None, если ярких пикселей нет."""
    with Image.open(frame_png(video, t, scratch / f"rows_{video.stem}_{t:.3f}.png")) as image:
        column = image.convert("L").crop((x, 0, x + 1, image.height))
        ys = [y for y in range(image.height) if column.getpixel((0, y)) > threshold]
    return (ys[0], ys[-1]) if ys else None


def mean_abs_diff(first: Path, second: Path) -> float:
    """Средняя абсолютная разница двух картинок по каналам RGB, 0..255."""
    with Image.open(first) as a, Image.open(second) as b:
        return sum(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean) / 3


def volume_db(video: Path, start: float, duration: float) -> tuple[float, float]:
    """(средняя, максимальная) громкость участка в dB; тишина → очень низкие значения (около −91)."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )

    def grab(name: str) -> float:
        match = re.search(rf"{name}: (-?[\d.]+|-inf) dB", result.stderr)
        assert match, result.stderr[-500:]
        return float("-inf") if match.group(1) == "-inf" else float(match.group(1))

    return grab("mean_volume"), grab("max_volume")


def audio_frequency(video: Path, start: float, duration: float, scratch: Path) -> float:
    """Частота моно-сигнала участка в Гц (по переходам через ноль)."""
    wav_path = scratch / f"tone_{video.stem}_{start:.3f}.wav"
    run_ffmpeg("-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video), "-vn", "-ac", "1", "-ar", "44100", str(wav_path))
    with wave.open(str(wav_path)) as wav:
        raw = wav.readframes(wav.getnframes())
    samples = [int.from_bytes(raw[i : i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / 44100)
