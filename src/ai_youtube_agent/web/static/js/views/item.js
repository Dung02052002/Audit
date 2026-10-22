// One content item, in tabs: overview, jobs & log, scenes, preview, results
// and edit. The overview and jobs tabs refresh while a run is active.

import { ApiError, api, links } from "../api.js";
import { S, label } from "../strings.js";
import {
  alert,
  badge,
  button,
  card,
  cardWithHeader,
  contentStatusBadge,
  el,
  emptyView,
  errorView,
  facts,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatValue,
  linkButton,
  loadingView,
  mount,
  qcBadge,
  runStatusBadge,
  stepper,
} from "../ui.js";
import { createPoller } from "../poller.js";
import { EDITABLE_STATUSES } from "./editor.js";

const TABS = [
  ["overview", S.item.tabOverview],
  ["jobs", S.item.tabJobs],
  ["scenes", S.item.tabScenes],
  ["preview", S.item.tabPreview],
  ["results", S.item.tabResults],
  ["edit", S.item.tabEdit],
];
const LIVE_TABS = new Set(["overview", "jobs"]);
const ACTIVE_JOB = new Set(["queued", "running"]);

export function render(container, ctx) {
  const itemId = ctx.params.itemId;
  const requested = ctx.query.get("tab");
  const tab = TABS.some(([key]) => key === requested) ? requested : "overview";
  const controller = new AbortController();
  const base = `#/items/${encodeURIComponent(itemId)}`;
  const connection = el("div", { "aria-live": "polite" });
  let poller = null;

  function frame(detail, content) {
    const item = detail.item;
    document.title = `${item.title} · ${S.app.title}`;
    mount(
      container,
      el(
        "div",
        { class: "page-header" },
        el(
          "div",
          {},
          el("h1", { text: item.title }),
          el("div", { class: "row page-header__sub" }, contentStatusBadge(item.status), el("span", { class: "muted", text: label("contentType", item.content_type) })),
        ),
        detail.active_run_id
          ? el("div", { class: "page-header__actions" }, linkButton(`#/runs/${encodeURIComponent(detail.active_run_id)}`, S.item.activeRun, { primary: true }))
          : null,
      ),
      el(
        "nav",
        { class: "tabs", "aria-label": item.title, role: "tablist" },
        TABS.map(([key, text]) =>
          el("a", { href: `${base}?tab=${key}`, role: "tab", "aria-selected": key === tab ? "true" : "false", text }),
        ),
      ),
      connection,
      el("div", { role: "tabpanel" }, content),
    );
  }

  function tabContent(detail) {
    switch (tab) {
      case "jobs":
        return jobsTab(detail);
      case "scenes":
        return scenesTab(detail, controller.signal);
      case "preview":
        return previewTab(detail, ctx);
      case "results":
        return resultsTab(detail);
      case "edit":
        return editTab(detail, controller.signal);
      default:
        return overviewTab(detail);
    }
  }

  function showError(error) {
    if (error instanceof ApiError && error.code === "domain.content_item_not_found") {
      mount(container, emptyView(error.message, null, linkButton("#/items", S.nav.items, { primary: true })));
      return;
    }
    mount(container, errorView(error, start));
  }

  function start() {
    mount(container, loadingView(4));
    if (LIVE_TABS.has(tab)) {
      poller = createPoller({
        load: (signal) => api.item(itemId, { signal }),
        isActive: (detail) => Boolean(detail.active_run_id) || Boolean(detail.latest_job && ACTIVE_JOB.has(detail.latest_job.status)),
        changeKey: (detail) => `${detail.item.updated_at}|${detail.latest_job ? detail.latest_job.updated_at : ""}|${detail.active_run_id || ""}`,
        onData: (detail) => frame(detail, tabContent(detail)),
        onError: showError,
        onStatus: (state) => {
          if (state === "reconnecting") mount(connection, alert("warning", S.errors.reconnecting));
          else if (state === "gave_up") {
            mount(connection, el("div", { class: "row" }, alert("error", S.errors.gaveUp), button(S.app.retry, () => poller.restart(), { primary: true })));
          } else connection.replaceChildren();
        },
      });
      poller.start();
      return;
    }
    api
      .item(itemId, { signal: controller.signal })
      .then((detail) => {
        if (!controller.signal.aborted) frame(detail, tabContent(detail));
      })
      .catch((error) => {
        if (!controller.signal.aborted) showError(error);
      });
  }

  start();
  return () => {
    controller.abort();
    if (poller) poller.stop();
  };
}

function overviewTab(detail) {
  const item = detail.item;
  const job = detail.latest_job;
  return el(
    "div",
    { class: "stack" },
    el(
      "div",
      { class: "grid grid--two" },
      card(
        S.item.tabOverview,
        facts([
          [S.item.type, label("contentType", item.content_type)],
          [S.item.status, contentStatusBadge(item.status)],
          [S.item.channel, el("span", { class: "mono", text: item.channel_id })],
          [S.item.created, formatDateTime(item.created_at)],
          [S.item.updated, formatDateTime(item.updated_at)],
        ]),
      ),
      card(
        S.item.latestJob,
        job
          ? facts([
              [S.item.status, runStatusBadge(job.status)],
              [S.item.lastStage, job.last_stage ? label("stage", job.last_stage) : null],
              [S.item.attempts, String(job.attempts)],
              [S.item.lastError, job.last_error ? el("span", { class: "mono", text: job.last_error }) : null],
              [S.item.updated, formatDateTime(job.updated_at)],
            ])
          : el("p", { class: "muted", text: S.item.noJobs }),
        job && job.status === "interrupted" ? alert("warning", null, label("run", "interrupted")) : null,
      ),
    ),
    detail.progress ? card(`${S.runView.stages} (${detail.progress.completed}/${detail.progress.total})`, stepper(detail.progress)) : null,
  );
}

function jobsTab(detail) {
  if (!detail.jobs.length) return emptyView(S.item.noJobs);
  const jobs = [...detail.jobs].reverse();
  return el(
    "div",
    { class: "stack" },
    jobs.map((job) =>
      cardWithHeader(
        job.kind,
        runStatusBadge(job.status),
        facts([
          [S.runView.job, el("span", { class: "mono", text: job.id })],
          [S.item.attempts, String(job.attempts)],
          [S.item.lastError, job.last_error ? el("span", { class: "mono", text: job.last_error }) : null],
          [S.item.created, formatDateTime(job.created_at)],
          [S.item.updated, formatDateTime(job.updated_at)],
        ]),
        el(
          "div",
          { class: "checkpoints" },
          el("h3", { class: "card__title", text: `${S.item.checkpoints} (${job.checkpoints.length})` }),
          job.checkpoints.length ? checkpointsTable(job.checkpoints) : el("p", { class: "muted", text: S.item.noCheckpoints }),
        ),
      ),
    ),
  );
}

function checkpointsTable(checkpoints) {
  return el(
    "div",
    { class: "table-wrap" },
    el(
      "table",
      { class: "table table--stack" },
      el("thead", {}, el("tr", {}, ["#", S.item.step, S.item.time, S.item.data].map((text) => el("th", { scope: "col", text })))),
      el(
        "tbody",
        {},
        checkpoints.map((checkpoint) =>
          el(
            "tr",
            {},
            el("td", { "data-label": "#", text: String(checkpoint.sequence) }),
            el(
              "td",
              { "data-label": S.item.step },
              el("div", { text: label("stage", checkpoint.step) }),
              el("div", { class: "mono muted", text: checkpoint.step }),
            ),
            el("td", { "data-label": S.item.time, text: formatDateTime(checkpoint.created_at) }),
            el(
              "td",
              { "data-label": S.item.data },
              el(
                "ul",
                { class: "data-list" },
                Object.entries(checkpoint.data || {}).map(([key, value]) => el("li", { text: `${key}: ${formatValue(value)}` })),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

function scenesTab(detail, signal) {
  const slot = el("div", {}, loadingView(3));
  const portrait = detail.item.content_type === "shorts";
  async function load() {
    mount(slot, loadingView(3));
    let data;
    try {
      data = await api.scenes(detail.item.id, { signal });
    } catch (error) {
      if (!signal.aborted) mount(slot, errorView(error, load));
      return;
    }
    if (signal.aborted) return;
    if (!data.scenes.length) {
      mount(slot, emptyView(S.item.noScenes));
      return;
    }
    mount(
      slot,
      el(
        "div",
        { class: "stack" },
        el("p", { class: "muted", text: S.item.scenesSource[data.source] || "" }),
        el("div", { class: "grid grid--scenes" }, data.scenes.map((scene) => sceneCard(scene, portrait))),
      ),
    );
  }
  load();
  return slot;
}

function sceneCard(scene, portrait) {
  let media;
  if (scene.image && scene.image.media_type.startsWith("video/")) {
    media = el("video", { src: scene.image.stream_url, muted: true, controls: true, preload: "metadata" });
  } else if (scene.image) {
    media = el("img", { src: scene.image.stream_url, alt: scene.title || scene.scene_id, loading: "lazy" });
  } else {
    media = el("span", { text: S.item.sceneNoImage });
  }
  const window_ =
    scene.start_ms !== null && scene.start_ms !== undefined
      ? `${formatDuration(scene.start_ms)} – ${formatDuration(scene.end_ms)} (${formatDuration(scene.duration_ms)})`
      : null;
  return el(
    "article",
    { class: "card scene" },
    el("div", { class: portrait ? "scene__media scene__media--portrait" : "scene__media" }, media),
    el(
      "div",
      { class: "scene__body" },
      el("div", { class: "scene__title", text: `${scene.index + 1}. ${scene.title || scene.scene_id}` }),
      window_ ? el("div", { class: "muted", text: window_ }) : null,
      el("div", { class: "row" }, badge(label("sceneStatus", scene.status)), scene.reused ? badge(S.item.sceneReused, "indigo") : null),
      scene.narration ? el("details", {}, el("summary", { text: S.item.narration }), el("p", { text: scene.narration })) : null,
      scene.visual_prompt ? el("details", {}, el("summary", { text: S.item.visualPrompt }), el("p", { text: scene.visual_prompt })) : null,
    ),
  );
}

function previewTab(detail, ctx) {
  const video = detail.artifacts.video;
  if (!video) return emptyView(S.item.noVideo, S.item.noVideoBody);
  const status = el("div", { "aria-live": "polite" });
  const player = el("video", { class: "player", controls: true, preload: "metadata", src: video.stream_url, playsinline: true });
  if (detail.artifacts.subtitles) {
    player.append(el("track", { kind: "subtitles", src: links.subtitlesVtt(detail.item.id), srclang: "vi", label: S.item.subtitles, default: true }));
  }
  // The current render provider says nothing about the renderer that made
  // this file, so the hint stays conditional and shows only on a real error.
  player.addEventListener("error", () => {
    mount(status, alert("warning", S.item.playbackError, S.item.playbackHint));
  });
  const thumbnail = detail.artifacts.thumbnail;
  return el(
    "div",
    { class: "stack" },
    card(null, player, status, el("div", { class: "row", style: "margin-top: 12px" }, linkButton(links.download(detail.item.id), S.item.download, { primary: true, download: true }))),
    thumbnail ? card(S.item.thumbnail, el("img", { class: "thumb", src: thumbnail.stream_url, alt: S.item.thumbnail })) : null,
  );
}

function resultsTab(detail) {
  const video = detail.artifacts.video;
  const checkpoints = latestScriptCheckpoints(detail);
  const qc = checkpoints["qc.completed"];
  const voice = checkpoints["voice.generated"];
  const scenes = checkpoints["scenes.planned"];
  const others = ["audio", "subtitles", "thumbnail", "metadata"].filter((kind) => detail.artifacts[kind]);
  return el(
    "div",
    { class: "stack" },
    video
      ? cardWithHeader(
          S.item.videoFile,
          linkButton(links.download(detail.item.id), S.item.download, { primary: true, download: true }),
          facts([
            [S.item.version, `v${video.version}`],
            [S.item.size, formatBytes(video.size_bytes)],
            [S.item.mediaType, video.media_type],
            [S.item.sha256, el("span", { class: "mono", text: video.sha256 })],
            [S.item.created, formatDateTime(video.created_at)],
          ]),
        )
      : emptyView(S.item.noVideo, S.item.noVideoBody),
    card(
      S.item.qc,
      facts([
        [S.item.status, contentStatusBadge(detail.item.status)],
        [S.item.qcStatus, qc && qc.overall_status ? qcBadge(qc.overall_status) : null],
        [S.item.sceneCount, scenes ? formatValue(scenes.scene_count) : null],
        [S.item.audioDuration, voice ? formatDuration(voice.duration_ms) : null],
      ]),
    ),
    others.length
      ? card(
          S.item.otherArtifacts,
          facts(
            others.map((kind) => {
              const artifact = detail.artifacts[kind];
              return [label("artifactKind", kind), `v${artifact.version} · ${artifact.media_type} · ${formatBytes(artifact.size_bytes)}`];
            }),
          ),
        )
      : null,
  );
}

function latestScriptCheckpoints(detail) {
  const latest = detail.latest_job;
  const job = latest ? detail.jobs.find((candidate) => candidate.id === latest.id) : null;
  const found = {};
  if (!job) return found;
  for (const checkpoint of job.checkpoints) found[checkpoint.step] = checkpoint.data || {};
  return found;
}

function editTab(detail, signal) {
  const item = detail.item;
  const editable = EDITABLE_STATUSES.has(item.status);
  const slot = el("div", {}, loadingView(2));
  api
    .script(item.id, { signal })
    .then((script) => {
      if (!signal.aborted) {
        mount(slot, card(`${S.editor.storedScript} · v${script.version}`, el("pre", { class: "textarea", style: "white-space: pre-wrap; min-height: 0", text: script.text })));
      }
    })
    .catch((error) => {
      if (signal.aborted) return;
      if (error instanceof ApiError && error.code === "domain.script_not_found") mount(slot, emptyView(S.item.noScript));
      else mount(slot, errorView(error));
    });
  return el(
    "div",
    { class: "stack" },
    editable
      ? el("div", { class: "row" }, linkButton(`#/items/${encodeURIComponent(item.id)}/edit`, S.item.editAction, { primary: true }))
      : alert("warning", S.item.editBlocked, `${S.item.status}: ${label("status", item.status)}`),
    slot,
  );
}
