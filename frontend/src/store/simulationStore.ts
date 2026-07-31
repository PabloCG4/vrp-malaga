/**
 * Global simulation store (Zustand).
 *
 * Owns workday selection, live WebSocket telemetry, the rolling activity log,
 * street-following route geometry, and thin async actions over the REST API.
 * On every `reoptimization` broadcast the store re-fetches the active plan and
 * its route geometry so map markers, counters and polylines stay in sync with
 * the backend without a manual page refresh.
 */

import { create } from 'zustand'
import { getWorkdayRouteGeometry } from '../api/geometryApi'
import { getWorkday, listWorkdays, optimizeWorkday } from '../api/workdaysApi'
import {
  injectTrafficIncident as injectTrafficIncidentRequest,
  injectUrgentOrder as injectUrgentOrderRequest,
  listEligibleUrgentOrderNodes,
} from '../api/eventsApi'
import { ApiError } from '../api/httpClient'
import { DEFAULT_TICK_INTERVAL_SECONDS, EVENT_LOG_CAPACITY } from '../config/env'
import { LiveSimulationSocket, type LiveConnectionStatus } from '../services/liveSimulationSocket'
import type {
  EligibleUrgentOrderNode,
  SimulationEventRecord,
  TrafficIncidentInjectionRequest,
  UrgentOrderInjectionRequest,
  WorkdayPlanDetail,
  WorkdayPlanSummary,
} from '../types/domain'
import type { WorkdayRouteGeometry } from '../types/geometry'
import type { LiveSimulationMessage, SimulationClockState, VehicleTelemetry } from '../types/telemetry'

/** One entry of the rolling live-activity log (`event`, `reoptimization`, `finished` or `error` messages). */
export interface LiveLogEntry {
  id: string
  receivedAt: number
  message: LiveSimulationMessage
}

function describeError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return error.detail
  }
  if (error instanceof Error) {
    return error.message
  }
  return fallback
}

function nextLogEntryId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
}

function emptyRouteGeometry(workdayPlanId: number): WorkdayRouteGeometry {
  return { workday_plan_id: workdayPlanId, legs: [] }
}

/**
 * Guarantee every nested collection on a workday detail payload is a real array.
 * Older backends (or partial responses during deploy skew) may omit
 * `simulation_events` / related fields; rendering must never see `undefined`.
 */
function normalizeWorkdayPlan(plan: WorkdayPlanDetail): WorkdayPlanDetail {
  return {
    ...plan,
    orders: plan.orders ?? [],
    route_stops: plan.route_stops ?? [],
    vehicles: plan.vehicles ?? [],
    simulation_events: plan.simulation_events ?? [],
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

function asFiniteNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback
}

/**
 * Rebuild the activity timeline from persisted `simulation_events` so navigating
 * away and back (or refreshing) restores disruptions without needing the live
 * WebSocket stream. When a row embeds a `reoptimization` summary (written by
 * the live session after Tabu Search), a matching reoptimization log entry is
 * synthesized immediately after the disruption event.
 */
function buildEventLogFromSimulationEvents(
  workdayPlanId: number,
  events: SimulationEventRecord[] | null | undefined,
): LiveLogEntry[] {
  const chronological = [...(events ?? [])].sort((left, right) => {
    const minuteDelta = (left.trigger_minute ?? 0) - (right.trigger_minute ?? 0)
    if (minuteDelta !== 0) {
      return minuteDelta
    }
    return (left.id ?? 0) - (right.id ?? 0)
  })

  const entries: LiveLogEntry[] = []
  for (const event of chronological) {
    const payload = asRecord(event.payload_json)
    const receivedAt = Date.parse(event.created_at) || Date.now()
    entries.push({
      id: `persisted-event-${event.id}`,
      receivedAt,
      message: {
        type: 'event',
        workday_plan_id: workdayPlanId,
        event_type: event.event_type,
        trigger_minute: event.trigger_minute ?? 0,
        payload,
      },
    })

    const reoptimization = asRecord(payload.reoptimization)
    if (Object.keys(reoptimization).length === 0) {
      continue
    }
    entries.push({
      id: `persisted-reopt-${event.id}`,
      receivedAt,
      message: {
        type: 'reoptimization',
        workday_plan_id: workdayPlanId,
        trigger_description:
          typeof reoptimization.trigger_description === 'string'
            ? reoptimization.trigger_description
            : 'Re-optimization',
        triggered_at_minute: asFiniteNumber(
          reoptimization.triggered_at_minute,
          event.trigger_minute ?? 0,
        ),
        iterations_completed: asFiniteNumber(reoptimization.iterations_completed),
        elapsed_seconds: asFiniteNumber(reoptimization.elapsed_seconds),
        cost_before: asFiniteNumber(reoptimization.cost_before),
        cost_after: asFiniteNumber(reoptimization.cost_after),
        feasible_before: asBoolean(reoptimization.feasible_before, true),
        feasible_after: asBoolean(reoptimization.feasible_after, true),
        locked_prefixes_respected: asBoolean(reoptimization.locked_prefixes_respected, true),
      },
    })
  }

  // Match the live feed's newest-first ordering.
  return entries.reverse().slice(0, EVENT_LOG_CAPACITY)
}

interface SimulationStoreState {
  // -- Workday list --------------------------------------------------------
  workdays: WorkdayPlanSummary[]
  isLoadingWorkdays: boolean
  workdaysError: string | null

  // -- Selected workday plan ------------------------------------------------
  activePlan: WorkdayPlanDetail | null
  isLoadingActivePlan: boolean
  activePlanError: string | null

  // -- Static optimization ---------------------------------------------------
  isOptimizing: boolean
  optimizeError: string | null

  // -- Street-following route geometry ---------------------------------------
  routeGeometry: WorkdayRouteGeometry | null
  isLoadingRouteGeometry: boolean
  routeGeometryError: string | null

  // -- Live WebSocket connection ---------------------------------------------
  connectionStatus: LiveConnectionStatus
  connectionDetail: string | null
  clock: SimulationClockState | null
  vehiclesById: Record<string, VehicleTelemetry>
  eventLog: LiveLogEntry[]

  // -- Event injection ---------------------------------------------------------
  eligibleUrgentOrderNodes: EligibleUrgentOrderNode[]
  isLoadingEligibleNodes: boolean
  isInjectingTrafficIncident: boolean
  isInjectingUrgentOrder: boolean
  injectionError: string | null

  // -- Actions ---------------------------------------------------------------
  fetchWorkdays: () => Promise<void>
  selectWorkday: (workdayId: number) => Promise<void>
  clearSelectedWorkday: () => void
  refreshActivePlan: () => Promise<void>
  fetchRouteGeometry: () => Promise<void>
  optimizeActivePlan: () => Promise<void>
  fetchEligibleUrgentOrderNodes: () => Promise<void>
  injectTrafficIncident: (payload: TrafficIncidentInjectionRequest) => Promise<void>
  injectUrgentOrder: (payload: UrgentOrderInjectionRequest) => Promise<void>
  clearEventLog: () => void
}

const liveSocket = new LiveSimulationSocket()

export const useSimulationStore = create<SimulationStoreState>((set, get) => {
  function appendLogEntry(message: LiveSimulationMessage): void {
    set((state) => ({
      eventLog: [{ id: nextLogEntryId(), receivedAt: Date.now(), message }, ...state.eventLog].slice(
        0,
        EVENT_LOG_CAPACITY,
      ),
    }))
  }

  /**
   * Optimistically append a traffic-incident audit row from a live `event`
   * message so the closure layer can paint before the subsequent plan refresh
   * lands. The next `refreshActivePlan` replaces this with the authoritative
   * server row (stable `id`).
   */
  function mergeTrafficIncidentFromEvent(message: Extract<LiveSimulationMessage, { type: 'event' }>): void {
    if (message.event_type !== 'TRAFFIC_INCIDENT') {
      return
    }
    const currentPlan = get().activePlan
    if (currentPlan === null || currentPlan.id !== message.workday_plan_id) {
      return
    }
    const optimisticEvent: SimulationEventRecord = {
      id: -Date.now(),
      workday_plan_id: message.workday_plan_id,
      event_type: 'TRAFFIC_INCIDENT',
      trigger_minute: message.trigger_minute,
      payload_json: message.payload,
      created_at: new Date().toISOString(),
    }
    set({
      activePlan: {
        ...currentPlan,
        simulation_events: [...(currentPlan.simulation_events ?? []), optimisticEvent],
      },
    })
  }

  function handleLiveMessage(message: LiveSimulationMessage): void {
    switch (message.type) {
      case 'snapshot':
      case 'tick': {
        const vehiclesById: Record<string, VehicleTelemetry> = {}
        for (const vehicle of message.vehicles ?? []) {
          vehiclesById[vehicle.vehicle_id] = vehicle
        }
        set({ clock: message.clock, vehiclesById })
        break
      }
      case 'event':
        appendLogEntry(message)
        mergeTrafficIncidentFromEvent(message)
        break
      case 'reoptimization':
        appendLogEntry(message)
        void get().refreshActivePlan()
        void get().fetchRouteGeometry()
        break
      case 'finished':
        appendLogEntry(message)
        set((state) =>
          state.activePlan ? { activePlan: { ...state.activePlan, status: 'COMPLETED' } } : {},
        )
        liveSocket.disconnect()
        set({ connectionStatus: 'idle', connectionDetail: null })
        void get().refreshActivePlan()
        void get().fetchRouteGeometry()
        break
      case 'error':
        appendLogEntry(message)
        break
    }
  }

  function connectLiveSocket(workdayId: number): void {
    liveSocket.connect(
      workdayId,
      {
        onStatusChange: (status, detail) => set({ connectionStatus: status, connectionDetail: detail ?? null }),
        onMessage: handleLiveMessage,
      },
      DEFAULT_TICK_INTERVAL_SECONDS,
    )
  }

  return {
    workdays: [],
    isLoadingWorkdays: false,
    workdaysError: null,

    activePlan: null,
    isLoadingActivePlan: false,
    activePlanError: null,

    isOptimizing: false,
    optimizeError: null,

    routeGeometry: null,
    isLoadingRouteGeometry: false,
    routeGeometryError: null,

    connectionStatus: 'idle',
    connectionDetail: null,
    clock: null,
    vehiclesById: {},
    eventLog: [],

    eligibleUrgentOrderNodes: [],
    isLoadingEligibleNodes: false,
    isInjectingTrafficIncident: false,
    isInjectingUrgentOrder: false,
    injectionError: null,

    async fetchWorkdays() {
      set({ isLoadingWorkdays: true, workdaysError: null })
      try {
        const workdays = await listWorkdays()
        set({ workdays, isLoadingWorkdays: false })
      } catch (error) {
        set({ isLoadingWorkdays: false, workdaysError: describeError(error, 'Failed to load workday plans.') })
      }
    },

    async selectWorkday(workdayId: number) {
      liveSocket.disconnect()
      set({
        isLoadingActivePlan: true,
        activePlanError: null,
        connectionStatus: 'idle',
        connectionDetail: null,
        clock: null,
        vehiclesById: {},
        eventLog: [],
        eligibleUrgentOrderNodes: [],
        injectionError: null,
        routeGeometry: null,
        routeGeometryError: null,
      })
      try {
        const plan = normalizeWorkdayPlan(await getWorkday(workdayId))
        set({
          activePlan: plan,
          isLoadingActivePlan: false,
          eventLog: buildEventLogFromSimulationEvents(plan.id, plan.simulation_events),
        })
        if (plan.route_stops.length > 0) {
          void get().fetchRouteGeometry()
        } else {
          set({ routeGeometry: emptyRouteGeometry(plan.id), routeGeometryError: null })
        }
        if (plan.status === 'ACTIVE') {
          connectLiveSocket(plan.id)
        }
      } catch (error) {
        set({
          activePlan: null,
          isLoadingActivePlan: false,
          activePlanError: describeError(error, 'Failed to load the selected workday plan.'),
        })
      }
    },

    clearSelectedWorkday() {
      liveSocket.disconnect()
      set({
        activePlan: null,
        activePlanError: null,
        connectionStatus: 'idle',
        connectionDetail: null,
        clock: null,
        vehiclesById: {},
        eventLog: [],
        eligibleUrgentOrderNodes: [],
        routeGeometry: null,
        routeGeometryError: null,
      })
    },

    async refreshActivePlan() {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      try {
        const plan = normalizeWorkdayPlan(await getWorkday(currentPlan.id))
        // Ignore stale responses if the dispatcher switched plans mid-flight.
        if (get().activePlan?.id !== plan.id) {
          return
        }
        set({
          activePlan: plan,
          // Preserve live-session log entries when present; otherwise restore from DB.
          eventLog:
            get().eventLog.length > 0
              ? get().eventLog
              : buildEventLogFromSimulationEvents(plan.id, plan.simulation_events),
        })
      } catch {
        // Keep the previous plan snapshot; a transient refresh failure must not blank the UI.
      }
    },

    async fetchRouteGeometry() {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      if (currentPlan.route_stops.length === 0) {
        set({
          routeGeometry: emptyRouteGeometry(currentPlan.id),
          isLoadingRouteGeometry: false,
          routeGeometryError: null,
        })
        return
      }
      const workdayId = currentPlan.id
      set({ isLoadingRouteGeometry: true, routeGeometryError: null })
      try {
        const geometry = await getWorkdayRouteGeometry(workdayId)
        if (get().activePlan?.id !== workdayId) {
          return
        }
        const legs = geometry.legs ?? []
        set({
          routeGeometry: { ...geometry, legs },
          isLoadingRouteGeometry: false,
          routeGeometryError:
            legs.length === 0
              ? 'Route geometry returned no street legs; falling back to straight-line segments.'
              : null,
        })
      } catch (error) {
        if (get().activePlan?.id === workdayId) {
          set({
            isLoadingRouteGeometry: false,
            routeGeometryError: describeError(
              error,
              'Failed to load street-following route geometry; falling back to straight-line segments.',
            ),
            // Keep any previous good geometry rather than wiping to Euclidean immediately
            // when a transient error occurs mid-session; only blank when nothing is cached.
            routeGeometry: get().routeGeometry ?? emptyRouteGeometry(workdayId),
          })
        }
      }
    },

    async optimizeActivePlan() {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      set({ isOptimizing: true, optimizeError: null })
      try {
        const result = await optimizeWorkday(currentPlan.id)
        const plan = normalizeWorkdayPlan(result.workday_plan)
        set({ activePlan: plan, isOptimizing: false })
        void get().fetchRouteGeometry()
        connectLiveSocket(plan.id)
        void get().fetchWorkdays()
      } catch (error) {
        set({ isOptimizing: false, optimizeError: describeError(error, 'Failed to optimize the workday plan.') })
      }
    },

    async fetchEligibleUrgentOrderNodes() {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      set({ isLoadingEligibleNodes: true })
      try {
        const nodes = await listEligibleUrgentOrderNodes(currentPlan.id)
        set({ eligibleUrgentOrderNodes: nodes, isLoadingEligibleNodes: false })
      } catch (error) {
        set({
          isLoadingEligibleNodes: false,
          injectionError: describeError(error, 'Failed to load eligible urgent-order nodes.'),
        })
      }
    },

    async injectTrafficIncident(payload: TrafficIncidentInjectionRequest) {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      set({ isInjectingTrafficIncident: true, injectionError: null })
      try {
        await injectTrafficIncidentRequest(currentPlan.id, payload)
        if (get().connectionStatus === 'idle') {
          connectLiveSocket(currentPlan.id)
        }
        set({ isInjectingTrafficIncident: false })
      } catch (error) {
        set({
          isInjectingTrafficIncident: false,
          injectionError: describeError(error, 'Failed to inject the traffic incident.'),
        })
      }
    },

    async injectUrgentOrder(payload: UrgentOrderInjectionRequest) {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      set({ isInjectingUrgentOrder: true, injectionError: null })
      try {
        await injectUrgentOrderRequest(currentPlan.id, payload)
        if (get().connectionStatus === 'idle') {
          connectLiveSocket(currentPlan.id)
        }
        set({ isInjectingUrgentOrder: false })
      } catch (error) {
        set({
          isInjectingUrgentOrder: false,
          injectionError: describeError(error, 'Failed to inject the urgent order.'),
        })
      }
    },

    clearEventLog() {
      set({ eventLog: [] })
    },
  }
})
