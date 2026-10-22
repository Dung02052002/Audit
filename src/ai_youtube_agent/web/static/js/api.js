// HTTP client of the web app. Every JSON call times out after 10 s; media and
// downloads are plain links and never pass through here. Errors keep the API
// envelope: {status, code, message, retryable, fields}.

import { S } from "./strings.js";

const TIMEOUT_MS = 10000;

export class ApiError extends Error {
  constructor({ status, code, message, retryable = false, fields = null }) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.fields = fields;
  }

  get isNetwork() {
    return this.code === "network.unreachable" || this.code === "network.timeout";
  }

  get isAbort() {
    return this.code === "request.aborted";
  }
}

async function request(method, path, { body, signal, timeoutMs = TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", onAbort, { once: true });
  }

  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined
        ? { Accept: "application/json" }
        : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (error) {
    if (timedOut) {
      throw new ApiError({ status: 0, code: "network.timeout", message: S.errors.timeout, retryable: true });
    }
    if (signal && signal.aborted) {
      throw new ApiError({ status: 0, code: "request.aborted", message: "aborted" });
    }
    throw new ApiError({ status: 0, code: "network.unreachable", message: S.errors.network, retryable: true });
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", onAbort);
  }

  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const envelope = data && data.error ? data.error : null;
    throw new ApiError({
      status: response.status,
      code: envelope ? envelope.code : `http.${response.status}`,
      message: envelope ? envelope.message : S.errors.generic,
      retryable: envelope ? Boolean(envelope.retryable) : response.status >= 500,
      fields: envelope && Array.isArray(envelope.fields) ? envelope.fields : null,
    });
  }
  return { status: response.status, data, location: response.headers.get("Location") };
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const id = (value) => encodeURIComponent(value);

async function get(path, options) {
  return (await request("GET", path, options)).data;
}

export const api = {
  capabilities: (options) => get("/script-to-video/capabilities", options),
  settings: (options) => get("/system/settings", options),
  items: (params, options) => get(`/content-items${query(params)}`, options),
  item: (itemId, options) => get(`/content-items/${id(itemId)}`, options),
  script: (itemId, options) => get(`/content-items/${id(itemId)}/script`, options),
  scenes: (itemId, options) => get(`/content-items/${id(itemId)}/scenes`, options),
  runs: (options) => get("/script-to-video/background-runs", options),
  run: (runId, options) => get(`/script-to-video/background-runs/${id(runId)}`, options),
  job: (jobId, options) => get(`/script-to-video/jobs/${id(jobId)}`, options),
  analyze: (payload, options) =>
    request("POST", "/script-to-video/analyze", { ...options, body: payload }).then((r) => r.data),
  submitRun: (payload, options) =>
    request("POST", "/script-to-video/background-runs", { ...options, body: payload }),
};

export const links = {
  download: (itemId) => `/content-items/${id(itemId)}/video/download`,
  subtitlesVtt: (itemId) => `/previews/${id(itemId)}/subtitles.vtt`,
};
