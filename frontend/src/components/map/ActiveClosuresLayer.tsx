import { Fragment } from 'react'
import { Marker, Polyline, Popup } from 'react-leaflet'
import L from 'leaflet'
import { useNetworkStore } from '../../store/networkStore'
import { useSimulationStore } from '../../store/simulationStore'
import type { SimulationEventRecord } from '../../types/domain'
import { WORKDAY_DURATION_MINUTES } from '../../utils/time'

const WARNING_ICON = L.divIcon({
  html: `<div class="flex h-6 w-6 items-center justify-center rounded-full border-2 border-white bg-danger text-xs shadow-lg shadow-black/50">⚠</div>`,
  className: '',
  iconSize: [24, 24],
  iconAnchor: [12, 12],
})

interface ActiveClosure {
  key: string
  firstNodeId: number
  secondNodeId: number
  description: string
  isHistorical: boolean
}

function asPayloadRecord(payload: unknown): Record<string, unknown> {
  if (typeof payload === 'object' && payload !== null && !Array.isArray(payload)) {
    return payload as Record<string, unknown>
  }
  return {}
}

function readNodeId(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function readReopenAfterMinutes(payload: Record<string, unknown>): number | null {
  const value = payload.reopen_after_minutes
  if (value === null || value === undefined) {
    return null
  }
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function isStillClosedAtMinute(event: SimulationEventRecord, currentMinute: number): boolean {
  if ((event.trigger_minute ?? Number.POSITIVE_INFINITY) > currentMinute) {
    return false
  }
  const reopenAfterMinutes = readReopenAfterMinutes(asPayloadRecord(event.payload_json))
  if (reopenAfterMinutes === null) {
    return true
  }
  return currentMinute < (event.trigger_minute ?? 0) + reopenAfterMinutes
}

/**
 * Collect traffic closures to draw on the map.
 *
 * - ACTIVE with a live clock: only streets still closed at `current_minute`.
 * - ACTIVE before the clock arrives, or COMPLETED: show the full persisted
 *   closure history so navigating away and back never blanks the overlay.
 * - Rows marked `reopened: true` (street reopen audit) are never drawn as closures.
 */
function collectClosuresForDisplay(
  events: SimulationEventRecord[] | null | undefined,
  options: { currentMinute: number | null; showHistorical: boolean },
): ActiveClosure[] {
  const closures: ActiveClosure[] = []
  for (const event of events ?? []) {
    if (event?.event_type !== 'TRAFFIC_INCIDENT') {
      continue
    }
    const payload = asPayloadRecord(event.payload_json)
    if (payload.reopened === true) {
      continue
    }
    const firstNodeId = readNodeId(payload, 'first_node')
    const secondNodeId = readNodeId(payload, 'second_node')
    if (firstNodeId === null || secondNodeId === null) {
      continue
    }

    const stillClosed =
      options.currentMinute === null
        ? true
        : isStillClosedAtMinute(event, options.currentMinute)

    if (!stillClosed && !options.showHistorical) {
      continue
    }

    const description = typeof payload.description === 'string' ? payload.description : 'Traffic incident'
    closures.push({
      key: `closure-${event.id ?? firstNodeId}-${firstNodeId}-${secondNodeId}`,
      firstNodeId,
      secondNodeId,
      description,
      isHistorical: !stillClosed,
    })
  }
  return closures
}

/**
 * Renders traffic incidents from the plan's persisted `simulation_events`
 * (and live optimistic merges) as warning polylines on the map.
 */
export function ActiveClosuresLayer() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const clock = useSimulationStore((state) => state.clock)
  const getNode = useNetworkStore((state) => state.getNode)

  if (activePlan === null || activePlan.status === 'DRAFT') {
    return null
  }

  const isCompleted = activePlan.status === 'COMPLETED'
  // Before the live clock arrives, do not treat minute 0 as "now" — that
  // incorrectly hides every closure that triggered after the workday start.
  const currentMinute = isCompleted
    ? WORKDAY_DURATION_MINUTES
    : clock != null
      ? (clock.current_minute ?? 0)
      : null
  const showHistorical = isCompleted || currentMinute === null

  const closures = collectClosuresForDisplay(activePlan.simulation_events, {
    currentMinute,
    showHistorical,
  })

  return (
    <>
      {closures.map((closure) => {
        const firstNode = getNode(closure.firstNodeId)
        const secondNode = getNode(closure.secondNodeId)
        if (firstNode === undefined || secondNode === undefined) {
          return null
        }
        const midLat = (firstNode.latitude + secondNode.latitude) / 2
        const midLon = (firstNode.longitude + secondNode.longitude) / 2
        return (
          <Fragment key={closure.key}>
            <Polyline
              positions={[
                [firstNode.latitude, firstNode.longitude],
                [secondNode.latitude, secondNode.longitude],
              ]}
              pathOptions={{
                color: closure.isHistorical ? '#fb923c' : '#f2555a',
                weight: closure.isHistorical ? 4 : 6,
                dashArray: '10,8',
                opacity: closure.isHistorical ? 0.55 : 0.95,
              }}
            />
            <Marker position={[midLat, midLon]} icon={WARNING_ICON} zIndexOffset={2000}>
              <Popup>
                <div className="text-sm">
                  <p className="font-semibold text-danger">
                    {closure.isHistorical ? 'Historical road closure' : 'Active road closure'}
                  </p>
                  <p className="mt-1 text-xs text-text-muted">{closure.description}</p>
                  <p className="mt-1 font-mono text-[11px] text-text-muted">
                    {closure.firstNodeId} ↔ {closure.secondNodeId}
                  </p>
                </div>
              </Popup>
            </Marker>
          </Fragment>
        )
      })}
    </>
  )
}
