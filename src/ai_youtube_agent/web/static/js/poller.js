// Polls a run or job while it is queued or running.
//
// - every 1.5 s, every 5 s once the stage has not changed for 2 minutes;
// - a network error, a server error (status 500 or more) or a retryable API
//   error backs off 2, 4, 8, then 15 s ("reconnecting"); after 10 failures
//   in a row it stops ("gave_up") until restart() is called;
// - another API error stops it (onError);
// - it stops on a final status and when the view is left (stop());
// - "slow" is reported once nothing changed for slowAfterMs (a number or a
//   function returning one); it never fails the run.

import { ApiError } from "./api.js";

const FAST_MS = 1500;
const SLOW_MS = 5000;
const SLOW_DOWN_AFTER_MS = 2 * 60 * 1000;
const BACKOFF_MS = [2000, 4000, 8000, 15000];
const MAX_FAILURES = 10;

function isTransient(error) {
  if (!(error instanceof ApiError)) return false;
  return error.isNetwork || error.retryable || error.status >= 500;
}

export function createPoller({ load, onData, onError, onStatus, isActive, changeKey, slowAfterMs }) {
  let timer = null;
  let controller = null;
  let stopped = true;
  let failures = 0;
  let lastKey = null;
  let lastChange = Date.now();
  let warnedSlow = false;

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(tick, delay);
  }

  async function tick() {
    if (stopped) return;
    controller = new AbortController();
    let data;
    try {
      data = await load(controller.signal);
    } catch (error) {
      if (stopped || (error instanceof ApiError && error.isAbort)) return;
      if (isTransient(error)) {
        failures += 1;
        if (failures >= MAX_FAILURES) {
          stopped = true;
          onStatus("gave_up");
          return;
        }
        onStatus("reconnecting");
        schedule(BACKOFF_MS[Math.min(failures - 1, BACKOFF_MS.length - 1)]);
        return;
      }
      stopped = true;
      onError(error);
      return;
    }
    if (stopped) return;
    if (failures) onStatus("connected");
    failures = 0;

    const key = changeKey(data);
    if (key !== lastKey) {
      lastKey = key;
      lastChange = Date.now();
      if (warnedSlow) {
        warnedSlow = false;
        onStatus("normal");
      }
    }
    onData(data);
    if (!isActive(data)) {
      stopped = true;
      return;
    }
    const unchangedFor = Date.now() - lastChange;
    const slowLimit = typeof slowAfterMs === "function" ? slowAfterMs() : slowAfterMs;
    if (slowLimit && !warnedSlow && unchangedFor >= slowLimit) {
      warnedSlow = true;
      onStatus("slow");
    }
    schedule(unchangedFor >= SLOW_DOWN_AFTER_MS ? SLOW_MS : FAST_MS);
  }

  return {
    start() {
      stopped = false;
      failures = 0;
      lastChange = Date.now();
      schedule(0);
    },
    restart() {
      this.stop();
      this.start();
    },
    stop() {
      stopped = true;
      window.clearTimeout(timer);
      if (controller) controller.abort();
    },
  };
}
