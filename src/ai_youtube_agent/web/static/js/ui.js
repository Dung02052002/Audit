// DOM helpers. Data reaches the page only through textContent and attributes,
// never as HTML markup.

import { S, label } from "./strings.js";

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
}

export function announce(message) {
  const region = document.getElementById("live-status");
  if (!region) return;
  region.textContent = "";
  window.setTimeout(() => {
    region.textContent = message;
  }, 50);
}

// Page structure

export function pageHeader(title, sub, ...actions) {
  return el(
    "div",
    { class: "page-header" },
    el("div", {}, el("h1", { text: title }), sub ? el("p", { class: "page-header__sub", text: sub }) : null),
    actions.length ? el("div", { class: "page-header__actions" }, actions) : null,
  );
}

export function card(title, ...children) {
  return el("section", { class: "card" }, title ? el("h2", { class: "card__title", text: title }) : null, children);
}

export function cardWithHeader(title, actions, ...children) {
  return el(
    "section",
    { class: "card" },
    el("div", { class: "card__header" }, el("h2", { class: "card__title", text: title }), actions),
    children,
  );
}

export function facts(pairs) {
  return el(
    "dl",
    { class: "facts" },
    pairs
      .filter((pair) => pair)
      .map(([term, value]) => [el("dt", { text: term }), el("dd", {}, value === undefined || value === null || value === "" ? S.app.none : value)]),
  );
}

export function linkButton(href, text, { primary = false, download = false } = {}) {
  return el("a", { class: primary ? "button button--primary" : "button", href, download: download ? "" : null }, text);
}

export function button(text, onClick, { primary = false, type = "button", disabled = false } = {}) {
  return el("button", { class: primary ? "button button--primary" : "button", type, onClick, disabled }, text);
}

// States

export function loadingView(rows = 3) {
  const skeletons = [];
  for (let index = 0; index < rows; index += 1) {
    skeletons.push(el("div", { class: index === 0 ? "skeleton skeleton--block" : "skeleton" }));
  }
  return el("div", { class: "stack", "aria-busy": "true" }, el("span", { class: "visually-hidden", text: S.app.loading }), skeletons);
}

export function emptyView(title, body, action) {
  return el(
    "div",
    { class: "card state" },
    el("p", { class: "state__title", text: title }),
    body ? el("p", { class: "state__body", text: body }) : null,
    action || null,
  );
}

export function errorAlert(error, title = S.errors.title) {
  const fields = error && Array.isArray(error.fields) ? error.fields : [];
  return el(
    "div",
    { class: "alert alert--error", role: "alert" },
    el("span", { class: "alert__title", text: title }),
    el("span", { text: (error && error.message) || S.errors.generic }),
    error && error.code ? el("span", { class: "alert__code", text: `${S.errors.code}: ${error.code}` }) : null,
    fields.length
      ? el("ul", {}, fields.map((field) => el("li", { text: `${field.field}: ${field.message}` })))
      : null,
  );
}

export function errorView(error, onRetry) {
  return el(
    "div",
    { class: "stack" },
    errorAlert(error),
    onRetry ? el("div", { class: "row" }, button(S.app.retry, onRetry, { primary: true })) : null,
  );
}

export function alert(kind, title, body) {
  return el(
    "div",
    { class: `alert alert--${kind}`, role: kind === "error" ? "alert" : "status" },
    title ? el("span", { class: "alert__title", text: title }) : null,
    body ? el("span", { text: body }) : null,
  );
}

// Badges

const CONTENT_TONES = {
  draft: "",
  generating: "blue",
  testing: "indigo",
  preview_ready: "green",
  awaiting_approval: "amber",
  approved: "green",
  publishing: "blue",
  published: "green",
  rejected: "red",
  failed: "red",
};

const RUN_TONES = {
  queued: "",
  running: "blue",
  succeeded: "green",
  failed: "red",
  interrupted: "amber",
  waiting: "amber",
  cancelled: "",
  awaiting_approval: "amber",
  blocked: "red",
};

export function badge(text, tone = "") {
  return el("span", { class: tone ? `badge badge--${tone}` : "badge", text });
}

export function contentStatusBadge(status) {
  return badge(label("status", status), CONTENT_TONES[status] || "");
}

export function runStatusBadge(status) {
  return badge(label("run", status), RUN_TONES[status] || "");
}

export function qcBadge(status) {
  const tones = { pass: "green", warn: "amber", fail: "red" };
  return badge(label("qc", status), tones[status] || "");
}

// Stage stepper

export function stepper(progress) {
  const percent = progress.total ? Math.round((progress.completed / progress.total) * 100) : 0;
  const marks = { done: "✓", active: "•", failed: "!", skipped: "", pending: "", waiting: "…", blocked: "×" };
  return el(
    "div",
    { class: "stack" },
    el(
      "div",
      {
        class: "progress",
        role: "progressbar",
        "aria-valuemin": "0",
        "aria-valuemax": String(progress.total),
        "aria-valuenow": String(progress.completed),
        "aria-label": S.runView.progress,
      },
      el("div", { class: "progress__bar", style: `width: ${percent}%` }),
    ),
    el(
      "ol",
      { class: "stepper" },
      progress.stages.map((stage, index) =>
        el(
          "li",
          { class: "stepper__item" },
          el("span", { class: `stepper__dot stepper__dot--${stage.state}`, "aria-hidden": "true", text: marks[stage.state] || String(index + 1) }),
          el(
            "span",
            { class: `stepper__label stepper__label--${stage.state}` },
            label("stage", stage.id),
            el("span", { class: "visually-hidden", text: ` (${label("stageState", stage.state)})` }),
          ),
          el("span", { class: "stepper__time", text: stage.completed_at ? formatTime(stage.completed_at) : label("stageState", stage.state) }),
        ),
      ),
    ),
  );
}

// Formatters

const dateTime = new Intl.DateTimeFormat("vi-VN", { dateStyle: "short", timeStyle: "medium" });
const timeOnly = new Intl.DateTimeFormat("vi-VN", { timeStyle: "medium" });

export function formatDateTime(iso) {
  if (!iso) return S.app.none;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : dateTime.format(date);
}

export function formatTime(iso) {
  if (!iso) return S.app.none;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : timeOnly.format(date);
}

export function formatDuration(ms) {
  if (ms === undefined || ms === null || Number.isNaN(Number(ms))) return S.app.none;
  const totalSeconds = Math.max(0, Math.round(Number(ms) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

export function formatBytes(bytes) {
  if (bytes === undefined || bytes === null) return S.app.none;
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toLocaleString("vi-VN", { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
}

export function shortId(value) {
  if (!value) return S.app.none;
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

export function formatValue(value) {
  if (value === null || value === undefined) return S.app.none;
  if (typeof value === "boolean") return value ? S.app.yes : S.app.no;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
