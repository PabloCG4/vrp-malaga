import { useEffect, useState, type FormEvent } from 'react'
import { useSimulationStore } from '../../store/simulationStore'

/**
 * Dispatcher actions for the currently selected workday plan: 1-click static
 * optimization for a `DRAFT` plan, and real-time disruption injection
 * (traffic incident / urgent order) for an `ACTIVE` one.
 *
 * Node ids are entered as raw numbers here; Block 2's Leaflet map is the
 * intended replacement for these inputs (click-to-select a street or a
 * delivery point), which is why this panel is deliberately minimal.
 */
export function ControlPanel() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const isOptimizing = useSimulationStore((state) => state.isOptimizing)
  const optimizeError = useSimulationStore((state) => state.optimizeError)
  const optimizeActivePlan = useSimulationStore((state) => state.optimizeActivePlan)

  const eligibleNodes = useSimulationStore((state) => state.eligibleUrgentOrderNodes)
  const isLoadingEligibleNodes = useSimulationStore((state) => state.isLoadingEligibleNodes)
  const fetchEligibleUrgentOrderNodes = useSimulationStore((state) => state.fetchEligibleUrgentOrderNodes)
  const isInjectingTrafficIncident = useSimulationStore((state) => state.isInjectingTrafficIncident)
  const isInjectingUrgentOrder = useSimulationStore((state) => state.isInjectingUrgentOrder)
  const injectionError = useSimulationStore((state) => state.injectionError)
  const injectTrafficIncident = useSimulationStore((state) => state.injectTrafficIncident)
  const injectUrgentOrder = useSimulationStore((state) => state.injectUrgentOrder)

  const [firstNode, setFirstNode] = useState('')
  const [secondNode, setSecondNode] = useState('')
  const [reopenAfterMinutes, setReopenAfterMinutes] = useState('')
  const [trafficDescription, setTrafficDescription] = useState('Traffic incident')

  const [deliveryNode, setDeliveryNode] = useState('')
  const [demandKg, setDemandKg] = useState('10')
  const [orderDescription, setOrderDescription] = useState('Urgent order')

  const isActive = activePlan?.status === 'ACTIVE'

  useEffect(() => {
    if (isActive) {
      void fetchEligibleUrgentOrderNodes()
    }
  }, [isActive, activePlan?.id, fetchEligibleUrgentOrderNodes])

  if (activePlan === null) {
    return (
      <section className="panel">
        <h2>Dispatch Controls</h2>
        <p className="panel__hint">Select a workday plan to enable dispatch controls.</p>
      </section>
    )
  }

  function handleOptimize(): void {
    void optimizeActivePlan()
  }

  function handleInjectTrafficIncident(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    const parsedFirstNode = Number.parseInt(firstNode, 10)
    const parsedSecondNode = Number.parseInt(secondNode, 10)
    if (Number.isNaN(parsedFirstNode) || Number.isNaN(parsedSecondNode)) {
      return
    }
    const parsedReopenAfterMinutes = reopenAfterMinutes.trim().length > 0 ? Number.parseInt(reopenAfterMinutes, 10) : null
    void injectTrafficIncident({
      first_node: parsedFirstNode,
      second_node: parsedSecondNode,
      reopen_after_minutes: parsedReopenAfterMinutes,
      description: trafficDescription,
    })
  }

  function handleInjectUrgentOrder(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    const parsedDeliveryNode = Number.parseInt(deliveryNode, 10)
    const parsedDemand = Number.parseFloat(demandKg)
    if (Number.isNaN(parsedDeliveryNode) || Number.isNaN(parsedDemand)) {
      return
    }
    void injectUrgentOrder({
      delivery_node: parsedDeliveryNode,
      demand: parsedDemand,
      description: orderDescription,
    })
  }

  return (
    <section className="panel">
      <h2>Dispatch Controls</h2>

      <div className="panel__block">
        <p>
          Orders: <strong>{activePlan.orders.length}</strong> &middot; Vehicles:{' '}
          <strong>{activePlan.vehicles.length}</strong> &middot; Route stops:{' '}
          <strong>{activePlan.route_stops.length}</strong>
        </p>
        <p>
          Total cost: <strong>{activePlan.total_cost.toFixed(2)}</strong> &middot; Distance:{' '}
          <strong>{activePlan.total_distance_km.toFixed(2)} km</strong>
        </p>
      </div>

      {activePlan.status === 'DRAFT' && (
        <div className="panel__block">
          <button type="button" className="button button--primary" onClick={handleOptimize} disabled={isOptimizing}>
            {isOptimizing ? 'Optimizing…' : '1-Click Dispatch Optimization'}
          </button>
          {optimizeError && <p className="panel__error">{optimizeError}</p>}
        </div>
      )}

      {isActive && (
        <>
          <form className="panel__block form" onSubmit={handleInjectTrafficIncident}>
            <h3>Inject Traffic Incident</h3>
            <label>
              First node id
              <input value={firstNode} onChange={(event) => setFirstNode(event.target.value)} required inputMode="numeric" />
            </label>
            <label>
              Second node id (adjacent street)
              <input value={secondNode} onChange={(event) => setSecondNode(event.target.value)} required inputMode="numeric" />
            </label>
            <label>
              Reopen after (minutes, optional)
              <input
                value={reopenAfterMinutes}
                onChange={(event) => setReopenAfterMinutes(event.target.value)}
                inputMode="numeric"
              />
            </label>
            <label>
              Description
              <input value={trafficDescription} onChange={(event) => setTrafficDescription(event.target.value)} />
            </label>
            <button type="submit" className="button" disabled={isInjectingTrafficIncident}>
              {isInjectingTrafficIncident ? 'Injecting…' : 'Close Street'}
            </button>
          </form>

          <form className="panel__block form" onSubmit={handleInjectUrgentOrder}>
            <h3>Inject Urgent Order</h3>
            <label>
              Delivery node
              <select value={deliveryNode} onChange={(event) => setDeliveryNode(event.target.value)} required>
                <option value="" disabled>
                  {isLoadingEligibleNodes ? 'Loading eligible nodes…' : 'Select a node'}
                </option>
                {eligibleNodes.map((node) => (
                  <option key={node.node_id} value={node.node_id}>
                    Node {node.node_id} ({node.latitude.toFixed(4)}, {node.longitude.toFixed(4)})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Demand (kg)
              <input value={demandKg} onChange={(event) => setDemandKg(event.target.value)} required inputMode="decimal" />
            </label>
            <label>
              Description
              <input value={orderDescription} onChange={(event) => setOrderDescription(event.target.value)} />
            </label>
            <button type="submit" className="button" disabled={isInjectingUrgentOrder}>
              {isInjectingUrgentOrder ? 'Injecting…' : 'Inject Urgent Order'}
            </button>
          </form>

          {injectionError && <p className="panel__error">{injectionError}</p>}
        </>
      )}

      {activePlan.status === 'COMPLETED' && <p className="panel__hint">This workday has finished simulating.</p>}
    </section>
  )
}
