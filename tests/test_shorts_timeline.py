"""I-094 Shorts timeline envelope and I-095 scenes (Prompt Pack v8, #094, #095).

Rules the user accepted on 2026-10-17 (the envelope; assets are I-096 and
captions I-097; I-095 adds the scenes, tested in the last part of this file):

- schema version 1 holds ``duration_ms`` (whole milliseconds, the length of
  the audio), ``timing_source`` and the lists ``scenes``, ``captions`` and
  ``music`` (captions need scenes, see tests/test_shorts_captions.py);
- the canonical JSON has sorted keys and compact separators, and its sha256 is
  what the later steps bind to;
- decoding is strict: an unknown key, a missing required key or a schema
  version this program does not know is a ``ValueError``; ``captions`` and
  ``music`` may be left out and then default to empty.
"""

import dataclasses
import hashlib
import json

import pytest

from ai_youtube_agent.content.shorts_timeline import (
    MAX_DURATION_MS,
    MAX_SCENES,
    MIN_SCENE_MS,
    TIMELINE_SCHEMA_VERSION,
    Scene,
    Timeline,
    TimingSource,
)

CANONICAL = (
    '{"captions":[],"duration_ms":30250,"music":[],"scenes":[],'
    '"schema_version":1,"timing_source":"estimated"}'
)


def make_data(**overrides) -> dict:
    data = {
        "schema_version": 1,
        "duration_ms": 30250,
        "timing_source": "estimated",
        "scenes": [],
        "captions": [],
        "music": [],
    }
    return data | overrides


# An empty timeline


def test_an_empty_timeline_has_the_audio_length_and_nothing_planned() -> None:
    timeline = Timeline.empty(30250)

    assert timeline.schema_version == TIMELINE_SCHEMA_VERSION == 1
    assert timeline.duration_ms == 30250
    assert timeline.timing_source is TimingSource.ESTIMATED
    assert timeline.scenes == () and timeline.captions == () and timeline.music == ()


def test_a_timeline_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Timeline.empty(1000).duration_ms = 5  # type: ignore[misc]


def test_to_dict_lists_every_key() -> None:
    assert Timeline.empty(30250).to_dict() == make_data()


def test_the_timing_sources_are_audio_chunks_and_estimated() -> None:
    assert [source.value for source in TimingSource] == ["audio_chunks", "estimated"]


# Canonical JSON and checksum


def test_to_json_is_canonical() -> None:
    assert Timeline.empty(30250).to_json() == CANONICAL


def test_sha256_is_the_checksum_of_the_canonical_json() -> None:
    timeline = Timeline.empty(30250)

    assert timeline.sha256 == hashlib.sha256(CANONICAL.encode("utf-8")).hexdigest()
    assert len(timeline.sha256) == 64


def test_the_checksum_is_stable_and_follows_the_content() -> None:
    first = Timeline.empty(30250)

    assert Timeline.empty(30250).sha256 == first.sha256
    assert Timeline.empty(30251).sha256 != first.sha256
    other_source = dataclasses.replace(first, timing_source=TimingSource.AUDIO_CHUNKS)
    assert other_source.sha256 != first.sha256


def test_json_round_trips() -> None:
    for source in TimingSource:
        timeline = dataclasses.replace(Timeline.empty(12345), timing_source=source)

        assert Timeline.from_json(timeline.to_json()) == timeline


def test_from_json_accepts_other_spacing() -> None:
    text = json.dumps(make_data(), indent=2)

    assert Timeline.from_json(text) == Timeline.empty(30250)


def test_from_json_refuses_text_that_is_not_json() -> None:
    with pytest.raises(ValueError):
        Timeline.from_json("{not json")


# Decoding


def test_from_dict_defaults_captions_and_music() -> None:
    data = make_data()
    del data["captions"], data["music"]

    assert Timeline.from_dict(data) == Timeline.empty(30250)


@pytest.mark.parametrize("source", ["audio_chunks", "estimated"])
def test_from_dict_reads_the_timing_source(source) -> None:
    timeline = Timeline.from_dict(make_data(timing_source=source))

    assert timeline.timing_source is TimingSource(source)


@pytest.mark.parametrize(
    "missing", ["schema_version", "duration_ms", "timing_source", "scenes"]
)
def test_from_dict_needs_the_required_keys(missing) -> None:
    data = make_data()
    del data[missing]

    with pytest.raises(ValueError):
        Timeline.from_dict(data)


def test_from_dict_refuses_an_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown keys: extra"):
        Timeline.from_dict(make_data(extra=1))


@pytest.mark.parametrize("version", [0, 2, 99, -1])
def test_from_dict_refuses_a_schema_version_it_does_not_know(version) -> None:
    with pytest.raises(ValueError, match="not supported"):
        Timeline.from_dict(make_data(schema_version=version))


@pytest.mark.parametrize("version", [True, "1", 1.0, None])
def test_from_dict_needs_a_whole_schema_version(version) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        Timeline.from_dict(make_data(schema_version=version))


@pytest.mark.parametrize("data", [None, [], "timeline", 3])
def test_from_dict_needs_an_object(data) -> None:
    with pytest.raises(ValueError, match="object"):
        Timeline.from_dict(data)


def test_from_dict_refuses_captions_without_scenes() -> None:
    caption = {"id": "c1", "scene_id": "s1", "text": "Hi", "start_ms": 0, "end_ms": 900}

    with pytest.raises(ValueError, match="need scenes"):
        Timeline.from_dict(make_data(captions=[caption]))


@pytest.mark.parametrize("name", ["scenes", "captions", "music"])
@pytest.mark.parametrize("value", [None, {}, "", 0])
def test_from_dict_needs_lists(name, value) -> None:
    with pytest.raises(ValueError, match="must be a list"):
        Timeline.from_dict(make_data(**{name: value}))


@pytest.mark.parametrize("source", ["live", "", None, 5, ["estimated"]])
def test_from_dict_refuses_an_unknown_timing_source(source) -> None:
    with pytest.raises(ValueError, match="timing_source"):
        Timeline.from_dict(make_data(timing_source=source))


# Validation


@pytest.mark.parametrize(
    "duration", [0, -1, MAX_DURATION_MS + 1, 1.5, "100", True, None]
)
def test_a_bad_duration_is_refused(duration) -> None:
    with pytest.raises(ValueError):
        Timeline(1, duration, TimingSource.ESTIMATED)
    with pytest.raises(ValueError):
        Timeline.from_dict(make_data(duration_ms=duration))


def test_the_duration_limits_are_accepted() -> None:
    assert Timeline.empty(1).duration_ms == 1
    assert Timeline.empty(MAX_DURATION_MS).duration_ms == MAX_DURATION_MS


@pytest.mark.parametrize("version", [0, 2, True])
def test_the_constructor_only_writes_version_one(version) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        Timeline(version, 1000, TimingSource.ESTIMATED)


def test_the_constructor_needs_a_timing_source_and_tuples() -> None:
    with pytest.raises(TypeError):
        Timeline(1, 1000, "estimated")  # type: ignore[arg-type]
    for name in ("scenes", "captions", "music"):
        with pytest.raises(TypeError):
            Timeline(1, 1000, TimingSource.ESTIMATED, **{name: []})


# Scenes (I-095)


def make_scene(position: int = 1, start: int = 0, end: int = 30250, **overrides):
    values = {
        "id": f"s{position}",
        "section_index": position - 1,
        "start_ms": start,
        "end_ms": end,
    }
    return Scene(**(values | overrides))


def make_scenes(*lengths: int, section_indexes=None) -> tuple[Scene, ...]:
    scenes = []
    start = 0
    for position, length in enumerate(lengths, start=1):
        index = section_indexes[position - 1] if section_indexes else position - 1
        scenes.append(
            Scene(f"s{position}", index, start, start + length, f"Part {position}")
        )
        start += length
    return tuple(scenes)


def make_timeline(*lengths: int) -> Timeline:
    return Timeline(
        1, sum(lengths), TimingSource.AUDIO_CHUNKS, make_scenes(*lengths)
    )


def test_a_scene_is_frozen_and_knows_its_duration() -> None:
    scene = make_scene(end=3200)

    assert scene.duration_ms == 3200
    assert scene.title is None and scene.assets == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        scene.end_ms = 5  # type: ignore[misc]


def test_a_scene_to_dict_lists_every_key() -> None:
    scene = make_scene(end=3200, title="Hook")

    assert scene.to_dict() == {
        "id": "s1",
        "section_index": 0,
        "start_ms": 0,
        "end_ms": 3200,
        "title": "Hook",
        "assets": [],
    }


def test_a_timeline_with_scenes_round_trips_with_a_stable_checksum() -> None:
    timeline = make_timeline(3200, 10000, 17050)

    decoded = Timeline.from_json(timeline.to_json())

    assert decoded == timeline
    assert decoded.sha256 == timeline.sha256
    assert Timeline.from_dict(json.loads(timeline.to_json())) == timeline
    assert make_timeline(3200, 10000, 17050).sha256 == timeline.sha256
    assert make_timeline(3201, 10000, 17049).sha256 != timeline.sha256


def test_the_canonical_json_of_scenes_has_sorted_compact_keys() -> None:
    timeline = make_timeline(30250)
    text = timeline.to_json()

    assert text == (
        '{"captions":[],"duration_ms":30250,"music":[],"scenes":[{"assets":[],'
        '"end_ms":30250,"id":"s1","section_index":0,"start_ms":0,'
        '"title":"Part 1"}],"schema_version":1,"timing_source":"audio_chunks"}'
    )
    assert timeline.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_scene_may_leave_out_title_and_assets_when_decoding() -> None:
    data = make_data(
        scenes=[{"id": "s1", "section_index": 0, "start_ms": 0, "end_ms": 30250}]
    )

    assert Timeline.from_dict(data).scenes == (make_scene(),)


@pytest.mark.parametrize("missing", ["id", "section_index", "start_ms", "end_ms"])
def test_a_scene_needs_its_required_keys(missing) -> None:
    scene = make_scene().to_dict()
    del scene[missing]

    with pytest.raises(ValueError, match="missing keys"):
        Timeline.from_dict(make_data(scenes=[scene]))


def test_a_scene_refuses_unknown_keys_and_non_objects() -> None:
    scene = make_scene().to_dict() | {"extra": 1}
    with pytest.raises(ValueError, match="unknown keys: extra"):
        Timeline.from_dict(make_data(scenes=[scene]))
    with pytest.raises(ValueError, match="object"):
        Timeline.from_dict(make_data(scenes=["s1"]))


def test_scene_assets_must_be_scene_assets_in_a_list() -> None:
    scene = make_scene().to_dict()
    with pytest.raises(TypeError):
        make_scene(assets=(object(),))
    with pytest.raises(TypeError):
        make_scene(assets=[])  # type: ignore[arg-type]
    scene["assets"] = {}
    with pytest.raises(ValueError, match="must be a list"):
        Timeline.from_dict(make_data(scenes=[scene]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"id": " "},
        {"id": 1},
        {"section_index": -1},
        {"section_index": True},
        {"section_index": 1.5},
        {"start_ms": -1},
        {"start_ms": "0"},
        {"end_ms": 0},
        {"end_ms": True},
        {"end_ms": 499},
        {"title": ""},
        {"title": " Hook"},
        {"title": 3},
        {"title": "x" * 201},
    ],
)
def test_a_bad_scene_is_refused(overrides) -> None:
    with pytest.raises((ValueError, TypeError)):
        make_scene(**overrides)


def test_a_scene_may_be_exactly_the_minimum_long() -> None:
    assert make_scene(end=MIN_SCENE_MS).duration_ms == MIN_SCENE_MS
    assert make_timeline(500, 29750).scenes[1].start_ms == 500


def test_a_scene_list_must_cover_the_audio_without_gaps_or_overlaps() -> None:
    ok = make_scenes(10000, 20250)

    with pytest.raises(ValueError, match="start at 0"):
        Timeline(1, 30250, TimingSource.ESTIMATED, (make_scene(start=1),))
    for start in (10001, 9999):
        with pytest.raises(ValueError, match="previous scene ends"):
            Timeline(
                1,
                30250,
                TimingSource.ESTIMATED,
                (ok[0], dataclasses.replace(ok[1], start_ms=start)),
            )
    for duration in (30251, 30249):
        with pytest.raises(ValueError, match="the audio is"):
            Timeline(1, duration, TimingSource.ESTIMATED, ok)


def test_scene_ids_are_s_and_the_position() -> None:
    ok = make_scenes(10000, 20250)

    with pytest.raises(ValueError, match="id s2"):
        Timeline(
            1,
            30250,
            TimingSource.ESTIMATED,
            (ok[0], dataclasses.replace(ok[1], id="s1")),
        )
    with pytest.raises(ValueError, match="id s1"):
        Timeline(1, 20250, TimingSource.ESTIMATED, (ok[1],))


def test_section_indexes_never_go_back_but_may_repeat() -> None:
    same = make_scenes(10000, 20250, section_indexes=[0, 0])
    assert Timeline(1, 30250, TimingSource.ESTIMATED, same).scenes == same
    skipping = make_scenes(10000, 20250, section_indexes=[0, 5])
    assert Timeline(1, 30250, TimingSource.ESTIMATED, skipping).scenes == skipping

    with pytest.raises(ValueError, match="earlier section"):
        Timeline(
            1,
            30250,
            TimingSource.ESTIMATED,
            make_scenes(10000, 20250, section_indexes=[1, 0]),
        )


def test_a_timeline_has_at_most_sixty_scenes() -> None:
    duration = MAX_SCENES * MIN_SCENE_MS
    scenes = make_scenes(*[MIN_SCENE_MS] * MAX_SCENES)
    assert Timeline(1, duration, TimingSource.ESTIMATED, scenes).scenes == scenes

    too_many = make_scenes(*[MIN_SCENE_MS] * (MAX_SCENES + 1))
    with pytest.raises(ValueError, match="at most 60"):
        Timeline(1, duration + MIN_SCENE_MS, TimingSource.ESTIMATED, too_many)


def test_the_scenes_must_be_scene_values() -> None:
    with pytest.raises(TypeError):
        Timeline(1, 1000, TimingSource.ESTIMATED, (object(),))


def test_a_decoded_scene_list_is_checked_like_a_built_one() -> None:
    data = make_timeline(10000, 20250).to_dict()
    data["scenes"][1]["start_ms"] = 9000

    with pytest.raises(ValueError, match="previous scene ends"):
        Timeline.from_dict(data)
