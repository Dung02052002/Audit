// Overview: counts, active runs, recent items and failures, capabilities.

import { api } from "../api.js";
import { S, label } from "../strings.js";
import {
  badge,
  button,
  card,
  cardWithHeader,
  contentStatusBadge,
  el,
  errorView,
  formatDateTime,
  linkButton,
  loadingView,
  mount,
  pageHeader,
  runStatusBadge,
} from "../ui.js";

const LIST_LIMIT = 200;
const RECENT = 5;

export function render(container, ctx) {
  const controller = new AbortController();
  document.title = `${S.overview.title} · ${S.app.title}`;

  async function load() {
    mount(container, pageHeader(S.overview.title, S.overview.sub), loadingView(4));
    let items;
    let runs;
    let caps;
    try {
      [items, runs, caps] = await Promise.all([
        api.items({ limit: LIST_LIMIT }, { signal: controller.signal }),
        api.runs({ signal: controller.signal }),
        ctx.capabilities(),
      ]);
    } catch (error) {
      if (controller.signal.aborted) return;
      mount(container, pageHeader(S.overview.title, S.overview.sub), errorView(error, load));
      return;
    }
    if (controller.signal.aborted) return;
    mount(
      container,
      pageHeader(
        S.overview.title,
        S.overview.sub,
        button(S.app.refresh, load),
        linkButton("#/new", S.nav.new, { primary: true }),
      ),
      el(
        "div",
        { class: "stack" },
        statsCard(items),
        el("div", { class: "grid grid--two" }, activeRunsCard(runs.runs), failuresCard(runs.runs, items.items)),
        el("div", { class: "grid grid--two" }, recentItemsCard(items.items), capabilitiesCard(caps)),
      ),
    );
  }

  load();
  return () => controller.abort();
}

function statsCard(items) {
  const byStatus = new Map();
  let shorts = 0;
  let longform = 0;
  for (const item of items.items) {
    byStatus.set(item.status, (byStatus.get(item.status) || 0) + 1);
    if (item.content_type === "shorts") shorts += 1;
    else longform += 1;
  }
  const stat = (value, text) =>
    el("div", { class: "card stat" }, el("span", { class: "stat__value", text: String(value) }), el("span", { class: "stat__label", text }));
  return el(
    "div",
    { class: "stack" },
    el("div", { class: "grid grid--stats" }, stat(items.total, S.overview.total), stat(shorts, S.overview.shorts), stat(longform, S.overview.longform)),
    card(
      S.overview.byStatus,
      byStatus.size
        ? el(
            "div",
            { class: "row" },
            [...byStatus.entries()].map(([status, count]) =>
              el("a", { href: `#/items?status=${encodeURIComponent(status)}`, class: "row" }, contentStatusBadge(status), el("span", { text: String(count) })),
            ),
          )
        : el("p", { class: "muted", text: S.overview.noItems }),
      items.total > items.items.length ? el("p", { class: "muted", text: S.overview.countNote }) : null,
    ),
  );
}

function activeRunsCard(runs) {
  const active = runs.filter((run) => run.status === "queued" || run.status === "running");
  return card(
    S.overview.activeRuns,
    active.length
      ? el(
          "ul",
          { class: "list" },
          active.map((run) =>
            el(
              "li",
              {},
              el(
                "div",
                { class: "list__main" },
                el("a", { class: "list__title", href: `#/runs/${encodeURIComponent(run.run_id)}`, text: run.request.title || run.run_id }),
                el("div", {
                  class: "list__meta",
                  text: run.progress.current_stage
                    ? `${label("stage", run.progress.current_stage)} · ${run.progress.completed}/${run.progress.total}`
                    : formatDateTime(run.created_at),
                }),
              ),
              runStatusBadge(run.status),
            ),
          ),
        )
      : el("p", { class: "muted", text: S.overview.noActiveRuns }),
  );
}

function failuresCard(runs, items) {
  const failures = [];
  for (const run of runs) {
    if (run.status === "failed" && run.error) {
      failures.push({
        href: `#/runs/${encodeURIComponent(run.run_id)}`,
        title: run.request.title || run.run_id,
        meta: `${run.error.code} · ${formatDateTime(run.finished_at)}`,
        status: run.status,
        at: run.finished_at || run.created_at,
      });
    }
  }
  for (const item of items) {
    const job = item.latest_job;
    if (job && (job.status === "failed" || job.status === "interrupted")) {
      failures.push({
        href: `#/items/${encodeURIComponent(item.id)}?tab=jobs`,
        title: item.title,
        meta: `${job.last_error || label("run", job.status)} · ${formatDateTime(job.updated_at)}`,
        status: job.status,
        at: job.updated_at,
      });
    }
  }
  failures.sort((a, b) => String(b.at).localeCompare(String(a.at)));
  const recent = failures.slice(0, RECENT);
  return card(
    S.overview.recentFailures,
    recent.length
      ? el(
          "ul",
          { class: "list" },
          recent.map((failure) =>
            el(
              "li",
              {},
              el(
                "div",
                { class: "list__main" },
                el("a", { class: "list__title", href: failure.href, text: failure.title }),
                el("div", { class: "list__meta", text: failure.meta }),
              ),
              runStatusBadge(failure.status),
            ),
          ),
        )
      : el("p", { class: "muted", text: S.overview.noFailures }),
  );
}

function recentItemsCard(items) {
  const recent = items.slice(0, RECENT);
  return cardWithHeader(
    S.overview.recentItems,
    items.length ? el("a", { href: "#/items", text: S.overview.viewAll }) : null,
    recent.length
      ? el(
          "ul",
          { class: "list" },
          recent.map((item) =>
            el(
              "li",
              {},
              el(
                "div",
                { class: "list__main" },
                el("a", { class: "list__title", href: `#/items/${encodeURIComponent(item.id)}`, text: item.title }),
                el("div", { class: "list__meta", text: `${label("contentType", item.content_type)} · ${formatDateTime(item.updated_at)}` }),
              ),
              contentStatusBadge(item.status),
            ),
          ),
        )
      : el("p", { class: "muted", text: S.overview.noItems }),
  );
}

export function capabilitiesCard(caps) {
  const row = (name, provider, available) =>
    el(
      "tr",
      {},
      el("td", { "data-label": "", class: "table__title", text: name }),
      el("td", { "data-label": S.capabilities.provider, class: "mono", text: provider }),
      el("td", { "data-label": S.capabilities.available }, available ? badge(S.app.yes, "green") : badge(S.app.no, "red")),
    );
  const reasons = Object.entries(caps.unavailable_reasons || {});
  return card(
    S.overview.capabilities,
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        { class: "table table--stack" },
        el(
          "thead",
          {},
          el("tr", {}, el("th", { text: "" }), el("th", { text: S.capabilities.provider }), el("th", { text: S.capabilities.available })),
        ),
        el(
          "tbody",
          {},
          row(S.capabilities.voice, caps.voice_provider, caps.voice_available),
          row(S.capabilities.image, caps.image_provider, caps.image_available),
          row(S.capabilities.clip, caps.video_clip_provider, caps.video_clip_available),
          row(S.capabilities.render, caps.render_provider, caps.render_available),
        ),
      ),
    ),
    el(
      "p",
      {},
      el("strong", { text: `${S.capabilities.supportedModes}: ` }),
      caps.supported_modes.length ? caps.supported_modes.map((mode) => label("mode", mode)).join(", ") : S.app.none,
    ),
    reasons.length
      ? el(
          "ul",
          { class: "muted" },
          reasons.map(([mode, reason]) => el("li", { text: `${label("mode", mode)}: ${reason}` })),
        )
      : null,
  );
}
