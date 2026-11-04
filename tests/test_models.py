"""Валидация запроса на задачу и раскладка рабочих папок."""
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import JobRequest, SourceType

UPLOAD = {"source_type": SourceType.UPLOAD, "upload_id": "a" * 32, "banner_id": "banner.mp4"}


def test_banner_is_required_when_it_has_to_be_shown():
    with pytest.raises(ValidationError):
        JobRequest(source_type=SourceType.UPLOAD, upload_id="a" * 32, banner_count=2)


def test_banner_is_optional_when_it_is_never_shown():
    request = JobRequest(source_type=SourceType.UPLOAD, upload_id="a" * 32, banner_count=0)
    assert request.banner_id is None


def test_each_source_type_needs_its_own_field():
    with pytest.raises(ValidationError):
        JobRequest(source_type=SourceType.LINK, banner_count=0)
    with pytest.raises(ValidationError):
        JobRequest(source_type=SourceType.UPLOAD, banner_count=0)


@pytest.mark.parametrize(
    "field, value",
    [
        ("num_clips", 0),
        ("num_clips", 51),
        ("banner_count", -1),
        ("banner_count", 6),
        ("banner_duration", 0.9),
        ("banner_duration", 20.1),
        ("banner_scale", 49),
        ("banner_scale", 96),
        ("clip_duration", 1.9),
        ("clip_duration", 180.5),
        ("start_offset", -1),
        ("end_offset", -1),
    ],
)
def test_values_outside_the_documented_range_are_rejected(field, value):
    with pytest.raises(ValidationError):
        JobRequest(**{**UPLOAD, field: value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("num_clips", 1),
        ("num_clips", 50),
        ("banner_count", 0),
        ("banner_count", 5),
        ("banner_duration", 1.0),
        ("banner_duration", 20.0),
        ("banner_scale", 50),
        ("banner_scale", 95),
        ("clip_duration", 2.0),
        ("clip_duration", 180),
    ],
)
def test_range_edges_are_accepted(field, value):
    JobRequest(**{**UPLOAD, field: value})


def test_settings_for_root_keep_writable_dirs_inside_the_root(tmp_path):
    settings = Settings.for_root(tmp_path)
    settings.ensure_dirs()
    for directory in (
        settings.banners_dir,
        settings.sfx_dir,
        settings.outputs_dir,
        settings.cache_dir,
        settings.uploads_dir,
    ):
        assert directory.is_dir()
        assert tmp_path in directory.parents


def test_default_settings_follow_the_layout_from_the_spec():
    settings = Settings.default()
    assert settings.banners_dir.as_posix().endswith("workspace/banners")
    assert settings.sfx_dir.as_posix().endswith("workspace/sfx")
    assert settings.outputs_dir.as_posix().endswith("workspace/outputs")
    assert settings.cache_dir.as_posix().endswith("workspace/cache")
    assert settings.uploads_dir.as_posix().endswith("workspace/uploads")


def test_new_options_default_to_the_old_behaviour():
    request = JobRequest(**{**UPLOAD, "banner_count": 0})
    assert request.end_offset == 0
    assert request.auto_trim is False  # автообрезка включается явно (интерфейс включает её сам)
    assert request.banner_sound is True
