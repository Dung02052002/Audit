// Read-only settings (the server's whitelist) and provider capabilities.

import { S } from "../strings.js";
import { badge, button, card, el, errorView, facts, loadingView, mount, pageHeader } from "../ui.js";
import { capabilitiesCard } from "./overview.js";

export function render(container, ctx) {
  let disposed = false;
  document.title = `${S.settings.title} · ${S.app.title}`;

  async function load() {
    mount(container, pageHeader(S.settings.title, S.settings.sub), loadingView(3));
    let settings;
    let caps;
    try {
      [settings, caps] = await Promise.all([ctx.settings(), ctx.capabilities()]);
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
