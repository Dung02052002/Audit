# T-236 Shorts Pilot Technical Report

## 1. Purpose & Scope

Task **T-236 (`Pilot Report`)** implements the controlled Shorts pilot runner (`ShortsPilotRunner`) and structured technical report (`ShortsPilotReport`) in `src/ai_youtube_agent/pipeline/pilot_runner.py`.

In accordance with **R-04**, **R-05**, and **R-08**:
- `SHORTS` is the initial pilot content type while `LONGFORM_ENABLED` defaults to `False`.
- No pilot item can be approved or published without an explicit human reviewer (`ActorKind.USER`).
- All pilot executions in test and build-first environments use controlled/mock providers (`MockYouTubeOAuthProvider`, `MockYouTubeChannelProvider`, `MockYouTubeUploadProvider`) with `YouTubePrivacyStatus.PRIVATE` by default and zero external network side effects.

## 2. Verified Pilot Stages

`ShortsPilotRunner.evaluate_pilot` verifies six mandatory technical stages before issuing `passed = True`:

| Stage | Verification Rule | Blocking Code on Failure |
|---|---|---|
| `channel_and_strategy` | Channel exists, is `ACTIVE`, and has a persisted `StrategyProfile`. | `pilot.channel_or_strategy_invalid` |
| `content_type_shorts` | `ContentItem.content_type` is `ContentType.SHORTS`. | `pilot.not_shorts` |
| `artifacts_stored` | Latest `VIDEO` and `METADATA` artifacts exist with SHA-256 digests. | `pilot.artifacts_missing` |
| `qc_verified` | Latest `QCResult` is `PASS` or `WARN` and covers the latest `VIDEO` artifact ID. | `pilot.qc_not_passed` |
| `human_approval` | Latest `ApprovalRequest` is `APPROVED` by `ActorKind.USER` and binds the exact `VIDEO` artifact ID. | `pilot.human_approval_missing` |
| `controlled_publish` | `ContentItem.status` is `PUBLISHED` with a `SUCCEEDED` `PublishJob` and `youtube_video_id`. | `pilot.publish_incomplete` |

## 3. Integration with T-237 (`LongFormUnlockService`)

`ShortsPilotReport.passed` serves as a mandatory technical precondition for **T-237 (`LongForm Unlock`)**: LongForm production (`LONGFORM_ENABLED`) cannot be unlocked for a channel until at least one Shorts pilot on that channel has passed all 6 stage checks.
