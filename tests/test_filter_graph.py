"""Устройство графа FFmpeg: то, что нельзя проверить по картинке на одной версии ffmpeg.

Локально у разработчика ffmpeg новее, чем в образе бота (там Debian: 7.1.x). Версии расходятся в мелочах, и одна из них
однажды испортила клипы: баннер замирал на первом кадре. Поэтому здесь проверяется сам граф — то, от чего результат
не зависит от версии. Сами картинки проверяют `test_engine.py` и `test_video_banners.py` (на сервере их гоняют в контейнере).
"""
import re
from pathlib import Path

import pytest

from app.config import FPS
from app.models import BannerMode
from app.services.video_processor import (
    BannerMedia,
    RenderOptions,
    build_ffmpeg_command,
    build_filter_graph,
    plan_banner_timeline,
)

VIDEO_BANNER = BannerMedia("video", 7.5, True)


def _outputs(chain: str) -> list[str]:
    """Метки, которые звено выдаёт: те, что стоят в самом конце (`…concat=n=2:v=1:a=1[v_cat][a_cat]` → v_cat, a_cat)."""
    tail = re.search(r"((?:\[[^\]]+\])+)$", chain)
    return re.findall(r"\[([^\]]+)\]", tail.group(1)) if tail else []


def position_of(graph: str, label: str) -> int:
    """Номер звена графа (звенья разделены «;»), которое выдаёт метку `[label]`."""
    found = [i for i, chain in enumerate(graph.split(";")) if label in _outputs(chain)]
    assert len(found) == 1, f"метку [{label}] должно выдавать ровно одно звено, а их {len(found)}:\n{graph}"
    return found[0]


def chain_making(graph: str, label: str) -> str:
    return graph.split(";")[position_of(graph, label)]


def freeze_graph(count: int = 1) -> str:
    timeline = plan_banner_timeline(45.0, count, 7.5, BannerMode.FREEZE)
    opts = RenderOptions(banner_count=count, banner_mode=BannerMode.FREEZE)
    return build_filter_graph(timeline, opts, banner_idx=1, banner=VIDEO_BANNER)


@pytest.mark.parametrize("count", [1, 2])
def test_frames_are_renumbered_after_the_freeze_so_the_banner_follows_the_clock(count):
    """`tpad` в ffmpeg 7.1 клонирует стоп-кадр с ОДНОЙ меткой времени на все копии. `overlay` подбирает кадр баннера
    по метке — и показывал бы в окне только первый. Метки после склейки пересчитываются по номеру кадра."""
    graph = freeze_graph(count)

    renumbering = chain_making(graph, "v_cat")
    assert f"setpts=N/({FPS}*TB)" in renumbering
    assert renumbering.startswith("[v_joined]")
    assert "concat=" in chain_making(graph, "v_joined")


def test_the_dimming_and_the_banner_are_placed_after_the_renumbering():
    """Окно затемнения и показ баннера читают метки времени — значит, идут строго после пересчёта."""
    graph = freeze_graph()
    assert position_of(graph, "v_cat") < position_of(graph, "v_dim") < position_of(graph, "v_out")


# ---------- команда ffmpeg: скорость на ограниченном процессоре ----------


def _command(**kwargs) -> list[str]:
    timeline = plan_banner_timeline(10.0, 0, 2.5, BannerMode.FREEZE)
    return build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), 0.0, timeline, RenderOptions(banner_count=0), **kwargs)


def test_ffmpeg_gets_no_more_threads_than_the_container_may_use():
    """На сервере боту дан один процессор, а ffmpeg видит все ядра хоста и плодит потоки: на замере они мешали друг
    другу и съедали четверть времени (3,2 кадра/с против 4,2 при одном потоке).
    """
    command = _command(threads=1)

    assert command[command.index("-filter_threads") + 1] == "1"
    assert command[command.index("-filter_complex_threads") + 1] == "1"
    decoder, encoder = [index for index, part in enumerate(command) if part == "-threads"]
    assert decoder < command.index("-i")  # ключ перед входом относится к декодеру
    assert encoder > command.index("-filter_complex")  # а ключ перед кодеком — к кодировщику
    assert command[decoder + 1] == command[encoder + 1] == "1"


def test_without_a_cpu_limit_ffmpeg_picks_its_threads_itself():
    command = _command()

    assert not {"-threads", "-filter_threads", "-filter_complex_threads"} & set(command)


def test_clips_are_encoded_with_the_fast_x264_preset():
    """Замер на 12 с настоящего ролика 1080p60 (1 ядро): veryfast crf 20 — 19,7 с процессора, 3,7 МБ, SSIM 0,9941;
    superfast crf 24 — около 14,8 с, около 4,2 МБ, SSIM около 0,994. Качество то же, рендер быстрее на четверть.
    """
    command = _command()

    assert command[command.index("-preset") + 1] == "superfast"
    assert command[command.index("-crf") + 1] == "24"


def test_keyframes_stand_every_two_seconds_and_the_frame_rate_is_set_once():
    """Частоту кадров задаёт фильтр `fps`; `-r` на выходе только пересобирал кадры. Ключевые кадры — каждые 2 с:
    так платформа и плеер нормально перематывают клип, цена — 2–5% битрейта.
    """
    command = _command()

    assert "-r" not in command, "частоту кадров уже выставил фильтр fps"
    assert command[command.index("-g") + 1] == "60"
    assert command[command.index("-keyint_min") + 1] == "30"


def _command_of(seconds: float) -> list[str]:
    """Команда для клипа нужной длины: битрейт-потолок зависит от того, сколько идёт клип."""
    timeline = plan_banner_timeline(seconds, 0, 2.5, BannerMode.FREEZE)
    return build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), 0.0, timeline, RenderOptions(banner_count=0))


def test_long_clips_get_a_bitrate_cap_so_they_fit_telegram_without_a_second_encode():
    """Клип длиннее минуты бот иначе сжимал бы ещё раз (720p) — это второй полный энкод."""
    short = _command_of(45.0)
    long = _command_of(200.0)

    assert "-maxrate" not in short, "короткий клип и так влезает в 50 МБ"
    cap = int(long[long.index("-maxrate") + 1])
    assert 1_000_000 < cap < 3_000_000  # 200 с: около 2 Мбит/с
    assert long[long.index("-bufsize") + 1] == str(cap * 2)
    assert 200 * cap / 8 / 1024 / 1024 < 50, "с таким потолком файл обязан влезть в лимит"
