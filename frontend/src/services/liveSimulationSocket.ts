/**
 * Resilient WebSocket client for the live simulation telemetry stream
 * (`WS /api/v1/workdays/{id}/live`).
 *
 * This is a plain class, deliberately not a React hook: the simulation
 * store (`store/simulationStore.ts`) owns exactly one instance for the
 * lifetime of the application and drives it imperatively from its actions,
 * so the socket's lifecycle is independent of which components happen to be
 * mounted at any given moment. A hook consuming the store gets the same
 * resilience "for free" without re-subscribing on every render.
 *
 * Reconnection uses capped exponential backoff and resumes automatically
 * after an unexpected drop; it does not resume after a deliberate
 * `disconnect()` call or after the backend closes the socket for an
 * application-level reason (unknown plan / plan not ACTIVE), since retrying
 * either would just fail again.
 */

import {
  DEFAULT_TICK_INTERVAL_SECONDS,
  WS_BASE_URL,
  WS_RECONNECT_BASE_DELAY_MS,
  WS_RECONNECT_MAX_DELAY_MS,
} from '../config/env'
import { isLiveSimulationMessage, type LiveSimulationMessage } from '../types/telemetry'

/** Lifecycle of a `LiveSimulationSocket`, surfaced to the UI as a connection badge. */
export type LiveConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error'

/** WebSocket close codes the backend uses for terminal, non-retryable application errors. */
const NON_RETRYABLE_CLOSE_CODES = new Set<number>([4404, 4409])

export interface LiveSimulationSocketHandlers {
  /** Invoked whenever the connection's lifecycle status changes. */
  onStatusChange?: (status: LiveConnectionStatus, detail?: string) => void
  /** Invoked once per well-formed telemetry message received. */
  onMessage?: (message: LiveSimulationMessage) => void
}

export class LiveSimulationSocket {
  private socket: WebSocket | null = null
  private workdayId: number | null = null
  private tickIntervalSeconds: number = DEFAULT_TICK_INTERVAL_SECONDS
  private handlers: LiveSimulationSocketHandlers = {}
  private status: LiveConnectionStatus = 'idle'
  private reconnectAttempts = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private intentionalDisconnect = false

  /** Open (or re-target) the connection for a given workday plan. Replaces any existing connection. */
  connect(workdayId: number, handlers: LiveSimulationSocketHandlers, tickIntervalSeconds?: number): void {
    this.disconnect()
    this.intentionalDisconnect = false
    this.workdayId = workdayId
    this.tickIntervalSeconds = tickIntervalSeconds ?? DEFAULT_TICK_INTERVAL_SECONDS
    this.handlers = handlers
    this.reconnectAttempts = 0
    this.openSocket()
  }

  /** Deliberately close the connection and cancel any pending reconnection attempt. */
  disconnect(): void {
    this.intentionalDisconnect = true
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.socket !== null) {
      const socket = this.socket
      this.socket = null
      socket.onopen = null
      socket.onmessage = null
      socket.onerror = null
      socket.onclose = null
      socket.close()
    }
    this.workdayId = null
    this.setStatus('idle')
  }

  getStatus(): LiveConnectionStatus {
    return this.status
  }

  private openSocket(): void {
    if (this.workdayId === null) {
      return
    }
    this.setStatus(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting')

    const url = `${WS_BASE_URL}/api/v1/workdays/${this.workdayId}/live?tick_interval_seconds=${this.tickIntervalSeconds}`
    const socket = new WebSocket(url)
    this.socket = socket

    socket.onopen = () => {
      this.reconnectAttempts = 0
      this.setStatus('open')
    }

    socket.onmessage = (event: MessageEvent<string>) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(event.data)
      } catch {
        return // Malformed frame; ignore rather than crash the whole stream.
      }
      if (isLiveSimulationMessage(parsed)) {
        this.handlers.onMessage?.(parsed)
      }
    }

    socket.onerror = () => {
      this.setStatus('error')
    }

    socket.onclose = (event: CloseEvent) => {
      this.socket = null
      if (this.intentionalDisconnect) {
        this.setStatus('idle')
        return
      }
      if (NON_RETRYABLE_CLOSE_CODES.has(event.code)) {
        this.setStatus('closed', event.reason || `Connection closed (code ${event.code}).`)
        return
      }
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    this.setStatus('reconnecting')
    const delay = Math.min(WS_RECONNECT_BASE_DELAY_MS * 2 ** this.reconnectAttempts, WS_RECONNECT_MAX_DELAY_MS)
    this.reconnectAttempts += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.openSocket()
    }, delay)
  }

  private setStatus(status: LiveConnectionStatus, detail?: string): void {
    this.status = status
    this.handlers.onStatusChange?.(status, detail)
  }
}
