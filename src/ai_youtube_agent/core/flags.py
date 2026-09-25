"""Feature flags (Prompt Pack v8, prompt #006).

Each default is the safe choice from docs/REQUIREMENTS.md:

- ``shorts_enabled``: on. Shorts is the pilot content type (section 5).
- ``longform_enabled``: off. LongForm stays locked until #237 LongForm Unlock.
- ``publish_enabled``: off. There is no auto-publish (R-08).
- ``test_required``: on. TEST is a mandatory control stage (section 4).
- ``approval_required``: on. A publish always needs approval (R-08).
- ``auto_reply_enabled``: off, as stated in #006 and #188.

Flags are set through environment variables such as
``AI_YOUTUBE_AGENT_FLAGS__PUBLISH_ENABLED=true``.
"""

from pydantic import BaseModel, ConfigDict, model_validator


class FeatureFlags(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shorts_enabled: bool = True
    longform_enabled: bool = False
    publish_enabled: bool = False
    test_required: bool = True
    approval_required: bool = True
    auto_reply_enabled: bool = False

    @model_validator(mode="after")
    def _publish_requires_control_stages(self) -> "FeatureFlags":
        if self.publish_enabled and not (self.test_required and self.approval_required):
            raise ValueError(
                "publish_enabled requires test_required and approval_required"
            )
        return self
