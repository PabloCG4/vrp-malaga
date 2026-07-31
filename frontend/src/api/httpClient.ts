/**
 * Minimal, dependency-free `fetch` wrapper shared by every REST API module.
 *
 * Kept intentionally small (no axios/react-query dependency) since the only
 * cross-cutting concerns this backend actually needs on the client are: a
 * consistent base URL, JSON encoding/decoding, and turning a non-2xx
 * response into a typed exception that carries the backend's own
 * `detail` message (FastAPI's standard `HTTPException` error shape).
 */

import { API_BASE_URL } from '../config/env'

/** Error thrown for any non-2xx HTTP response, carrying the backend's own error detail when present. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(`Request failed with status ${status}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** Shape of FastAPI's default error response body, `{"detail": ...}`. */
interface FastApiErrorBody {
  detail?: unknown
}

async function extractErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as FastApiErrorBody
    if (typeof body.detail === 'string') {
      return body.detail
    }
    if (body.detail !== undefined) {
      return JSON.stringify(body.detail)
    }
  } catch {
    // Response body was not JSON (or empty); fall through to the status text below.
  }
  return response.statusText || `HTTP ${response.status}`
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
}

/**
 * Perform a JSON request against the Control Tower backend.
 *
 * `TResponse` is `void`-safe: a `204 No Content` (or any empty body)
 * response resolves to `undefined` rather than attempting to parse JSON.
 */
export async function apiRequest<TResponse>(path: string, options: RequestOptions = {}): Promise<TResponse> {
  const init: RequestInit = {
    method: options.method ?? 'GET',
    ...(options.body !== undefined && {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(options.body),
    }),
    ...(options.signal !== undefined && { signal: options.signal }),
  }
  const response = await fetch(`${API_BASE_URL}${path}`, init)

  if (!response.ok) {
    throw new ApiError(response.status, await extractErrorDetail(response))
  }

  if (response.status === 204) {
    return undefined as TResponse
  }

  const text = await response.text()
  return (text.length > 0 ? JSON.parse(text) : undefined) as TResponse
}
