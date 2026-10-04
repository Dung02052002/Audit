import hashlib
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_youtube_agent.bootstrap import build_container
from ai_youtube_agent.content import (
    asset,
    asset_usage,
    provenance,
    script_revision,
    script_validation,
)
from ai_youtube_agent.content.analytics import MetricScope
from ai_youtube_agent.content.approval import ApprovalStatus
from ai_youtube_agent.content.asset import AssetCategory, AssetKind
from ai_youtube_agent.content.channel import ChannelStatus
from ai_youtube_agent.content.comment import CommentLabel, ReplyStatus
from ai_youtube_agent.content.cost import CostCategory
from ai_youtube_agent.content.experiment import ExperimentStatus, ExperimentType
from ai_youtube_agent.content.fact_check import FactCheckStatus
from ai_youtube_agent.content.originality import (
    MAX_FINDINGS,
    MAX_METHOD,
    MAX_PRIORS,
    MAX_WORDS,
    OriginalityStatus,
)
from ai_youtube_agent.content.qc import QCStatus
from ai_youtube_agent.content.research_report import Uncertainty
from ai_youtube_agent.content.research_request import ResearchStatus
from ai_youtube_agent.content.revenue import RevenueStage
from ai_youtube_agent.content.rights import RiskLevel, RiskResolution
from ai_youtube_agent.content.script import (
    MAX_SECTIONS,
    ClaimKind,
    NumberAgreement,
    SectionKind,
)
from ai_youtube_agent.content.script_validation import ValidationStatus
from ai_youtube_agent.content.source import DuplicateReason
from ai_youtube_agent.content.topic import EvidenceField
from ai_youtube_agent.core.artifact import ArtifactKind
from ai_youtube_agent.core.audit import ActorKind, AuditResult
from ai_youtube_agent.core.config import DEFAULT_DATABASE_PATH, Settings
from ai_youtube_agent.core.content_item import ContentStatus, ContentType
from ai_youtube_agent.core.db.codec import format_datetime, format_decimal
from ai_youtube_agent.core.db.migrate import (
    Migration,
    MigrationError,
    connect,
    current_version,
    default_migrations,
    load_migrations,
    migrate,
    restore_backup,
)
from ai_youtube_agent.main import create_app
from ai_youtube_agent.pipeline.job import AIJobStatus, SessionStatus
from ai_youtube_agent.pipeline.publish import PublishStatus
from ai_youtube_agent.providers.research_cache import CacheKind

T0 = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 30, 9, 30, 15, 250, tzinfo=UTC)
TS = format_datetime(T0)
SHA = "a" * 64
# 0001 initial schema, 0002 production starts (C-036), 0003 partial strategy
# (D-044), 0004 format settings (D-049), 0005 cadence schedule (D-050),
# 0006 budget alerts (D-051), 0007 sources (E-055), 0008 research requests
# (E-056), 0009 source duplicates (E-057), 0010 research topics (E-058),
# 0011 topic scores (E-059), 0012 research reports (E-060), 0013 research
# cache (E-061), 0014 research recovery (E-062), 0015 script sections (F-064),
# 0016 hook generations (F-065), 0017 claim extractions (F-068),
# 0018 evidence matches (F-069), 0019 fact checks (F-070),
# 0020 originality checks (F-071), 0021 script validations (F-072),
# 0022 script revisions (F-073), 0023 asset registry (G-076),
# 0024 asset provenance (G-077)
LATEST = 24

ENTITY_TABLES = {
    "channels",
    "production_starts",
    "strategy_profiles",
    "voice_profiles",
    "content_items",
    "artifacts",
    "scripts",
    "claims",
    "evidence",
    "audio_metadata",
    "rights_records",
    "qc_results",
    "qc_result_artifacts",
    "qc_checks",
    "approval_requests",
    "approval_artifacts",
    "publish_jobs",
    "metric_snapshots",
    "metric_values",
    "revenue_records",
    "cost_records",
    "comments",
    "comment_classifications",
    "reply_drafts",
    "sessions",
    "ai_jobs",
    "job_checkpoints",
    "experiments",
    "experiment_variants",
    "audit_events",
    "sources",  # E-055
    "research_requests",  # E-056
    "research_request_sources",  # E-056
    "source_deduplications",  # E-057
    "source_duplicates",  # E-057
    "topic_extractions",  # E-058
    "research_topics",  # E-058
    "topic_evidence",  # E-058
    "topic_scorings",  # E-059
    "topic_scores",  # E-059
    "research_reports",  # E-060
    "research_cache",  # E-061
    "hook_generations",  # F-065
    "claim_extractions",  # F-068
    "evidence_matches",  # F-069
    "fact_checks",  # F-070
    "fact_check_results",  # F-070
    "originality_checks",  # F-071
    "originality_findings",  # F-071
    "script_validations",  # F-072
    "script_validation_findings",  # F-072
    "script_revisions",  # F-073
    "script_revision_sections",  # F-073
    "assets",  # G-076
    "asset_usages",  # G-076
    "asset_provenance",  # G-077
}

ENUM_COLUMNS = {
    ("channels", "status"): ChannelStatus,
    ("content_items", "content_type"): ContentType,
    ("content_items", "status"): ContentStatus,
    ("production_starts", "content_type"): ContentType,
    ("artifacts", "kind"): ArtifactKind,
    ("rights_records", "risk_level"): RiskLevel,
    ("rights_records", "resolution"): RiskResolution,
    ("qc_checks", "status"): QCStatus,
    ("approval_requests", "status"): ApprovalStatus,
    ("approval_requests", "requested_by_kind"): ActorKind,
    ("approval_artifacts", "kind"): ArtifactKind,
    ("publish_jobs", "content_type"): ContentType,
    ("publish_jobs", "status"): PublishStatus,
    ("metric_snapshots", "scope"): MetricScope,
    ("revenue_records", "stage"): RevenueStage,
    ("revenue_records", "scope"): MetricScope,
    ("cost_records", "category"): CostCategory,
    ("comment_classifications", "label"): CommentLabel,
    ("comment_classifications", "classified_by_kind"): ActorKind,
    ("reply_drafts", "status"): ReplyStatus,
    ("reply_drafts", "created_by_kind"): ActorKind,
    ("sessions", "status"): SessionStatus,
    ("research_requests", "status"): ResearchStatus,  # E-056
    ("research_requests", "requested_by_kind"): ActorKind,  # E-056
    ("source_duplicates", "reason"): DuplicateReason,  # E-057
    ("topic_evidence", "field"): EvidenceField,  # E-058
    ("research_reports", "uncertainty"): Uncertainty,  # E-060
    ("research_cache", "kind"): CacheKind,  # E-061
    ("hook_generations", "content_type"): ContentType,  # F-065
    ("hook_generations", "requested_by_kind"): ActorKind,  # F-065
    ("claim_extractions", "requested_by_kind"): ActorKind,  # F-068
    ("evidence_matches", "requested_by_kind"): ActorKind,  # F-069
    ("fact_checks", "requested_by_kind"): ActorKind,  # F-070
    ("fact_check_results", "status"): FactCheckStatus,  # F-070
    ("originality_checks", "requested_by_kind"): ActorKind,  # F-071
    ("originality_findings", "status"): OriginalityStatus,  # F-071
    ("script_validations", "requested_by_kind"): ActorKind,  # F-072
    ("script_validation_findings", "status"): ValidationStatus,  # F-072
    ("script_revisions", "requested_by_kind"): ActorKind,  # F-073
    ("script_revision_sections", "kind"): SectionKind,  # F-073
    ("assets", "kind"): AssetKind,  # G-076
    ("assets", "category"): AssetCategory,  # G-076
    ("asset_usages", "attached_by_kind"): ActorKind,  # G-076
    ("asset_provenance", "recorded_by_kind"): ActorKind,  # G-077
    ("ai_jobs", "status"): AIJobStatus,
    ("experiments", "type"): ExperimentType,
    ("experiments", "status"): ExperimentStatus,
    ("experiments", "proposed_by_kind"): ActorKind,
    ("audit_events", "actor_kind"): ActorKind,
    ("audit_events", "result"): AuditResult,
}


def at(moment: datetime):
    return lambda: moment


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    migrate(path, clock=at(T0))
    return path


@pytest.fixture
def conn(db: Path):
    connection = connect(db)
    yield connection
    connection.close()


def write_migrations(folder: Path, **sql: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in sql.items():
        (folder / f"{name}.sql").write_text(text, encoding="utf-8")
    return folder


def tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    )
    return {name for (name,) in rows}


def seed_item(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO channels VALUES ('ch1', 'Channel', 'UCxxxxxxxxxxxxxxxxxxxxxx', "
        "NULL, 'active', ?, ?)",
        (TS, TS),
    )
    connection.execute(
        "INSERT INTO strategy_profiles VALUES ('sp1', 'ch1', 'VN', 'vi', '[]', "
        "'{}', '{}', '{}', 2, 0, 'USD', '5.00', '100.00', '{}', 1, 'user', 'u1', "
        "?, ?, NULL, NULL, NULL)",
        (TS, TS),
    )
    connection.execute(
        "INSERT INTO content_items VALUES ('ci1', 'ch1', 'sp1', 1, 'shorts', "
        "'Title', 'draft', ?, ?)",
        (TS, TS),
    )


def insert_cost(connection: sqlite3.Connection, amount, row_id: str = "c1") -> None:
    connection.execute(
        "INSERT INTO cost_records VALUES (?, 'ch1', 'tts', 'mock_tts', ?, 'USD', "
        "?, NULL, NULL)",
        (row_id, amount, TS),
    )


# Packaged migrations


def test_default_migrations_are_packaged() -> None:
    migrations = default_migrations()
    path = files("ai_youtube_agent.core.db") / "migrations" / "0001_initial_schema.sql"

    assert [m.version for m in migrations] == list(range(1, LATEST + 1))
    assert [m.name for m in migrations] == [
        "initial_schema",
        "production_starts",
        "partial_strategy",
        "format_settings",
        "cadence_schedule",
        "budget_alerts",
        "sources",
        "research_requests",
        "source_duplicates",
        "research_topics",
        "topic_scores",
        "research_reports",
        "research_cache",
        "research_recovery",
        "script_sections",
        "hook_generations",
        "claim_extractions",
        "evidence_matches",
        "fact_checks",
        "originality_checks",
        "script_validations",
        "script_revisions",
        "asset_registry",
        "asset_provenance",
    ]
    lf_text = path.read_bytes().replace(b"\r\n", b"\n")
    assert migrations[0].checksum == hashlib.sha256(lf_text).hexdigest()


def test_checksum_ignores_line_endings() -> None:
    lf = "CREATE TABLE a (x TEXT);\nCREATE TABLE b (x TEXT);\n"
    crlf = lf.replace("\n", "\r\n")
    assert (
        Migration.from_text("0001_a.sql", lf).checksum
        == Migration.from_text("0001_a.sql", crlf).checksum
    )
    assert (
        Migration.from_text("0001_a.sql", lf).checksum
        != Migration.from_text("0001_a.sql", lf.replace("b", "c")).checksum
    )


def test_a_crlf_checkout_of_an_applied_migration_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "0001_a.sql").write_bytes(b"CREATE TABLE a (x TEXT);\n")
    migrate(path, migrations=load_migrations(folder))
    (folder / "0001_a.sql").write_bytes(b"CREATE TABLE a (x TEXT);\r\n")

    assert migrate(path, migrations=load_migrations(folder)).applied == ()


def test_default_database_path_setting(monkeypatch) -> None:
    monkeypatch.delenv("AI_YOUTUBE_AGENT_DATABASE_PATH", raising=False)
    assert (
        Settings().database_path
        == DEFAULT_DATABASE_PATH
        == Path("data/ai_youtube_agent.db")
    )
    monkeypatch.setenv("AI_YOUTUBE_AGENT_DATABASE_PATH", "other/app.db")
    assert Settings().database_path == Path("other/app.db")


# Fresh database


def test_fresh_database_gets_every_table(tmp_path: Path) -> None:
    path = tmp_path / "new" / "folder" / "app.db"

    report = migrate(path, clock=at(T0))

    assert report.applied == tuple(range(1, LATEST + 1))
    assert report.current_version == LATEST
    assert report.backup_path is None
    assert current_version(path) == LATEST
    connection = connect(path)
    try:
        assert tables(connection) == ENTITY_TABLES | {"schema_migrations"}
    finally:
        connection.close()
    assert not (path.parent / "backups").exists()


def test_applied_migration_is_recorded(conn) -> None:
    rows = conn.execute("SELECT * FROM schema_migrations").fetchall()
    assert rows == [(m.version, m.name, m.checksum, TS) for m in default_migrations()]


def test_every_table_is_strict(conn) -> None:
    rows = conn.execute("PRAGMA table_list").fetchall()
    strict = {row[1]: row[5] for row in rows if not row[1].startswith("sqlite_")}
    assert strict.keys() == ENTITY_TABLES | {"schema_migrations"}
    assert set(strict.values()) == {1}


def test_foreign_keys_are_on(conn) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_second_run_changes_nothing(db: Path) -> None:
    report = migrate(db, clock=at(T1))
    assert report.applied == ()
    assert report.current_version == LATEST
    assert report.backup_path is None
    assert not (db.parent / "backups").exists()


def test_current_version_does_not_create_anything(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    assert current_version(path) == 0
    assert not path.exists()


# Schema rules


def test_enum_checks_match_python_enums(conn) -> None:
    found = {}
    pattern = re.compile(r"^\s+(\w+) TEXT NOT NULL CHECK \(\1 IN \(([^)]*)\)\)", re.M)
    for table, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
    ):
        for column, values in pattern.findall(sql):
            found[(table, column)] = [v.strip().strip("'") for v in values.split(",")]

    assert found.keys() == ENUM_COLUMNS.keys()
    for key, enum in ENUM_COLUMNS.items():
        assert found[key] == [member.value for member in enum], key


def test_nullable_claim_kind_check_matches_the_python_enum(conn) -> None:
    # Added by ALTER TABLE (F-068), so the NOT NULL pattern above misses it.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
    ).fetchone()[0]
    values = re.search(r"kind TEXT CHECK \(kind IS NULL OR kind IN \(([^)]*)\)\)", sql)

    assert values is not None
    assert [v.strip().strip("'") for v in values.group(1).split(",")] == [
        member.value for member in ClaimKind
    ]


def test_nullable_evidence_numbers_check_matches_the_python_enum(conn) -> None:
    # Added by ALTER TABLE (F-069), so the NOT NULL pattern above misses it.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'evidence'"
    ).fetchone()[0]
    values = re.search(
        r"numbers TEXT CHECK \(numbers IS NULL OR numbers IN \(([^)]*)\)\)", sql
    )

    assert values is not None
    assert [v.strip().strip("'") for v in values.group(1).split(",")] == [
        member.value for member in NumberAgreement
    ]


@pytest.mark.parametrize(
    ("column", "enum"),
    [("numbers", NumberAgreement), ("uncertainty", Uncertainty)],
)
def test_nullable_fact_check_result_checks_match_the_python_enums(
    conn, column, enum
) -> None:
    # Nullable columns, so the NOT NULL pattern above misses them (F-070).
    sql = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'fact_check_results'"
    ).fetchone()[0]
    values = re.search(
        rf"{column} TEXT CHECK \({column} IS NULL OR {column} IN \(([^)]*)\)\)", sql
    )

    assert values is not None
    assert [v.strip().strip("'") for v in values.group(1).split(",")] == [
        member.value for member in enum
    ]


def test_originality_limits_match_the_python_constants(conn) -> None:
    # The SQL bounds repeat the Python constants; this catches a drift (F-071).
    sql = {
        name: conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()[0]
        for name in ("originality_checks", "originality_findings")
    }
    checks = sql["originality_checks"]

    def upper(column: str) -> int:
        found = re.search(rf"{column} BETWEEN 0 AND (\d+)\)", checks)
        assert found is not None, column
        return int(found.group(1))

    assert upper("words") == MAX_WORDS
    assert upper("priors_count") == MAX_PRIORS
    assert upper("findings_count") == MAX_FINDINGS
    method = re.search(r"length\(trim\(method\)\) BETWEEN 1 AND (\d+)", checks)
    assert method is not None
    assert int(method.group(1)) == MAX_METHOD
    code = re.search(
        r"length\(trim\(code\)\) BETWEEN 1 AND (\d+)", sql["originality_findings"]
    )
    assert code is not None
    assert int(code.group(1)) == 100


def test_script_validation_limits_match_the_python_constants(conn) -> None:
    # The SQL bounds repeat the Python constants; this catches a drift (F-072).
    sql = {
        name: conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()[0]
        for name in ("script_validations", "script_validation_findings")
    }
    runs = sql["script_validations"]

    def upper(column: str) -> int:
        found = re.search(rf"{column} BETWEEN 0 AND (\d+)\)", runs)
        assert found is not None, column
        return int(found.group(1))

    assert upper("language_hits") == script_validation.MAX_WORDS
    assert upper("findings_count") == script_validation.MAX_FINDINGS
    method = re.search(r"length\(trim\(method\)\) BETWEEN 1 AND (\d+)", runs)
    assert method is not None
    assert int(method.group(1)) == script_validation.MAX_METHOD
    code = re.search(
        r"length\(trim\(code\)\) BETWEEN 1 AND (\d+)",
        sql["script_validation_findings"],
    )
    assert code is not None
    assert int(code.group(1)) == 100


def test_script_revision_limits_match_the_python_constants(conn) -> None:
    # The SQL bounds repeat the Python constants; this catches a drift (F-073).
    sql = {
        name: conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()[0]
        for name in ("script_revisions", "script_revision_sections")
    }
    revisions, sections = sql["script_revisions"], sql["script_revision_sections"]

    entries = re.search(r"entries_count BETWEEN 0 AND (\d+)\)", revisions)
    assert entries is not None
    assert int(entries.group(1)) == script_revision.MAX_ENTRIES
    version = re.search(r"version BETWEEN 2 AND (\d+)\)", revisions)
    assert version is not None
    assert int(version.group(1)) == script_revision.MAX_VERSION
    method = re.search(r"length\(trim\(method\)\) BETWEEN 1 AND (\d+)", revisions)
    assert method is not None
    assert int(method.group(1)) == script_revision.MAX_METHOD
    change = re.search(r"length\(trim\(change\)\) BETWEEN 1 AND (\d+)", sections)
    assert change is not None
    assert int(change.group(1)) == 100
    for column in ("old_index", "new_index"):
        index = re.search(rf"{column} BETWEEN 0 AND (\d+)\)", sections)
        assert index is not None, column
        assert int(index.group(1)) == MAX_SECTIONS - 1
    # A change is open text in SQL, closed in Python: every Python value fits.
    for kind in script_revision.ChangeKind:
        assert 1 <= len(kind.value) <= 100


def test_asset_limits_match_the_python_constants(conn) -> None:
    # The SQL bounds repeat the Python constants; this catches a drift (G-076).
    sql = {
        name: conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()[0]
        for name in ("assets", "asset_usages")
    }
    expected = {
        ("assets", "title"): asset.MAX_TITLE,
        ("assets", "source"): asset.MAX_SOURCE,
        ("assets", "license_ref"): asset.MAX_LICENSE_REF,
        ("assets", "attribution"): asset.MAX_ATTRIBUTION,
        ("assets", "owner"): asset.MAX_OWNER,
        ("asset_usages", "purpose"): asset_usage.MAX_PURPOSE,
    }
    for (table, column), limit in expected.items():
        found = re.search(
            rf"length\(trim\({column}\)\) BETWEEN 1 AND (\d+)", sql[table]
        )
        assert found is not None, (table, column)
        assert int(found.group(1)) == limit, (table, column)
    assert asset.USER_SOURCE == "user"
    assert "lower(trim(source)) <> 'user'" in sql["assets"]


def asset_row(**overrides) -> dict:
    row = {
        "id": "as1",
        "channel_id": "ch1",
        "kind": "image",
        "category": "unknown",
        "title": "Logo",
        "source": "stock.example",
        "source_key": "stock.example",
        "title_key": "logo",
        "artifact_id": None,
        "license_ref": None,
        "attribution": None,
        "owner": None,
        "created_at": TS,
    }
    return row | overrides


def insert_asset(connection: sqlite3.Connection, **overrides) -> None:
    row = asset_row(**overrides)
    connection.execute(
        f"INSERT INTO assets ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def seed_artifact(connection: sqlite3.Connection) -> None:
    seed_item(connection)
    connection.execute(
        "INSERT INTO artifacts VALUES ('a1', 'ci1', 'video', 1, 'u1', ?, 1, "
        "'video/mp4', ?)",
        (SHA, TS),
    )


def test_a_valid_asset_of_every_category_is_accepted(conn) -> None:
    seed_artifact(conn)
    insert_asset(conn, id="a", title_key="a")
    insert_asset(
        conn, id="b", title_key="b", category="licensed", license_ref="CC-BY-4.0"
    )
    insert_asset(conn, id="c", title_key="c", category="public_domain")
    insert_asset(conn, id="d", title_key="d", category="user_owned", owner="Lan")
    insert_asset(conn, id="e", title_key="e", category="generated", artifact_id="a1")

    assert conn.execute("SELECT count(*) FROM assets").fetchone() == (5,)


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "licensed"},
        {"category": "licensed", "license_ref": "   "},
        {"category": "user_owned"},
        {"category": "user_owned", "owner": ""},
        {"category": "unknown", "artifact_id": "a1"},
        {"category": "licensed", "license_ref": "CC", "artifact_id": "a1"},
        {"category": "generated", "source": "user"},
        {"category": "generated", "source": " User "},
        {"category": "bogus"},
        {"kind": "bogus"},
        {"title": "   "},
        {"title": "x" * 201},
        {"source": ""},
        {"source": "x" * 501},
        {"source_key": ""},
        {"title_key": ""},
        {"license_ref": "x" * 501},
        {"attribution": "x" * 501},
        {"attribution": " "},
        {"owner": "x" * 201},
        {"created_at": "2026-09-30T08:00:00Z"},
        {"channel_id": "nope"},
        {"category": "generated", "artifact_id": "nope"},
    ],
)
def test_asset_rules_are_enforced_in_sql(conn, overrides) -> None:
    seed_artifact(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_asset(conn, **overrides)


def test_asset_boundaries_are_accepted_in_sql(conn) -> None:
    seed_artifact(conn)
    insert_asset(
        conn,
        title="t" * 200,
        source="s" * 500,
        category="licensed",
        license_ref="l" * 500,
        attribution="a" * 500,
        owner="o" * 200,
    )

    assert conn.execute("SELECT count(*) FROM assets").fetchone() == (1,)


def test_an_asset_is_unique_per_channel_source_and_title_key(conn) -> None:
    seed_item(conn)
    insert_asset(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_asset(conn, id="as2")
    insert_asset(conn, id="as3", title_key="other")
    insert_asset(conn, id="as4", source_key="other.example")

    conn.execute(
        "INSERT INTO channels VALUES ('ch2', 'Other', 'UCyyyyyyyyyyyyyyyyyyyyyy', "
        "NULL, 'active', ?, ?)",
        (TS, TS),
    )
    insert_asset(conn, id="as5", channel_id="ch2")


def test_an_artifact_belongs_to_at_most_one_asset(conn) -> None:
    seed_artifact(conn)
    insert_asset(conn, category="generated", artifact_id="a1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_asset(
            conn, id="as2", title_key="x", category="generated", artifact_id="a1"
        )
    # Many assets without an artifact are fine: the unique index is partial.
    insert_asset(conn, id="as3", title_key="y")
    insert_asset(conn, id="as4", title_key="z")


def insert_usage(connection: sqlite3.Connection, **overrides) -> None:
    row = {
        "id": "u1",
        "asset_id": "as1",
        "content_item_id": "ci1",
        "purpose": None,
        "attached_by_kind": "user",
        "attached_by_id": "owner",
        "created_at": TS,
    } | overrides
    connection.execute(
        f"INSERT INTO asset_usages ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def test_an_asset_usage_is_unique_per_pair(conn) -> None:
    seed_item(conn)
    insert_asset(conn)
    insert_usage(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_usage(conn, id="u2", purpose="again")
    insert_asset(conn, id="as2", title_key="two")
    insert_usage(
        conn, id="u3", asset_id="as2", purpose="p" * 200, attached_by_kind="ai"
    )

    assert conn.execute("SELECT count(*) FROM asset_usages").fetchone() == (2,)


@pytest.mark.parametrize(
    "overrides",
    [
        {"purpose": "p" * 201},
        {"purpose": " "},
        {"attached_by_kind": "robot"},
        {"attached_by_id": None},
        {"created_at": "2026-09-30"},
        {"content_item_id": "nope"},
        {"asset_id": "nope"},
    ],
)
def test_asset_usage_rules_are_enforced_in_sql(conn, overrides) -> None:
    seed_item(conn)
    insert_asset(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_usage(conn, **overrides)


def test_the_asset_registry_migration_changes_no_rights_table(conn) -> None:
    indexes = {
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'rights_records' AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = [row[1] for row in conn.execute("PRAGMA table_info(rights_records)")]

    assert indexes == set()
    assert columns == [
        "id",
        "content_item_id",
        "asset_ref",
        "source",
        "license",
        "risk_level",
        "resolution",
        "resolved_by_kind",
        "resolved_by_id",
        "resolved_at",
        "created_at",
        "updated_at",
    ]


def test_provenance_limits_match_the_python_constants(conn) -> None:
    # The SQL bounds repeat the Python constants; this catches a drift (G-077).
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("asset_provenance",),
    ).fetchone()[0]
    expected = {
        "license_name": provenance.MAX_LICENSE_NAME,
        "license_ref": provenance.MAX_LICENSE_REF,
        "attribution": provenance.MAX_ATTRIBUTION,
        "owner": provenance.MAX_OWNER,
        "proof": provenance.MAX_PROOF,
    }
    for column, limit in expected.items():
        found = re.search(rf"length\(trim\({column}\)\) BETWEEN 1 AND (\d+)", sql)
        assert found is not None, column
        assert int(found.group(1)) == limit, column
    for column in ("source_url", "license_url"):
        found = re.search(rf"length\({column}\) BETWEEN 1 AND (\d+)", sql)
        assert found is not None, column
        assert int(found.group(1)) == provenance.MAX_URL_LENGTH, column
    assert "length(file_sha256) = 64" in sql
    assert provenance.SHA256_PATTERN.pattern == "[0-9a-f]{64}"


def provenance_row(**overrides) -> dict:
    row = {
        "id": "pv1",
        "asset_id": "as1",
        "source_url": "https://stock.example/logo",
        "retrieved_at": TS,
        "license_name": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "license_ref": "CC-BY-4.0",
        "attribution": "Photo by Lan",
        "owner": "Lan",
        "file_sha256": SHA,
        "proof": "invoice 7",
        "recorded_by_kind": "user",
        "recorded_by_id": "owner",
        "created_at": TS,
    }
    return row | overrides


def insert_provenance(connection: sqlite3.Connection, **overrides) -> None:
    row = provenance_row(**overrides)
    connection.execute(
        f"INSERT INTO asset_provenance ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def only(**details) -> dict:
    """A row whose only detail is the given one."""
    row = dict.fromkeys(
        (
            "source_url",
            "retrieved_at",
            "license_name",
            "license_url",
            "license_ref",
            "attribution",
            "owner",
            "file_sha256",
            "proof",
        )
    )
    return row | details


def test_a_valid_provenance_row_is_accepted(conn) -> None:
    seed_item(conn)
    insert_asset(conn)
    insert_provenance(conn)

    assert conn.execute("SELECT count(*) FROM asset_provenance").fetchone() == (1,)


@pytest.mark.parametrize(
    "details",
    [
        {"source_url": "http://a.example/x"},
        {"retrieved_at": TS},
        {"license_name": "MIT"},
        {"license_url": "https://a.example/license"},
        {"license_ref": "ref"},
        {"attribution": "credit"},
        {"owner": "Lan"},
        {"file_sha256": "0123456789abcdef" * 4},
        {"proof": "note"},
    ],
)
def test_a_provenance_row_with_one_detail_is_accepted(conn, details) -> None:
    seed_item(conn)
    insert_asset(conn)
    insert_provenance(conn, **only(**details))

    assert conn.execute("SELECT count(*) FROM asset_provenance").fetchone() == (1,)


def test_a_provenance_row_with_no_detail_is_refused(conn) -> None:
    seed_item(conn)
    insert_asset(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_provenance(conn, **only())


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_url": "ftp://a.example/x"},
        {"source_url": "a.example/x"},
        {"source_url": ""},
        {"source_url": "https://a.example/" + "a" * 2031},
        {"license_url": "mailto:a@b.example"},
        {"license_url": "https://a.example/" + "a" * 2031},
        {"retrieved_at": "2026-09-30T08:00:00Z"},
        {"retrieved_at": "2026-09-30"},
        {"license_name": "   "},
        {"license_name": "x" * 201},
        {"license_ref": ""},
        {"license_ref": "x" * 501},
        {"attribution": " "},
        {"attribution": "x" * 501},
        {"owner": "x" * 201},
        {"owner": "  "},
        {"proof": "x" * 1001},
        {"proof": ""},
        {"file_sha256": "a" * 63},
        {"file_sha256": "a" * 65},
        {"file_sha256": "A" * 64},
        {"file_sha256": "g" * 64},
        {"file_sha256": "a" * 63 + " "},
        {"file_sha256": ""},
        {"recorded_by_kind": "robot"},
        {"recorded_by_kind": None},
        {"recorded_by_id": None},
        {"created_at": "2026-09-30T08:00:00Z"},
        {"created_at": None},
        {"asset_id": "nope"},
        {"asset_id": None},
    ],
)
def test_provenance_rules_are_enforced_in_sql(conn, overrides) -> None:
    seed_item(conn)
    insert_asset(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_provenance(conn, **overrides)


def test_provenance_boundaries_are_accepted_in_sql(conn) -> None:
    seed_item(conn)
    insert_asset(conn)
    insert_provenance(
        conn,
        source_url="https://a.example/" + "a" * (provenance.MAX_URL_LENGTH - 18),
        license_url="http://a.example/" + "a" * (provenance.MAX_URL_LENGTH - 17),
        license_name="n" * provenance.MAX_LICENSE_NAME,
        license_ref="r" * provenance.MAX_LICENSE_REF,
        attribution="a" * provenance.MAX_ATTRIBUTION,
        owner="o" * provenance.MAX_OWNER,
        proof="p" * provenance.MAX_PROOF,
        file_sha256="0123456789abcdef" * 4,
        recorded_by_kind="ai",
    )

    assert conn.execute("SELECT count(*) FROM asset_provenance").fetchone() == (1,)


def test_an_asset_may_have_many_provenance_rows(conn) -> None:
    seed_item(conn)
    insert_asset(conn)
    insert_provenance(conn)
    insert_provenance(conn, id="pv2")  # the same statement again
    insert_provenance(conn, id="pv3", owner="Minh")
    insert_asset(conn, id="as2", title_key="two")
    insert_provenance(conn, id="pv4", asset_id="as2")

    assert conn.execute("SELECT count(*) FROM asset_provenance").fetchone() == (4,)
    assert conn.execute(
        "SELECT count(*) FROM asset_provenance WHERE asset_id = 'as1'"
    ).fetchone() == (3,)


def test_provenance_rows_are_indexed_by_asset_and_time(conn) -> None:
    indexes = {
        name: [row[2] for row in conn.execute(f"PRAGMA index_info({name})")]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'asset_provenance' AND name NOT LIKE 'sqlite_%'"
        )
    }
    # PRAGMA index_list columns: seq, name, unique, origin, partial. The origin
    # is 'pk' for the primary key, 'u' for a UNIQUE constraint, 'c' for an index.
    unique = {
        row[3] for row in conn.execute("PRAGMA index_list(asset_provenance)") if row[2]
    }

    assert indexes == {"asset_provenance_by_asset": ["asset_id", "created_at"]}
    assert unique == {"pk"}  # only the primary key is unique: no UNIQUE key


def test_the_provenance_migration_changes_no_other_table(conn) -> None:
    columns = {
        table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in ("assets", "asset_usages", "rights_records")
    }

    assert columns["assets"] == [
        "id",
        "channel_id",
        "kind",
        "category",
        "title",
        "source",
        "source_key",
        "title_key",
        "artifact_id",
        "license_ref",
        "attribution",
        "owner",
        "created_at",
    ]
    assert columns["asset_usages"] == [
        "id",
        "asset_id",
        "content_item_id",
        "purpose",
        "attached_by_kind",
        "attached_by_id",
        "created_at",
    ]
    assert columns["rights_records"] == [
        "id",
        "content_item_id",
        "asset_ref",
        "source",
        "license",
        "risk_level",
        "resolution",
        "resolved_by_kind",
        "resolved_by_id",
        "resolved_at",
        "created_at",
        "updated_at",
    ]


def test_foreign_keys_are_enforced(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO content_items VALUES ('ci1', 'nope', 'nope', 1, 'shorts', "
            "'T', 'draft', ?, ?)",
            (TS, TS),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO strategy_profiles SELECT 'sp2', channel_id, market_country, "
        "primary_language, secondary_languages_json, audience_json, niche_json, "
        "brand_json, cadence_shorts_per_day, cadence_longform_per_day, "
        "budget_currency, budget_daily_limit, budget_monthly_limit, "
        "monetization_json, version, updated_by_kind, updated_by_id, created_at, "
        "updated_at, format_json, cadence_schedule_json, "
        "budget_alert_thresholds_json FROM strategy_profiles",
        "INSERT INTO artifacts VALUES ('a2', 'ci1', 'video', 1, 'u2', '" + SHA + "', "
        "1, 'video/mp4', '" + TS + "')",
        "INSERT INTO scripts (id, content_item_id, version, text, created_at) "
        "VALUES ('s2', 'ci1', 1, 'Other', '" + TS + "')",
    ],
    ids=["one-strategy-per-channel", "artifact-version", "script-version"],
)
def test_unique_domain_rules_are_enforced(conn, statement: str) -> None:
    seed_item(conn)
    conn.execute(
        "INSERT INTO artifacts VALUES ('a1', 'ci1', 'video', 1, 'u1', ?, 1, "
        "'video/mp4', ?)",
        (SHA, TS),
    )
    conn.execute(
        "INSERT INTO scripts (id, content_item_id, version, text, created_at) "
        "VALUES ('s1', 'ci1', 1, 'Text', ?)",
        (TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement)


def test_idempotency_keys_are_unique(conn) -> None:
    for job_id in ("j1", "j2"):
        statement = (
            "INSERT INTO ai_jobs VALUES (?, 'script.generate', 'key-1', NULL, NULL, "
            "'queued', 0, NULL, ?, ?)"
        )
        if job_id == "j2":
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement, (job_id, TS, TS))
        else:
            conn.execute(statement, (job_id, TS, TS))


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-09-30T08:00:00+00:00",
        "2026-09-30T08:00:00Z",
        "2026-09-30 08:00:00.000000Z",
        "2026-09-30T08:00:00.000000",
    ],
)
def test_non_canonical_datetimes_are_rejected(conn, stamp: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions VALUES ('s1', 'user', 'u1', 'active', ?, NULL)",
            (stamp,),
        )


def test_canonical_datetime_is_accepted(conn) -> None:
    conn.execute(
        "INSERT INTO sessions VALUES ('s1', 'user', 'u1', 'active', ?, NULL)",
        (format_datetime(T1),),
    )
    assert conn.execute("SELECT started_at FROM sessions").fetchone() == (
        "2026-09-30T09:30:15.000250Z",
    )


@pytest.mark.parametrize("amount", ["12.34", "0", "5.00", "0.0000001"])
def test_canonical_money_is_accepted(conn, amount: str) -> None:
    seed_item(conn)
    insert_cost(conn, amount)
    assert conn.execute("SELECT amount FROM cost_records").fetchone() == (amount,)


@pytest.mark.parametrize("amount", ["-1", "1e3", "1.", ".5", "1.2.3", "", "12,5"])
def test_money_must_be_canonical_non_negative_text(conn, amount) -> None:
    seed_item(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_cost(conn, amount)


def test_sqlite_cannot_block_floats_so_the_codec_must(conn) -> None:
    # SQLite turns a number bound to a TEXT column into text before any CHECK
    # runs, and rounds floats on the way (0.1 + 0.2 is stored as '0.3'). Money
    # must therefore reach the database only through format_decimal, which
    # refuses floats. Repositories (#030) write money through the codec.
    seed_item(conn)
    insert_cost(conn, 0.1 + 0.2)
    assert conn.execute("SELECT amount FROM cost_records").fetchone() == ("0.3",)
    with pytest.raises(ValueError):
        format_decimal(0.1 + 0.2)


@pytest.mark.parametrize(
    ("value", "accepted"),
    [("-3", True), ("1520", True), ("-0.5", True), ("-", False), ("--1", False)],
)
def test_metric_values_may_be_negative(conn, value: str, accepted: bool) -> None:
    conn.execute(
        "INSERT INTO metric_snapshots VALUES ('m1', 'video', 'v1', 'mock', ?, ?, ?)",
        (TS, format_datetime(T1), format_datetime(T1)),
    )
    statement = "INSERT INTO metric_values VALUES ('m1', 'views', ?)"
    if accepted:
        conn.execute(statement, (value,))
    else:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, (value,))


def test_audit_events_are_append_only(conn) -> None:
    conn.execute(
        "INSERT INTO audit_events VALUES ('e1', ?, 'job.started', 'system', 'x', "
        "'ai_job', 'j1', 'success', NULL, NULL, NULL, '{}')",
        (TS,),
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_events SET result = 'failure'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_events")
    assert conn.execute("SELECT count(*) FROM audit_events").fetchone() == (1,)


# Loading migration files


@pytest.mark.parametrize(
    "names",
    [
        ["0001_a", "0003_c"],
        ["0002_b"],
        ["0001_a", "0001_b"],
        ["0000_zero"],
        ["1_short"],
        ["0001_Bad-Name"],
    ],
)
def test_migration_files_must_be_numbered_without_gaps(tmp_path: Path, names) -> None:
    folder = write_migrations(tmp_path / "m", **dict.fromkeys(names, "SELECT 1;"))
    with pytest.raises(MigrationError):
        load_migrations(folder)


def test_other_files_are_ignored(tmp_path: Path) -> None:
    folder = write_migrations(tmp_path / "m", **{"0001_a": "SELECT 1;"})
    (folder / "README.md").write_text("notes")
    assert [m.version for m in load_migrations(folder)] == [1]


# Safety checks


def test_changed_migration_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    folder = write_migrations(tmp_path / "m", **{"0001_a": "CREATE TABLE a (x TEXT);"})
    migrate(path, migrations=load_migrations(folder))
    write_migrations(folder, **{"0001_a": "CREATE TABLE a (x TEXT, y TEXT);"})

    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(path, migrations=load_migrations(folder))


def test_applied_migration_without_a_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    migrate(path, migrations=[Migration.from_text("0001_a.sql", "SELECT 1;")])
    with pytest.raises(MigrationError, match="no file"):
        migrate(path, migrations=[])


def test_unmanaged_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE foreign_table (x TEXT)")
    connection.close()
    with pytest.raises(MigrationError, match="not created by these migrations"):
        migrate(path)


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nCREATE TABLE a (x TEXT);\nCOMMIT;",
        "CREATE TABLE a (x TEXT);\nCOMMIT;",
        "PRAGMA foreign_keys = OFF;\nCREATE TABLE a (x TEXT);",
        "CREATE TABLE a (x TEXT);\nCREATE TABLE b (",
    ],
    ids=["begin", "commit", "pragma", "incomplete"],
)
def test_bad_statements_are_refused_before_anything_runs(tmp_path: Path, sql) -> None:
    path = tmp_path / "app.db"
    with pytest.raises(MigrationError):
        migrate(path, migrations=[Migration.from_text("0001_a.sql", sql)])
    assert current_version(path) == 0
    connection = connect(path)
    try:
        assert tables(connection) == {"schema_migrations"}
    finally:
        connection.close()


# Transactions, backup and restore


def v1_and_v2(v2_sql: str) -> tuple[Migration, Migration]:
    return (
        Migration.from_text("0001_a.sql", "CREATE TABLE a (x TEXT) STRICT;"),
        Migration.from_text("0002_b.sql", v2_sql),
    )


def test_failed_migration_is_rolled_back(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    v1, v2 = v1_and_v2("CREATE TABLE b (x TEXT);\nINSERT INTO missing VALUES (1);")
    migrate(path, migrations=[v1])

    with pytest.raises(MigrationError, match="rolled back"):
        migrate(path, migrations=[v1, v2], clock=at(T1))

    assert current_version(path) == 1
    connection = connect(path)
    try:
        assert tables(connection) == {"schema_migrations", "a"}
    finally:
        connection.close()
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_backup_is_taken_before_pending_migrations(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    v1, v2 = v1_and_v2("CREATE TABLE b (x TEXT) STRICT;")
    migrate(path, migrations=[v1])
    connection = connect(path)
    connection.execute("INSERT INTO a VALUES ('kept')")
    connection.close()

    report = migrate(path, migrations=[v1, v2], clock=at(T1))

    assert report.applied == (2,)
    assert report.backup_path == (
        tmp_path / "backups" / "app.20260930T093015000250Z.v0001.db"
    )
    assert current_version(report.backup_path) == 1
    backup = connect(report.backup_path)
    try:
        assert backup.execute("SELECT x FROM a").fetchall() == [("kept",)]
        assert tables(backup) == {"schema_migrations", "a"}
    finally:
        backup.close()


def test_restore_backup_returns_to_the_old_version(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    v1, v2 = v1_and_v2("CREATE TABLE b (x TEXT) STRICT;")
    migrate(path, migrations=[v1])
    connection = connect(path)
    connection.execute("INSERT INTO a VALUES ('before')")
    connection.close()
    report = migrate(path, migrations=[v1, v2], clock=at(T1))
    connection = connect(path)
    connection.execute("DELETE FROM a")
    connection.close()

    restore_backup(report.backup_path, path)

    assert current_version(path) == 1
    connection = connect(path)
    try:
        assert tables(connection) == {"schema_migrations", "a"}
        assert connection.execute("SELECT x FROM a").fetchall() == [("before",)]
    finally:
        connection.close()


def test_restore_refuses_a_missing_or_foreign_file(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="does not exist"):
        restore_backup(tmp_path / "nope.db", tmp_path / "app.db")
    foreign = tmp_path / "foreign.db"
    sqlite3.connect(foreign).close()
    with pytest.raises(MigrationError, match="not a migrated database"):
        restore_backup(foreign, tmp_path / "app.db")


# Startup only


def test_create_app_does_not_touch_the_database(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    create_app(build_container(Settings(database_path=path)))
    assert not path.exists()


def test_startup_applies_migrations(tmp_path: Path) -> None:
    path = tmp_path / "data" / "app.db"
    app = create_app(build_container(Settings(database_path=path)))

    with TestClient(app) as client:
        assert current_version(path) == LATEST
        assert client.get("/health").status_code == 200


def test_importing_the_app_does_not_create_a_database(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import ai_youtube_agent.main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "data").exists()
