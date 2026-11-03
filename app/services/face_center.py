"""Умное центрирование вертикального кропа по лицу (OpenCV — необязательная зависимость).

Если OpenCV не установлен, его версия не умеет каскады Хаара (OpenCV 5 вынес их в contrib) или лиц в клипе
нет — возвращается None, и кроп остаётся по центру кадра. Любая ошибка OpenCV тоже даёт None: это
необязательное улучшение, оно не должно ронять рендер.
"""
from __future__ import annotations

import statistics
from pathlib import Path

CASCADE_NAME = "haarcascade_frontalface_default.xml"
DETECT_WIDTH = 640  # лица ищем на уменьшенном кадре: результат — доля ширины, от масштаба не зависит
MIN_FACE_PIXELS = 32  # лица мельче на кадре шириной 640 px — случайные срабатывания


def _load_cascade(cv2):
    """Каскад Хаара для лиц или None: нет класса (OpenCV 5), нет файла или он не загрузился."""
    cascade_class = getattr(cv2, "CascadeClassifier", None)
    data = getattr(cv2, "data", None)
    if cascade_class is None or data is None:
        return None
    cascade_path = Path(data.haarcascades) / CASCADE_NAME
    if not cascade_path.is_file():
        return None
    cascade = cascade_class(str(cascade_path))
    return None if cascade.empty() else cascade


def face_crop_available() -> bool:
    """Умеет ли эта установка искать лица (иначе режим «Face Crop» режет по центру кадра)."""
    try:
        import cv2

        return _load_cascade(cv2) is not None
    except Exception:  # noqa: BLE001 — нет OpenCV или он сломан: просто «недоступно»
        return False


def detect_face_center_x(source: Path, clip_start: float, clip_duration: float, samples: int = 8) -> float | None:
    """Горизонтальный центр лица в клипе как доля ширины кадра (0–1) или None.

    Берёт `samples` кадров, равномерно распределённых по окну клипа; на каждом выбирает самое крупное лицо,
    результат — медиана их центров (один неудачный кадр её не сдвигает).
    """
    try:
        return _detect(Path(source), clip_start, clip_duration, samples)
    except Exception:  # noqa: BLE001 — любой сбой OpenCV означает «лица не нашли»
        return None


def _detect(source: Path, clip_start: float, clip_duration: float, samples: int) -> float | None:
    try:
        import cv2
    except ImportError:
        return None

    cascade = _load_cascade(cv2)
    if cascade is None:
        return None

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return None
    centers: list[float] = []
    try:
        for index in range(samples):
            moment = clip_start + clip_duration * (index + 0.5) / samples
            capture.set(cv2.CAP_PROP_POS_MSEC, moment * 1000)
            grabbed, frame = capture.read()
            if not grabbed or frame is None:
                continue
            width = frame.shape[1]
            if width > DETECT_WIDTH:
                frame = cv2.resize(frame, (DETECT_WIDTH, max(1, round(frame.shape[0] * DETECT_WIDTH / width))))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(MIN_FACE_PIXELS, MIN_FACE_PIXELS))
            if len(faces) == 0:
                continue
            x, _, w, _ = max(faces, key=lambda face: face[2] * face[3])
            centers.append(min(1.0, max(0.0, (x + w / 2) / gray.shape[1])))
    finally:
        capture.release()
    return float(statistics.median(centers)) if centers else None
