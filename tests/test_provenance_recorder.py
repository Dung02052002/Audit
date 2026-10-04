"""G-077 Provenance Record: recording the source and licence of an asset.

Rules the user approved on 2026-10-04:

- an append-only provenance history per asset, the newest record is the
  current one and none is ever edited or deleted;
- recording changes neither the ``RightsRecord`` nor the ``Asset``;
- any actor may record and the actor is stored;
- the asset category sets what a record must hold (licensed: a licence name or
  reference, user_owned: an owner), the other categories add nothing;
- a statement equal to the current record writes and audits nothing;
- a service with an entity, a migration (0024) and a repository, registered in
  the bootstrap, and an audit with ids, flags and the category only.
"""

import json
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import pytest

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_registry import (
    AssetNotFoundError,
    AssetRegistry,
)
from ai_youtube_agent.content.provenance import Provenance
from ai_youtube_agent.content.provenance_recorder import (
    ProvenanceInputError,
    ProvenanceRecorder,
)
from ai_youtube_agent.content.rights import RightsRecord
from ai_youtube_agent.core.audit import (
    Actor,
    ActorKind,
    AuditLog,
    InMemoryAuditSink,
)
from ai_youtube_agent.core.config import Environment, Settings
from ai_youtube_agent.core.content_item import ContentItem
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.channel import (
    ChannelRepository,
    StrategyProfileRepository,
)
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.provenance import ProvenanceRepository
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from factories import (
    make_artifact,
    make_channel,
    make_content_item,
    make_strategy_profile,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
USER = Actor(ActorKind.USER, "owner")
AI = Actor(ActorKind.AI, "mock/mock-1")
SYSTEM = Actor(ActorKind.SYSTEM, "pipeline")
SECRET = "zebrafish"  # a word no audit event may hold
URL_SECRET = "SECRET"  # a word no error message or detail may hold
BAD_URLS_WITH_A_SECRET = [
    "ftp://user:SECRET@host/x",
    "http://user:SECRET@/x",
    "http:\\user:SECRET@host",
    "http://user:SECRET＠host/x",  # a fullwidth at sign: urlsplit raises
    "https://user:SECRET@stock.example/logo",
    "http://user:SECRET%40host.com/",  # the "port" is not a number
    "http://host:abc/SECRET",
    "http://host:99999/SECRET",
    "https://stock.example/a?token=SECRET",
    "https://stock.example/a?mode=1&X-Amz-Signature=SECRET",
    "https://stock.example/a#access_token=SECRET",
    "https://stock.example/a#/route?password=SECRET",
]
SHA = "cd" * 32
IMAGE = AssetKind.IMAGE
GENERATED, LICENSED, PUBLIC_DOMAIN, USER_OWNED, UNKNOWN = (
    AssetCategory.GENERATED,
    AssetCategory.LICENSED,
    AssetCategory.PUBLIC_DOMAIN,
    AssetCategory.USER_OWNED,
    AssetCategory.UNKNOWN,
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class World:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.versions = 0
        self.channel = make_channel()
        self.strategy = make_strategy_profile(self.channel)
        with database.transaction() as connection:
            ChannelRepository(connection).add(self.channel)
            StrategyProfileRepository(connection).add(self.strategy)
        self.item = self.new_item()
        self.sink = InMemoryAuditSink()
        audit = AuditLog(self.sink)
        self.registry = AssetRegistry(database, audit, clock=self.clock)
        self.recorder = ProvenanceRecorder(database, audit, clock=self.clock)
        self.titles = 0

    def new_item(self) -> ContentItem:
        item = make_content_item(self.channel, self.strategy)
        with self.database.transaction() as connection:
            ContentItemRepository(connection).add(item)
        return item

    def asset(self, category: AssetCategory = UNKNOWN, **overrides) -> Asset:
        self.titles += 1
        arguments = {
            "title": f"Asset {self.titles}",
            "source": "stock.example",
            "actor": USER,
        } | overrides
        if category is GENERATED:
            self.versions += 1
            artifact = make_artifact(self.item, version=self.versions)
            with self.database.transaction() as connection:
                ArtifactRepository(connection).add(artifact)
            arguments |= {"source": "mock-image", "artifact_id": artifact.id}
        return self.registry.register(
            self.channel.id, arguments.pop("kind", IMAGE), category, **arguments
        )

    def licensed(self, **overrides) -> Asset:
        return self.asset(LICENSED, license_ref="CC-BY-4.0", **overrides)

    def owned(self, **overrides) -> Asset:
        return self.asset(USER_OWNED, source="user", owner="Lan", **overrides)

    def record(self, asset: Asset, **overrides) -> Provenance:
        arguments = {"source_url": "https://stock.example/a", "actor": USER} | overrides
        return self.recorder.record(asset.id, **arguments)

    def count(self, table: str) -> int:
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rows(self, *tables: str) -> list[tuple]:
        with self.database.transaction() as connection:
            return [
                tuple(row)
                for table in tables
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]

    def actions(self) -> list[str]:
        return [event.action for event in self.sink.events()]

    def recorded_actions(self) -> list[str]:
        return [a for a in self.actions() if a == "asset.provenance_recorded"]


@pytest.fixture
def world(database: Database) -> World:
    return World(database)


# one record per category


def test_a_record_of_a_generated_asset_is_stored(world: World) -> None:
    asset = world.asset(GENERATED)

    made = world.record(asset, retrieved_at=T0, file_sha256=SHA.upper())

    assert made.asset_id == asset.id
    assert made.file_sha256 == SHA
    assert world.recorder.current(asset.id) == made
    assert world.recorder.history(asset.id) == [made]


def test_a_record_of_a_licensed_asset_with_a_licence_name(world: World) -> None:
    asset = world.licensed()

    made = world.record(
        asset,
        license_name="  CC  BY 4.0 ",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Photo by Lan",
    )

    assert made.license_name == "CC BY 4.0"
    assert world.recorder.current(asset.id) == made


def test_a_record_of_a_licensed_asset_with_a_licence_reference(world: World) -> None:
    asset = world.licensed()

    made = world.record(asset, source_url=None, license_ref="CC-BY-4.0")

    assert made.license_ref == "CC-BY-4.0"
    assert world.recorder.current(asset.id) == made


def test_a_record_of_a_user_owned_asset_with_an_owner(world: World) -> None:
    asset = world.owned()

    made = world.record(asset, source_url=None, owner="Lan Nguyen", proof="invoice 7")

    assert (made.owner, made.proof) == ("Lan Nguyen", "invoice 7")
    assert world.recorder.current(asset.id) == made


@pytest.mark.parametrize("category", [PUBLIC_DOMAIN, UNKNOWN, GENERATED])
def test_the_other_categories_add_no_rule(world: World, category) -> None:
    asset = world.asset(category)

    made = world.record(asset, source_url=None, proof="seen on the archive page")

    assert made.owner is None
    assert (made.license_name, made.license_ref) == (None, None)
    assert world.recorder.current(asset.id) == made


# category rules


@pytest.mark.parametrize(
    "details",
    [
        {"source_url": "https://stock.example/a"},
        {"owner": "Lan", "attribution": "Photo by Lan", "proof": "note"},
        {"license_url": "https://creativecommons.org/licenses/by/4.0/"},
    ],
)
def test_a_licensed_asset_needs_a_licence_name_or_reference_in_the_record(
    world: World, details: dict
) -> None:
    asset = (
        world.licensed()
    )  # the asset holds a license_ref, the record may not rely on it

    with pytest.raises(ProvenanceInputError, match="license_name") as caught:
        world.recorder.record(asset.id, actor=USER, **details)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert caught.value.code == "domain.provenance_input"
    assert world.count("asset_provenance") == 0
    assert world.recorded_actions() == []


@pytest.mark.parametrize(
    "details",
    [
        {"source_url": "https://stock.example/a"},
        {"license_name": "MIT", "license_ref": "x", "proof": "note"},
    ],
)
def test_a_user_owned_asset_needs_an_owner_in_the_record(
    world: World, details: dict
) -> None:
    asset = world.owned()  # the asset holds an owner, the record may not rely on it

    with pytest.raises(ProvenanceInputError, match="owner") as caught:
        world.recorder.record(asset.id, actor=USER, **details)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert world.count("asset_provenance") == 0
    assert world.recorded_actions() == []


def test_a_rule_failure_writes_nothing_and_leaves_the_history_alone(
    world: World,
) -> None:
    asset = world.licensed()
    first = world.record(asset, license_name="MIT")
    before = world.rows("asset_provenance", "assets", "rights_records")

    with pytest.raises(ProvenanceInputError):
        world.record(asset, source_url="https://stock.example/other")

    assert world.rows("asset_provenance", "assets", "rights_records") == before
    assert world.recorder.history(asset.id) == [first]
    assert len(world.recorded_actions()) == 1


# input errors and a missing asset


@pytest.mark.parametrize(
    "details",
    [
        {},
        {"owner": "   "},
        {"source_url": "ftp://stock.example/a"},
        {"source_url": "https://user:secret@stock.example/a"},
        {"source_url": "http://host:abc/"},
        {"license_url": "https://stock.example/a?api_key=1"},
        {"license_url": "https://stock.example/a b"},
        {"file_sha256": "not-a-hash"},
        {"proof": "x" * 1001},
        {"retrieved_at": datetime(2026, 10, 1)},
        {"owner": 5},
    ],
)
def test_a_bad_record_is_a_422_and_writes_nothing(world: World, details) -> None:
    asset = world.asset()

    with pytest.raises(ProvenanceInputError) as caught:
        world.recorder.record(asset.id, actor=USER, **details)

    assert caught.value.to_public().http_status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert caught.value.code == "domain.provenance_input"
    assert world.count("asset_provenance") == 0
    assert world.recorded_actions() == []


def test_a_retrieval_time_after_the_clock_is_a_422(world: World) -> None:
    asset = world.asset()
    future = world.clock.now + timedelta(days=1)

    with pytest.raises(ProvenanceInputError, match="retrieved_at"):
        world.record(asset, retrieved_at=future)

    assert world.count("asset_provenance") == 0


def test_the_recording_time_comes_from_the_injected_clock(world: World) -> None:
    asset = world.asset()

    first = world.record(asset)
    second = world.record(asset, source_url="https://stock.example/b")

    assert first.created_at == T0 + timedelta(seconds=2)  # the asset took the first
    assert second.created_at == first.created_at + timedelta(seconds=1)


@pytest.mark.parametrize("field", ["source_url", "license_url"])
@pytest.mark.parametrize("url", BAD_URLS_WITH_A_SECRET)
def test_a_refused_url_never_reaches_the_error_text_or_detail(
    world: World, field: str, url: str
) -> None:
    asset = world.asset()

    with pytest.raises(ProvenanceInputError, match=field) as caught:
        world.recorder.record(asset.id, actor=USER, **{field: url})

    error = caught.value
    assert URL_SECRET not in str(error)
    assert URL_SECRET not in error.detail
    assert URL_SECRET not in repr(error)
    assert URL_SECRET not in str(error.to_public())
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__
    assert world.count("asset_provenance") == 0
    assert world.recorded_actions() == []


def test_a_url_with_a_valid_port_is_recorded(world: World) -> None:
    asset = world.asset()

    made = world.record(asset, source_url="http://host:8080/x")

    assert made.source_url == "http://host:8080/x"


def test_an_nfc_and_an_nfd_url_are_different_statements(world: World) -> None:
    asset = world.asset()
    nfc = unicodedata.normalize("NFC", "https://stock.example/café")
    nfd = unicodedata.normalize("NFD", nfc)

    one = world.record(asset, source_url=nfc)
    two = world.record(asset, source_url=nfd)

    assert nfc != nfd
    assert two.id != one.id
    assert (one.source_url, two.source_url) == (nfc, nfd)
    assert world.count("asset_provenance") == 2
    assert world.record(asset, source_url=nfd) == two
    assert world.count("asset_provenance") == 2


class ProbeClock:
    """A clock that reports whether another writer could start when it is read."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.now = T0
        self.locked: list[bool] = []

    def __call__(self) -> datetime:
        other = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            self.locked.append(False)
            other.execute("ROLLBACK")
        except sqlite3.OperationalError:
            self.locked.append(True)
        finally:
            other.close()
        self.now += timedelta(seconds=1)
        return self.now


def test_the_recording_time_is_read_inside_the_transaction(
    database: Database,
) -> None:
    world = World(database)
    asset = world.asset()
    probe = ProbeClock(database.path)
    world.recorder = ProvenanceRecorder(database, AuditLog(world.sink), clock=probe)

    first = world.record(asset, proof="one")
    second = world.record(asset, proof="two")
    world.record(asset, proof="two")  # a repeat reads the clock too

    assert probe.locked == [True, True, True]
    assert first.created_at < second.created_at
    assert world.recorder.history(asset.id) == [first, second]


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 4, 12, 0),
        datetime(2026, 10, 4, 12, 0, tzinfo=timezone(timedelta(hours=7))),
    ],
)
def test_a_clock_that_is_not_utc_is_an_application_error_not_a_422(
    database: Database, moment: datetime
) -> None:
    world = World(database)
    asset = world.asset()
    recorder = ProvenanceRecorder(database, AuditLog(world.sink), clock=lambda: moment)
    events = len(world.sink.events())

    with pytest.raises(ValueError, match="clock") as caught:
        recorder.record(asset.id, source_url="https://a.example/x", actor=USER)

    assert not isinstance(caught.value, ProvenanceInputError)
    assert world.count("asset_provenance") == 0
    assert len(world.sink.events()) == events


def test_a_missing_asset_is_a_404(world: World) -> None:
    with pytest.raises(AssetNotFoundError) as caught:
        world.recorder.record("missing", source_url="https://a.example/x", actor=USER)

    assert caught.value.to_public().http_status == HTTPStatus.NOT_FOUND
    assert caught.value.code == "domain.asset_not_found"
    assert world.count("asset_provenance") == 0
    assert world.sink.events() == ()


def test_the_reads_of_a_missing_asset_are_a_404(world: World) -> None:
    with pytest.raises(AssetNotFoundError):
        world.recorder.current("missing")
    with pytest.raises(AssetNotFoundError):
        world.recorder.history("missing")


def test_an_input_error_wins_over_a_missing_asset(world: World) -> None:
    with pytest.raises(ProvenanceInputError):
        world.recorder.record("missing", actor=USER)


# actors


@pytest.mark.parametrize("actor", [USER, AI, SYSTEM])
def test_any_actor_may_record_and_is_stored(world: World, actor: Actor) -> None:
    asset = world.asset()

    made = world.record(asset, actor=actor)

    assert made.recorded_by == actor
    assert world.recorder.current(asset.id).recorded_by == actor
    assert [event.actor for event in world.sink.events()][-1] == actor


# idempotency


def test_an_identical_repeat_writes_and_audits_nothing(world: World) -> None:
    asset = world.licensed()
    details = {
        "source_url": "https://stock.example/a",
        "retrieved_at": T0,
        "license_name": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "license_ref": "CC-BY-4.0",
        "attribution": "Photo by Lan",
        "owner": "Lan",
        "file_sha256": SHA,
        "proof": "invoice 7",
    }
    first = world.recorder.record(asset.id, actor=USER, **details)
    rows = world.rows("asset_provenance")
    events = len(world.sink.events())

    again = world.recorder.record(asset.id, actor=AI, **details)

    assert again == first
    assert again.recorded_by == USER
    assert world.rows("asset_provenance") == rows
    assert len(world.sink.events()) == events


def test_a_repeat_with_other_spacing_and_an_uppercase_checksum_is_the_same(
    world: World,
) -> None:
    asset = world.asset()
    first = world.record(asset, owner="Lan  Nguyen", file_sha256=SHA)

    again = world.record(asset, owner=" Lan Nguyen ", file_sha256=SHA.upper())

    assert again == first
    assert world.count("asset_provenance") == 1


def test_a_repeat_in_nfd_of_an_nfc_record_is_the_same(world: World) -> None:
    asset = world.asset()
    nfc = unicodedata.normalize("NFC", "Nguyễn Văn Ánh")
    nfd = unicodedata.normalize("NFD", nfc)
    first = world.record(asset, source_url=None, owner=nfc)

    again = world.record(asset, source_url=None, owner=nfd)

    assert nfc != nfd
    assert again == first
    assert again.owner == nfc
    assert world.count("asset_provenance") == 1
    assert len(world.recorded_actions()) == 1


def test_a_repeat_in_nfc_of_an_nfd_record_is_the_same(world: World) -> None:
    asset = world.asset()
    nfc = unicodedata.normalize("NFC", "Nhạc nền Việt")
    nfd = unicodedata.normalize("NFD", nfc)
    first = world.record(asset, source_url=None, attribution=nfd)

    assert first.attribution == nfd  # stored as given, not normalised
    assert world.record(asset, source_url=None, attribution=nfc) == first
    assert world.count("asset_provenance") == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"owner": "Lan"}, {"owner": "lan"}),
        ({"owner": "Lan"}, {"owner": "Lan Nguyen"}),
        ({"license_name": "MIT"}, {"license_name": "mit"}),
        ({"source_url": "https://a.example/x"}, {"source_url": "https://A.example/x"}),
        ({"source_url": "https://a.example/x"}, {"source_url": "https://a.example/x/"}),
        ({"file_sha256": "a" * 64}, {"file_sha256": "b" * 64}),
        ({"retrieved_at": T0}, {"retrieved_at": T0 - timedelta(microseconds=1)}),
        ({"proof": "note"}, {"proof": "Note"}),
        ({"owner": "Lan"}, {"owner": "Lan", "proof": "note"}),
        ({"owner": "Lan", "proof": "note"}, {"owner": "Lan"}),
    ],
)
def test_a_different_statement_is_a_new_row(
    world: World, first: dict, second: dict
) -> None:
    asset = world.asset()
    one = world.record(asset, **({"source_url": None} | first))

    two = world.record(asset, **({"source_url": None} | second))

    assert two.id != one.id
    assert world.count("asset_provenance") == 2
    assert world.recorder.current(asset.id) == two
    assert world.recorder.history(asset.id) == [one, two]
    assert len(world.recorded_actions()) == 2


def test_returning_to_an_earlier_statement_is_a_new_row(world: World) -> None:
    asset = world.asset()
    a = world.record(asset, owner="Lan")
    b = world.record(asset, owner="Minh")

    third = world.record(asset, owner="Lan")

    assert third.id not in (a.id, b.id)
    assert third.content_key() == a.content_key()
    assert world.count("asset_provenance") == 3
    assert world.recorder.current(asset.id) == third
    assert world.recorder.history(asset.id) == [a, b, third]
    assert len(world.recorded_actions()) == 3
    # And an immediate repeat of the third one is a no-op again.
    assert world.record(asset, owner="Lan") == third
    assert world.count("asset_provenance") == 3


def test_a_repeat_compares_only_with_the_current_record(world: World) -> None:
    asset = world.asset()
    world.record(asset, owner="Lan")
    second = world.record(asset, owner="Minh")

    again = world.record(asset, owner="Minh")

    assert world.count("asset_provenance") == 2
    assert again.owner == "Minh"
    assert again.id == second.id


def test_the_histories_of_two_assets_are_separate(world: World) -> None:
    one, two = world.asset(), world.asset()

    a = world.record(one, owner="Lan")
    b = world.record(two, owner="Lan")

    assert a.id != b.id
    assert world.recorder.history(one.id) == [a]
    assert world.recorder.history(two.id) == [b]


# reads


def test_current_is_none_while_there_is_no_record(world: World) -> None:
    asset = world.asset()

    assert world.recorder.current(asset.id) is None
    assert world.recorder.history(asset.id) == []


def test_history_is_ordered_by_time_and_current_is_the_newest(world: World) -> None:
    asset = world.asset()
    made = [world.record(asset, proof=f"note {n}") for n in range(4)]

    assert world.recorder.history(asset.id) == made
    assert world.recorder.current(asset.id) == made[-1]
    stamps = [record.created_at for record in made]
    assert stamps == sorted(stamps)


def test_records_with_the_same_time_keep_their_insertion_order(
    database: Database,
) -> None:
    world = World(database)
    asset = world.asset()
    world.recorder = ProvenanceRecorder(
        database, AuditLog(world.sink), clock=lambda: T0
    )

    made = [world.record(asset, proof=f"note {n}") for n in range(3)]

    assert world.recorder.history(asset.id) == made
    assert world.recorder.current(asset.id) == made[-1]


def test_the_reads_write_nothing(world: World) -> None:
    asset = world.asset()
    world.record(asset)
    rows = world.rows("asset_provenance", "assets")
    events = len(world.sink.events())

    world.recorder.current(asset.id)
    world.recorder.history(asset.id)

    assert world.rows("asset_provenance", "assets") == rows
    assert len(world.sink.events()) == events


# append only


def test_the_history_only_grows(world: World) -> None:
    asset = world.asset()
    counts = []
    for details in (
        {"owner": "Lan"},
        {"owner": "Lan"},  # a repeat
        {"owner": "Minh"},
        {"owner": "Lan"},
    ):
        world.record(asset, **details)
        counts.append(world.count("asset_provenance"))

    assert counts == [1, 1, 2, 3]


def test_a_stored_record_is_never_changed_by_a_later_one(world: World) -> None:
    asset = world.asset()
    first = world.record(asset, owner="Lan")
    first_row = world.rows("asset_provenance")[0]

    world.record(asset, owner="Minh")
    world.record(asset, owner="Lan")

    assert world.rows("asset_provenance")[0] == first_row
    assert world.recorder.history(asset.id)[0] == first


def test_the_repository_has_no_way_to_change_or_delete_a_record() -> None:
    public = {name for name in dir(ProvenanceRepository) if not name.startswith("_")}

    assert public - {"table"} == {"add", "latest", "list_by_asset"}


# no side effect on the asset and the rights record


def test_recording_changes_neither_the_asset_nor_the_rights_record(
    world: World,
) -> None:
    asset = world.licensed()
    world.registry.attach(asset.id, world.item.id, actor=USER)
    with world.database.transaction() as connection:
        stored = RightsRecordRepository(connection).list_by_content_item(world.item.id)
    assert len(stored) == 1 and isinstance(stored[0], RightsRecord)
    before = world.rows("assets", "rights_records", "asset_usages")
    events = world.actions()

    world.record(asset, license_name="CC BY 4.0", source_url="https://s.example/x")
    world.record(asset, license_name="MIT")

    assert world.rows("assets", "rights_records", "asset_usages") == before
    assert world.registry.get(asset.id) == asset
    with world.database.transaction() as connection:
        after = RightsRecordRepository(connection).list_by_content_item(world.item.id)
    assert after == stored
    assert world.actions() == events + ["asset.provenance_recorded"] * 2


# persistence


def test_the_history_survives_a_new_database_and_recorder(
    world: World, database: Database
) -> None:
    asset = world.licensed()
    first = world.record(asset, license_name="MIT", retrieved_at=T0, file_sha256=SHA)
    second = world.record(asset, license_name="CC BY 4.0", actor=AI)

    reopened = ProvenanceRecorder(
        Database(database.path), AuditLog(InMemoryAuditSink())
    )

    assert reopened.history(asset.id) == [first, second]
    assert reopened.current(asset.id) == second
    assert (
        reopened.record(
            asset.id,
            source_url="https://stock.example/a",
            license_name="CC BY 4.0",
            actor=USER,
        )
        == second
    )
    assert reopened.history(asset.id)[0].retrieved_at == T0
    assert reopened.history(asset.id)[0].recorded_by == USER
    assert reopened.history(asset.id)[1].recorded_by == AI


# audit


def test_the_audit_holds_ids_the_category_and_flags_only(world: World) -> None:
    asset = world.asset(
        LICENSED,
        title=f"The {SECRET} title",
        source=f"{SECRET}.example",
        license_ref=f"{SECRET} licence",
    )
    made = world.record(
        asset,
        source_url=f"https://{SECRET}.example/path?id={SECRET}",
        retrieved_at=T0,
        license_name=f"{SECRET} licence",
        license_url=f"https://{SECRET}.example/licence",
        license_ref=f"{SECRET} ref",
        attribution=f"{SECRET} credit",
        owner=f"{SECRET} owner",
        file_sha256=SHA,
        proof=f"{SECRET} proof",
    )

    events = [e for e in world.sink.events() if e.action == "asset.provenance_recorded"]
    assert len(events) == 1
    event = events[0]
    assert event.entity.type == "asset"
    assert event.entity.id == asset.id
    assert event.actor == USER
    assert dict(event.metadata) == {
        "provenance_id": made.id,
        "channel_id": world.channel.id,
        "category": "licensed",
        "has_source_url": True,
        "has_license_url": True,
        "has_file_sha256": True,
        "has_proof": True,
    }
    dumped = json.dumps([e.as_dict() for e in world.sink.events()], ensure_ascii=False)
    assert SECRET not in dumped


def test_the_audit_flags_are_false_for_a_record_without_those_details(
    world: World,
) -> None:
    asset = world.owned()

    world.record(asset, source_url=None, owner="Lan")

    event = world.sink.events()[-1]
    assert event.action == "asset.provenance_recorded"
    assert event.metadata["has_source_url"] is False
    assert event.metadata["has_license_url"] is False
    assert event.metadata["has_file_sha256"] is False
    assert event.metadata["has_proof"] is False
    assert event.metadata["category"] == "user_owned"


def test_a_failing_insert_audits_nothing_and_stores_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = world.asset()
    events = len(world.sink.events())

    def boom(repository, provenance):
        raise sqlite3.OperationalError("store down")

    monkeypatch.setattr(ProvenanceRepository, "add", boom)
    with pytest.raises(sqlite3.OperationalError, match="store down"):
        world.record(asset)
    monkeypatch.undo()

    assert world.count("asset_provenance") == 0
    assert len(world.sink.events()) == events
    # The record works once the store is back.
    assert world.record(asset).asset_id == asset.id
    assert world.count("asset_provenance") == 1


# bootstrap


def test_bootstrap_registers_the_provenance_recorder(tmp_path: Path) -> None:
    container = build_container(
        Settings(environment=Environment.TEST, database_path=tmp_path / "a.db")
    )

    assert isinstance(container.resolve(ProvenanceRecorder), ProvenanceRecorder)
