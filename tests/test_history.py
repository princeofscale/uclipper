"""История нарезок: свежие сверху, оценки переживают перезапуск, битый файл бота не роняет."""
from __future__ import annotations

import json

from app.services.history import FILE_NAME, MAX_ENTRIES, History, Record


def make_record(**fields) -> Record:
    """Запись с правдоподобными значениями: тест задаёт только то, что проверяет."""
    values: dict = {
        "ts": "2026-01-01T00:00:00+00:00",
        "user_id": 1,
        "job_id": "job-1",
        "index": 1,
        "of": 3,
        "platform": "youtube",
        "source": "abc12345678",
        "title": "Ролик",
        "mode": "auto",
        "start": 0.0,
        "end": 30.0,
        "clip_len": 30.0,
        "banner": "strip.png",
        "banner_count": 2,
        "render_s": 12.5,
        "size_mb": 8.4,
        "rating": None,
        "reason": None,
    }
    values.update(fields)
    return Record(**values)


# ---------- append / recent ----------


def test_recent_puts_fresh_first_and_respects_limit(tmp_path):
    history = History(tmp_path)
    for index in (1, 2, 3):
        history.append(make_record(index=index))

    assert [record.index for record in history.recent()] == [3, 2, 1]
    assert [record.index for record in history.recent(2)] == [3, 2]
    assert history.recent(0) == []


def test_recent_filters_by_user(tmp_path):
    history = History(tmp_path)
    history.append(make_record(user_id=1, index=1))
    history.append(make_record(user_id=2, index=2))
    history.append(make_record(user_id=1, index=3))

    assert [record.index for record in history.recent(user_id=1)] == [3, 1]
    assert [record.index for record in history.recent(user_id=2)] == [2]
    assert history.recent(user_id=3) == []


def test_lines_are_written_as_readable_utf8(tmp_path):
    # в файл смотрит человек и grep на сервере: \uXXXX в нём бесполезен
    history = History(tmp_path)
    history.append(make_record(title="Ролик о котах"))

    assert "Ролик о котах" in (tmp_path / FILE_NAME).read_text("utf-8")


# ---------- устойчивость к битому файлу ----------


def test_a_broken_line_between_valid_ones_is_skipped(tmp_path):
    history = History(tmp_path)
    history.append(make_record(index=1))
    with (tmp_path / FILE_NAME).open("a", encoding="utf-8") as handle:
        handle.write("не json\n\n[1, 2]\n")  # мусор, пустая строка и валидный JSON, но не запись
    history.append(make_record(index=2))

    assert [record.index for record in history.recent()] == [2, 1]


def test_a_torn_tail_keeps_the_rest_of_the_history(tmp_path):
    # падение на середине записи режет русскую букву на два байта: строгий декодер отверг бы весь файл
    history = History(tmp_path)
    history.append(make_record(index=1))
    with (tmp_path / FILE_NAME).open("ab") as handle:
        handle.write(b'{"index": 2, "title": "\xd0')

    assert [record.index for record in history.recent()] == [1]


def test_missing_or_empty_file_is_an_empty_history(tmp_path):
    assert History(tmp_path).recent() == []
    assert History(tmp_path).remembered(1) is None
    assert History(tmp_path).summary(1) == {"clips": 0, "jobs": 0, "up": 0, "down": 0}

    (tmp_path / FILE_NAME).write_text("", "utf-8")

    assert History(tmp_path).recent() == []


# ---------- оценки ----------


def test_rate_saves_the_rating_on_disk(tmp_path):
    history = History(tmp_path)
    history.append(make_record(index=1))
    history.append(make_record(index=2))

    assert history.rate("job-1:1", "up") is True

    again = History(tmp_path)  # новый объект читает файл заново: оценка должна пережить перезапуск бота
    assert [record.rating for record in again.recent()] == [None, "up"]
    assert again.recent()[1].id == "job-1:1"


def test_rate_down_keeps_the_reason(tmp_path):
    history = History(tmp_path)
    history.append(make_record(index=1))

    assert history.rate("job-1:1", "down", "frame") is True

    record = history.recent()[0]
    assert (record.rating, record.reason) == ("down", "frame")


def test_rate_up_forgets_the_old_reason(tmp_path):
    # «почему 👎» рядом с 👍 значило бы «что не так», хотя пользователь сказал, что всё хорошо
    history = History(tmp_path)
    history.append(make_record(index=1))
    history.rate("job-1:1", "down", "banner")

    history.rate("job-1:1", "up", "banner")

    record = history.recent()[0]
    assert (record.rating, record.reason) == ("up", None)


def test_rate_unknown_record_is_false(tmp_path):
    history = History(tmp_path)
    history.append(make_record(index=1))

    assert history.rate("job-1:99", "up") is False
    assert [record.rating for record in history.recent()] == [None]


def test_rate_rejects_an_unknown_rating_without_touching_the_file(tmp_path):
    history = History(tmp_path)
    history.append(make_record(index=1))
    history.append(make_record(index=2))

    assert history.rate("job-1:1", "любимый") is False

    assert [record.rating for record in history.recent()] == [None, None]
    assert len(History(tmp_path).recent()) == 2


# ---------- «как в прошлый раз» ----------


def test_remembered_returns_the_last_settings_of_the_user(tmp_path):
    history = History(tmp_path)
    history.append(make_record(user_id=1, job_id="a", index=1, mode="auto", clip_len=30.0))
    history.append(
        make_record(
            user_id=1,
            job_id="b",
            index=1,
            mode="manual",
            clip_len=45.0,
            of=2,
            platform=None,
            source="upload-01",
            title="Второй ролик",
            banner=None,
            banner_count=0,
        )
    )
    history.append(make_record(user_id=2, job_id="c", index=1, mode="auto", clip_len=15.0))

    assert history.remembered(1) == {
        "mode": "manual",
        "clip_len": 45.0,
        "of": 2,
        "platform": None,
        "source": "upload-01",
        "title": "Второй ролик",
        "banner": None,
        "banner_count": 0,
    }
    assert history.remembered(3) is None  # у этого пользователя ещё ничего не нарезано


# ---------- ротация и сводка ----------


def test_rotation_keeps_the_latest_entries(tmp_path):
    history = History(tmp_path)
    total = MAX_ENTRIES + 10
    for number in range(1, total + 1):
        history.append(make_record(job_id=f"job-{number}", index=number))

    lines = (tmp_path / FILE_NAME).read_text("utf-8").splitlines()
    assert len(lines) == MAX_ENTRIES

    kept = history.recent(limit=MAX_ENTRIES + 100)  # больше, чем лежит в файле
    assert len(kept) == MAX_ENTRIES
    assert kept[0].index == total  # самая свежая на месте
    assert kept[-1].index == total - MAX_ENTRIES + 1  # а самая старая — ровно на границе потолка


def test_rotation_also_works_for_a_file_from_the_previous_run(tmp_path):
    # бот перезапустился, а файл уже полон: счётчик строк должен взяться из файла, а не из памяти процесса
    lines = []
    for number in range(1, MAX_ENTRIES + 1):
        lines.append(json.dumps(make_record(job_id=f"old-{number}", index=number).to_dict(), ensure_ascii=False))
    (tmp_path / FILE_NAME).write_text("\n".join(lines) + "\n", "utf-8")

    history = History(tmp_path)
    history.append(make_record(job_id="new", index=1))

    kept = history.recent(limit=MAX_ENTRIES + 1)
    assert len(kept) == MAX_ENTRIES
    assert kept[0].id == "new:1"
    assert kept[-1].job_id == "old-2"  # самая старая запись старого файла ушла первой


def test_summary_counts_clips_jobs_and_ratings(tmp_path):
    history = History(tmp_path)
    history.append(make_record(user_id=1, job_id="a", index=1))
    history.append(make_record(user_id=1, job_id="a", index=2))
    history.append(make_record(user_id=1, job_id="b", index=1))
    history.append(make_record(user_id=2, job_id="c", index=1))
    assert history.rate("a:1", "up") is True
    assert history.rate("a:2", "down", "moment") is True

    assert history.summary(1) == {"clips": 3, "jobs": 2, "up": 1, "down": 1}
    assert history.summary(2) == {"clips": 1, "jobs": 1, "up": 0, "down": 0}


# ---------- чтение словаря ----------


def test_from_dict_tolerates_missing_and_extra_fields():
    # файл с сервера читают после обновления бота: полей могло стать больше или меньше
    record = Record.from_dict({"job_id": "j-1", "index": 2, "rating": "up", "banner_count": "3", "старое_поле": 1})

    assert record.id == "j-1:2"
    assert (record.rating, record.banner_count) == ("up", 3)
    assert record.user_id == 0 and record.platform is None and record.banner is None
    assert record.start == 0.0 and record.title == ""
    assert Record.from_dict({}).mode == "auto"  # пустой словарь — тоже не повод падать
    assert Record.from_dict({"rating": "как-то так", "reason": "настроение"}).rating is None


def test_dict_roundtrip_keeps_every_field():
    record = make_record(index=3, rating="down", reason="banner", platform=None)

    assert Record.from_dict(record.to_dict()) == record
    assert json.loads(json.dumps(record.to_dict(), ensure_ascii=False)) == record.to_dict()
