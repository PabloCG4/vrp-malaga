/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL of the Control Tower FastAPI backend, e.g. "http://127.0.0.1:8000".
   * Falls back to a sane local default when unset (see `src/config/env.ts`).
   */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
