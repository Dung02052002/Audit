// Script editor: new video (#/new) or edit and regenerate (#/items/:id/edit).
// "Phân tích" shows the plan only; "Tạo video" queues a background run.

import { ApiError, api } from "../api.js";
import { S, label } from "../strings.js";
import {
  alert,
  announce,
  card,
  contentStatusBadge,
  el,
  errorAlert,
  errorView,
  facts,
  formatDuration,
  linkButton,
  loadingView,
  mount,
  pageHeader,
} from "../ui.js";

export const EDITABLE_STATUSES = new Set(["draft", "generating", "testing", "preview_ready", "failed"]);
const MAX_FILE_BYTES = 1024 * 1024;
const STYLES = ["cinematic_story", "cinematic_watercolor", "historical_cinematic", "documentary", "anime_storybook"];
const TYPES = ["shorts", "longform"];
const MODES = ["story_video", "ai_film"];

export function render(container, ctx) {
  const controller = new AbortController();
  const itemId = ctx.params.itemId || null;
  const title = itemId ? S.editor.editTitle : S.editor.newTitle;
  const sub = itemId ? S.editor.editSub : S.editor.newSub;
  document.title = `${title} · ${S.app.title}`;

  async function load() {
    mount(container, pageHeader(title, sub), loadingView(3));
    let caps;
    let item = null;
    let script = null;
    try {
      const capsPromise = ctx.capabilities();
      if (itemId) {
        const [detail, stored] = await Promise.all([
          api.item(itemId, { signal: controller.signal }),
          api.script(itemId, { signal: controller.signal }).catch((error) => {
            if (error instanceof ApiError && error.code === "domain.script_not_found") return null;
            throw error;
          }),
        ]);
        item = detail.item;
        script = stored;
      }
      caps = await capsPromise;
    } catch (error) {
      if (controller.signal.aborted) return;
      mount(container, pageHeader(title, sub), errorView(error, load));
      return;
    }
    if (controller.signal.aborted) return;

    if (item && !EDITABLE_STATUSES.has(item.status)) {
      mount(
        container,
        pageHeader(title, item.title),
        el(
          "div",
          { class: "stack" },
          alert("warning", S.editor.notEditable, `${S.item.status}: ${label("status", item.status)}`),
          el("div", { class: "row" }, linkButton(`#/items/${encodeURIComponent(item.id)}`, S.app.back)),
        ),
      );
      return;
    }
    mount(
      container,
      pageHeader(title, item ? item.title : sub, item ? contentStatusBadge(item.status) : null),
      editorForm({ ctx, caps, item, script, signal: controller.signal }),
    );
  }

  load();
  return () => controller.abort();
}

function editorForm({ ctx, caps, item, script, signal }) {
  const supported = new Set(caps.supported_modes || []);
  const reasons = caps.unavailable_reasons || {};

  const titleInput = el("input", { id: "f-title", class: "input", type: "text", maxlength: "200", autocomplete: "off" });
  titleInput.value = item ? item.title : "";
  const typeSelect = el(
    "select",
    { id: "f-type", class: "select", disabled: Boolean(item) },
    TYPES.map((value) => el("option", { value, text: label("contentType", value) })),
  );
  typeSelect.value = item ? item.content_type : "shorts";
  const modeSelect = el(
    "select",
    { id: "f-mode", class: "select", "aria-describedby": "f-mode-hint" },
    MODES.map((value) =>
      el("option", {
        value,
        disabled: !supported.has(value),
        text: supported.has(value) ? label("mode", value) : `${label("mode", value)} (${S.editor.modeUnavailable})`,
      }),
    ),
  );
  modeSelect.value = supported.has("story_video") ? "story_video" : MODES.find((mode) => supported.has(mode)) || "story_video";
  const unavailableModes = MODES.filter((mode) => !supported.has(mode));
  const modeHint = el(
    "div",
    { id: "f-mode-hint", class: "field__hint" },
    unavailableModes.map((mode) => el("div", { text: `${label("mode", mode)}: ${reasons[mode] || S.editor.modeUnavailable}` })),
  );
  const styleInput = el("input", { id: "f-style", class: "input", type: "text", list: "f-style-list", autocomplete: "off" });
  styleInput.value = "cinematic_story";
  const styleList = el("datalist", { id: "f-style-list" }, STYLES.map((value) => el("option", { value })));
  const languageInput = el("input", { id: "f-language", class: "input", type: "text", maxlength: "35", autocomplete: "off" });
  languageInput.value = "vi";
  const durationInput = el("input", { id: "f-duration", class: "input", type: "number", min: "5", max: "3600", step: "1", inputmode: "numeric" });
  const subtitlesInput = el("input", { id: "f-subtitles", type: "checkbox", checked: true });
  const approvalInput = el("input", { id: "f-approval", type: "checkbox", "aria-describedby": "f-approval-hint" });

  const scriptInput = el("textarea", { id: "f-script", class: "textarea", spellcheck: "false", "aria-describedby": "f-script-hint f-script-count f-script-error" });
  scriptInput.value = script ? script.text : "";
  const scriptCount = el("span", { id: "f-script-count", class: "field__hint" });
  const scriptError = el("span", { id: "f-script-error", class: "field__error", role: "alert" });
  const updateCount = () => {
    scriptCount.textContent = `${scriptInput.value.length.toLocaleString("vi-VN")} ${S.editor.chars}`;
  };
  scriptInput.addEventListener("input", () => {
    updateCount();
    if (scriptInput.value.trim()) {
      scriptInput.removeAttribute("aria-invalid");
      scriptError.textContent = "";
    }
  });
  updateCount();

  const fileInput = el("input", { id: "f-file", type: "file", accept: ".txt,text/plain", hidden: true, tabindex: "-1" });
  const fileStatus = el("span", { class: "field__hint", role: "status" });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    if (!/\.txt$/i.test(file.name) && file.type !== "text/plain") {
      fileStatus.textContent = S.editor.fileWrongType;
      return;
    }
    if (file.size > MAX_FILE_BYTES) {
      fileStatus.textContent = S.editor.fileTooLarge;
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      scriptInput.value = String(reader.result || "");
      scriptInput.dispatchEvent(new Event("input"));
      if (!titleInput.value.trim()) titleInput.value = file.name.replace(/\.txt$/i, "");
      fileStatus.textContent = `${S.editor.fileLoaded}: ${file.name}`;
    };
    reader.onerror = () => {
      fileStatus.textContent = S.editor.fileReadError;
    };
    reader.readAsText(file, "utf-8");
  });
  const fileLabel = el("button", { type: "button", class: "button", text: S.editor.loadFile, onClick: () => fileInput.click() });

  const formError = el("div", {});
  const planSlot = el("div", {});
  const analyzeButton = el("button", { type: "button", class: "button", text: S.editor.analyze });
  const createButton = el("button", { type: "submit", class: "button button--primary", text: S.editor.create });

  function payload() {
    const duration = durationInput.value.trim();
    return {
      script_text: scriptInput.value,
      title: titleInput.value.trim() || null,
      content_type: typeSelect.value,
      production_mode: modeSelect.value,
      visual_style: styleInput.value.trim() || "cinematic_story",
      language: languageInput.value.trim() || "vi",
      target_duration_seconds: duration ? Number.parseInt(duration, 10) : null,
      subtitles_enabled: subtitlesInput.checked,
      request_approval: approvalInput.checked,
      content_item_id: item ? item.id : null,
    };
  }

  function checkScript() {
    if (scriptInput.value.trim()) return true;
    scriptInput.setAttribute("aria-invalid", "true");
    scriptError.textContent = S.editor.scriptRequired;
    scriptInput.focus();
    return false;
  }

  function setBusy(busy, which) {
    analyzeButton.disabled = busy;
    createButton.disabled = busy;
    analyzeButton.textContent = busy && which === "analyze" ? S.editor.analyzing : S.editor.analyze;
    createButton.textContent = busy && which === "create" ? S.editor.creating : S.editor.create;
  }

  function showError(error) {
    mount(formError, errorAlert(error));
    if (error.code === "pipeline.script_to_video_empty_script") {
      scriptInput.setAttribute("aria-invalid", "true");
    }
    formError.scrollIntoView({ block: "nearest" });
  }

  analyzeButton.addEventListener("click", async () => {
    formError.replaceChildren();
    if (!checkScript()) return;
    setBusy(true, "analyze");
    try {
      const plan = await api.analyze(payload(), { signal });
      mount(planSlot, planCard(plan));
      announce(S.editor.planTitle);
    } catch (error) {
      if (!signal.aborted) showError(error);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  });

  const form = el(
    "form",
    {
      class: "form",
      novalidate: true,
      onSubmit: async (event) => {
        event.preventDefault();
        formError.replaceChildren();
        if (!checkScript()) return;
        setBusy(true, "create");
        try {
          const response = await api.submitRun(payload(), { signal });
          if (response.status === 200) announce(S.editor.duplicateRun);
          ctx.navigate(`#/runs/${encodeURIComponent(response.data.run_id)}`);
        } catch (error) {
          if (!signal.aborted) {
            showError(error);
            setBusy(false);
          }
        }
      },
    },
    card(
      null,
      el(
        "div",
        { class: "form-grid" },
        field("f-title", S.editor.fieldTitle, titleInput, S.editor.fieldTitleHint),
        field("f-type", S.editor.fieldType, typeSelect),
        field("f-mode", S.editor.fieldMode, modeSelect, null, modeHint),
        field("f-style", S.editor.fieldStyle, [styleInput, styleList]),
        field("f-language", S.editor.fieldLanguage, languageInput),
        field("f-duration", S.editor.fieldDuration, durationInput, S.editor.fieldDurationHint),
      ),
      el(
        "div",
        { class: "grid grid--two", style: "margin-top: 16px" },
        el("div", { class: "check" }, subtitlesInput, el("label", { for: "f-subtitles", text: S.editor.fieldSubtitles })),
        el(
          "div",
          { class: "check" },
          approvalInput,
          el(
            "div",
            {},
            el("label", { for: "f-approval", text: S.editor.fieldApproval }),
            el("div", { id: "f-approval-hint", class: "field__hint", text: S.editor.fieldApprovalHint }),
          ),
        ),
      ),
    ),
    card(
      null,
      el(
        "div",
        { class: "field" },
        el(
          "div",
          { class: "card__header" },
          el("label", { for: "f-script", class: "field__label", text: S.editor.fieldScript }),
          el("div", { class: "row" }, fileLabel, fileInput),
        ),
        el("span", { id: "f-script-hint", class: "field__hint", text: S.editor.fieldScriptHint }),
        scriptInput,
        el("div", { class: "row" }, scriptCount, fileStatus),
        scriptError,
      ),
    ),
    formError,
    el("div", { class: "form-actions" }, item ? linkButton(`#/items/${encodeURIComponent(item.id)}`, S.app.back) : null, analyzeButton, createButton),
    planSlot,
  );
  return form;
}

function field(id, text, control, hint, extra) {
  return el(
    "div",
    { class: "field" },
    el("label", { for: id, text }),
    control,
    hint ? el("span", { class: "field__hint", text: hint }) : null,
    extra || null,
  );
}

function planCard(plan) {
  const bible = plan.story_bible;
  const breakdown = plan.scene_breakdown;
  return el(
    "section",
    { class: "card", "aria-labelledby": "plan-title" },
    el("h2", { id: "plan-title", class: "card__title", text: S.editor.planTitle }),
    el("p", { class: "muted", text: S.editor.planSub }),
    facts([
      [S.editor.fieldTitle, bible.title],
      [S.editor.fieldType, label("contentType", bible.content_type)],
      [S.editor.fieldMode, label("mode", bible.production_mode)],
      [S.editor.planDuration, formatDuration(breakdown.total_duration_ms)],
      [S.editor.planCharacters, bible.characters.map((character) => character.name).join(", ")],
      [S.editor.planLocations, bible.locations.map((location) => location.name).join(", ")],
    ]),
    el("h3", { text: `${S.editor.planScenes} (${breakdown.scene_count})` }),
    el(
      "div",
      { class: "stack" },
      breakdown.scenes.map((scene, index) =>
        el(
          "div",
          { class: "plan-scene" },
          el("div", { class: "scene__title", text: `${index + 1}. ${scene.title}` }),
          el("div", { class: "muted", text: `${formatDuration(scene.start_ms)} – ${formatDuration(scene.end_ms)} · ${scene.shots.length} shot` }),
          el("details", {}, el("summary", { text: S.item.narration }), el("p", { text: scene.narration_and_dialogue })),
        ),
      ),
    ),
  );
}
