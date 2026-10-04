from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_youtube_agent.content.asset import (
    MAX_ATTRIBUTION,
    MAX_LICENSE_REF,
    MAX_OWNER,
    MAX_SOURCE,
    MAX_TITLE,
    Asset,
    AssetCategory,
    AssetKind,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def make(category=AssetCategory.UNKNOWN, **overrides) -> Asset:
    values = {
        "title": "Sunrise",
        "source": "stock-site",
        "clock": clock,
    }
    values.update(overrides)
    return Asset.create("chan1", AssetKind.IMAGE, category, **values)


def test_generated_happy_path() -> None:
    asset = make(AssetCategory.GENERATED, source="image-model", artifact_id="art1")
    assert asset.category is AssetCategory.GENERATED
    assert asset.artifact_id == "art1"
    assert asset.created_at == NOW
    assert len(asset.id) == 32


def test_licensed_happy_path() -> None:
    asset = make(AssetCategory.LICENSED, license_ref="CC-BY-4.0", attribution="By Jane")
    assert asset.license_ref == "CC-BY-4.0"
    assert asset.attribution == "By Jane"


def test_public_domain_happy_path() -> None:
    asset = make(AssetCategory.PUBLIC_DOMAIN, attribution="Museum")
    assert asset.category is AssetCategory.PUBLIC_DOMAIN
    assert make(AssetCategory.PUBLIC_DOMAIN).attribution is None


def test_user_owned_happy_path() -> None:
    asset = make(AssetCategory.USER_OWNED, source="user", owner="Dung")
    assert asset.owner == "Dung"


def test_unknown_needs_only_base_fields() -> None:
    asset = make()
    assert asset.category is AssetCategory.UNKNOWN
    assert asset.license_ref is None
    assert asset.owner is None
    assert asset.artifact_id is None


def test_licensed_requires_license_ref() -> None:
    with pytest.raises(ValueError, match="license_ref"):
        make(AssetCategory.LICENSED)


def test_user_owned_requires_owner() -> None:
    with pytest.raises(ValueError, match="owner"):
        make(AssetCategory.USER_OWNED, source="user")


@pytest.mark.parametrize("source", ["user", "USER", " user "])
def test_generated_source_must_not_be_user(source: str) -> None:
    with pytest.raises(ValueError, match="provider"):
        make(AssetCategory.GENERATED, source=source)


def test_a_directly_built_generated_asset_refuses_a_padded_user_source() -> None:
    with pytest.raises(ValueError, match="provider"):
        Asset(
            id="a1",
            channel_id="chan1",
            kind=AssetKind.IMAGE,
            category=AssetCategory.GENERATED,
            title="Sunrise",
            source=" user ",
            artifact_id=None,
            license_ref=None,
            attribution=None,
            owner=None,
            created_at=NOW,
        )


@pytest.mark.parametrize(
    ("category", "extra"),
    [
        (AssetCategory.LICENSED, {"license_ref": "CC0"}),
        (AssetCategory.PUBLIC_DOMAIN, {}),
        (AssetCategory.USER_OWNED, {"owner": "Dung"}),
        (AssetCategory.UNKNOWN, {}),
    ],
)
def test_artifact_id_only_for_generated(category, extra) -> None:
    with pytest.raises(ValueError, match="artifact_id"):
        make(category, artifact_id="art1", **extra)


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_text_fields_fail(value: str) -> None:
    with pytest.raises(ValueError, match="title"):
        make(title=value)
    with pytest.raises(ValueError, match="source"):
        make(source=value)
    with pytest.raises(ValueError, match="license_ref"):
        make(AssetCategory.LICENSED, license_ref=value)
    with pytest.raises(ValueError, match="attribution"):
        make(attribution=value)
    with pytest.raises(ValueError, match="owner"):
        make(AssetCategory.USER_OWNED, owner=value)
    with pytest.raises(ValueError, match="artifact_id"):
        make(AssetCategory.GENERATED, source="model", artifact_id=value)


def test_empty_ids_fail() -> None:
    with pytest.raises(ValueError, match="channel_id"):
        Asset.create(
            "  ", AssetKind.IMAGE, AssetCategory.UNKNOWN, title="a", source="b"
        )


def test_title_limit() -> None:
    assert make(title="a" * MAX_TITLE).title == "a" * MAX_TITLE
    with pytest.raises(ValueError, match="title"):
        make(title="a" * (MAX_TITLE + 1))


def test_source_limit() -> None:
    assert make(source="a" * MAX_SOURCE).source == "a" * MAX_SOURCE
    with pytest.raises(ValueError, match="source"):
        make(source="a" * (MAX_SOURCE + 1))


def test_license_ref_limit() -> None:
    kwargs = {"license_ref": "a" * MAX_LICENSE_REF}
    assert make(AssetCategory.LICENSED, **kwargs).license_ref == "a" * MAX_LICENSE_REF
    with pytest.raises(ValueError, match="license_ref"):
        make(AssetCategory.LICENSED, license_ref="a" * (MAX_LICENSE_REF + 1))


def test_attribution_limit() -> None:
    assert make(attribution="a" * MAX_ATTRIBUTION).attribution == "a" * MAX_ATTRIBUTION
    with pytest.raises(ValueError, match="attribution"):
        make(attribution="a" * (MAX_ATTRIBUTION + 1))


def test_owner_limit() -> None:
    asset = make(AssetCategory.USER_OWNED, owner="a" * MAX_OWNER)
    assert asset.owner == "a" * MAX_OWNER
    with pytest.raises(ValueError, match="owner"):
        make(AssetCategory.USER_OWNED, owner="a" * (MAX_OWNER + 1))


def test_title_whitespace_is_collapsed() -> None:
    assert make(title="  a \t b\n\nc  ").title == "a b c"


def test_title_limit_applies_after_collapsing() -> None:
    title = "a" + " " * 50 + "b" * (MAX_TITLE - 2)
    assert len(make(title=title).title) == MAX_TITLE


def test_direct_construction_requires_collapsed_title() -> None:
    asset = make()
    with pytest.raises(ValueError, match="title"):
        Asset(**{**asset.__dict__, "title": "a  b"})


def test_vietnamese_title_is_kept_without_normalisation() -> None:
    nfc = "Bình minh trên biển Đà Nẵng"
    nfd = "Bình minh"
    assert make(title=nfc).title == nfc
    assert make(title=nfd).title == nfd
    assert make(title="  Bình   minh  ").title == "Bình minh"


def test_invalid_enum_values_are_refused() -> None:
    with pytest.raises(TypeError, match="kind"):
        Asset.create("chan1", "image", AssetCategory.UNKNOWN, title="a", source="b")
    with pytest.raises(TypeError, match="category"):
        Asset.create("chan1", AssetKind.IMAGE, "unknown", title="a", source="b")
    with pytest.raises(ValueError):
        AssetCategory("stolen")
    with pytest.raises(ValueError):
        AssetKind("gif")


def test_naive_and_non_utc_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="created_at"):
        make(clock=lambda: datetime(2026, 10, 4, 12, 0))
    zone = timezone(timedelta(hours=7))
    with pytest.raises(ValueError, match="created_at"):
        make(clock=lambda: datetime(2026, 10, 4, 12, 0, tzinfo=zone))


def test_asset_is_frozen() -> None:
    asset = make()
    with pytest.raises(AttributeError):
        asset.title = "other"  # type: ignore[misc]


def test_as_dict_round_trip_values() -> None:
    asset = make(
        AssetCategory.LICENSED,
        title="Song",
        source="https://example.com/s",
        license_ref="CC-BY-4.0",
        attribution="By Jane",
    )
    assert asset.as_dict() == {
        "id": asset.id,
        "channel_id": "chan1",
        "kind": "image",
        "category": "licensed",
        "title": "Song",
        "source": "https://example.com/s",
        "artifact_id": None,
        "license_ref": "CC-BY-4.0",
        "attribution": "By Jane",
        "owner": None,
        "created_at": "2026-10-04T12:00:00+00:00",
    }


def test_enum_values_are_pinned() -> None:
    assert [item.value for item in AssetCategory] == [
        "generated",
        "licensed",
        "public_domain",
        "user_owned",
        "unknown",
    ]
    assert [item.value for item in AssetKind] == [
        "image",
        "video_clip",
        "audio",
        "music",
        "voice",
        "font",
        "subtitle",
        "template",
        "other",
    ]


def test_ids_are_unique() -> None:
    assert make().id != make().id
