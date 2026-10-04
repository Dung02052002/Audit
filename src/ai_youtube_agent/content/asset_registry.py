"""Asset Registry (Prompt Pack v8, prompt #076), context C6 Rights & Policy.

``AssetRegistry`` registers every external or generated asset a channel uses in
its videos and links it to the content items that use it. The rules were
approved by the user on 2026-10-04:

- Scope: a service over the ``Asset`` entity (#075) with a migration (0023) and
  repositories (``core/db/repositories/asset.py``); no HTTP route. Any actor may
  register and attach, and the channel must exist (``ChannelNotFoundError``).
- ``register`` builds the asset with ``Asset.create``: a rule of the entity that
  fails (``ValueError``, ``TypeError``) is ``AssetInputError`` (422). An
  ``artifact_id`` must be an artifact whose content item belongs to the same
  channel (422).
- Duplicates in one channel: the same normalised source and title (NFC of
  casefold(NFC(stripped text)), see ``normalise_key``) is one asset, and so is a
  generated asset with the same ``artifact_id``. When the new request equals
  the stored asset (kind, category, artifact, licence, attribution, owner, and
  the source and title up to that normalisation) the stored asset is returned,
  with no write and no audit. Otherwise ``AssetConflictError`` (409). A key that
  matches one asset and an artifact that matches another is also a conflict. The
  lookup and the insert share one ``BEGIN IMMEDIATE`` transaction, and a unique
  constraint that still fires is resolved the same way from a fresh read. The
  title is stored as given (collapsed whitespace only, no NFC), as in ``Asset``.
- Usage: an asset is used by content items of its own channel (a different
  channel is 422), many items per asset and many assets per item. ``attach`` is
  idempotent: a repeat returns the stored usage unchanged, ignores a different
  purpose, and writes and audits nothing.
- Rights: a new usage creates, in the same transaction, a ``RightsRecord`` for
  the item with ``asset_ref`` = ``Asset.id``, the asset ``source``, the asset
  ``license_ref`` as licence, risk ``unknown`` and unresolved, unless the item
  already has a record for that ``asset_ref``: a record in any state is left
  untouched. #078 sets the risk level, and the rights gate (#038) blocks a
  publish while the record is unresolved with an unknown or high level.
- ``asset.registered`` and ``asset.attached`` are audited after commit with ids,
  flags and counts only, never a title, a source or a licence text. A repeat of
  either call audits nothing.
- ``get``, ``list_by_channel``, ``list_by_content_item`` and ``usages_of`` read
  and write nothing.
"""

import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus

from ai_youtube_agent.content.asset import Asset, AssetCategory, AssetKind
from ai_youtube_agent.content.asset_usage import AssetUsage
from ai_youtube_agent.content.channel_settings import ChannelNotFoundError
from ai_youtube_agent.content.hook_generator import ContentItemNotFoundError
from ai_youtube_agent.content.rights import RightsRecord
from ai_youtube_agent.core.audit import Actor, AuditLog, AuditResult, EntityRef
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.asset import (
    AssetRepository,
    AssetUsageRepository,
)
from ai_youtube_agent.core.db.repositories.channel import ChannelRepository
from ai_youtube_agent.core.db.repositories.content import (
    ArtifactRepository,
    ContentItemRepository,
)
from ai_youtube_agent.core.db.repositories.review import RightsRecordRepository
from ai_youtube_agent.core.errors import DomainError

Clock = Callable[[], datetime]


class AssetInputError(DomainError):
    default_code = "domain.asset_input"
    default_user_message = "This asset cannot be registered or attached as given."


class AssetConflictError(DomainError):
    default_code = "domain.asset_conflict"
    default_user_message = (
        "A different asset with the same source and title is already registered "
        "for this channel."
    )
    default_http_status = HTTPStatus.CONFLICT


class AssetNotFoundError(DomainError):
    default_code = "domain.asset_not_found"
    default_user_message = "This asset does not exist."
    default_http_status = HTTPStatus.NOT_FOUND


def normalise_key(text: str) -> str:
    """The key of a source or a title: NFC(casefold(NFC(stripped text)))."""
    stripped = unicodedata.normalize("NFC", text.strip())
    return unicodedata.normalize("NFC", stripped.casefold())


class AssetRegistry:
    def __init__(
        self, database: Database, audit: AuditLog, *, clock: Clock | None = None
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock

    def register(
        self,
        channel_id: str,
        kind: AssetKind,
        category: AssetCategory,
        *,
        title: str,
        source: str,
        artifact_id: str | None = None,
        license_ref: str | None = None,
        attribution: str | None = None,
        owner: str | None = None,
        actor: Actor,
    ) -> Asset:
        try:
            asset = Asset.create(
                channel_id,
                kind,
                category,
                title=title,
                source=source,
                artifact_id=artifact_id,
                license_ref=license_ref,
                attribution=attribution,
                owner=owner,
                clock=self._clock,
            )
        except (ValueError, TypeError) as error:
            raise AssetInputError(str(error)) from error
        source_key, title_key = normalise_key(asset.source), normalise_key(asset.title)
        try:
            with self._database.transaction() as connection:
                if ChannelRepository(connection).get(channel_id) is None:
                    raise ChannelNotFoundError(f"channel {channel_id} does not exist")
                if asset.artifact_id is not None:
                    self._check_artifact(connection, asset)
                assets = AssetRepository(connection)
                existing = _existing(assets, asset, source_key, title_key)
                if existing is not None:
                    return existing
                assets.add(asset, source_key=source_key, title_key=title_key)
        except sqlite3.IntegrityError:
            # Expected: a concurrent call committed the same asset first. Any
            # other integrity error is hidden only if such an asset exists,
            # else it is raised again below.
            with self._database.transaction() as connection:
                existing = _existing(
                    AssetRepository(connection), asset, source_key, title_key
                )
            if existing is None:
                raise
            return existing
        self._audit.record(
            "asset.registered",
            actor,
            EntityRef("asset", asset.id),
            AuditResult.SUCCESS,
            {
                "channel_id": asset.channel_id,
                "kind": asset.kind.value,
                "category": asset.category.value,
                "has_artifact": asset.artifact_id is not None,
            },
        )
        return asset

    def attach(
        self,
        asset_id: str,
        content_item_id: str,
        *,
        purpose: str | None = None,
        actor: Actor,
    ) -> AssetUsage:
        try:
            usage = AssetUsage.create(
                asset_id,
                content_item_id,
                purpose=purpose,
                attached_by=actor,
                clock=self._clock,
            )
        except (ValueError, TypeError) as error:
            raise AssetInputError(str(error)) from error
        with self._database.transaction() as connection:
            asset = AssetRepository(connection).get(asset_id)
            if asset is None:
                raise AssetNotFoundError(f"asset {asset_id} does not exist")
            item = ContentItemRepository(connection).get(content_item_id)
            if item is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            if item.channel_id != asset.channel_id:
                raise AssetInputError(
                    f"asset {asset_id} and content item {content_item_id} "
                    "belong to different channels"
                )
            usages = AssetUsageRepository(connection)
            stored = usages.get_by_pair(asset_id, content_item_id)
            if stored is not None:
                return stored
            usages.add(usage)
            rights = RightsRecordRepository(connection)
            record = next(
                (
                    r
                    for r in rights.list_by_content_item(content_item_id)
                    if r.asset_ref == asset.id
                ),
                None,
            )
            created = record is None
            if record is None:
                record = RightsRecord.create(
                    content_item_id,
                    asset.id,
                    source=asset.source,
                    license=asset.license_ref,
                    clock=self._clock,
                )
                rights.add(record)
        self._audit.record(
            "asset.attached",
            actor,
            EntityRef("asset", asset.id),
            AuditResult.SUCCESS,
            {
                "usage_id": usage.id,
                "content_item_id": content_item_id,
                "rights_record_id": record.id,
                "rights_record_created": created,
            },
        )
        return usage

    def get(self, asset_id: str) -> Asset:
        with self._database.transaction() as connection:
            return _require_asset(connection, asset_id)

    def list_by_channel(self, channel_id: str) -> list[Asset]:
        with self._database.transaction() as connection:
            if ChannelRepository(connection).get(channel_id) is None:
                raise ChannelNotFoundError(f"channel {channel_id} does not exist")
            return AssetRepository(connection).list_by_channel(channel_id)

    def list_by_content_item(self, content_item_id: str) -> list[Asset]:
        with self._database.transaction() as connection:
            if ContentItemRepository(connection).get(content_item_id) is None:
                raise ContentItemNotFoundError(
                    f"content item {content_item_id} does not exist"
                )
            return AssetRepository(connection).list_by_content_item(content_item_id)

    def usages_of(self, asset_id: str) -> list[AssetUsage]:
        with self._database.transaction() as connection:
            _require_asset(connection, asset_id)
            return AssetUsageRepository(connection).list_by_asset(asset_id)

    def _check_artifact(self, connection: sqlite3.Connection, asset: Asset) -> None:
        artifact = ArtifactRepository(connection).get(asset.artifact_id)
        if artifact is None:
            raise AssetInputError(f"artifact {asset.artifact_id} does not exist")
        item = ContentItemRepository(connection).get(artifact.content_item_id)
        if item is None or item.channel_id != asset.channel_id:
            raise AssetInputError(
                f"artifact {asset.artifact_id} does not belong to channel "
                f"{asset.channel_id}"
            )


def _require_asset(connection: sqlite3.Connection, asset_id: str) -> Asset:
    asset = AssetRepository(connection).get(asset_id)
    if asset is None:
        raise AssetNotFoundError(f"asset {asset_id} does not exist")
    return asset


def _existing(
    assets: AssetRepository, asset: Asset, source_key: str, title_key: str
) -> Asset | None:
    """The stored asset the new one duplicates, or None; a different one conflicts."""
    by_key = assets.find_by_key(asset.channel_id, source_key, title_key)
    by_artifact = (
        assets.get_by_artifact(asset.artifact_id)
        if asset.artifact_id is not None
        else None
    )
    if by_key is not None and by_artifact is not None and by_key.id != by_artifact.id:
        raise AssetConflictError(
            f"assets {by_key.id} and {by_artifact.id} each match part of the request"
        )
    stored = by_key or by_artifact
    if stored is None:
        return None
    if not _same(stored, asset):
        raise AssetConflictError(
            f"asset {stored.id} has the same source and title or artifact "
            "but different details"
        )
    return stored


def _same(stored: Asset, new: Asset) -> bool:
    return (
        normalise_key(stored.source) == normalise_key(new.source)
        and normalise_key(stored.title) == normalise_key(new.title)
        and (stored.kind, stored.category, stored.artifact_id)
        == (new.kind, new.category, new.artifact_id)
        and (stored.license_ref, stored.attribution, stored.owner)
        == (new.license_ref, new.attribution, new.owner)
    )
