"""I-096 Shorts scene assets and music (Prompt Pack v8, prompt #096).

Rules settled on 2026-10-17 (design D5 and the status rule):

- a ``SceneAsset`` places an asset in a scene (times relative to the scene), an
  ``AssetPlacement`` places music on the timeline (absolute times); decoding is
  strict and the timing, fade, gain, box and count rules hold;
- ``ROLE_KINDS`` maps a role to the asset kinds it accepts;
- ``save_timeline`` checks every referenced asset (exists, same channel,
  attached to the item, rights category not unknown, kind fits the role) and
  snapshots the current provenance id; a different provenance id is refused;
- ``ready`` needs one background per scene and, with captions on, valid captions
  (I-097, tests/test_shorts_captions.py), so without captions it stays ``planned``.
"""

import dataclasses
import uuid

import pytest
from test_shorts_planner import add_sectioned_script, words
from test_shorts_project import STANDARD, World, refusal

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.shorts_config import ShortsConfig
from ai_youtube_agent.content.shorts_project import ShortsProjectStatus
from ai_youtube_agent.content.shorts_service import (
    ShortsProjectErrorCode as Code,
)
from ai_youtube_agent.content.shorts_service import readiness
from ai_youtube_agent.content.shorts_timeline import (
    MAX_MUSIC,
    MAX_SCENE_ASSETS,
    ROLE_KINDS,
    AssetFit,
    AssetPlacement,
    AssetRole,
    Box,
    Scene,
    SceneAsset,
    Timeline,
    TimingSource,
)
from ai_youtube_agent.content.strategy import ShortsFormat
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from factories import USER, at, make_channel, T0

# Value objects


def asset(role=AssetRole.BACKGROUND, **overrides) -> SceneAsset:
    return SceneAsset(overrides.pop("asset_id", "a1"), role, **overrides)


def music(**overrides) -> AssetPlacement:
    return AssetPlacement(overrides.pop("asset_id", "m1"), **overrides)


def test_defaults_of_a_scene_asset() -> None:
    placed = asset()

    assert placed.start_ms == 0 and placed.end_ms is None
    assert placed.source_in_ms is None and placed.source_out_ms is None
    assert placed.fit is AssetFit.COVER and placed.box is None
    assert placed.gain_db is None and placed.fade_in_ms == placed.fade_out_ms == 0
    assert placed.provenance_id is None


def test_music_defaults_to_the_music_role() -> None:
    assert music().role is AssetRole.MUSIC


def test_the_role_decides_who_may_hold_it() -> None:
    with pytest.raises(ValueError, match="role"):
        SceneAsset("a", AssetRole.MUSIC)
    with pytest.raises(ValueError, match="role"):
        AssetPlacement("a", AssetRole.BACKGROUND)
    with pytest.raises(ValueError, match="role"):
        SceneAsset("a", "background")  # type: ignore[arg-type]


def test_role_to_kind_map() -> None:
    image_or_clip = {AssetKind.IMAGE, AssetKind.VIDEO_CLIP}
    assert ROLE_KINDS[AssetRole.BACKGROUND] == image_or_clip
    assert ROLE_KINDS[AssetRole.B_ROLL] == image_or_clip
    assert ROLE_KINDS[AssetRole.OVERLAY] == image_or_clip | {AssetKind.TEMPLATE}
    assert ROLE_KINDS[AssetRole.MUSIC] == {AssetKind.MUSIC}
    assert ROLE_KINDS[AssetRole.SFX] == {AssetKind.AUDIO}


@pytest.mark.parametrize("asset_id", ["", " ", " a", "a ", None, 3, "x" * 201])
def test_asset_id_is_a_trimmed_text(asset_id) -> None:
    with pytest.raises(ValueError, match="asset_id"):
        asset(asset_id=asset_id)


@pytest.mark.parametrize("value", ["", " p", 3])
def test_provenance_id_is_a_trimmed_text_when_set(value) -> None:
    with pytest.raises(ValueError, match="provenance_id"):
        asset(provenance_id=value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_ms": -1},
        {"start_ms": True},
        {"start_ms": 1.5},
        {"end_ms": 0},
        {"end_ms": 1000, "start_ms": 1000},
        {"end_ms": 500, "start_ms": 1000},
        {"source_in_ms": -1},
        {"source_in_ms": 1000, "source_out_ms": 1000},
        {"source_in_ms": 2000, "source_out_ms": 1000},
    ],
)
def test_timing_rules_of_the_fields(overrides) -> None:
    with pytest.raises(ValueError):
        asset(**overrides)


def test_source_range_may_be_half_open() -> None:
    assert asset(source_in_ms=500).source_out_ms is None
    assert asset(source_out_ms=500).source_in_ms is None
    assert asset(source_in_ms=0, source_out_ms=1).source_out_ms == 1


@pytest.mark.parametrize("gain", [-60.1, 12.1, True, "3", float("nan")])
def test_gain_must_be_in_range(gain) -> None:
    with pytest.raises(ValueError, match="gain_db"):
        asset(AssetRole.SFX, gain_db=gain)


def test_gain_limits_are_allowed() -> None:
    assert asset(AssetRole.SFX, gain_db=-60).gain_db == -60
    assert asset(AssetRole.SFX, gain_db=12.0).gain_db == 12.0


def test_fades_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="fade_in_ms"):
        asset(AssetRole.SFX, fade_in_ms=-1)
    with pytest.raises(ValueError, match="fade_out_ms"):
        music(fade_out_ms=-1)


@pytest.mark.parametrize(
    "overrides",
    [{"gain_db": -3}, {"fade_in_ms": 100}, {"fade_out_ms": 100}],
)
@pytest.mark.parametrize(
    "role", [AssetRole.BACKGROUND, AssetRole.B_ROLL, AssetRole.OVERLAY]
)
def test_audio_fields_are_only_for_audio_roles(role, overrides) -> None:
    with pytest.raises(ValueError, match="audio"):
        asset(role, **overrides)


def test_a_box_is_only_for_visual_roles() -> None:
    box = Box(100, 100, 200, 200)
    assert asset(AssetRole.OVERLAY, box=box).box == box
    with pytest.raises(ValueError, match="no box"):
        asset(AssetRole.SFX, box=box)
    with pytest.raises(ValueError, match="no box"):
        music(box=box)
    with pytest.raises(TypeError):
        asset(box={"x": 0})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        (-1, 0, 10, 10),
        (0, 0, 0, 10),
        (0, 0, 10, 0),
        (500, 0, 501, 10),
        (0, 500, 10, 501),
        (0, 0, 1001, 10),
        (True, 0, 10, 10),
    ],
)
def test_a_box_is_a_permille_rectangle_inside_the_canvas(values) -> None:
    with pytest.raises(ValueError):
        Box(*values)


def test_a_full_canvas_box_is_allowed() -> None:
    assert Box(0, 0, 1000, 1000).to_dict() == {
        "x": 0,
        "y": 0,
        "width": 1000,
        "height": 1000,
    }


def test_the_fit_must_be_a_fit() -> None:
    with pytest.raises(TypeError):
        asset(fit="cover")  # type: ignore[arg-type]


# Round trip and strict decoding


def full_asset() -> SceneAsset:
    return SceneAsset(
        "a1",
        AssetRole.OVERLAY,
        500,
        2500,
        1000,
        4000,
        AssetFit.CONTAIN,
        Box(10, 20, 300, 400),
        None,
        0,
        0,
        "prov1",
    )


def test_a_scene_asset_round_trips_through_its_dict() -> None:
    placed = full_asset()

    assert SceneAsset.from_dict(placed.to_dict()) == placed
    assert placed.to_dict() == {
        "asset_id": "a1",
        "role": "overlay",
        "start_ms": 500,
        "end_ms": 2500,
        "source_in_ms": 1000,
        "source_out_ms": 4000,
        "fit": "contain",
        "box": {"x": 10, "y": 20, "width": 300, "height": 400},
        "gain_db": None,
        "fade_in_ms": 0,
        "fade_out_ms": 0,
        "provenance_id": "prov1",
    }


def test_missing_optional_keys_take_their_defaults() -> None:
    placed = SceneAsset.from_dict({"asset_id": "a1", "role": "background"})

    assert placed == asset()


def test_music_may_leave_out_the_role_but_not_the_asset() -> None:
    assert AssetPlacement.from_dict({"asset_id": "m1"}) == music()
    with pytest.raises(ValueError, match="missing keys: asset_id"):
        AssetPlacement.from_dict({"role": "music"})
    with pytest.raises(ValueError, match="missing keys: role"):
        SceneAsset.from_dict({"asset_id": "a1"})


def test_decoding_refuses_unknown_keys_and_bad_values() -> None:
    base = {"asset_id": "a1", "role": "background"}
    with pytest.raises(ValueError, match="unknown keys: volume"):
        SceneAsset.from_dict(base | {"volume": 1})
    with pytest.raises(ValueError, match="object"):
        SceneAsset.from_dict([])
    with pytest.raises(ValueError, match="role"):
        SceneAsset.from_dict(base | {"role": "foreground"})
    with pytest.raises(ValueError, match="role"):
        SceneAsset.from_dict(base | {"role": None})
    with pytest.raises(ValueError, match="fit"):
        SceneAsset.from_dict(base | {"fit": "stretch"})
    with pytest.raises(ValueError, match="box"):
        SceneAsset.from_dict(base | {"box": {"x": 0}})
    with pytest.raises(ValueError, match="box"):
        SceneAsset.from_dict(base | {"box": []})
    with pytest.raises(ValueError, match="role"):
        AssetPlacement.from_dict({"asset_id": "m1", "role": "sfx"})


# Scene rules


def test_scene_assets_are_relative_to_the_scene_and_must_fit() -> None:
    scene = Scene("s1", 0, 0, 3000, None, (asset(end_ms=3000),))
    assert scene.assets[0].end_ms == 3000
    with pytest.raises(ValueError, match="within"):
        Scene("s1", 0, 0, 3000, None, (asset(end_ms=3001),))
    with pytest.raises(ValueError, match="within"):
        Scene("s1", 0, 0, 3000, None, (asset(AssetRole.B_ROLL, start_ms=3000),))


def test_scene_times_are_not_absolute() -> None:
    # The scene starts at 10 s but the asset runs 0..2 s of the scene.
    scene = Scene("s2", 1, 10_000, 13_000, None, (asset(end_ms=2000),))

    assert scene.assets[0].end_ms == 2000


def test_fades_must_fit_the_placement() -> None:
    sfx = asset(AssetRole.SFX, start_ms=0, end_ms=1000, fade_in_ms=500, fade_out_ms=500)
    Scene("s1", 0, 0, 3000, None, (sfx,))
    with pytest.raises(ValueError, match="fades"):
        Scene("s1", 0, 0, 3000, None, (dataclasses.replace(sfx, fade_out_ms=501),))
    with pytest.raises(ValueError, match="fades"):
        Scene(
            "s1", 0, 0, 3000, None, (asset(AssetRole.SFX, fade_in_ms=2000, fade_out_ms=1001),)
        )


def test_a_scene_has_at_most_one_background() -> None:
    with pytest.raises(ValueError, match="more than one background"):
        Scene("s1", 0, 0, 3000, None, (asset(asset_id="a"), asset(asset_id="b")))
    Scene(
        "s1",
        0,
        0,
        3000,
        None,
        (asset(asset_id="a"), asset(AssetRole.B_ROLL, asset_id="b")),
    )


def test_a_scene_has_a_bounded_number_of_assets() -> None:
    sfx = tuple(asset(AssetRole.SFX, asset_id=f"s{i}") for i in range(MAX_SCENE_ASSETS))
    assert len(Scene("s1", 0, 0, 3000, None, sfx).assets) == MAX_SCENE_ASSETS
    with pytest.raises(ValueError, match="at most"):
        Scene("s1", 0, 0, 3000, None, sfx + (asset(AssetRole.SFX, asset_id="x"),))


def test_scene_assets_must_be_scene_assets() -> None:
    with pytest.raises(TypeError):
        Scene("s1", 0, 0, 3000, None, (music(),))


def test_a_scene_round_trips_with_its_assets() -> None:
    scene = Scene("s1", 0, 0, 3000, "Hook", (asset(), full_asset()))

    assert Scene.from_dict(scene.to_dict()) == scene
    assert [item["asset_id"] for item in scene.to_dict()["assets"]] == ["a1", "a1"]


def test_scene_decoding_reports_a_bad_asset() -> None:
    data = Scene("s1", 0, 0, 3000).to_dict() | {"assets": [{"asset_id": "a"}]}

    with pytest.raises(ValueError, match="missing keys: role"):
        Scene.from_dict(data)


# Timeline music


def timeline_with(*, scenes=(), placements=()) -> Timeline:
    return Timeline(
        1, 3000, TimingSource.ESTIMATED, tuple(scenes), (), tuple(placements)
    )


def test_music_is_absolute_and_round_trips() -> None:
    timeline = timeline_with(
        scenes=[Scene("s1", 0, 0, 3000, None, (asset(),))],
        placements=[music(start_ms=1000, end_ms=3000, gain_db=-12, fade_out_ms=500)],
    )

    decoded = Timeline.from_json(timeline.to_json())

    assert decoded == timeline
    assert decoded.sha256 == timeline.sha256
    assert decoded.music[0].gain_db == -12


def test_music_must_lie_within_the_timeline() -> None:
    with pytest.raises(ValueError, match="within"):
        timeline_with(placements=[music(end_ms=3001)])
    with pytest.raises(ValueError, match="within"):
        timeline_with(placements=[music(start_ms=3000)])
    with pytest.raises(ValueError, match="fades"):
        timeline_with(placements=[music(fade_in_ms=2000, fade_out_ms=1001)])


def test_music_is_bounded_and_typed() -> None:
    many = [music(asset_id=f"m{i}") for i in range(MAX_MUSIC)]
    assert len(timeline_with(placements=many).music) == MAX_MUSIC
    with pytest.raises(ValueError, match="at most"):
        timeline_with(placements=many + [music(asset_id="x")])
    with pytest.raises(TypeError):
        timeline_with(placements=[asset(AssetRole.SFX)])


def test_music_decodes_and_may_be_left_out() -> None:
    data = Timeline.empty(3000).to_dict()
    data["music"] = [{"asset_id": "m1"}]

    assert Timeline.from_dict(data).music == (music(),)
    del data["music"]
    assert Timeline.from_dict(data).music == ()
    data["music"] = {}
    with pytest.raises(ValueError, match="must be a list"):
        Timeline.from_dict(data)
    data["music"] = [{"asset_id": "m1", "role": "music", "extra": 1}]
    with pytest.raises(ValueError, match="unknown keys"):
        Timeline.from_dict(data)


def test_a_caption_with_missing_keys_cannot_be_decoded() -> None:
    data = Timeline.empty(3000).to_dict() | {"captions": [{"id": "c1"}]}

    with pytest.raises(ValueError, match="caption is missing keys"):
        Timeline.from_dict(data)


# Readiness


def ready_timeline(*, backgrounds=(1, 1)) -> Timeline:
    scenes = []
    for position, count in enumerate(backgrounds):
        assets = tuple(asset(asset_id=f"a{position}") for _ in range(count))
        if count > 1:
            assets = (assets[0], dataclasses.replace(assets[0], role=AssetRole.B_ROLL))
        scenes.append(
            Scene(
                f"s{position + 1}", position, position * 1500, (position + 1) * 1500,
                None, assets,
            )
        )
    return timeline_with(scenes=scenes)


def test_readiness_is_ready_with_one_background_per_scene_and_no_captions() -> None:
    config = ShortsConfig.from_format(ShortsFormat(15, 60, captions=False))

    assert readiness(config, ready_timeline()) is ShortsProjectStatus.READY


def test_readiness_stays_planned_without_a_background_in_every_scene() -> None:
    config = ShortsConfig.from_format(ShortsFormat(15, 60, captions=False))
    timeline = ready_timeline()
    scenes = (timeline.scenes[0], dataclasses.replace(timeline.scenes[1], assets=()))

    assert (
        readiness(config, dataclasses.replace(timeline, scenes=scenes))
        is ShortsProjectStatus.PLANNED
    )


def test_b_roll_is_not_a_background() -> None:
    config = ShortsConfig.from_format(ShortsFormat(15, 60, captions=False))
    scene = Scene("s1", 0, 0, 3000, None, (asset(AssetRole.B_ROLL),))

    assert (
        readiness(config, timeline_with(scenes=[scene])) is ShortsProjectStatus.PLANNED
    )


def test_readiness_is_not_ready_while_captions_are_on() -> None:
    config = ShortsConfig.from_format(STANDARD)
    assert config.captions

    assert readiness(config, ready_timeline()) is ShortsProjectStatus.PLANNED


def test_readiness_without_scenes_is_a_draft_and_music_does_not_matter() -> None:
    config = ShortsConfig.from_format(ShortsFormat(15, 60, captions=False))

    assert (
        readiness(config, timeline_with(placements=[music()]))
        is ShortsProjectStatus.DRAFT
    )


# The service: asset checks


class Stage:
    """A world with a planned project and helpers to register assets."""

    def __init__(self, database: Database, *, captions: bool = True) -> None:
        self.world = World(database, ShortsFormat(15, 60, captions=captions))
        self.database = database
        add_sectioned_script(self.world, [words(2), words(3), words(5)])
        project = self.world.service.create(self.world.item.id, actor=USER)
        self.project = self.world.service.plan(
            project.id, expected_version=project.version, actor=USER
        )

    def add_asset(
        self,
        kind=AssetKind.IMAGE,
        category=AssetCategory.USER_OWNED,
        *,
        channel_id: str | None = None,
        attach: bool = True,
        provenance: bool = False,
    ) -> Asset:
        key = uuid.uuid4().hex
        asset_entity = Asset.create(
            channel_id or self.world.channel.id,
            kind,
            category,
            title=f"Asset {key}",
            source="user",
            owner="Owner" if category is AssetCategory.USER_OWNED else None,
            license_ref="CC" if category is AssetCategory.LICENSED else None,
            clock=at(T0),
        )
        with self.database.transaction() as connection:
            AssetRepository(connection).add(
                asset_entity, source_key=key, title_key=key
            )
            if attach:
                AssetUsageRepository(connection).add(
                    AssetUsage.create(
                        asset_entity.id,
                        self.world.item.id,
                        attached_by=USER,
                        clock=at(T0),
                    )
                )
        if provenance:
            self.add_provenance(asset_entity.id)
        return asset_entity

    def add_provenance(self, asset_id: str) -> Provenance:
        record = Provenance.create(
            asset_id,
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            recorded_by=USER,
            clock=at(T0),
        )
        with self.database.transaction() as connection:
            ProvenanceRepository(connection).add(record)
        return record

    def timeline(self, *placed: SceneAsset, music_items=()) -> Timeline:
        scenes = list(self.project.timeline.scenes)
        scenes[0] = dataclasses.replace(scenes[0], assets=tuple(placed))
        return dataclasses.replace(
            self.project.timeline, scenes=tuple(scenes), music=tuple(music_items)
        )

    def save(self, timeline: Timeline):
        return self.world.service.save_timeline(
            self.project.id,
            timeline,
            expected_version=self.project.version,
            actor=USER,
        )

    def refuse(self, timeline: Timeline, code: Code):
        error = refusal(lambda: self.save(timeline), code)
        assert self.world.project_of() == self.project
        return error


@pytest.fixture
def stage(database: Database) -> Stage:
    return Stage(database)


def test_a_valid_background_is_saved_and_the_project_stays_planned(
    stage: Stage,
) -> None:
    image = stage.add_asset()

    saved = stage.save(stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND)))

    assert saved.version == stage.project.version + 1
    assert saved.timeline.scenes[0].assets[0].asset_id == image.id
    # The planned timeline has several scenes and captions are on.
    assert saved.status is ShortsProjectStatus.PLANNED
    assert stage.world.project_of() == saved


def test_every_kind_a_role_accepts_can_be_used(stage: Stage) -> None:
    for role, kinds in ROLE_KINDS.items():
        for kind in kinds:
            placed = stage.add_asset(kind)
            if role is AssetRole.MUSIC:
                timeline = stage.timeline(music_items=[AssetPlacement(placed.id)])
            else:
                timeline = stage.timeline(SceneAsset(placed.id, role))
            saved = stage.save(timeline)
            stage.project = saved


def test_music_is_checked_like_a_scene_asset(stage: Stage) -> None:
    track = stage.add_asset(AssetKind.MUSIC)

    saved = stage.save(stage.timeline(music_items=[AssetPlacement(track.id)]))

    assert saved.timeline.music[0].asset_id == track.id


def test_an_unknown_asset_is_refused(stage: Stage) -> None:
    stage.refuse(
        stage.timeline(SceneAsset("missing", AssetRole.BACKGROUND)),
        Code.ASSET_NOT_FOUND,
    )


def test_an_unknown_music_asset_is_refused(stage: Stage) -> None:
    stage.refuse(
        stage.timeline(music_items=[AssetPlacement("missing")]), Code.ASSET_NOT_FOUND
    )


def test_an_asset_of_another_channel_is_refused(
    stage: Stage, database: Database
) -> None:
    other = make_channel()
    with database.transaction() as connection:
        ChannelRepository(connection).add(other)
    foreign = stage.add_asset(channel_id=other.id, attach=False)

    stage.refuse(
        stage.timeline(SceneAsset(foreign.id, AssetRole.BACKGROUND)),
        Code.ASSET_OTHER_CHANNEL,
    )


def test_an_asset_not_attached_to_the_item_is_refused(stage: Stage) -> None:
    loose = stage.add_asset(attach=False)

    stage.refuse(
        stage.timeline(SceneAsset(loose.id, AssetRole.BACKGROUND)),
        Code.ASSET_NOT_ATTACHED,
    )


def test_an_asset_attached_to_another_item_is_refused(stage: Stage) -> None:
    other_item = stage.world.add_item()
    image = stage.add_asset(attach=False)
    with stage.database.transaction() as connection:
        AssetUsageRepository(connection).add(
            AssetUsage.create(
                image.id, other_item.id, attached_by=USER, clock=at(T0)
            )
        )

    stage.refuse(
        stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND)),
        Code.ASSET_NOT_ATTACHED,
    )


def test_an_asset_with_an_unknown_category_is_refused(stage: Stage) -> None:
    unknown = stage.add_asset(category=AssetCategory.UNKNOWN)

    stage.refuse(
        stage.timeline(SceneAsset(unknown.id, AssetRole.BACKGROUND)),
        Code.ASSET_RIGHTS_UNKNOWN,
    )


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        (AssetKind.AUDIO, AssetRole.BACKGROUND),
        (AssetKind.MUSIC, AssetRole.B_ROLL),
        (AssetKind.FONT, AssetRole.OVERLAY),
        (AssetKind.TEMPLATE, AssetRole.BACKGROUND),
        (AssetKind.IMAGE, AssetRole.SFX),
        (AssetKind.MUSIC, AssetRole.SFX),
    ],
)
def test_an_asset_of_the_wrong_kind_is_refused(stage: Stage, kind, role) -> None:
    wrong = stage.add_asset(kind)

    stage.refuse(stage.timeline(SceneAsset(wrong.id, role)), Code.ASSET_KIND_MISMATCH)


def test_music_of_the_wrong_kind_is_refused(stage: Stage) -> None:
    image = stage.add_asset(AssetKind.IMAGE)

    stage.refuse(
        stage.timeline(music_items=[AssetPlacement(image.id)]),
        Code.ASSET_KIND_MISMATCH,
    )


def test_a_refused_save_audits_nothing_new(stage: Stage) -> None:
    before = len(stage.world.events())

    stage.refuse(
        stage.timeline(SceneAsset("missing", AssetRole.BACKGROUND)),
        Code.ASSET_NOT_FOUND,
    )

    assert len(stage.world.events()) == before


def test_error_codes_have_messages_and_the_provenance_one_is_a_conflict() -> None:
    from ai_youtube_agent.content.shorts_service import HTTP_STATUS, USER_MESSAGES

    assert set(USER_MESSAGES) == set(Code)
    assert all(
        code.value.startswith("domain.shorts_asset_")
        for code in Code
        if "ASSET" in code.name
    )
    assert HTTP_STATUS[Code.ASSET_PROVENANCE_MISMATCH].value == 409


# The service: provenance snapshot


def test_the_current_provenance_is_snapshotted(stage: Stage) -> None:
    image = stage.add_asset()
    stage.add_provenance(image.id)
    current = stage.add_provenance(image.id)

    saved = stage.save(stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND)))

    assert saved.timeline.scenes[0].assets[0].provenance_id == current.id


def test_an_asset_without_provenance_snapshots_null(stage: Stage) -> None:
    image = stage.add_asset()

    saved = stage.save(stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND)))

    assert saved.timeline.scenes[0].assets[0].provenance_id is None


def test_music_provenance_is_snapshotted_too(stage: Stage) -> None:
    track = stage.add_asset(AssetKind.MUSIC)
    current = stage.add_provenance(track.id)

    saved = stage.save(stage.timeline(music_items=[AssetPlacement(track.id)]))

    assert saved.timeline.music[0].provenance_id == current.id


def test_a_matching_provenance_id_is_accepted(stage: Stage) -> None:
    image = stage.add_asset()
    current = stage.add_provenance(image.id)

    saved = stage.save(
        stage.timeline(
            SceneAsset(image.id, AssetRole.BACKGROUND, provenance_id=current.id)
        )
    )

    assert saved.timeline.scenes[0].assets[0].provenance_id == current.id


def test_a_different_provenance_id_is_refused_not_overwritten(stage: Stage) -> None:
    image = stage.add_asset()
    old = stage.add_provenance(image.id)
    stage.add_provenance(image.id)

    stage.refuse(
        stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND, provenance_id=old.id)),
        Code.ASSET_PROVENANCE_MISMATCH,
    )


def test_a_provenance_id_for_an_asset_without_provenance_is_refused(
    stage: Stage,
) -> None:
    image = stage.add_asset()

    stage.refuse(
        stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND, provenance_id="x")),
        Code.ASSET_PROVENANCE_MISMATCH,
    )


def test_saving_again_after_the_provenance_changed_is_refused(stage: Stage) -> None:
    image = stage.add_asset()
    stage.add_provenance(image.id)
    stage.project = stage.save(
        stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND))
    )
    stage.add_provenance(image.id)

    stage.refuse(stage.project.timeline, Code.ASSET_PROVENANCE_MISMATCH)


# The service: status


def test_ready_with_a_background_in_every_scene_when_captions_are_off(
    database: Database,
) -> None:
    stage = Stage(database, captions=False)
    scenes = stage.project.timeline.scenes
    images = [stage.add_asset() for _ in scenes]
    timeline = dataclasses.replace(
        stage.project.timeline,
        scenes=tuple(
            dataclasses.replace(
                scene, assets=(SceneAsset(image.id, AssetRole.BACKGROUND),)
            )
            for scene, image in zip(scenes, images, strict=True)
        ),
    )

    saved = stage.save(timeline)

    assert saved.status is ShortsProjectStatus.READY
    assert stage.world.project_of().status is ShortsProjectStatus.READY


def test_one_missing_background_keeps_the_project_planned(database: Database) -> None:
    stage = Stage(database, captions=False)
    image = stage.add_asset()

    saved = stage.save(stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND)))

    assert len(saved.timeline.scenes) > 1
    assert saved.status is ShortsProjectStatus.PLANNED


def test_ready_is_unreachable_while_captions_are_on(stage: Stage) -> None:
    scenes = stage.project.timeline.scenes
    images = [stage.add_asset() for _ in scenes]
    timeline = dataclasses.replace(
        stage.project.timeline,
        scenes=tuple(
            dataclasses.replace(
                scene, assets=(SceneAsset(image.id, AssetRole.BACKGROUND),)
            )
            for scene, image in zip(scenes, images, strict=True)
        ),
    )

    assert stage.save(timeline).status is ShortsProjectStatus.PLANNED


def test_planning_again_drops_the_assets(stage: Stage) -> None:
    image = stage.add_asset()
    stage.project = stage.save(
        stage.timeline(SceneAsset(image.id, AssetRole.BACKGROUND))
    )

    replanned = stage.world.service.plan(
        stage.project.id, expected_version=stage.project.version, actor=USER
    )

    assert all(not scene.assets for scene in replanned.timeline.scenes)
