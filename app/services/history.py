"""История нарезок: что бот уже отдал, с какими настройками и как это оценили.

Зачем файл: `ClipResult` живёт только в памяти задачи и исчезает вместе с ней, поэтому вернуться к прошлой
нарезке нельзя. Из истории берутся три вещи: список последних клипов, настройки для кнопки «как в прошлый
раз» и оценки 👍/👎 с причиной — по ним видно, какие эвристики врут.

Формат — JSONL: одна запись в строке, новые дописываются с конца. Дописать строку дешевле, чем переписать
весь файл, но такая запись не атомарна — зато огрызок при падении испортит только свою строку (её `_load`
пропустит). Целиком файл переписывается редко: когда меняется прошлая запись (оценка) или он перерос
`MAX_ENTRIES`; тогда пишем во временный файл и `replace`, как в `UserStore`.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE_NAME = "history.jsonl"
MAX_ENTRIES = 2000  # потолок строк: нужны последние нарезки, а не весь архив — файл не должен расти вечно

RATING_UP = "up"  # 👍
RATING_DOWN = "down"  # 👎
RATINGS = frozenset({RATING_UP, RATING_DOWN})  # всё, что принимает `rate`

# причины 👎 — их же предлагаем кнопками: не тот момент / плохо обрезанный кадр / баннер мешает
REASONS = frozenset({"moment", "frame", "banner"})
MODES = frozenset({"auto", "manual"})  # то же, что `models.CutMode`: автоподбор или ручные настройки
DEFAULT_MODE = "auto"  # в битом файле режим неизвестен — считаем, что клипы нарезал автоподбор


@dataclass(frozen=True)
class Record:
    """Одна запись о готовом клипе.

    Значения по умолчанию есть у всех полей: они же служат ответом на недостающее поле старого или битого
    файла (`from_dict`), и с ними запись удобно собирать из `ClipResult` в коде бота.
    """

    ts: str = ""  # ISO-время создания записи
    user_id: int = 0
    job_id: str = ""
    index: int = 0  # номер клипа в задаче, с 1
    of: int = 0  # сколько всего клипов в задаче
    platform: str | None = None  # id площадки, как `VideoInfo.platform` (None — присланный файл)
    source: str = ""  # ключ источника — как `platforms.Source.key`
    title: str = ""
    mode: str = DEFAULT_MODE  # "auto" или "manual"
    start: float = 0.0  # с какой секунды источника взят клип
    end: float = 0.0  # по какую
    clip_len: float = 0.0  # длительность готового клипа вместе с паузами под баннер
    banner: str | None = None  # имя файла баннера
    banner_count: int = 0  # сколько показов на клип
    render_s: float = 0.0  # сколько секунд занял рендер
    size_mb: float = 0.0  # размер готового файла
    rating: str | None = None  # None — не оценён, "up"/"down" — 👍/👎
    reason: str | None = None  # почему 👎: "moment" | "frame" | "banner"

    @property
    def id(self) -> str:
        """Стабильный id записи: задача + номер клипа. По нему `rate` находит строку для оценки."""
        return f"{self.job_id}:{self.index}"

    def to_dict(self) -> dict[str, Any]:
        """Плоский словарь ровно из известных полей — одна строка JSONL."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Record:
        """Запись из словаря: лишние ключи отбрасываем, недостающие и испорченные заменяем значениями по умолчанию.

        Файл читают и после обновления бота (поля могли убрать или переименовать), и после падения на середине
        записи, поэтому чтение не должно падать ни на чём: неизвестное значение поля — это «не знаем», а не
        ошибка. Приведение типов тоже мягкое: `"3"` из старого файла станет `3`.
        """
        return cls(
            ts=_as_text(data.get("ts"), ""),
            user_id=_as_int(data.get("user_id"), 0),
            job_id=_as_text(data.get("job_id"), ""),
            index=_as_int(data.get("index"), 0),
            of=_as_int(data.get("of"), 0),
            platform=_as_optional_text(data.get("platform")),
            source=_as_text(data.get("source"), ""),
            title=_as_text(data.get("title"), ""),
            mode=_known(data.get("mode"), MODES, DEFAULT_MODE),
            start=_as_float(data.get("start"), 0.0),
            end=_as_float(data.get("end"), 0.0),
            clip_len=_as_float(data.get("clip_len"), 0.0),
            banner=_as_optional_text(data.get("banner")),
            banner_count=_as_int(data.get("banner_count"), 0),
            render_s=_as_float(data.get("render_s"), 0.0),
            size_mb=_as_float(data.get("size_mb"), 0.0),
            rating=_known(data.get("rating"), RATINGS, None),
            reason=_known(data.get("reason"), REASONS, None),
        )


# ---------- мягкое чтение полей ----------


def _as_int(value: Any, default: int) -> int:
    """Целое из файла; `None`, строка-мусор и список дают значение по умолчанию."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """Дробное из файла; `None` и мусор дают значение по умолчанию."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any, default: str) -> str:
    """Текст из файла; число или `None` в текстовом поле — уже не текст, берём значение по умолчанию."""
    return value if isinstance(value, str) else default


def _as_optional_text(value: Any) -> str | None:
    """Необязательный текст; пустая строка значит «не задано» — так же, как отсутствие поля."""
    return value if isinstance(value, str) and value else None


def _known(value: Any, allowed: frozenset[str], default: str | None) -> str | None:
    """Значение из закрытого набора или `default`: чужие состояния файла нам неинтересны и только путают сводку."""
    return value if isinstance(value, str) and value in allowed else default


def _count_lines(path: Path) -> int:
    """Строк в файле по переводу строки — без разбора JSON: это дешевле, чем читать историю целиком."""
    try:
        with path.open("rb") as handle:
            return handle.read().count(b"\n")
    except FileNotFoundError:
        return 0
    except OSError:
        log.warning("Не удалось сосчитать строки истории нарезок в %s.", path, exc_info=True)
        return 0


class History:
    """История нарезок в рабочей папке — тот же файл, что и `users.json`, рядом с ним.

    Объект не держит историю в памяти: строк немного (не больше `MAX_ENTRIES`), а читается файл редко — зато
    запись, сделанная прошлым запуском бота или другим объектом, видна сразу. Ошибки чтения и записи бота не
    роняют: история — вспомогательная функция, из-за неё задача срываться не должна (как в `UserStore`).
    """

    def __init__(self, workspace_dir: Path) -> None:
        self._path = workspace_dir / FILE_NAME
        self._lock = threading.RLock()  # задачи нарезки и обработчики кнопок живут в разных потоках
        # Сколько строк в файле; None — ещё не считали. Счётчик избавляет от разбора всей истории на каждое
        # дописывание, а после каждой перезаписи пересчитывается из того, что реально легло на диск.
        self._lines: int | None = None

    # ---------- запись ----------

    def append(self, record: Record) -> None:
        """Дописывает запись в конец файла.

        Атомарность на уровне строки не нужна: строки самодостаточны, и испорченную `_load` пропустит. Зато
        после дописывания проверяем потолок — иначе файл рос бы бесконечно (история нужна последними клипами).
        """
        with self._lock:
            lines = self._line_count() + 1  # считаем до записи: так счётчик не зависит от того, легла ли строка
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            except OSError:
                log.warning("Не удалось дописать историю нарезок в %s.", self._path, exc_info=True)
                return
            self._lines = lines
            if lines > MAX_ENTRIES:
                # Заодно чиним файл: `_rewrite` выбрасывает битые строки, а не только старые записи.
                self._rewrite(self._load())

    def rate(self, record_id: str, rating: str, reason: str | None = None) -> bool:
        """Ставит оценку записи: `True` — запись нашлась и сохранена, `False` — нет записи или оценка чужая.

        Причина есть только у 👎: у 👍 она бы значила «что не так», а пользователь уже сказал, что всё хорошо.
        Неизвестную причину тоже не сохраняем — сводка по ней всё равно не построится.

        `True` значит «запись нашлась и перезапись выполнена»: сбой диска только логируется, как и в `UserStore`,
        потому что оценка — не то, ради чего стоит показывать пользователю ошибку.
        """
        if rating not in RATINGS:
            log.warning("Оценка «%s» неизвестна — принимаю только %s.", rating, ", ".join(sorted(RATINGS)))
            return False
        wanted_reason = _known(reason, REASONS, None) if rating == RATING_DOWN else None
        with self._lock:
            records = self._load()
            # Идём с конца: если клип записан дважды (перерендер той же задачи), оцениваем свежую запись —
            # именно её пользователь видел последней.
            for position in range(len(records) - 1, -1, -1):
                if records[position].id != record_id:
                    continue
                records[position] = replace(records[position], rating=rating, reason=wanted_reason)
                self._rewrite(records)
                return True
        return False

    # ---------- чтение ----------

    def recent(self, limit: int = 10, *, user_id: int | None = None) -> list[Record]:
        """Последние записи, свежие первыми. `user_id` — только по одному пользователю (его меню и статистика)."""
        if limit <= 0:
            return []
        with self._lock:
            records = self._load()
        if user_id is not None:
            records = [record for record in records if record.user_id == user_id]
        return list(reversed(records))[:limit]

    def remembered(self, user_id: int) -> dict[str, Any] | None:
        """Настройки прошлой нарезки пользователя — заготовка для кнопки «как в прошлый раз».

        Полей ровно столько, сколько нужно, чтобы повторить нарезку: `of` вместо списка клипов, `banner` вместо
        всех настроек баннера. `None` — этот пользователь ещё ничего не нарезал.
        """
        latest = self.recent(1, user_id=user_id)
        if not latest:
            return None
        record = latest[0]
        return {
            "mode": record.mode,
            "clip_len": record.clip_len,
            "of": record.of,
            "platform": record.platform,
            "source": record.source,
            "title": record.title,
            "banner": record.banner,
            "banner_count": record.banner_count,
        }

    def summary(self, user_id: int) -> dict[str, int]:
        """Счётчики для экрана статистики: клипов, задач (по уникальным `job_id`), 👍 и 👎.

        Считаем по тому, что осталось в файле: после ротации это последние `MAX_ENTRIES` записей. Счётчик «за
        всё время» живёт в `UserStore` — история для него слишком короткая.
        """
        with self._lock:
            records = [record for record in self._load() if record.user_id == user_id]
        return {
            "clips": len(records),
            "jobs": len({record.job_id for record in records}),
            RATING_UP: sum(1 for record in records if record.rating == RATING_UP),
            RATING_DOWN: sum(1 for record in records if record.rating == RATING_DOWN),
        }

    # ---------- файл (вызывать под `self._lock`) ----------

    def _load(self) -> list[Record]:
        """Все читаемые записи файла, старые первыми. Битые строки пропускаются, нет файла — пустой список."""
        try:
            # errors="replace": оборванная запись может разрезать букву посимвольно, и тогда строгий декодер
            # отверг бы весь файл. С заменой сломается только эта строка — её отбросит разбор JSON.
            raw = self._path.read_text("utf-8", errors="replace")
        except FileNotFoundError:
            return []  # первый запуск бота: истории ещё нет
        except (OSError, ValueError):
            log.warning("Не удалось прочитать историю нарезок %s — считаю её пустой.", self._path, exc_info=True)
            return []
        records: list[Record] = []
        for number, line in enumerate(raw.splitlines(), start=1):
            text = line.strip()
            if not text:
                continue  # хвост от оборванной записи
            try:
                data = json.loads(text)
            except ValueError:
                log.warning("Строка %d в %s — не JSON, пропускаю.", number, self._path)
                continue
            if not isinstance(data, dict):
                log.warning("Строка %d в %s — не запись, пропускаю.", number, self._path)
                continue
            records.append(Record.from_dict(data))
        return records

    def _rewrite(self, records: list[Record]) -> None:
        """Перезаписывает файл атомарно (временный файл + `replace`), оставляя последние `MAX_ENTRIES` записей."""
        kept = records[-MAX_ENTRIES:]
        payload = "".join(json.dumps(record.to_dict(), ensure_ascii=False) + "\n" for record in kept)
        staging = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            staging.write_text(payload, "utf-8")
            staging.replace(self._path)
        except OSError:
            log.warning("Не удалось переписать историю нарезок в %s.", self._path, exc_info=True)
            try:
                staging.unlink(missing_ok=True)
            except OSError:  # папку без прав на запись так просто не убрать — но и ронять бота не за что
                log.warning("Временный файл истории %s остался на диске.", staging)
            self._lines = None  # на диске осталось неизвестно что — сосчитаем заново при следующей записи
            return
        self._lines = len(kept)

    def _line_count(self) -> int:
        """Строк в файле по счётчику; первый раз — считаем по файлу (он мог достаться от прошлого запуска)."""
        if self._lines is None:
            self._lines = _count_lines(self._path)
        return self._lines
