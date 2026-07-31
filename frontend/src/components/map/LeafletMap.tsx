import { useEffect } from 'react'
import { MapContainer, TileLayer } from 'react-leaflet'
import { DepotMarker } from './DepotMarker'
import { OrderMarkers } from './OrderMarkers'
import { VehicleMarkers } from './VehicleMarkers'
import { RouteLayer } from './RouteLayer'
import { ActiveClosuresLayer } from './ActiveClosuresLayer'
import { RoadClosureInteractionLayer } from './RoadClosureInteractionLayer'
import { RecenterOnDepot } from './RecenterOnDepot'
import { TileToggleControl, RoadClosureBanner } from './MapOverlayControls'
import { useNetworkStore } from '../../store/networkStore'
import { useSimulationStore } from '../../store/simulationStore'
import { useUiStore } from '../../store/uiStore'

/** Malaga city center, used as the map's fallback view before the depot's real coordinates load. */
const MALAGA_FALLBACK_CENTER: [number, number] = [36.7213, -4.4214]

const DARK_TILE_URL = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
const DARK_TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'

const LIGHT_TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
const LIGHT_TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

/**
 * Interactive Leaflet map centered on the regional depot.
 *
 * Renders street-following route polylines, live/historical fleet markers,
 * persisted active traffic closures, and the interactive road-closure tool.
 */
export function LeafletMap() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const vehiclesById = useSimulationStore((state) => state.vehiclesById)

  const fetchNetwork = useNetworkStore((state) => state.fetchNetwork)
  const isLoadingNetwork = useNetworkStore((state) => state.isLoading)
  const networkError = useNetworkStore((state) => state.error)
  const depotNodeId = useNetworkStore((state) => state.depotNodeId)
  const getNode = useNetworkStore((state) => state.getNode)

  const tileMode = useUiStore((state) => state.tileMode)
  const routeGeometryError = useSimulationStore((state) => state.routeGeometryError)

  useEffect(() => {
    void fetchNetwork()
  }, [fetchNetwork])

  const depotNode = depotNodeId !== null ? getNode(depotNodeId) : undefined
  const isCompletedPlan = activePlan?.status === 'COMPLETED'

  return (
    <div className="relative h-full w-full overflow-hidden rounded-xl border border-border">
      <MapContainer center={MALAGA_FALLBACK_CENTER} zoom={13} scrollWheelZoom className="h-full w-full">
        {tileMode === 'dark' ? (
          <TileLayer url={DARK_TILE_URL} attribution={DARK_TILE_ATTRIBUTION} />
        ) : (
          <TileLayer url={LIGHT_TILE_URL} attribution={LIGHT_TILE_ATTRIBUTION} />
        )}

        {depotNode && <RecenterOnDepot latitude={depotNode.latitude} longitude={depotNode.longitude} />}
        {depotNode && <DepotMarker depotNode={depotNode} />}

        {activePlan && (
          <OrderMarkers orders={activePlan.orders ?? []} routeStops={activePlan.route_stops ?? []} />
        )}
        {activePlan && (
          <RouteLayer
            vehicles={activePlan.vehicles ?? []}
            routeStops={activePlan.route_stops ?? []}
            vehiclesById={vehiclesById ?? {}}
            forceFullyTraversed={isCompletedPlan}
          />
        )}
        {activePlan && !isCompletedPlan && (
          <VehicleMarkers vehicles={activePlan.vehicles ?? []} vehiclesById={vehiclesById ?? {}} />
        )}

        <ActiveClosuresLayer />
        <RoadClosureInteractionLayer />
      </MapContainer>

      <TileToggleControl />
      <RoadClosureBanner />

      {isLoadingNetwork && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 z-[500] flex justify-center">
          <span className="rounded-full border border-border-strong bg-surface-raised/90 px-3 py-1 text-xs text-text-muted backdrop-blur">
            Loading street network…
          </span>
        </div>
      )}
      {networkError && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 z-[500] flex justify-center">
          <span className="rounded-full border border-danger/40 bg-surface-raised/90 px-3 py-1 text-xs text-danger backdrop-blur">
            {networkError}
          </span>
        </div>
      )}
      {!networkError && routeGeometryError && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 z-[500] flex justify-center">
          <span className="rounded-full border border-warning/40 bg-surface-raised/90 px-3 py-1 text-xs text-warning backdrop-blur">
            {routeGeometryError}
          </span>
        </div>
      )}
    </div>
  )
}
