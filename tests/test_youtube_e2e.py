"""T-229 YouTube E2E: Controlled OAuth token lifecycle, channel verification,
expired token auto-refresh, and publishing flow.
"""

import pytest

from ai_youtube_agent.core.audit import AuditLog, InMemoryAuditSink
from ai_youtube_agent.core.content_item import ContentStatus
from ai_youtube_agent.core.db.database import Database
from ai_youtube_agent.core.db.repositories.content import ContentItemRepository
from ai_youtube_agent.core.token_storage import InMemoryTokenVault, ManagedTokenStore
from ai_youtube_agent.pipeline.publish_audit import (
    PUBLISH_AUTHORIZATION_FAILED,
    PUBLISH_TOKEN_RENEWED,
)
from ai_youtube_agent.pipeline.shorts_publisher import ShortsPublisher
from ai_youtube_agent.pipeline.token_recovery import PublishAuthorizationError
from ai_youtube_agent.providers.youtube_oauth import OAuthScope
from factories import SYSTEM
from publish_helpers import PUBLISH_FLAGS, build_publishing, clock, seed_approved_item


def _stored_status(database: Database, item_id: str) -> ContentStatus:
    connection = database.connect()
    try:
        item = ContentItemRepository(connection).get(item_id)
    finally:
        connection.close()
    assert item is not None
    return item.status


def test_youtube_e2e_oauth_refresh_channel_verification_and_publish(
    database: Database, tmp_path
) -> None:
    """Verifies controlled OAuth token provisioning, encrypted vault storage,
    expired-token refresh during publish, and remote channel verification."""
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)

    # 1. Provision and store token in ManagedTokenStore, then expire the provider token
    vault = InMemoryTokenVault()
    store_sink = InMemoryAuditSink()
    token_store = ManagedTokenStore(vault, pub.oauth, AuditLog(store_sink), clock=clock)
    initial_token = pub.oauth.get_token(
        seed.channel.id,
        required_scopes=(OAuthScope.YOUTUBE_UPLOAD, OAuthScope.YOUTUBE_READONLY),
    )
    token_store.save_token(initial_token)

    # Expire the token in the OAuth provider; PublishExecutor must refresh it
    pub.oauth.expire_channel_token(seed.channel.id)

    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )
    outcome = publisher.publish(seed.item.id, actor=SYSTEM)
    assert _stored_status(database, seed.item.id) is ContentStatus.PUBLISHED
    assert outcome.job.result.youtube_video_id != ""
    assert len(pub.uploader.uploads) == 1
    assert any(e.action == PUBLISH_TOKEN_RENEWED for e in pub.sink.events())


def test_youtube_e2e_revoked_token_blocks_publish(database: Database, tmp_path) -> None:
    """Verifies that a revoked OAuth token fails closed before any upload occurs."""
    seed = seed_approved_item(database, tmp_path)
    pub = build_publishing(seed)
    pub.oauth.revoke_token(seed.channel.id)

    publisher = ShortsPublisher(
        database, seed.storage, pub.executor, flags=PUBLISH_FLAGS
    )
    with pytest.raises(PublishAuthorizationError):
        publisher.publish(seed.item.id, actor=SYSTEM)
    assert len(pub.uploader.uploads) == 0
    assert any(e.action == PUBLISH_AUTHORIZATION_FAILED for e in pub.sink.events())
