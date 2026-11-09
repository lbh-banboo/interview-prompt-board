// Ported from ukez15/interview-copilot@4a80d842fa22a359b4ab05eea8b2c8432c113e07:src/desktop/src/capture/backoff.ts, MIT © 2026 ukez15.
// Modified: none (verbatim logic).
const SCHEDULE_MS = [500, 1000, 2000];
const CEILING_MS = 4000;
/** Reconnect delay for the given attempt (0-based): 0.5 s, 1 s, 2 s, then every 4 s. */
export function backoffDelay(attempt) {
    return SCHEDULE_MS[attempt] ?? CEILING_MS;
}
