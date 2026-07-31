/**
 * Global simulation store (Zustand).
 *
 * This is the single source of truth the rest of the frontend (dashboard
 * layout, map, control panels, telemetry views) reads from and dispatches
 * actions against. It owns:
 *
 *   - The list of workday plans and whichever one is currently selected
 *     (`workdays`, `activePlan`).
 *   - The one, module-level `LiveSimulationSocket` connection for the
 *     selected plan, and the live telemetry it produces (`connectionStatus`,
 *     `clock`, `vehiclesById`).
 *   - A rolling audit log of every `event`/`reoptimization`/`finished`/`error`
 *     message observed, for a dispatcher-facing activity feed.
 *   - Thin async actions wrapping `api/workdaysApi.ts` and `api/eventsApi.ts`,
 *     so components never call `fetch`/the API layer directly.
 *
 * No React component holds simulation state locally: every panel is a thin
 * view over `useSimulationStore`, which keeps the map and control panels
 * planned for Block 2 trivial to add without re-plumbing data flow.
 */

import { create } from 'zustand'
import { getWorkday, listWorkdays, optimizeWorkday } from '../api/workdaysApi'
import { injectTrafficIncident as injectTrafficIncidentRequest, injectUrgentOrder as injectUrgentOrderRequest, listEligibleUrgentOrderNodes } from '../api/eventsApi'
import { ApiError } from '../api/httpClient'
import { DEFAULT_TICK_INTERVAL_SECONDS, EVENT_LOG_CAPACITY } from '../config/env'
import { LiveSimulationSocket, type LiveConnectionStatus } from '../services/liveSimulationSocket'
import type {
  EligibleUrgentOrderNode,
  TrafficIncidentInjectionRequest,
  UrgentOrderInjectionRequest,
  WorkdayPlanDetail,
  WorkdayPlanSummary,
} from '../types/domain'
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
  optimizeActivePlan: () => Promise<void>
  fetchEligibleUrgentOrderNodes: () => Promise<void>
  injectTrafficIncident: (payload: TrafficIncidentInjectionRequest) => Promise<void>
  injectUrgentOrder: (payload: UrgentOrderInjectionRequest) => Promise<void>
  clearEventLog: () => void
}

/**
 * Single, module-level WebSocket connection shared by the whole app.
 *
 * Deliberately not stored in Zustand state itself (a class instance is not
 * meaningfully serializable/comparable state); only the *data it produces*
 * (`connectionStatus`, `clock`, `vehiclesById`, `eventLog`) lives in the store.
 */
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

  function handleLiveMessage(message: LiveSimulationMessage): void {
    switch (message.type) {
      case 'snapshot':
      case 'tick': {
        const vehiclesById: Record<string, VehicleTelemetry> = {}
        for (const vehicle of message.vehicles) {
          vehiclesById[vehicle.vehicle_id] = vehicle
        }
        set({ clock: message.clock, vehiclesById })
        break
      }
      case 'event':
      case 'reoptimization':
        appendLogEntry(message)
        break
      case 'finished':
        appendLogEntry(message)
        set((state) =>
          state.activePlan ? { activePlan: { ...state.activePlan, status: 'COMPLETED' } } : {},
        )
        liveSocket.disconnect()
        set({ connectionStatus: 'idle', connectionDetail: null })
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
      })
      try {
        const plan = await getWorkday(workdayId)
        set({ activePlan: plan, isLoadingActivePlan: false })
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
      })
    },

    async optimizeActivePlan() {
      const currentPlan = get().activePlan
      if (currentPlan === null) {
        return
      }
      set({ isOptimizing: true, optimizeError: null })
      try {
        const result = await optimizeWorkday(currentPlan.id)
        set({ activePlan: result.workday_plan, isOptimizing: false })
        connectLiveSocket(result.workday_plan.id)
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
