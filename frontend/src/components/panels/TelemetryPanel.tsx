import { useSimulationStore } from '../../store/simulationStore'

const STATUS_LABELS: Record<string, string> = {
  driving: 'Driving',
  waiting: 'Waiting',
  serving: 'Serving customer',
  on_break: 'EU rest break',
  idle_at_depot: 'Idle at depot',
  finished: 'Finished',
}

/** Live per-vehicle telemetry table, driven entirely by the store's `vehiclesById`. */
export function TelemetryPanel() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const clock = useSimulationStore((state) => state.clock)
  const vehiclesById = useSimulationStore((state) => state.vehiclesById)

  const vehicles = Object.values(vehiclesById).sort((a, b) => a.vehicle_id.localeCompare(b.vehicle_id))

  return (
    <section className="panel">
      <h2>Fleet Telemetry</h2>

      {activePlan === null && <p className="panel__hint">Select an ACTIVE workday plan to see live telemetry.</p>}

      {activePlan !== null && clock === null && (
        <p className="panel__hint">
          {activePlan.status === 'ACTIVE'
            ? 'Connecting to the live simulation…'
            : `This plan is ${activePlan.status.toLowerCase()}; no live telemetry to show.`}
        </p>
      )}

      {clock !== null && (
        <div className="panel__block">
          <p>
            Simulated time: <strong>{clock.formatted_time}</strong> (minute {clock.current_minute}
            {clock.is_finished ? ', finished' : ''})
          </p>
        </div>
      )}

      {vehicles.length > 0 && (
        <table className="telemetry-table">
          <thead>
            <tr>
              <th>Vehicle</th>
              <th>Status</th>
              <th>Current node</th>
              <th>Next node</th>
              <th>Active customer</th>
              <th>Locked prefix</th>
            </tr>
          </thead>
          <tbody>
            {vehicles.map((vehicle) => (
              <tr key={vehicle.vehicle_id}>
                <td>#{vehicle.vehicle_id}</td>
                <td>
                  <span className={`vehicle-status vehicle-status--${vehicle.status}`}>
                    {STATUS_LABELS[vehicle.status] ?? vehicle.status}
                  </span>
                </td>
                <td>{vehicle.current_node}</td>
                <td>{vehicle.next_node ?? '—'}</td>
                <td>{vehicle.active_customer_id ?? '—'}</td>
                <td>{vehicle.locked_prefix_length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
