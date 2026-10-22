"""Mock Approval Email Provider (Prompt Pack v8, prompt #138), context C11 Review.

``MockApprovalEmailProvider`` is a deterministic, in-memory implementation of
``ApprovalEmailProvider`` that stores sent approval emails in ``outbox`` and
never touches the network or sends real external email.
"""

import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from ai_youtube_agent.providers.approval_email import (
    ApprovalEmailMessage,
    ApprovalEmailProviderError,
    ApprovalEmailReceipt,
    EmailErrorCode,
)

Clock = Callable[[], datetime]


class MockApprovalEmailProvider:
    def __init__(self, *, name: str = "mock", clock: Clock | None = None) -> None:
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: list[EmailErrorCode] = []
        self._healthy = True
        self.calls: list[ApprovalEmailMessage] = []
        self.outbox: list[ApprovalEmailMessage] = []
        self.receipts: list[ApprovalEmailReceipt] = []

    def fail_next(self, code: EmailErrorCode, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be 1 or more")
        with self._lock:
            self._failures.extend([code] * times)

    def set_healthy(self, healthy: bool) -> None:
        with self._lock:
            self._healthy = healthy

    def send(self, message: ApprovalEmailMessage) -> ApprovalEmailReceipt:
        with self._lock:
            self.calls.append(message)
            if self._failures:
                code = self._failures.pop(0)
                raise ApprovalEmailProviderError(
                    code, "scripted approval email failure", provider=self.name
                )
            seq = len(self.outbox) + 1
            digest = hashlib.sha256(
                "\x00".join(
                    (
                        str(seq),
                        message.recipient,
                        message.approval_request_id,
                        message.video_artifact_id,
                        message.video_artifact_sha256,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
            now = self._clock() if self._clock else datetime.now(UTC)
            receipt = ApprovalEmailReceipt(
                message_id=f"mock-email-{digest}",
                provider=self.name,
                recipient=message.recipient,
                approval_request_id=message.approval_request_id,
                video_artifact_id=message.video_artifact_id,
                sent_at=now,
            )
            self.outbox.append(message)
            self.receipts.append(receipt)
            return receipt

    def check(self) -> None:
        with self._lock:
            healthy = self._healthy
        if not healthy:
            raise ApprovalEmailProviderError(
                EmailErrorCode.UNAVAILABLE,
                "mock email provider set unhealthy",
                provider=self.name,
            )
