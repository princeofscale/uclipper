"""Умное центрирование вертикального кропа по лицу.

OpenCV — необязательная зависимость, поэтому детектор обязан молча отдавать None, когда лиц/OpenCV нет.
Настоящее лицо в тест не подсунуть, поэтому модель лиц подменяется: она отдаёт заданные прямоугольники
(в долях кадра), а видео читается настоящим OpenCV — проверяется выбор лица и итоговая доля ширины.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.services.face_center import detect_face_center_x, face_crop_available  # noqa: E402
from tests.media_utils import luma_at  # noqa: E402


def install_fake_cascade(monkeypatch, per_call):
    """Подменяет CascadeClassifier: на i-м вызове detectMultiScale отдаёт per_call[i] — доли (x, y, w, h) кадра.

    Возвращает список средних яркостей кадров, которые получил детектор, — у тестового видео яркость растёт
    со временем, так что по ним видно, из каких секунд взяты кадры.
    """
    seen_brightness = []

    class FakeCascade:
        def __init__(self, path):
            assert Path(path).is_file(), f"детектор должен загружать существующий файл каскада, а не {path}"

        def empty(self):
            return False

        def detectMultiScale(self, image, *args, **kwargs):
            height, width = image.shape[:2]
            boxes = per_call[len(seen_brightness)]
            seen_brightness.append(float(image.mean()))
            rows = [[round(x * width), round(y * height), round(w * width), round(h * height)] for x, y, w, h in boxes]
            return np.array(rows, dtype=int).reshape(-1, 4)

    monkeypatch.setattr(cv2, "CascadeClassifier", FakeCascade)
    return seen_brightness


def test_center_is_the_median_of_the_largest_face_in_each_sample(synthetic_video, monkeypatch):
    install_fake_cascade(
        monkeypatch,
        [
            [(0.05, 0.1, 0.08, 0.10), (0.70, 0.1, 0.15, 0.20)],  # два лица: берём крупное, его центр 0.775
            [(0.25, 0.1, 0.06, 0.10)],  # центр 0.28
            [(0.78, 0.1, 0.12, 0.20)],  # центр 0.84
        ],
    )
    assert detect_face_center_x(synthetic_video, 2.0, 6.0, samples=3) == pytest.approx(0.775, abs=0.01)


def test_samples_without_faces_are_ignored(synthetic_video, monkeypatch):
    install_fake_cascade(monkeypatch, [[], [(0.25, 0.1, 0.10, 0.20)], []])  # единственное лицо — центр 0.30
    assert detect_face_center_x(synthetic_video, 2.0, 6.0, samples=3) == pytest.approx(0.30, abs=0.01)


def test_none_when_no_sample_has_a_face(synthetic_video, monkeypatch):
    install_fake_cascade(monkeypatch, [[], [], []])
    assert detect_face_center_x(synthetic_video, 2.0, 6.0, samples=3) is None


def test_frames_are_taken_evenly_from_the_clip_window(synthetic_video, monkeypatch, tmp_path):
    """Клип 2–8 с, три кадра → из середин третей окна: 3, 5 и 7 с источника."""
    seen = install_fake_cascade(monkeypatch, [[], [], []])
    detect_face_center_x(synthetic_video, 2.0, 6.0, samples=3)
    expected = [luma_at(synthetic_video, second, (640, 360), tmp_path) for second in (3.0, 5.0, 7.0)]
    assert seen == pytest.approx(expected, abs=4)


def test_none_on_a_clip_without_faces_with_the_real_detector(synthetic_video):
    assert detect_face_center_x(synthetic_video, 0.0, 10.0) is None


def test_none_when_opencv_is_not_installed(synthetic_video, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)  # `import cv2` теперь бросает ImportError
    assert detect_face_center_x(synthetic_video, 0.0, 10.0) is None


def test_none_when_opencv_has_no_cascade_classifier(synthetic_video, monkeypatch):
    monkeypatch.delattr(cv2, "CascadeClassifier")  # так выглядит OpenCV 5: Haar-каскады вынесены в contrib
    assert detect_face_center_x(synthetic_video, 0.0, 10.0) is None


def test_face_crop_is_available_with_a_working_opencv():
    if not hasattr(cv2, "CascadeClassifier"):
        pytest.skip("OpenCV 5: каскады Хаара вынесены в contrib")
    assert face_crop_available() is True


def test_face_crop_is_unavailable_without_opencv(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert face_crop_available() is False


def test_face_crop_is_unavailable_when_opencv_has_no_cascade_classifier(monkeypatch):
    monkeypatch.delattr(cv2, "CascadeClassifier")
    assert face_crop_available() is False


def test_none_when_the_video_cannot_be_read(tmp_path):
    assert detect_face_center_x(tmp_path / "missing.mp4", 0.0, 5.0) is None


def test_an_opencv_failure_never_propagates(synthetic_video, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("внутренняя ошибка OpenCV")

    monkeypatch.setattr(cv2, "VideoCapture", broken)
    assert detect_face_center_x(synthetic_video, 0.0, 10.0) is None
