import { useMemo } from 'react'
import { Polyline } from 'react-leaflet'
import { buildVehicleRouteSegments } from './routeSegments'
import { getVehicleColor } from './vehicleColors'
import { useNetworkStore } from '../../store/networkStore'
import { useSimulationStore } from '../../store/simulationStore'
import type { RouteStop, Vehicle } from '../../types/domain'
import type { VehicleTelemetry } from '../../types/telemetry'

interface VehicleRouteProps {
  vehicle: Vehicle
  routeStops: RouteStop[]
  lockedPrefixLength: number
}

function VehicleRoute({ vehicle, routeStops, lockedPrefixLength }: VehicleRouteProps) {
  const getNode = useNetworkStore((state) => state.getNode)
  const routeGeometry = useSimulationStore((state) => state.routeGeometry)
  const color = getVehicleColor(vehicle.id)
  const geometryLegs = routeGeometry?.legs ?? null
  const segments = useMemo(
    () => buildVehicleRouteSegments(vehicle.id, routeStops ?? [], getNode, lockedPrefixLength, geometryLegs),
    [vehicle.id, routeStops, getNode, lockedPrefixLength, geometryLegs],
  )

  return (
    <>
      {(segments ?? []).map((segment) => (
        <Polyline
          key={segment.key}
          positions={segment.positions}
          pathOptions={{
            color,
            weight: segment.isTraversed ? 3 : 5,
            opacity: segment.isTraversed ? 0.35 : 0.95,
            dashArray: segment.isTraversed ? '4,6' : undefined,
          }}
        />
      ))}
    </>
  )
}

interface RouteLayerProps {
  vehicles: Vehicle[] | null | undefined
  routeStops: RouteStop[] | null | undefined
  vehiclesById: Record<string, VehicleTelemetry> | null | undefined
  /** When true, every segment is styled as fully traversed (COMPLETED workdays). */
  forceFullyTraversed?: boolean
}

/**
 * Renders every vehicle's planned route as a distinctly-colored polyline,
 * preferring street-following geometry from the backend and dimming legs
 * already committed to via `locked_prefix_length`.
 */
export function RouteLayer({
  vehicles,
  routeStops,
  vehiclesById,
  forceFullyTraversed = false,
}: RouteLayerProps) {
  const safeVehicles = vehicles ?? []
  const safeVehiclesById = vehiclesById ?? {}

  const totalCustomerStopsByVehicle = useMemo(() => {
    const counts = new Map<number, number>()
    for (const stop of routeStops ?? []) {
      if (stop?.stop_type === 'CUSTOMER_DELIVERY' || stop?.stop_type === 'DEPOT_PICKUP') {
        counts.set(stop.vehicle_id, (counts.get(stop.vehicle_id) ?? 0) + 1)
      }
    }
    return counts
  }, [routeStops])

  return (
    <>
      {safeVehicles.map((vehicle) => {
        const totalCustomerStops = totalCustomerStopsByVehicle.get(vehicle.id) ?? 0
        const lockedPrefixLength = forceFullyTraversed
          ? totalCustomerStops
          : (safeVehiclesById[String(vehicle.id)]?.locked_prefix_length ?? 0)
        return (
          <VehicleRoute
            key={vehicle.id}
            vehicle={vehicle}
            routeStops={routeStops ?? []}
            lockedPrefixLength={lockedPrefixLength}
          />
        )
      })}
    </>
  )
}
