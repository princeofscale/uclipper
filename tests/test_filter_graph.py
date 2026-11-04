"""Устройство графа FFmpeg: то, что нельзя проверить по картинке на одной версии ffmpeg.

Локально у разработчика ffmpeg новее, чем в образе бота (там Debian: 7.1.x). Версии расходятся в мелочах, и одна из них
однажды испортила клипы: баннер замирал на первом кадре. Поэтому здесь проверяется сам граф — то, от чего результат
не зависит от версии. Сами картинки проверяют `test_engine.py` и `test_video_banners.py` (на сервере их гоняют в контейнере).
"""
import re

import pytest

from app.config import FPS
from app.models import BannerMode
from app.services.video_processor import (
    BannerMedia,
    RenderOptions,
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
