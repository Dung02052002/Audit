// Entry point: hash router, shared context and the demo-limits banner.
//
// Routes: #/  #/items  #/new  #/items/:id  #/items/:id/edit  #/runs/:runId
// #/settings; anything else is "not found". A view is
// render(container, ctx) and may return a dispose function, called when the
// route changes (it stops pollers and aborts requests).

import { api } from "./api.js";
import { S } from "./strings.js";
import { el, emptyView, linkButton, mount } from "./ui.js";
import * as editorView from "./views/editor.js";
import * as itemView from "./views/item.js";
import * as itemsView from "./views/items.js";
import * as overviewView from "./views/overview.js";
import * as runView from "./views/run.js";
import * as settingsView from "./views/settings.js";

const MOCK_IMAGE_PROVIDERS = new Set(["mock", "local_deterministic_png"]);

const routes = [
  { pattern: /^\/?$/, view: overviewView, nav: "overview" },
  { pattern: /^\/items$/, view: itemsView, nav: "items" },
  { pattern: /^\/new$/, view: editorView, nav: "new" },
  { pattern: /^\/items\/([^/]+)\/edit$/, view: editorView, nav: "items", keys: ["itemId"] },
  { pattern: /^\/items\/([^/]+)$/, view: itemView, nav: "items", keys: ["itemId"] },
  { pattern: /^\/runs\/([^/]+)$/, view: runView, nav: "items", keys: ["runId"] },
  { pattern: /^\/settings$/, view: settingsView, nav: "settings" },
];

// Capabilities and settings are read once and shared; a failed read is retried
// on the next use.
const shared = {};

function once(name, loader) {
  return () => {
    if (!shared[name]) {
      shared[name] = loader().catch((error) => {
        delete shared[name];
        throw error;
      });
    }
    return shared[name];
  };
}

const context = {
  capabilities: once("capabilities", () => api.capabilities()),
  settings: once("settings", () => api.settings()),
  navigate(hash) {
    window.location.hash = hash;
  },
};

let dispose = null;

function parseHash() {
  const raw = window.location.hash.replace(/^#/, "");
  const [path, search = ""] = raw.split("?");
  return { path: path || "/", query: new URLSearchParams(search) };
}

function render() {
  if (dispose) {
    try {
      dispose();
    } catch (error) {
      // A view that fails to clean up must not block the next one.
    }
    dispose = null;
  }
  const { path, query } = parseHash();
  const container = document.getElementById("view");
  let match = null;
  let route = null;
  for (const candidate of routes) {
    match = candidate.pattern.exec(path);
    if (match) {
      route = candidate;
      break;
    }
  }
  const params = {};
  if (route) {
    try {
      (route.keys || []).forEach((key, index) => {
        params[key] = decodeURIComponent(match[index + 1]);
      });
    } catch (error) {
      // A malformed escape in the hash (URIError) reads as an unknown page.
      route = null;
    }
  }

  for (const link of document.querySelectorAll("[data-nav]")) {
    if (route && link.dataset.nav === route.nav) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }

  if (!route) {
    document.title = `${S.app.notFoundTitle} · ${S.app.title}`;
    mount(container, emptyView(S.app.notFoundTitle, S.app.notFoundBody, linkButton("#/", S.app.goHome, { primary: true })));
    return;
  }
  const result = route.view.render(container, { ...context, params, query, path });
  dispose = typeof result === "function" ? result : null;
  container.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

async function showDemoBanner() {
  const slot = document.getElementById("demo-banner");
  let caps;
  try {
    caps = await context.capabilities();
  } catch (error) {
    return;
  }
  const notes = [];
  if (caps.voice_provider === "mock") notes.push(S.demo.mockVoice);
  if (MOCK_IMAGE_PROVIDERS.has(caps.image_provider)) notes.push(S.demo.mockImage);
  const aiFilmReason = caps.unavailable_reasons && caps.unavailable_reasons.ai_film;
  if (aiFilmReason) notes.push(`${S.demo.aiFilm}: ${aiFilmReason}`);
  if (caps.render_provider === "mock") notes.push(S.demo.mockRender);
  if (!notes.length) return;
  mount(
    slot,
    el(
      "div",
      { class: "alert alert--warning" },
      el("span", { class: "alert__title", text: S.demo.title }),
      el("ul", {}, notes.map((note) => el("li", { text: note }))),
    ),
  );
  slot.hidden = false;
}

function start() {
  const skip = document.querySelector(".skip-link");
  if (skip) {
    skip.addEventListener("click", (event) => {
      event.preventDefault();
      document.getElementById("view").focus();
    });
  }
  window.addEventListener("hashchange", render);
  render();
  showDemoBanner();
}

start();
