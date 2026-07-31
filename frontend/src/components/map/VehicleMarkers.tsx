import { useMemo } from 'react'
import { Marker, Popup } from 'react-leaflet'
import { createVehicleIcon } from './mapIcons'
import { getVehicleColor } from './vehicleColors'
import { useNetworkStore } from '../../store/networkStore'
import type { Vehicle } from '../../types/domain'
import type { VehicleTelemetry } from '../../types/telemetry'

const STATUS_LABELS: Record<string, string> = {
  driving: 'Driving',
  waiting: 'Waiting',
  serving: 'Serving customer',
  on_break: 'EU rest break',
  idle_at_depot: 'Idle at depot',
  finished: 'Finished',
}

interface VehicleMarkerProps {
  vehicle: Vehicle
  telemetry: VehicleTelemetry
}

function VehicleMarker({ vehicle, telemetry }: VehicleMarkerProps) {
  const getNode = useNetworkStore((state) => state.getNode)
  const position = getNode(telemetry.current_node)
  const color = useMemo(() => getVehicleColor(vehicle.id), [vehicle.id])
  const icon = useMemo(() => createVehicleIcon(color, vehicle.license_plate.slice(0, 3)), [color, vehicle.license_plate])

  if (position === undefined) {
    return null
  }

  return (
    <Marker position={[position.latitude, position.longitude]} icon={icon} zIndexOffset={1000}>
      <Popup>
        <div className="text-sm">
          <p className="font-semibold text-text-heading">{vehicle.license_plate}</p>
          <dl className="mt-1.5 grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
            <dt className="text-text-muted">Status</dt>
            <dd className="text-text-heading">{STATUS_LABELS[telemetry.status] ?? telemetry.status}</dd>
            <dt className="text-text-muted">Current node</dt>
            <dd className="font-mono text-text-heading">{telemetry.current_node}</dd>
            <dt className="text-text-muted">Next node</dt>
            <dd className="font-mono text-text-heading">{telemetry.next_node ?? '—'}</dd>
            <dt className="text-text-muted">Stops locked</dt>
            <dd className="text-text-heading">{telemetry.locked_prefix_length}</dd>
          </dl>
        </div>
      </Popup>
    </Marker>
  )
}

interface VehicleMarkersProps {
  vehicles: Vehicle[]
  vehiclesById: Record<string, VehicleTelemetry>
}

/** Live, per-vehicle position markers, colored consistently with their route polyline. */
export function VehicleMarkers({ vehicles, vehiclesById }: VehicleMarkersProps) {
  const safeVehicles = vehicles ?? []
  const safeVehiclesById = vehiclesById ?? {}
  return (
    <>
      {safeVehicles.map((vehicle) => {
        const telemetry = safeVehiclesById[String(vehicle.id)]
        return telemetry ? <VehicleMarker key={vehicle.id} vehicle={vehicle} telemetry={telemetry} /> : null
      })}
    </>
  )
}
