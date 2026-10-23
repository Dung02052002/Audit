// Settings: the paid AI API budget card (the only editable setting, BUDGET-01),
// the server's read-only whitelist and provider capabilities.

import { api } from "../api.js";
import { S } from "../strings.js";
import { announce, badge, button, card, cardWithHeader, el, errorAlert, errorView, facts, formatDateTime, loadingView, mount, pageHeader } from "../ui.js";
import { capabilitiesCard } from "./overview.js";

export function render(container, ctx) {
  let disposed = false;
  document.title = `${S.settings.title} · ${S.app.title}`;

  async function load() {
    mount(container, pageHeader(S.settings.title, S.settings.sub), loadingView(3));
    let settings;
    let caps;
    let policy;
    try {
      // The budget policy is read fresh on every visit; it is never cached.
      [settings, caps, policy] = await Promise.all([ctx.settings(), ctx.capabilities(), api.budgetPolicy()]);
    } catch (error) {
      if (!disposed) mount(container, pageHeader(S.settings.title, S.settings.sub), errorView(error, load));
      return;
    }
    if (disposed) return;
    const yesNo = (value) => (value ? badge(S.app.yes, "green") : badge(S.app.no));
    mount(
      container,
      pageHeader(S.settings.title, S.settings.sub, button(S.app.refresh, () => window.location.reload())),
      el(
        "div",
        { class: "stack" },
        budgetCard(policy, () => !disposed),
        el(
          "div",
          { class: "grid grid--two" },
          card(
            S.settings.general,
            facts([
              [S.settings.appName, settings.app_name],
              [S.settings.version, settings.version],
              [S.settings.environment, settings.environment],
              [S.settings.debug, yesNo(settings.debug)],
              [S.settings.logLevel, settings.log_level],
            ]),
          ),
          card(
            S.settings.render,
            facts([
              [S.settings.renderProvider, settings.render.provider],
              [S.settings.ffmpegBinary, el("span", { class: "mono", text: settings.render.ffmpeg_binary_name })],
              [S.settings.ffmpegFound, yesNo(settings.render.ffmpeg_found)],
              [S.settings.timeout, `${settings.render.timeout_seconds} ${S.settings.seconds}`],
              [S.settings.fontFile, yesNo(settings.render.font_file_configured)],
            ]),
          ),
          card(
            S.settings.providers,
            facts(
              Object.entries(settings.providers).map(([name, value]) => [
                S.settings.providerNames[name] || name,
                el("span", { class: "mono", text: value }),
              ]),
            ),
          ),
          card(
            S.settings.flags,
            facts(Object.entries(settings.flags).map(([name, value]) => [S.settings.flagNames[name] || name, yesNo(value)])),
          ),
          card(
            S.settings.policies,
            facts([
              [S.settings.approvalTtl, `${settings.approval_ttl_hours} ${S.settings.hours}`],
              [S.settings.policyVersion, String(settings.policy_rule_set_version)],
              [S.settings.rightsLevels, settings.rights_block_levels.join(", ")],
              [S.settings.jobAttempts, String(settings.job_max_attempts)],
              [S.settings.uploadAttempts, String(settings.upload_max_attempts)],
            ]),
          ),
          capabilitiesCard(caps),
        ),
      ),
    );
  }

  load();
  return () => {
    disposed = true;
  };
}

function pick(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : String(key);
}

// The paid-API mode: two radio choices and a save button. Saving sends the
// version that was read, so a change made elsewhere is refused (409).
function budgetCard(initialPolicy, isMounted) {
  const slot = el("div", {});

  function draw(policy) {
    const status = el("div", { "aria-live": "polite" });
    const choices = policy.modes.map((mode) => {
      const text = S.budget.modes[mode] || { title: mode, body: "" };
      const input = el("input", {
        type: "radio",
        name: "paid-api-mode",
        id: `paid-api-mode-${mode}`,
        value: mode,
        checked: mode === policy.mode,
      });
      return el(
        "label",
        { class: "choice", for: input.id },
        input,
        el(
          "span",
          { class: "choice__text" },
          el("span", { class: "choice__title", text: text.title }),
          el("span", { class: "choice__body", text: text.body }),
        ),
      );
    });
    const fieldset = el("fieldset", { class: "choices" }, el("legend", { text: S.budget.legend }), choices);

    let saving = false;
    const saveButton = button(S.budget.save, save, { primary: true });

    async function save() {
      if (saving) return;
      const selected = fieldset.querySelector('input[name="paid-api-mode"]:checked');
      if (!selected) return;
      if (selected.value === policy.mode) {
        mount(status, el("p", { class: "muted", text: S.budget.unchanged }));
        announce(S.budget.unchanged);
        return;
      }
      saving = true;
      saveButton.disabled = true;
      saveButton.textContent = S.budget.saving;
      try {
        await api.saveBudgetPolicy({ mode: selected.value, expected_version: policy.version });
        const fresh = await api.budgetPolicy();
        if (!isMounted()) return;
        draw(fresh);
        announce(S.budget.saved);
      } catch (error) {
        if (!isMounted()) return;
        saving = false;
        saveButton.disabled = false;
        saveButton.textContent = S.budget.save;
        mount(status, errorAlert(error));
      }
    }

    const providers = Array.isArray(policy.providers) ? policy.providers : [];
    mount(
      slot,
      cardWithHeader(
        S.budget.cardTitle,
        policy.is_default ? badge(S.budget.isDefault) : null,
        el("p", { class: "muted", text: S.budget.cardSub }),
        el(
          "div",
          { class: "stack" },
          fieldset,
          el("div", { class: "form-actions" }, saveButton),
          status,
          facts([
            [S.budget.version, String(policy.version)],
            [S.budget.updatedAt, policy.updated_at ? formatDateTime(policy.updated_at) : null],
            [S.budget.updatedBy, policy.updated_by_id ? el("span", { class: "mono", text: policy.updated_by_id }) : null],
          ]),
          providers.length
            ? el(
                "div",
                { class: "stack" },
                el("h3", { class: "card__title", text: S.budget.providersTitle }),
                el(
                  "div",
                  { class: "table-wrap" },
                  el(
                    "table",
                    { class: "table" },
                    el(
                      "thead",
                      {},
                      el(
                        "tr",
                        {},
                        el("th", { scope: "col", text: S.budget.colProvider }),
                        el("th", { scope: "col", text: S.budget.colTask }),
                        el("th", { scope: "col", text: S.budget.colCost }),
                        el("th", { scope: "col", text: S.budget.colPrice }),
                      ),
                    ),
                    el(
                      "tbody",
                      {},
                      providers.map((entry) =>
                        el(
                          "tr",
                          {},
                          el("td", {}, el("span", { class: "mono", text: entry.provider })),
                          el("td", { text: pick(S.budget.taskType, entry.task_type) }),
                          el("td", {}, costBadge(entry.cost_class)),
                          el("td", { text: budgetPriceLabel(entry) }),
                        ),
                      ),
                    ),
                  ),
                ),
              )
            : null,
        ),
      ),
    );
  }

  draw(initialPolicy);
  return slot;
}

function costBadge(costClass) {
  const tones = { mock: "", local_free: "green", free_service: "green", paid: "amber", unknown: "red" };
  return badge(pick(S.budget.costClass, costClass), tones[costClass] || "");
}

// Only a paid or unknown-cost provider has a price to know; free ones cost nothing.
function budgetPriceLabel(entry) {
  if (entry.cost_class === "paid" || entry.cost_class === "unknown") {
    return entry.price_known ? S.budget.priceKnown : S.budget.priceUnknown;
  }
  return S.budget.priceFree;
}
