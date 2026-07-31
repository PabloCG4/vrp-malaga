import { useState, type FormEvent } from 'react'
import { Modal } from './Modal'
import { Spinner } from '../common/Spinner'
import { useSimulationStore } from '../../store/simulationStore'
import { useUiStore } from '../../store/uiStore'

/**
 * Traffic incident / road closure injection dialog.
 *
 * Accepts `first_node`/`second_node` either pre-selected on the map (via the
 * "Road Closure Selection Mode" tool, shown as read-only chips) or entered
 * manually when opened directly from the Dispatch Controls panel, and always
 * prompts for the optional `reopen_after_minutes` delay.
 */
export function TrafficIncidentModal() {
  const isOpen = useUiStore((state) => state.isTrafficModalOpen)
  const prefill = useUiStore((state) => state.trafficModalPrefill)
  const closeModal = useUiStore((state) => state.closeTrafficModal)

  const isInjecting = useSimulationStore((state) => state.isInjectingTrafficIncident)
  const injectionError = useSimulationStore((state) => state.injectionError)
  const injectTrafficIncident = useSimulationStore((state) => state.injectTrafficIncident)

  const [manualFirstNode, setManualFirstNode] = useState('')
  const [manualSecondNode, setManualSecondNode] = useState('')
  const [reopenAfterMinutes, setReopenAfterMinutes] = useState('')
  const [description, setDescription] = useState('Traffic incident')

  const isFromMap = prefill !== null

  function handleClose(): void {
    setManualFirstNode('')
    setManualSecondNode('')
    setReopenAfterMinutes('')
    setDescription('Traffic incident')
    closeModal()
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    const firstNode = isFromMap ? prefill.firstNode : Number.parseInt(manualFirstNode, 10)
    const secondNode = isFromMap ? prefill.secondNode : Number.parseInt(manualSecondNode, 10)
    if (Number.isNaN(firstNode) || Number.isNaN(secondNode)) {
      return
    }
    const parsedReopenAfterMinutes =
      reopenAfterMinutes.trim().length > 0 ? Number.parseInt(reopenAfterMinutes, 10) : null

    await injectTrafficIncident({
      first_node: firstNode,
      second_node: secondNode,
      reopen_after_minutes: parsedReopenAfterMinutes,
      description,
    })

    if (useSimulationStore.getState().injectionError === null) {
      handleClose()
    }
  }

  return (
    <Modal
      title="Inject Traffic Incident"
      subtitle="Closes a street segment and triggers a bounded re-optimization of every affected route."
      isOpen={isOpen}
      onClose={handleClose}
    >
      <form className="flex flex-col gap-3.5" onSubmit={(event) => void handleSubmit(event)}>
        <div className="grid grid-cols-2 gap-3">
          <label className="field-label">
            First node
            {isFromMap ? (
              <span className="rounded-lg border border-warning/40 bg-warning/10 px-2.5 py-1.5 font-mono text-sm text-warning">
                {prefill.firstNode}
              </span>
            ) : (
              <input
                className="field-input"
                value={manualFirstNode}
                onChange={(event) => setManualFirstNode(event.target.value)}
                required
                inputMode="numeric"
                placeholder="e.g. 21497142"
              />
            )}
          </label>
          <label className="field-label">
            Second node
            {isFromMap ? (
              <span className="rounded-lg border border-warning/40 bg-warning/10 px-2.5 py-1.5 font-mono text-sm text-warning">
                {prefill.secondNode}
              </span>
            ) : (
              <input
                className="field-input"
                value={manualSecondNode}
                onChange={(event) => setManualSecondNode(event.target.value)}
                required
                inputMode="numeric"
                placeholder="e.g. 420291106"
              />
            )}
          </label>
        </div>

        <label className="field-label">
          Reopen after (simulated minutes, optional)
          <input
            className="field-input"
            value={reopenAfterMinutes}
            onChange={(event) => setReopenAfterMinutes(event.target.value)}
            inputMode="numeric"
            placeholder="Leave blank to keep the street closed all day"
          />
        </label>

        <label className="field-label">
          Description
          <input className="field-input" value={description} onChange={(event) => setDescription(event.target.value)} />
        </label>

        {injectionError && <p className="text-xs text-danger">{injectionError}</p>}

        <div className="mt-1 flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={handleClose}>
            Cancel
          </button>
          <button type="submit" className="btn-danger" disabled={isInjecting}>
            {isInjecting && <Spinner />}
            {isInjecting ? 'Closing street…' : 'Close Street'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
