# Security Scopes & Least Privilege Policy (S-218)

This document defines the minimal OAuth 2.0 scopes and actor permission boundaries enforced by `ai_youtube_agent.core.least_privilege`.

## 1. YouTube OAuth 2.0 Scope Matrix

| Operation | Minimal Required Scope | Accepted Alternative | Justification |
|---|---|---|---|
| `channel.read` | `https://www.googleapis.com/auth/youtube.readonly` | None | Verifying channel identity and status requires read-only access. |
| `video.upload` | `https://www.googleapis.com/auth/youtube.upload` | None | Uploading human-approved videos requires upload scope without full account control. |
| `analytics.read` | `https://www.googleapis.com/auth/yt-analytics.readonly` | None | Syncing non-monetary channel and video metrics requires read-only analytics access. |
| `revenue.read` | `https://www.googleapis.com/auth/yt-analytics-monetary.readonly` | None | Reading estimated and finalized revenue requires monetary analytics read-only access. |
| `comment.read` | `https://www.googleapis.com/auth/youtube.readonly` | `https://www.googleapis.com/auth/youtube.force-ssl` | Reading comment threads prefers `youtube.readonly` and accepts `youtube.force-ssl` when reply posting is enabled. |
| `comment.reply_post` | `https://www.googleapis.com/auth/youtube.force-ssl` | None | Posting human-approved comment replies requires `youtube.force-ssl` per YouTube Data API v3. |

## 2. Actor Permission Matrix

| Privileged Action | `ActorKind.USER` | `ActorKind.SYSTEM` | `ActorKind.AI` | Invariant Reference |
|---|---|---|---|---|
| `content.approve` / `reject` / `request_changes` | Allowed | Denied | Denied | `R-08` Mandatory Human Approval |
| `strategy.update` | Allowed | Denied | Denied | `R-09` No Strategy Drift |
| `voice_profile.update` | Allowed | Denied | Denied | `B-018` Voice Profile Guard |
| `rights.resolve` | Allowed | Denied | Denied | `B-019` Rights Risk Resolution |
| `experiment.start` / `conclude` / `cancel` | Allowed | Denied | Denied | `B-028` Experiment Governance |
| `session.start` | Allowed | Denied | Denied | `B-027` / `R-209` User-Initiated Session |
| `kill_switch.activate` | Allowed | Allowed | Denied | `R-213` Emergency Stop |
| `kill_switch.deactivate` | Allowed | Denied | Denied | `R-213` Human Reset Only |
| `comment_reply.approve` | Allowed | Denied | Denied | `P-186` / `P-188` Human Reply Approval |
