import { Spinner } from '../common/Spinner'
import { useSimulationStore } from '../../store/simulationStore'
import { useUiStore } from '../../store/uiStore'

/**
 * Dispatcher actions for the currently selected workday plan.
 *
 * For a `DRAFT` plan, this is the 1-click static optimization trigger. For
 * an `ACTIVE` one, it opens the two disruption-injection modals (traffic
 * incident / urgent order); the traffic modal can alternatively be opened
 * with `first_node`/`second_node` already chosen on the map via the
 * "Road Closure Selection Mode" tool (see `map/LeafletMap.tsx`).
 */
export function ControlPanel() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const isOptimizing = useSimulationStore((state) => state.isOptimizing)
  const optimizeError = useSimulationStore((state) => state.optimizeError)
  const optimizeActivePlan = useSimulationStore((state) => state.optimizeActivePlan)

  const openTrafficModal = useUiStore((state) => state.openTrafficModal)
  const openUrgentOrderModal = useUiStore((state) => state.openUrgentOrderModal)
  const toggleRoadClosureMode = useUiStore((state) => state.toggleRoadClosureMode)
  const isRoadClosureMode = useUiStore((state) => state.isRoadClosureMode)

  if (activePlan === null) {
    return (
      <section className="panel">
        <h2 className="panel-title">Dispatch Controls</h2>
        <p className="text-sm text-text-muted">Select a workday plan to enable dispatch controls.</p>
      </section>
    )
  }

  const isActive = activePlan.status === 'ACTIVE'

  return (
    <section className="panel">
      <h2 className="panel-title">Dispatch Controls</h2>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
        <div>
          <dt className="text-xs text-text-muted">Orders</dt>
          <dd className="font-semibold text-text-heading">{(activePlan.orders ?? []).length}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Vehicles</dt>
          <dd className="font-semibold text-text-heading">{(activePlan.vehicles ?? []).length}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Total cost</dt>
          <dd className="font-semibold text-text-heading">{(activePlan.total_cost ?? 0).toFixed(1)}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Distance</dt>
          <dd className="font-semibold text-text-heading">{(activePlan.total_distance_km ?? 0).toFixed(1)} km</dd>
        </div>
      </dl>

      {isActive && (
        <div className="flex flex-col gap-2 border-t border-border pt-3">
          <button type="button" className="btn-danger justify-start" onClick={() => toggleRoadClosureMode()}>
            🚧 {isRoadClosureMode ? 'Cancel Road Closure Selection' : 'Select Road Closure on Map'}
          </button>
          <button type="button" className="btn-secondary justify-start" onClick={() => openTrafficModal()}>
            Inject Traffic Incident (manual)
          </button>
          <button type="button" className="btn-secondary justify-start" onClick={openUrgentOrderModal}>
            Inject Urgent Order
          </button>
        </div>
      )}

      {activePlan.status === 'COMPLETED' && (
        <p className="border-t border-border pt-3 text-sm text-text-muted">This workday has finished simulating.</p>
      )}

      {activePlan.status === 'DRAFT' && (
        <div className="border-t border-border pt-3">
          <button
            type="button"
            className="btn-primary w-full"
            onClick={() => void optimizeActivePlan()}
            disabled={isOptimizing}
          >
            {isOptimizing && <Spinner />}
            {isOptimizing ? 'Optimizing route plan…' : '1-Click Dispatch Optimization'}
          </button>
          {optimizeError && <p className="mt-2 text-xs text-danger">{optimizeError}</p>}
        </div>
      )}
    </section>
  )
}
