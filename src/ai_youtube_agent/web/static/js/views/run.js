// One background run: live stage stepper, error panel and result. A run
// stopped by the paid-API budget (BUDGET-01) shows its quotes: approve or
// reject each, or "Duyệt và chạy lại" (approve the rest, then run again).

import { ApiError, api } from "../api.js";
import { S, label } from "../strings.js";
import {
  alert,
  announce,
  badge,
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
const BUDGET_STOPPED = new Set(["awaiting_approval", "blocked"]);

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
      if (!disposed) mount(body, runBody(run, { ...ctx, isMounted: () => !disposed }));
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

function runBody(run, ctx) {
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
    run.error ? errorPanel(run.error, run.status) : null,
    BUDGET_STOPPED.has(run.status) ? quotesPanel(run, ctx) : null,
    run.result ? resultPanel(run.result) : null,
    card(
      `${S.runView.stages} (${run.progress.completed}/${run.progress.total})`,
      run.progress.reused ? alert("info", null, S.runView.reused) : null,
      stepper(run.progress),
    ),
  );
}

// For a failed stage (and a budget stop) the error names the last stage that
// completed, not the one that failed.
const LAST_STAGE_CODES = new Set([
  "pipeline.script_to_video_stage_failed",
  "pipeline.script_to_video_awaiting_paid_approval",
  "pipeline.script_to_video_budget_blocked",
]);

function errorPanel(error, status) {
  const stageTitle = LAST_STAGE_CODES.has(error.code) ? S.runView.errorLastStage : S.runView.errorStage;
  const title = BUDGET_STOPPED.has(status) ? label("run", status) : S.runView.errorTitle;
  return el(
    "div",
    { class: "stack" },
    errorAlert(error, title),
    facts([
      error.stage ? [stageTitle, label("stage", error.stage)] : null,
      error.cause_code ? [S.runView.errorCause, el("span", { class: "mono", text: error.cause_code })] : null,
    ]),
  );
}

function pick(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : String(key);
}

function quoteTone(status) {
  const tones = { pending: "amber", approved: "blue", consumed: "green", rejected: "", expired: "", blocked: "red" };
  return tones[status] || "";
}

function money(amount, currency) {
  return amount !== null && amount !== undefined ? `${amount} ${currency || ""}`.trim() : null;
}

// The quotes of a run stopped by the budget: the ones its error names (each
// fetched by id, so none is missed), else the job's pending and blocked ones.
// Loaded when the panel is drawn.
function quotesPanel(run, ctx) {
  const slot = el("div", { class: "stack" }, loadingView(2));
  const wanted = new Set((run.error && run.error.quote_ids) || []);
  const status = el("div", { "aria-live": "polite" });
  let busy = false;

  async function load() {
    let quotes = [];
    try {
      if (wanted.size) {
        quotes = await Promise.all([...wanted].map((quoteId) => api.paidQuote(quoteId)));
      } else if (run.job_id) {
        const data = await api.paidQuotes({ job_id: run.job_id, limit: 200 });
        quotes = (data.quotes || []).filter((quote) => quote.status === "pending" || quote.status === "blocked");
      }
    } catch (error) {
      if (ctx.isMounted()) mount(slot, errorView(error, load));
      return;
    }
    if (!ctx.isMounted()) return;
    draw(quotes);
  }

  async function act(action, message) {
    if (busy) return;
    busy = true;
    try {
      await action();
      announce(message);
      await load();
    } catch (error) {
      if (ctx.isMounted()) mount(status, errorAlert(error));
    } finally {
      busy = false;
    }
  }

  async function approveAndRetry(quotes) {
    if (busy) return;
    busy = true;
    try {
      for (const quote of quotes) {
        if (quote.status === "pending" && quote.price_known) {
          await api.approvePaidQuote(quote.id, quote);
        }
      }
      const response = await api.retryRun(run.run_id);
      announce(S.budget.retried);
      if (response.data && response.data.run_id) ctx.navigate(`#/runs/${encodeURIComponent(response.data.run_id)}`);
    } catch (error) {
      if (ctx.isMounted()) {
        mount(status, errorAlert(error));
        await load();
      }
    } finally {
      busy = false;
    }
  }

  function quoteCard(quote) {
    const actions = [];
    if (quote.status === "pending" && quote.price_known) {
      actions.push(
        button(S.budget.approve, () => act(() => api.approvePaidQuote(quote.id, quote), S.budget.approved), { primary: true }),
        button(S.budget.reject, () => act(() => api.rejectPaidQuote(quote.id), S.budget.rejected)),
      );
    } else if (quote.status === "approved") {
      actions.push(button(S.budget.reject, () => act(() => api.rejectPaidQuote(quote.id), S.budget.rejected)));
    }
    const units = `${quote.unit_count} ${pick(S.budget.unitNames, quote.unit)}`;
    return el(
      "section",
      { class: "card" },
      el(
        "div",
        { class: "card__header" },
        el("h3", { class: "card__title", text: `${quote.provider} · ${pick(S.budget.taskType, quote.task_type)}` }),
        badge(pick(S.budget.quoteStatus, quote.status), quoteTone(quote.status)),
      ),
      facts([
        [S.budget.provider, el("span", { class: "mono", text: quote.provider })],
        [S.budget.model, el("span", { class: "mono", text: quote.model })],
        [S.budget.task, pick(S.budget.taskType, quote.task_type)],
        quote.scene_id ? [S.budget.scene, el("span", { class: "mono", text: quote.scene_id })] : null,
        [S.budget.units, units],
        [S.budget.unitPrice, money(quote.unit_price, quote.currency)],
        [S.budget.estimate, quote.price_known ? money(quote.estimated_amount, quote.currency) : S.budget.priceUnknownNoApprove],
        [S.budget.priceSource, quote.price_source],
        [S.budget.conditions, quote.conditions.length ? quote.conditions.join("; ") : null],
        [S.budget.unknowns, quote.unknowns.length ? quote.unknowns.join("; ") : null],
        quote.blocked_reason ? [S.budget.reason, pick(S.budget.reasons, quote.blocked_reason)] : null,
        [S.budget.expiresAt, formatDateTime(quote.expires_at)],
      ]),
      actions.length ? el("div", { class: "row", style: "margin-top: 16px" }, actions) : null,
    );
  }

  function draw(quotes) {
    const blocked = run.status === "blocked";
    const approvable = quotes.filter((quote) => quote.status === "pending" && quote.price_known);
    const footer = [];
    if (!blocked && quotes.length && quotes.every((quote) => quote.price_known)) {
      footer.push(button(S.budget.approveAndRetry, () => approveAndRetry(approvable), { primary: true }));
    }
    if (blocked) {
      footer.push(linkButton("#/settings", S.budget.openSettings));
      footer.push(button(S.budget.retry, () => approveAndRetry([])));
    }
    mount(
      slot,
      card(
        S.budget.quotesTitle,
        blocked
          ? alert("error", S.budget.blockedIntro, S.budget.localSuggestion)
          : alert("warning", null, S.budget.awaitingIntro),
        quotes.length ? el("div", { class: "stack" }, quotes.map(quoteCard)) : el("p", { class: "muted", text: S.budget.noQuotes }),
        status,
        footer.length ? el("div", { class: "row", style: "margin-top: 16px" }, footer) : null,
      ),
    );
  }

  load();
  return slot;
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
