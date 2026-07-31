/**
 * Centralized runtime configuration for the Control Tower frontend.
 *
 * Every other module resolves the backend's HTTP/WebSocket base URL through
 * this file rather than reading `import.meta.env` directly, so the fallback
 * default and the http->ws derivation logic exist in exactly one place.
 */

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'

/** Base URL of the FastAPI backend, with any trailing slash stripped. */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/+$/, '')

/**
 * Base URL for WebSocket connections, derived from `API_BASE_URL`.
 *
 * `http://` becomes `ws://` and `https://` becomes `wss://`, mirroring the
 * scheme translation a browser performs automatically when it upgrades an
 * HTTP connection to a WebSocket one.
 */
export const WS_BASE_URL: string = API_BASE_URL.replace(/^http/i, 'ws')

/** Default number of real-world seconds per simulated minute for a new live session. */
export const DEFAULT_TICK_INTERVAL_SECONDS = 1.0

/** Maximum number of recent telemetry/audit entries the store keeps in memory. */
export const EVENT_LOG_CAPACITY = 200

/** Base delay, in milliseconds, for the WebSocket's exponential reconnection backoff. */
export const WS_RECONNECT_BASE_DELAY_MS = 1000

/** Ceiling, in milliseconds, the WebSocket's reconnection backoff never exceeds. */
export const WS_RECONNECT_MAX_DELAY_MS = 15000
