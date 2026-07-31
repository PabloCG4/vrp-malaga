import { useSimulationStore } from '../../store/simulationStore'

/**
 * Placeholder for the Leaflet map to be built in Phase 5, Block 2.
 *
 * Deliberately renders a lightweight, functional stand-in (a simple list of
 * live vehicle positions) rather than a static empty box, to prove the data
 * this component will eventually plot on a real map (`activePlan.orders`
 * for stop markers, `vehiclesById`/`clock` for live vehicle positions) is
 * already flowing correctly through the store. Replace the body of this
 * component with a `react-leaflet` `<MapContainer>` in Block 2; every prop
 * it will need is already available from `useSimulationStore` exactly as
 * consumed here.
 */
export function MapPlaceholder() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const vehiclesById = useSimulationStore((state) => state.vehiclesById)

  const vehicles = Object.values(vehiclesById)

  return (
    <section className="map-placeholder">
      <div className="map-placeholder__banner">Leaflet street map arrives in Phase 5, Block 2</div>

      {activePlan === null && <p className="panel__hint">Select a workday plan to preview its stops here.</p>}

      {activePlan !== null && (
        <div className="map-placeholder__body">
          <div className="map-placeholder__column">
            <h3>Stops ({activePlan.orders.length})</h3>
            <ul className="map-placeholder__list">
              {activePlan.orders.slice(0, 12).map((order) => (
                <li key={order.id}>
                  {order.customer_name} &middot; node {order.node_id}
                  {order.is_urgent && <span className="tag tag--urgent">urgent</span>}
                </li>
              ))}
              {activePlan.orders.length > 12 && <li className="panel__hint">…and {activePlan.orders.length - 12} more.</li>}
            </ul>
          </div>

          <div className="map-placeholder__column">
            <h3>Live vehicles ({vehicles.length})</h3>
            <ul className="map-placeholder__list">
              {vehicles.map((vehicle) => (
                <li key={vehicle.vehicle_id}>
                  Vehicle #{vehicle.vehicle_id} at node {vehicle.current_node} &middot; {vehicle.status}
                </li>
              ))}
              {vehicles.length === 0 && <li className="panel__hint">No live telemetry yet.</li>}
            </ul>
          </div>
        </div>
      )}
    </section>
  )
}
