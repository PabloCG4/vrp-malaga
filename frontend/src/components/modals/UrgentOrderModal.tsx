import { useEffect, useState, type FormEvent } from 'react'
import { Modal } from './Modal'
import { Spinner } from '../common/Spinner'
import { useSimulationStore } from '../../store/simulationStore'
import { useUiStore } from '../../store/uiStore'

/**
 * Urgent, same-day VRPPD order injection dialog ("VIP order").
 *
 * The destination list is fetched on open from
 * `GET /api/v1/workdays/{id}/events/urgent-order-nodes`: the live session's
 * cost matrix is a fixed-size structure, so only this pre-reserved pool of
 * nodes may legally be targeted (see `services/live_simulation.py`).
 */
export function UrgentOrderModal() {
  const isOpen = useUiStore((state) => state.isUrgentOrderModalOpen)
  const closeModal = useUiStore((state) => state.closeUrgentOrderModal)

  const eligibleNodes = useSimulationStore((state) => state.eligibleUrgentOrderNodes)
  const isLoadingEligibleNodes = useSimulationStore((state) => state.isLoadingEligibleNodes)
  const fetchEligibleUrgentOrderNodes = useSimulationStore((state) => state.fetchEligibleUrgentOrderNodes)
  const isInjecting = useSimulationStore((state) => state.isInjectingUrgentOrder)
  const injectionError = useSimulationStore((state) => state.injectionError)
  const injectUrgentOrder = useSimulationStore((state) => state.injectUrgentOrder)

  const [deliveryNode, setDeliveryNode] = useState('')
  const [demandKg, setDemandKg] = useState('10')
  const [deadlineMinutes, setDeadlineMinutes] = useState('90')
  const [description, setDescription] = useState('Urgent order')

  useEffect(() => {
    if (isOpen) {
      void fetchEligibleUrgentOrderNodes()
    }
  }, [isOpen, fetchEligibleUrgentOrderNodes])

  function handleClose(): void {
    setDeliveryNode('')
    setDemandKg('10')
    setDeadlineMinutes('90')
    setDescription('Urgent order')
    closeModal()
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    const parsedDeliveryNode = Number.parseInt(deliveryNode, 10)
    const parsedDemand = Number.parseFloat(demandKg)
    const parsedDeadline = Number.parseFloat(deadlineMinutes)
    if (Number.isNaN(parsedDeliveryNode) || Number.isNaN(parsedDemand) || Number.isNaN(parsedDeadline)) {
      return
    }

    await injectUrgentOrder({
      delivery_node: parsedDeliveryNode,
      demand: parsedDemand,
      deadline_minutes_after_trigger: parsedDeadline,
      description,
    })

    if (useSimulationStore.getState().injectionError === null) {
      handleClose()
    }
  }

  return (
    <Modal
      title="Inject Urgent Order"
      subtitle="Models a same-day depot pickup + customer delivery pair (VRPPD) and re-optimizes the fleet to fit it in."
      isOpen={isOpen}
      onClose={handleClose}
    >
      <form className="flex flex-col gap-3.5" onSubmit={(event) => void handleSubmit(event)}>
        <label className="field-label">
          Destination node
          <select
            className="field-input"
            value={deliveryNode}
            onChange={(event) => setDeliveryNode(event.target.value)}
            required
          >
            <option value="" disabled>
              {isLoadingEligibleNodes ? 'Loading eligible destinations…' : 'Select a destination'}
            </option>
            {eligibleNodes.map((node) => (
              <option key={node.node_id} value={node.node_id}>
                Node {node.node_id} ({node.latitude.toFixed(4)}, {node.longitude.toFixed(4)})
              </option>
            ))}
          </select>
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="field-label">
            Demand (kg)
            <input
              className="field-input"
              value={demandKg}
              onChange={(event) => setDemandKg(event.target.value)}
              required
              inputMode="decimal"
            />
          </label>
          <label className="field-label">
            Deadline (minutes)
            <input
              className="field-input"
              value={deadlineMinutes}
              onChange={(event) => setDeadlineMinutes(event.target.value)}
              required
              inputMode="numeric"
            />
          </label>
        </div>

        <label className="field-label">
          Description
          <input className="field-input" value={description} onChange={(event) => setDescription(event.target.value)} />
        </label>

        {injectionError && <p className="text-xs text-danger">{injectionError}</p>}

        <div className="mt-1 flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={handleClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={isInjecting || deliveryNode === ''}>
            {isInjecting && <Spinner />}
            {isInjecting ? 'Injecting…' : 'Inject Urgent Order'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
