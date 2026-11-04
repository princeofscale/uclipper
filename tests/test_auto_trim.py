"""Автообрезка начала и конца: чёрный старт, тишина, чёрный хвост и титры. Только края ролика — середину не трогаем.

Видео собирается из кусков (`make_episode`): известно, где на самом деле кончается сюжет, и проверяется, что
программа находит эту секунду с допуском на ключевые кадры (раз в 3 с).
"""
import pytest

from app.services.auto_trim import TrimSuggestion, analyze_trim
from app.services.video_processor import probe_media
from tests.media_utils import make_episode


@pytest.fixture(scope="module")
def episodes(media_dir):
    """Серии с известной раскладкой; каждая собирается один раз."""
    cache = {}

    def build(name, **layout):
        if name not in cache:
            cache[name] = make_episode(media_dir / f"{name}.mp4", **layout)
        return cache[name]

    return build


def analyse(path):
    return analyze_trim(path, probe_media(path))


# ---------- что находит ----------


def test_black_silent_lead_in_is_cut_and_the_reason_is_in_russian(episodes):
    result = analyse(episodes("lead_in", head_black=3, content=25))
    assert result.start == pytest.approx(3.0, abs=0.6)
    assert result.end == pytest.approx(28.0, abs=0.3)
    assert "чёрн" in result.head and result.tail is None


def test_silent_lead_in_is_cut_even_when_the_picture_is_already_there(episodes):
    result = analyse(episodes("silent_lead_in", head_silent=4, content=25))
    assert result.start == pytest.approx(4.0, abs=0.6)
    assert result.head == "тишина"


def test_black_tail_is_cut(episodes):
    result = analyse(episodes("black_tail", content=25, tail_black=4))
    assert result.start == pytest.approx(0.0, abs=0.3)
    assert result.end == pytest.approx(25.0, abs=0.6)
    assert result.tail is not None and "чёрн" in result.tail


def test_scrolling_credits_are_cut_where_they_begin(episodes):
    result = analyse(episodes("credits", head_black=2, content=30, credits=20, tail_black=2))
    assert result.start == pytest.approx(2.0, abs=0.6)
    assert result.end == pytest.approx(32.0, abs=3.5)  # титры начались на 32-й секунде; ключевые кадры раз в 3 с
    assert "титры" in result.tail


def test_without_sound_the_picture_alone_is_enough(episodes):
    result = analyse(episodes("mute_lead_in", head_black=3, content=20, audio=False))
    assert result.start == pytest.approx(3.0, abs=0.6)


def test_suggestion_exposes_offsets_for_the_cutter(episodes):
    path = episodes("offsets", head_black=3, content=25, tail_black=2)
    result = analyse(path)
    assert result.start_offset == pytest.approx(result.start)
    assert result.end_offset == pytest.approx(result.duration - result.end)
    assert isinstance(result, TrimSuggestion)


# ---------- чего не трогает ----------


def test_plain_episode_is_left_alone(episodes):
    result = analyse(episodes("plain", content=40))
    assert (result.start, result.head, result.tail) == (0.0, None, None)
    assert result.end == pytest.approx(result.duration, abs=0.01)


def test_a_dark_scene_without_text_is_not_credits(episodes):
    result = analyse(episodes("dark_finale", content=25, dark_scene=18))
    assert result.end == pytest.approx(result.duration, abs=0.6)
    assert result.tail is None


def test_a_few_seconds_of_credits_are_not_worth_cutting(episodes):
    result = analyse(episodes("short_credits", content=30, credits=9))  # три ключевых кадра с текстом, но меньше 12 с
    assert result.end == pytest.approx(result.duration, abs=0.6)
    assert result.tail is None


def test_when_almost_nothing_would_remain_nothing_is_cut(episodes):
    result = analyse(episodes("mostly_black", head_black=10, content=10, tail_black=10))  # остался бы 1/3 ролика
    assert (result.start, result.head, result.tail) == (0.0, None, None)
    assert result.end == pytest.approx(result.duration, abs=0.01)


def test_an_all_black_video_is_not_reduced_to_nothing(episodes):
    result = analyse(episodes("all_black", content=0, head_black=20))
    assert (result.start, result.end) == (0.0, pytest.approx(result.duration, abs=0.01))
    assert result.head is None and result.tail is None


def test_a_clip_too_short_to_judge_is_returned_whole(episodes):
    result = analyse(episodes("tiny", head_black=2, content=5))
    assert (result.start, result.end) == (0.0, pytest.approx(result.duration, abs=0.01))
