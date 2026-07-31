import { useSimulationStore, type LiveLogEntry } from '../../store/simulationStore'
import type { LiveSimulationMessage } from '../../types/telemetry'

function summarize(message: LiveSimulationMessage): string {
  switch (message.type) {
    case 'event':
      return `Disruption injected: ${message.event_type} at minute ${message.trigger_minute}.`
    case 'reoptimization':
      return (
        `Re-optimized (${message.trigger_description}) at minute ${message.triggered_at_minute}: ` +
        `cost ${message.cost_before.toFixed(1)} -> ${message.cost_after.toFixed(1)}, ` +
        `${message.iterations_completed} iterations in ${message.elapsed_seconds.toFixed(2)}s ` +
        `(${message.feasible_after ? 'feasible' : 'INFEASIBLE'}).`
      )
    case 'finished':
      return `Workday finished. Final cost ${message.final_cost.toFixed(1)} (${message.is_feasible ? 'feasible' : 'infeasible'}).`
    case 'error':
      return `Simulation error: ${message.detail}`
    case 'snapshot':
    case 'tick':
      return `Telemetry tick at minute ${message.clock.current_minute}.`
  }
}

function badgeVariant(message: LiveSimulationMessage): string {
  if (message.type === 'error') return 'error'
  if (message.type === 'event') return 'event'
  if (message.type === 'reoptimization') return 'reoptimization'
  if (message.type === 'finished') return 'finished'
  return 'tick'
}

/** Rolling, human-readable audit feed of disruption/re-optimization/lifecycle messages observed live. */
export function EventLogPanel() {
  const eventLog = useSimulationStore((state) => state.eventLog)
  const clearEventLog = useSimulationStore((state) => state.clearEventLog)

  return (
    <section className="panel">
      <div className="panel__header">
        <h2>Live Activity Log</h2>
        <button type="button" className="button button--ghost" onClick={clearEventLog} disabled={eventLog.length === 0}>
          Clear
        </button>
      </div>

      {eventLog.length === 0 && <p className="panel__hint">No disruptions observed yet for this session.</p>}

      <ul className="event-log">
        {eventLog.map((entry: LiveLogEntry) => (
          <li key={entry.id} className={`event-log__item event-log__item--${badgeVariant(entry.message)}`}>
            <span className="event-log__time">{new Date(entry.receivedAt).toLocaleTimeString()}</span>
            <span className="event-log__text">{summarize(entry.message)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
