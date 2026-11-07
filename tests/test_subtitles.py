"""Субтитры: разбор `json3` от площадки, сборка строк и файл `.ass` с караоке.

Здесь нет ни сети, ни ffmpeg: проверяется именно логика — тайминги, переносы, экранирование
и то, что субтитры не выходят за окно клипа.
"""
from __future__ import annotations

import pytest

from app.services.subtitles import (
    MAX_CHARS,
    MAX_SECONDS,
    MAX_WORDS,
    Line,
    Word,
    ass_escape,
    ass_time,
    lines_from_words,
    subtitles_for,
    to_ass,
    words_from_json3,
)


def json3(*segments: tuple[float, float, str]) -> dict:
    """`json3` как его отдаёт YouTube: сегменты со временем начала, длительностью и текстом."""
    return {
        "events": [
            {"tStartMs": round(start * 1000), "dDurationMs": round(length * 1000), "segs": [{"utf8": text}]}
            for start, length, text in segments
        ]
    }


# ---------- разбор ответа площадки ----------


def test_words_are_split_evenly_inside_a_segment():
    words = words_from_json3(json3((0.0, 2.0, "я уволился и не пожалел")))

    assert [word.text for word in words] == ["я", "уволился", "и", "не", "пожалел"]
    assert words[0].start == 0.0 and words[-1].end == pytest.approx(2.0, abs=0.01)
    # слова идут подряд без дыр: иначе караоке мигало бы
    assert words[1].start == pytest.approx(words[0].end)
    assert words[-1].start < words[-1].end


def test_service_events_and_music_notes_are_dropped():
    payload = {
        "events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "[Музыка]"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "привет"}]},
            {"tStartMs": 2000, "dDurationMs": 0, "segs": []},  # служебное событие без текста
            {"tStartMs": 3000, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
        ]
    }

    assert [word.text for word in words_from_json3(payload)] == ["привет"]


def test_words_after_the_clip_are_cut_off():
    words = words_from_json3(json3((0.0, 1.0, "раз два"), (30.0, 1.0, "далеко")), limit=10.0)

    assert [word.text for word in words] == ["раз", "два"]


def test_nonsense_payload_does_not_break_anything():
    assert words_from_json3({}) == []
    assert words_from_json3({"events": "не список"}) == []
    assert words_from_json3({"events": [None, 42, {"segs": [{"utf8": "x"}]}]}) == []


# ---------- строки ----------


def _words(*pairs: tuple[str, float, float]) -> list[Word]:
    return [Word(text, start, end) for text, start, end in pairs]


def test_a_line_holds_at_most_four_words():
    words = _words(*[(f"с{index}", index * 0.2, index * 0.2 + 0.2) for index in range(10)])

    lines = lines_from_words(words)

    assert all(len(line.words) <= MAX_WORDS for line in lines)
    assert " ".join(word.text for line in lines for word in line.words) == " ".join(word.text for word in words)


def test_a_line_is_never_wider_than_the_screen_allows():
    words = _words(("длинноеслово", 0.0, 0.3), ("ещёодно", 0.3, 0.6), ("итретье", 0.6, 0.9))

    lines = lines_from_words(words)

    assert all(len(line.text) <= MAX_CHARS for line in lines)


def test_a_line_does_not_live_longer_than_a_second_and_a_half():
    words = _words(*[(f"с{index}", index * 0.5, index * 0.5 + 0.5) for index in range(6)])

    lines = lines_from_words(words)

    assert all(line.end - line.start <= MAX_SECONDS for line in lines)


def test_a_sentence_end_finishes_the_line():
    words = _words(("Привет.", 0.0, 0.3), ("Как", 0.3, 0.5), ("дела?", 0.5, 0.8))

    lines = lines_from_words(words)

    assert [line.text for line in lines] == ["Привет.", "Как дела?"]


def test_a_pause_in_speech_finishes_the_line_too():
    words = _words(("первая", 0.0, 0.4), ("часть", 0.4, 0.8), ("вторая", 5.0, 5.4))

    assert [line.text for line in lines_from_words(words)] == ["первая часть", "вторая"]


def test_lines_never_go_past_the_clip_window():
    words = _words(("тут", 0.0, 0.4), ("а", 20.0, 20.2), ("это", 20.2, 20.4), ("уже", 20.4, 20.6))

    assert " ".join(word.text for line in lines_from_words(words, limit=10.0) for word in line.words) == "тут"


# ---------- файл .ass ----------


def test_ass_time_is_formatted_like_the_format_requires():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(75.5) == "0:01:15.50"
    assert ass_time(3725.25) == "1:02:05.25"
    assert ass_time(-5) == "0:00:00.00"


def test_braces_in_speech_do_not_break_the_file():
    assert ass_escape("смотри {это}") == "смотри (это)"


def test_speech_cannot_inject_ass_tags():
    """Фигурные скобки в речи — единственный способ вставить чужой тег: они превращаются в скобки."""
    line = Line((Word("{\\an8}взлом", 0.0, 1.0),))

    text = to_ass([line])

    assert text.count("{") == 1, "единственная фигурная скобка — наш караоке-тег"
    assert "{\\k100}" + ass_escape("{\\an8}взлом") in text


def test_ass_has_two_styles_and_karaoke_for_every_word():
    lines = lines_from_words(_words(("раз", 0.0, 0.5), ("два", 0.5, 1.0)))

    text = to_ass(lines)

    assert "Style: Speech" in text and "Style: Hook" in text
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "{\\k50}раз" in text and "{\\k50}два" in text
    assert text.count("Dialogue:") == 1


def test_subtitles_for_gives_the_whole_file_in_one_call():
    text = subtitles_for(json3((0.0, 1.0, "привет мир")))

    assert text.startswith("[Script Info]") and "{\\k50}привет" in text


def test_empty_source_gives_an_empty_but_valid_file():
    text = subtitles_for({})

    assert text.startswith("[Script Info]") and "Dialogue:" not in text


# ---------- и главное: файл принимает libass ----------


def test_generated_ass_is_rendered_by_ffmpeg(tmp_path, synthetic_video, ffmpeg_tools):
    """Синтаксис проверяет не наш код, а libass: рендерим 2 секунды с нашими субтитрами.

    Локальная сборка ffmpeg может быть без libass (в контейнере бота он есть) — тогда проверку пропускаем,
    а не притворяемся, что субтитры отрендерились.
    """
    import subprocess

    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    if " ass " not in filters:
        pytest.skip("в этой сборке ffmpeg нет фильтра ass (libass)")

    source = tmp_path / "subs.ass"
    source.write_text(subtitles_for(json3((0.0, 1.2, "привет мир"), (1.2, 0.8, "это субтитр"))), encoding="utf-8")
    out = tmp_path / "out.mp4"

    # Ровно так это будет работать в бою: файл лежит рядом, в фильтр идёт короткое имя без путей.
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(synthetic_video), "-t", "2",
         "-vf", "ass=subs.ass", "-an", "out.mp4"],
        capture_output=True, text=True, cwd=tmp_path,
    )  # fmt: skip

    assert done.returncode == 0, done.stderr[-800:]
    assert out.is_file() and out.stat().st_size > 0
