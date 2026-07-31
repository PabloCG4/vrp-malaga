/**
 * TypeScript mirrors of the JSON messages pushed over the live simulation
 * WebSocket (`WS /api/v1/workdays/{id}/live`).
 *
 * These shapes are not backed by a Pydantic response model on the backend
 * (FastAPI has no first-class WebSocket response schema); they are built by
 * hand in `backend/src/api/services/live_simulation.py`
 * (`_build_state_payload`, `_reoptimization_payload`, `_broadcast_injected_event`,
 * `_finalize`, and the generic error broadcast in `_run_loop`). This module
 * is the frontend's single source of truth for that wire contract, and
 * every field name/shape here must be kept in lockstep with that file.
 */

import type { SimulationEventType, VehicleStatus } from './enums'

/** Mirrors the `"clock"` object embedded in every snapshot/tick message. */
export interface SimulationClockState {
  current_minute: number
  current_time_seconds: number
  formatted_time: string
  is_finished: boolean
}

/** Mirrors one entry of the `"vehicles"` array embedded in every snapshot/tick message. */
export interface VehicleTelemetry {
  vehicle_id: string
  status: VehicleStatus
  current_node: number
  next_node: number | null
  active_customer_id: string | null
  locked_prefix_length: number
}

interface LiveMessageBase {
  workday_plan_id: number
}

/** Sent once, immediately after a client subscribes, with the session's current state. */
export interface SnapshotMessage extends LiveMessageBase {
  type: 'snapshot'
  clock: SimulationClockState
  vehicles: VehicleTelemetry[]
}

/** Sent once per simulated minute (and once immediately when a session starts). */
export interface TickMessage extends LiveMessageBase {
  type: 'tick'
  clock: SimulationClockState
  vehicles: VehicleTelemetry[]
}

/** Sent whenever a disruption (traffic incident or urgent order) is dispatched into the session. */
export interface SimulationEventMessage extends LiveMessageBase {
  type: 'event'
  event_type: SimulationEventType
  trigger_minute: number
  payload: Record<string, unknown>
}

/** Sent whenever a locked-prefix-aware Tabu Search re-optimization completes. */
export interface ReoptimizationMessage extends LiveMessageBase {
  type: 'reoptimization'
  trigger_description: string
  triggered_at_minute: number
  iterations_completed: number
  elapsed_seconds: number
  cost_before: number
  cost_after: number
  feasible_before: boolean
  feasible_after: boolean
  locked_prefixes_respected: boolean
}

/** Sent once, when the simulated workday clock reaches its end. */
export interface FinishedMessage extends LiveMessageBase {
  type: 'finished'
  final_cost: number
  is_feasible: boolean
}

/** Sent if the background tick loop fails unexpectedly, immediately before the connection drops. */
export interface LiveErrorMessage extends LiveMessageBase {
  type: 'error'
  detail: string
}

/** Discriminated union of every message shape the `/live` WebSocket may send, keyed by `type`. */
export type LiveSimulationMessage =
  | SnapshotMessage
  | TickMessage
  | SimulationEventMessage
  | ReoptimizationMessage
  | FinishedMessage
  | LiveErrorMessage

/** Runtime type guard narrowing an arbitrary decoded JSON value into a `LiveSimulationMessage`. */
export function isLiveSimulationMessage(value: unknown): value is LiveSimulationMessage {
  if (typeof value !== 'object' || value === null || !('type' in value)) {
    return false
  }
  const messageType = (value as { type: unknown }).type
  return (
    messageType === 'snapshot' ||
    messageType === 'tick' ||
    messageType === 'event' ||
    messageType === 'reoptimization' ||
    messageType === 'finished' ||
    messageType === 'error'
  )
}
