// One background run: live stage stepper, error panel and result.

import { ApiError, api } from "../api.js";
import { S, label } from "../strings.js";
import {
  alert,
  button,
  card,
  el,
  emptyView,
  errorAlert,
  errorView,
  facts,
  formatBytes,
  formatDateTime,
  formatDuration,
  linkButton,
  loadingView,
  mount,
  pageHeader,
  qcBadge,
  runStatusBadge,
  shortId,
  stepper,
} from "../ui.js";
import { createPoller } from "../poller.js";

const DEFAULT_RENDER_TIMEOUT_S = 600;
const ACTIVE = new Set(["queued", "running"]);

export function render(container, ctx) {
  const runId = ctx.params.runId;
  document.title = `${S.runView.title} · ${S.app.title}`;
  const connection = el("div", { "aria-live": "polite" });
  const body = el("div", { class: "stack" }, loadingView(4));
  let slowAfterMs = (DEFAULT_RENDER_TIMEOUT_S + 60) * 1000;
  let poller = null;
  let disposed = false;

  ctx
    .settings()
    .then((settings) => {
      slowAfterMs = (settings.render.timeout_seconds + 60) * 1000;
    })
    .catch(() => {});

  mount(container, pageHeader(S.runView.title, runId), connection, body);

  function showConnection(state) {
    if (state === "reconnecting") mount(connection, alert("warning", S.errors.reconnecting));
    else if (state === "gave_up") {
      mount(
        connection,
        el(
          "div",
          { class: "stack" },
          alert("error", S.errors.gaveUp),
          el("div", { class: "row" }, button(S.app.retry, () => {
            connection.replaceChildren();
            poller.restart();
          }, { primary: true })),
        ),
      );
    } else if (state === "slow") mount(connection, alert("warning", S.errors.slow));
    else connection.replaceChildren();
  }

  poller = createPoller({
    load: (signal) => api.run(runId, { signal }),
    isActive: (run) => ACTIVE.has(run.status),
    changeKey: (run) => `${run.status}|${run.progress.completed}|${run.progress.current_stage}`,
    slowAfterMs: () => slowAfterMs,
    onData: (run) => {
      if (!disposed) mount(body, runBody(run));
    },
    onStatus: showConnection,
    onError: (error) => {
      if (disposed) return;
      if (error instanceof ApiError && error.code === "domain.run_not_found") {
        mount(body, emptyView(S.runView.notFound, null, linkButton("#/items", S.nav.items, { primary: true })));
      } else {
        mount(body, errorView(error, () => {
          mount(body, loadingView(4));
          poller.restart();
        }));
      }
    },
  });
  poller.start();

  return () => {
    disposed = true;
    poller.stop();
  };
}

function runBody(run) {
  const request = run.request;
  const itemId = run.content_item_id;
  const itemHref = itemId ? `#/items/${encodeURIComponent(itemId)}` : null;
  const elapsedEnd = run.finished_at ? new Date(run.finished_at) : new Date();
  const elapsed = run.started_at ? elapsedEnd - new Date(run.started_at) : null;

  const actions = [];
  if (itemHref) actions.push(linkButton(itemHref, S.runView.openItem));
  if (run.status === "succeeded" && itemHref) {
    actions.push(linkButton(`${itemHref}?tab=preview`, S.runView.watch, { primary: true }));
  }
  if (run.status === "failed") {
    actions.push(
      itemHref
        ? linkButton(`#/items/${encodeURIComponent(itemId)}/edit`, S.runView.editAgain, { primary: true })
        : linkButton("#/new", S.runView.newRun, { primary: true }),
    );
  }

  return el(
    "div",
    { class: "stack" },
    el(
      "section",
      { class: "card" },
      el(
        "div",
        { class: "card__header" },
        el("h2", { class: "card__title", text: request.title || S.runView.title }),
        runStatusBadge(run.status),
      ),
      facts([
        [S.items.colType, label("contentType", request.content_type)],
        [S.editor.fieldMode, label("mode", request.production_mode)],
        [S.editor.fieldStyle, request.visual_style],
        [S.runView.scriptChars, `${request.script_chars.toLocaleString("vi-VN")} ${S.editor.chars}`],
        [S.runView.created, formatDateTime(run.created_at)],
        [S.runView.started, formatDateTime(run.started_at)],
        [S.runView.finished, formatDateTime(run.finished_at)],
        [S.runView.elapsed, elapsed !== null ? formatDuration(elapsed) : null],
        [S.runView.job, run.job_id ? el("span", { class: "mono", text: run.job_id }) : null],
      ]),
      actions.length ? el("div", { class: "row", style: "margin-top: 16px" }, actions) : null,
    ),
    run.error ? errorPanel(run.error) : null,
    run.result ? resultPanel(run.result) : null,
    card(
      `${S.runView.stages} (${run.progress.completed}/${run.progress.total})`,
      run.progress.reused ? alert("info", null, S.runView.reused) : null,
      stepper(run.progress),
    ),
  );
}

// For a failed stage the error names the last stage that completed, not the
// one that failed.
const STAGE_FAILED_CODE = "pipeline.script_to_video_stage_failed";

function errorPanel(error) {
  const stageTitle = error.code === STAGE_FAILED_CODE ? S.runView.errorLastStage : S.runView.errorStage;
  return el(
    "div",
    { class: "stack" },
    errorAlert(error, S.runView.errorTitle),
    facts([
      error.stage ? [stageTitle, label("stage", error.stage)] : null,
      error.cause_code ? [S.runView.errorCause, el("span", { class: "mono", text: error.cause_code })] : null,
    ]),
  );
}

function resultPanel(result) {
  return card(
    S.runView.resultTitle,
    facts([
      [S.item.status, label("status", result.item_status)],
      [S.item.qcStatus, qcBadge(result.qc_status)],
      [S.item.size, formatBytes(result.video_size_bytes)],
      [S.item.sha256, el("span", { class: "mono", title: result.video_sha256, text: shortId(result.video_sha256) })],
      [S.item.sceneCount, String(result.scene_count)],
      [S.item.audioDuration, formatDuration(result.audio_duration_ms)],
    ]),
  );
}
