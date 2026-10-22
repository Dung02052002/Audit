// Content list with status and type filters (kept in the hash query).

import { api } from "../api.js";
import { S, label } from "../strings.js";
import {
  badge,
  button,
  contentStatusBadge,
  el,
  emptyView,
  errorView,
  formatBytes,
  formatDateTime,
  linkButton,
  loadingView,
  mount,
  pageHeader,
  runStatusBadge,
} from "../ui.js";

const STATUSES = [
  "draft",
  "generating",
  "testing",
  "preview_ready",
  "awaiting_approval",
  "approved",
  "publishing",
  "published",
  "rejected",
  "failed",
];
const TYPES = ["shorts", "longform"];

export function render(container, ctx) {
  const controller = new AbortController();
  const filters = {
    status: ctx.query.get("status") || "",
    content_type: ctx.query.get("content_type") || "",
  };
  document.title = `${S.items.title} · ${S.app.title}`;

  function setFilter(name, value) {
    const query = new URLSearchParams();
    const next = { ...filters, [name]: value };
    for (const [key, val] of Object.entries(next)) if (val) query.set(key, val);
    const text = query.toString();
    ctx.navigate(text ? `#/items?${text}` : "#/items");
  }

  function filterBar() {
    const select = (id, text, options, groupName, value, name) =>
      el(
        "div",
        { class: "field" },
        el("label", { for: id, text }),
        el(
          "select",
          { id, class: "select", onChange: (event) => setFilter(name, event.target.value) },
          el("option", { value: "", text: S.items.all }),
          options.map((option) => el("option", { value: option, text: label(groupName, option), selected: option === value })),
        ),
      );
    return el(
      "div",
      { class: "card" },
      el(
        "div",
        { class: "form-grid" },
        select("filter-status", S.items.filterStatus, STATUSES, "status", filters.status, "status"),
        select("filter-type", S.items.filterType, TYPES, "contentType", filters.content_type, "content_type"),
      ),
    );
  }

  async function load() {
    const header = pageHeader(S.items.title, S.items.sub, button(S.app.refresh, load), linkButton("#/new", S.nav.new, { primary: true }));
    mount(container, header, el("div", { class: "stack" }, filterBar(), loadingView(3)));
    let data;
    try {
      data = await api.items({ ...filters, limit: 200 }, { signal: controller.signal });
    } catch (error) {
      if (controller.signal.aborted) return;
      mount(container, header, el("div", { class: "stack" }, filterBar(), errorView(error, load)));
      return;
    }
    if (controller.signal.aborted) return;
    const filtered = Boolean(filters.status || filters.content_type);
    let body;
    if (!data.items.length) {
      body = filtered
        ? emptyView(S.items.emptyFiltered, null, linkButton("#/items", S.items.all))
        : emptyView(S.items.emptyTitle, S.items.emptyBody, linkButton("#/new", S.nav.new, { primary: true }));
    } else {
      body = el("section", { class: "card" }, el("p", { class: "muted", text: `${data.total} ${S.items.count}` }), itemsTable(data.items));
    }
    mount(container, header, el("div", { class: "stack" }, filterBar(), body));
  }

  load();
  return () => controller.abort();
}

function itemsTable(items) {
  const head = [S.items.colTitle, S.items.colType, S.items.colStatus, S.items.colJob, S.items.colVideo, S.items.colUpdated];
  return el(
    "div",
    { class: "table-wrap" },
    el(
      "table",
      { class: "table table--stack" },
      el("thead", {}, el("tr", {}, head.map((text) => el("th", { scope: "col", text })))),
      el(
        "tbody",
        {},
        items.map((item) => {
          const job = item.latest_job;
          return el(
            "tr",
            {},
            el(
              "td",
              { class: "table__title", "data-label": S.items.colTitle },
              el("a", { href: `#/items/${encodeURIComponent(item.id)}`, text: item.title }),
            ),
            el("td", { "data-label": S.items.colType, text: label("contentType", item.content_type) }),
            el("td", { "data-label": S.items.colStatus }, contentStatusBadge(item.status)),
            el(
              "td",
              { "data-label": S.items.colJob },
              job
                ? el(
                    "div",
                    { class: "row" },
                    item.active_run_id
                      ? el("a", { href: `#/runs/${encodeURIComponent(item.active_run_id)}` }, runStatusBadge(job.status))
                      : runStatusBadge(job.status),
                    job.last_stage ? el("span", { class: "muted", text: label("stage", job.last_stage) }) : null,
                  )
                : item.active_run_id
                  ? el("a", { href: `#/runs/${encodeURIComponent(item.active_run_id)}` }, badge(S.items.running, "blue"))
                  : S.app.none,
            ),
            el("td", { "data-label": S.items.colVideo, text: item.video ? `v${item.video.version} · ${formatBytes(item.video.size_bytes)}` : S.app.none }),
            el("td", { "data-label": S.items.colUpdated, text: formatDateTime(item.updated_at) }),
          );
        }),
      ),
    ),
  );
}
