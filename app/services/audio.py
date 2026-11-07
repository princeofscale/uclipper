"""Звук клипа: замер громкости, выравнивание баннера под контент и доводка файла до нормы площадок.

Зачем это нужно. Рендер склеивает клип из контента и рекламного баннера, и звук у них живёт сам по себе:
баннер оказывается громче контента на 2–8 дБ (замерено: баннер −21,7 и −18,9 LUFS против контента −27,3
и −23,6), а сам клип при этом тише того, что ждут площадки. Слушатель воспринимает это как «реклама орёт,
а ролик шёпотом», поэтому громкость надо и мерить, и править.

Три операции, каждая за одно:
- `measure` — замер по EBU R128: `I` (LUFS), true peak и LRA;
- `median_db` и `banner_gain_db` — поправка звуку баннера, чтобы он сравнился с типичной громкостью контента;
- `normalize_file` и `limit_file` — доводка готового клипа до целевой громкости и до пиков.

Видео нигде не перекодируется (`-c:v copy`): к этому моменту клип уже прошёл пару перекодов при рендере,
и каждый лишний проход стоит и качества, и времени.

Модуль не роняет бота никогда: любая проблема с FFmpeg — это `log.warning` и `None`/`False`. Вызывающий код
сам решает, что делать с ненормализованным файлом; падать на этом задании, где уже отснят час материала,
недопустимо.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# −14 LUFS — это конвенция стримингов (Spotify, YouTube, Apple Music сводят всё к этому же уровню), а НЕ
# официальная спецификация TikTok: своего норматива у него нет, он просто приводит громкость к своему
# уровню при воспроизведении. Отсюда и цели: попадание в −14 LUFS гарантирует, что клип не окажется
# «тише всех» после чужой нормализации, а true peak −1 dBFS оставляет запас под AAC-перекодирование площадки.
TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.0
TARGET_LRA = 11.0

# AAC 160k — прозрачно для речи и музыки клипа и не раздувает файл; 44,1 кГц и стерео — то, что площадки
# принимают без переупаковки (моно и 48 кГц они пересчитывают сами, а лишний ресемплинг на их стороне
# может сдвинуть пики).
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "160k"
AUDIO_RATE = 44100
AUDIO_CHANNELS = 2

# Сводка ebur128 идёт в stderr последними строками; якорь `^` с MULTILINE отсекает бегущие строки вида
# `t: 1.2 ... I: -41.8 LUFS ...` — они приходят на каждом кадре и могут нести промежуточные значения.
INTEGRATED_RE = re.compile(r"^\s*I:\s+(-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)
TRUE_PEAK_RE = re.compile(r"^\s*Peak:\s+(-?\d+(?:\.\d+)?)\s*dBFS", re.MULTILINE)
LRA_RE = re.compile(r"^\s*LRA:\s+(-?\d+(?:\.\d+)?)\s*LU\b", re.MULTILINE)
# loudnorm печатает объект с замерами одной строкой-блоком; забираем его целиком, включая возможные переводы строк.
LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


@dataclass(frozen=True)
class Loudness:
    """Замер громкости по EBU R128 — та же шкала, в которой заданы нормы площадок и замеры баннеров."""

    integrated: float  # средняя громкость `I`, LUFS
    true_peak: float  # пик с учётом межсэмпловой интерполяции, dBFS
    lra: float  # разброс громкости между тихими и громкими местами, LU


def measure(path: Path) -> Loudness | None:
    """Громкость файла: `I`, true peak и LRA. `None` — если в файле нет звука, он битый или ffmpeg упал.

    Меряем именно `ebur128`, а не `volumedetect`: у `volumedetect` шкала — простой RMS по сэмплам, у площадок
    и у `loudnorm` — громкость, взвешенная по K-кривой и с гейтингом тишины. Числа у этих шкал не сходятся,
    поэтому «−14 LUFS» не получить, вычитая RMS-громкости.
    """
    command = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
        "-af", "ebur128=peak=true", "-f", "null", "-",
    ]  # fmt: skip
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except (OSError, ValueError):
        log.warning("Не получилось запустить ffmpeg для замера громкости: %s", path, exc_info=True)
        return None
    if result.returncode != 0:
        log.warning("ffmpeg не смог прочитать звук %s: %s", path, _tail(result.stderr))
        return None
    return _parse_ebur128(result.stderr, path)


def median_db(levels: list[float]) -> float | None:
    """Медиана уровней в дБ — «типичная» громкость контента. `None` для пустого списка.

    Медиана, а не среднее: в контенте бывают всплески (крик, музыкальная подложка, смех), среднее от них
    уезжает вверх — и баннер после выравнивания по среднему звучит тише, чем должен.
    """
    if not levels:
        return None
    return float(statistics.median(levels))


def banner_gain_db(banner: Loudness, content_median_db: float, *, limit_db: float = 18.0) -> float:
    """На сколько дБ поправить звук баннера, чтобы его `integrated` совпал с медианой контента.

    Знак — как у обычного гейна: баннер громче контента → число отрицательное (приглушить). Округляем до
    0,1 дБ: тоньше человеческое ухо не различает, а в логах и в отчётах читается легче.

    `limit_db` — предохранитель. Замер баннера и контента может разойтись с реальностью (тишина в файле,
    другой характер сигнала: музыка против речи), и тогда честная арифметика даёт поправку в 20–30 дБ —
    такой звук уже невыносим. Лучше зажать поправку, чем получить клип, который выключают.
    """
    gain = content_median_db - banner.integrated
    return round(max(-limit_db, min(limit_db, gain)), 1)


def normalize_file(
    path: Path,
    *,
    target_i: float = TARGET_LUFS,
    target_tp: float = TARGET_TRUE_PEAK,
    target_lra: float = TARGET_LRA,
) -> bool:
    """Приводит звук файла к целевой громкости на месте: видео копируется, перекодируется только AAC-звук.

    `loudnorm` работает в два прохода, и это принципиально. В один проход фильтр правит громкость по
    «бегущему» замеру и на коротком клипе промахивается на несколько дБ — а нам нужно попасть в ±1 LU.
    Поэтому первый проход только меряет (`print_format=json` отдаёт в stderr `input_i`, `input_tp`,
    `input_lra`, `input_thresh`), а второй повторяет фильтр с этими числами и делает точную поправку.

    `-c:v copy` обязателен: клип уже перекодировали при рендере, третий проход съел бы качество картинки
    ради нуля пользы — громкость живёт в звуковой дорожке.

    `True` — файл переписан. `False` — не взялись (файла нет, звука нет, ffmpeg не смог) и файл не тронут.
    Второй проход заканчивается AAC-кодированием, а оно добавляет межсэмпловые пики, поэтому за true peak
    после нормализации отвечает `limit_file`, а не этот фильтр.
    """
    if not path.is_file():
        log.warning("Нормализация: файла нет — %s", path)
        return False
    measured = _loudnorm_measure(path, target_i, target_tp, target_lra)
    if measured is None:
        return False
    # Все параметры loudnorm в одном месте: measured_* — из первого прохода, offset — его же поправка на
    # нелинейность. Без measured_* фильтр посчитал бы замер заново и снова промахнулся.
    graph = (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
    )
    return _rewrite_audio(path, graph, tag="loudnorm", what="Нормализация")


def limit_file(path: Path, *, limit: float = 0.95) -> bool:
    """Подрезает пики файла через `alimiter`, тоже копируя видео (`-c:v copy`).

    `limit=0.95` — это −0,45 dBFS: запас под AAC-перекодирование, который добавляет межсэмпловые пики.
    Целиться ровно в 0 dBFS нельзя: площадка перекодирует звук у себя, и пики, стоящие вплотную к потолку,
    превращаются в слышимые щелчки.

    `level=false` обязателен. У `alimiter` по умолчанию `level=true` — автовыравнивание, которое поднимает
    выход обратно до полной шкалы. Проверено на живом ffmpeg: с `level=true` пик остаётся 0 dBFS при любом
    `limit`, то есть фильтр делает ровно противоположное своему названию. С `level=false` пик встаёт на
    заданный лимит (−0,2 dBFS при `limit=0.95`).

    `False` — не вышло; файл при этом не тронут. Запасного варианта с перекодированием видео нет специально:
    качество от него теряется заметнее, чем польза от пары дБ пиков.
    """
    if not path.is_file():
        log.warning("Ограничение пиков: файла нет — %s", path)
        return False
    return _rewrite_audio(path, f"alimiter=limit={limit}:level=false", tag="limiter", what="Ограничение пиков")


def _loudnorm_measure(path: Path, target_i: float, target_tp: float, target_lra: float) -> dict[str, str] | None:
    """Первый проход `loudnorm`: только замер, звук никуда не пишется. `None`, если разобрать не удалось."""
    graph = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    command = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-af", graph, "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except (OSError, ValueError):
        log.warning("Не получилось запустить ffmpeg для замера перед нормализацией: %s", path, exc_info=True)
        return None
    if result.returncode != 0:
        log.warning("Первый проход loudnorm не удался для %s: %s", path, _tail(result.stderr))
        return None
    match = LOUDNORM_JSON_RE.search(result.stderr)
    if match is None:
        log.warning("loudnorm не отдал JSON замера для %s — скорее всего в файле нет звуковой дорожки", path)
        return None
    try:
        measured = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("Не разобрал JSON от loudnorm для %s", path)
        return None
    if not isinstance(measured, dict):
        log.warning("loudnorm вернул не объект с замерами для %s", path)
        return None
    missing = [name for name in ("input_i", "input_tp", "input_lra", "input_thresh") if name not in measured]
    if missing:
        log.warning("В замере loudnorm для %s нет полей: %s", path, ", ".join(missing))
        return None
    # На полностью тихом файле loudnorm может не отдать target_offset — подставляем нулевую поправку,
    # иначе второй проход упадёт на неизвестном параметре.
    measured.setdefault("target_offset", "0.0")
    return measured


def _rewrite_audio(path: Path, filter_graph: str, *, tag: str, what: str) -> bool:
    """Переписывает файл на месте, перекодируя только звук, через временный файл рядом с исходником.

    Временный файл лежит в той же папке и с тем же расширением: `Path.replace` тогда — атомарная операция
    в пределах одной файловой системы (битого файла не остаётся никогда), а ffmpeg пишет в тот же контейнер,
    что у исходника, и не спотыкается о несовпадение кодека и формата. В имени есть pid: два процесса бота
    на одном файле не должны затирать заготовки друг друга.
    """
    temp = path.with_name(f".{path.stem}.{tag}{os.getpid()}{path.suffix}")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
        "-map", "0", "-c:v", "copy", "-af", filter_graph,
        "-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS),
        str(temp),
    ]  # fmt: skip
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("%s не удалась для %s: %s", what, path, _tail(result.stderr))
            return False
        if not temp.is_file() or temp.stat().st_size == 0:
            log.warning("%s не удалась для %s: ffmpeg не создал файл", what, path)
            return False
        # Именно заготовка накрывает исходник: `replace` наоборот унёс бы рабочий файл под временным именем.
        temp.replace(path)
        return True
    except (OSError, ValueError):
        log.warning("%s сорвалась для %s", what, path, exc_info=True)
        return False
    finally:
        # После успешного replace заготовки уже нет; сюда попадаем только с мусором от неудачи.
        with suppress(OSError):
            temp.unlink(missing_ok=True)


def _parse_ebur128(stderr: str, path: Path) -> Loudness | None:
    """Достаёт `I`/`Peak`/`LRA` из сводки ebur128. `None`, если сводки нет или в ней не числа."""
    summary = stderr.rpartition("Summary:")[2] or stderr
    integrated, true_peak, lra = INTEGRATED_RE.search(summary), TRUE_PEAK_RE.search(summary), LRA_RE.search(summary)
    if integrated is None or true_peak is None or lra is None:
        # Сводки нет, когда звуковой поток в файле отсутствует: ffmpeg молча делает пустой проход.
        # Тишина тоже сюда попадает — у неё пик печатается как `-inf`, а гейн от такого замера бессмысленен.
        log.warning("В выводе ebur128 нет полного замера для %s — вероятно, в файле нет звука", path)
        return None
    return Loudness(integrated=float(integrated[1]), true_peak=float(true_peak[1]), lra=float(lra[1]))


def _tail(text: str | None, limit: int = 400) -> str:
    """Последние строки вывода ffmpeg для лога: причина ошибки всегда внизу, а весь stderr в лог не влезет."""
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-3:])[:limit] or "вывод пустой"
