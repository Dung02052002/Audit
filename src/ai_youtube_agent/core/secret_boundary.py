"""Secret Boundary enforcement (Prompt Pack v8, prompt #216), area X4 Security.

Ensures all secrets, API keys, and OAuth credentials remain outside source
control, ``.gitignore`` rules protect local secret files, ``.env.example``
contains only safe placeholders, and ``Settings`` secret fields use ``SecretStr``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, get_args, get_origin

from pydantic import SecretStr

from ai_youtube_agent.core.config import Settings
from ai_youtube_agent.core.errors import DomainError

REQUIRED_GITIGNORE_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    ".env.*",
    "!.env.example",
    "data/",
)

SECRET_FIELD_NAME_TOKENS: Final[tuple[str, ...]] = (
    "secret",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "private_key",
    "access_token",
    "refresh_token",
    "client_secret",
)

NON_SECRET_FIELD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "max_tokens",
        "token_ttl_seconds",
        "approval_ttl_hours",
    }
)

REAL_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("google_oauth_secret", re.compile(r"\bGOCSPX-[0-9A-Za-z\-_]{20,}\b")),
    ("google_access_token", re.compile(r"\bya29\.[0-9A-Za-z\-_]{20,}\b")),
    ("openai_or_provider_key", re.compile(r"\bsk-[0-9A-Za-z]{24,}\b")),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)

SAFE_PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
    "changeme",
    "your_",
    "example",
    "placeholder",
    "<",
    "mock",
    "test",
    "none",
    "false",
    "true",
)


class SecretBoundaryError(DomainError):
    """Raised when a secret boundary violation is detected."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "domain.secret_boundary_violation",
        user_message: str = "A secret boundary policy violation was detected.",
    ) -> None:
        super().__init__(detail, code=code, user_message=user_message)


@dataclass(frozen=True)
class SecretBoundaryViolation:
    """Single secret boundary violation finding."""

    rule_code: str
    location: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "location": self.location,
            "message": self.message,
        }


@dataclass(frozen=True)
class SecretBoundaryReport:
    """Result of inspecting the repository and configuration secret boundary."""

    passed: bool
    checked_files: int
    violations: tuple[SecretBoundaryViolation, ...]

    def raise_if_violated(self) -> None:
        if not self.passed:
            codes = ", ".join(v.rule_code for v in self.violations)
            raise SecretBoundaryError(
                f"Secret boundary violations found: {codes}",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_files": self.checked_files,
            "violations": [v.as_dict() for v in self.violations],
        }


def _annotation_uses_secret_str(annotation: Any) -> bool:
    if annotation is SecretStr:
        return True
    origin = get_origin(annotation)
    if origin is not None:
        args = [a for a in get_args(annotation) if a is not type(None)]
        return bool(args) and all(_annotation_uses_secret_str(a) for a in args)
    return False


def validate_settings_model(
    settings_cls: type[Settings] = Settings,
) -> tuple[SecretBoundaryViolation, ...]:
    """Verify every secret-bearing field on ``settings_cls`` uses ``SecretStr`` with no plaintext default."""
    violations: list[SecretBoundaryViolation] = []
    for name, field_info in settings_cls.model_fields.items():
        lowered = name.lower()
        if lowered in NON_SECRET_FIELD_ALLOWLIST:
            continue
        if not any(tok in lowered for tok in SECRET_FIELD_NAME_TOKENS):
            continue
        if not _annotation_uses_secret_str(field_info.annotation):
            violations.append(
                SecretBoundaryViolation(
                    rule_code="secret.field_not_secret_str",
                    location=f"{settings_cls.__name__}.{name}",
                    message=f"Secret field '{name}' must be typed as SecretStr.",
                )
            )
        if field_info.default is not None and not field_info.is_required():
            violations.append(
                SecretBoundaryViolation(
                    rule_code="secret.field_has_default",
                    location=f"{settings_cls.__name__}.{name}",
                    message=f"Secret field '{name}' must not have a non-None default value.",
                )
            )
    return tuple(violations)


def scan_text_for_secrets(
    text: str,
    *,
    location: str = "<memory>",
) -> tuple[SecretBoundaryViolation, ...]:
    """Scan ``text`` for high-confidence credential patterns without echoing secret values."""
    violations: list[SecretBoundaryViolation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern in REAL_SECRET_PATTERNS:
            if pattern.search(line):
                violations.append(
                    SecretBoundaryViolation(
                        rule_code=f"secret.leaked_{pattern_name}",
                        location=f"{location}:{line_no}",
                        message=(
                            f"Potential credential pattern '{pattern_name}' detected at "
                            f"{location} line {line_no}."
                        ),
                    )
                )
    return tuple(violations)


class SecretBoundaryValidator:
    """Enforces repository and configuration secret boundary rules (#216)."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self._root = (repo_root or Path.cwd()).resolve()

    def check_gitignore(
        self, gitignore_path: Path | None = None
    ) -> tuple[SecretBoundaryViolation, ...]:
        target = gitignore_path or (self._root / ".gitignore")
        if not target.is_file():
            return (
                SecretBoundaryViolation(
                    rule_code="secret.gitignore_missing",
                    location=str(target),
                    message=".gitignore file is missing.",
                ),
            )
        lines = {
            line.strip()
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        violations: list[SecretBoundaryViolation] = []
        for required in REQUIRED_GITIGNORE_PATTERNS:
            if required not in lines:
                violations.append(
                    SecretBoundaryViolation(
                        rule_code="secret.gitignore_missing_rule",
                        location=str(target),
                        message=f".gitignore is missing required rule '{required}'.",
                    )
                )
        return tuple(violations)

    def check_env_example(
        self, env_example_path: Path | None = None
    ) -> tuple[SecretBoundaryViolation, ...]:
        target = env_example_path or (self._root / ".env.example")
        if not target.is_file():
            return ()
        content = target.read_text(encoding="utf-8")
        violations = list(scan_text_for_secrets(content, location=target.name))
        for line_no, raw in enumerate(content.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            key_lower = key.strip().lower()
            val_clean = val.strip().strip("\"'")
            if any(tok in key_lower for tok in SECRET_FIELD_NAME_TOKENS):
                if val_clean and not any(
                    marker in val_clean.lower() for marker in SAFE_PLACEHOLDER_MARKERS
                ):
                    violations.append(
                        SecretBoundaryViolation(
                            rule_code="secret.env_example_non_placeholder",
                            location=f"{target.name}:{line_no}",
                            message=(
                                f"Secret key '{key.strip()}' in {target.name} has a "
                                "non-placeholder value."
                            ),
                        )
                    )
        return tuple(violations)

    def audit_repository(
        self,
        *,
        extra_paths: Sequence[Path] = (),
        settings_cls: type[Settings] = Settings,
    ) -> SecretBoundaryReport:
        """Audit `.gitignore`, `.env.example`, `Settings`, and source files."""
        violations: list[SecretBoundaryViolation] = []
        checked_files = 0

        violations.extend(self.check_gitignore())
        checked_files += 1

        violations.extend(self.check_env_example())
        checked_files += 1

        violations.extend(validate_settings_model(settings_cls))

        src_dir = self._root / "src"
        scan_files: list[Path] = []
        if src_dir.is_dir():
            scan_files.extend(sorted(src_dir.rglob("*.py")))
        scan_files.extend(extra_paths)

        for file_path in scan_files:
            if not file_path.is_file():
                continue
            checked_files += 1
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            rel = (
                str(file_path.relative_to(self._root))
                if file_path.is_relative_to(self._root)
                else str(file_path)
            )
            violations.extend(scan_text_for_secrets(text, location=rel))

        return SecretBoundaryReport(
            passed=len(violations) == 0,
            checked_files=checked_files,
            violations=tuple(violations),
        )
