import { useSimulationStore, type LiveLogEntry } from '../../store/simulationStore'
import type { LiveSimulationMessage, ReoptimizationMessage } from '../../types/telemetry'
import { formatWorkdayMinutes, WORKDAY_DURATION_MINUTES } from '../../utils/time'

function badgeVariant(message: LiveSimulationMessage): { label: string; dotColor: string } {
  switch (message?.type) {
    case 'event':
      return {
        label: message.event_type === 'TRAFFIC_INCIDENT' ? 'Traffic Incident' : 'Urgent Order',
        dotColor: 'bg-warning',
      }
    case 'reoptimization':
      return { label: 'Re-optimization', dotColor: 'bg-accent' }
    case 'finished':
      return { label: 'Workday Finished', dotColor: 'bg-success' }
    case 'error':
      return { label: 'Error', dotColor: 'bg-danger' }
    default:
      return { label: 'Telemetry', dotColor: 'bg-text-muted' }
  }
}

function ReoptimizationCostComparison({ message }: { message: ReoptimizationMessage }) {
  const costBefore = message.cost_before ?? 0
  const costAfter = message.cost_after ?? 0
  const costDelta = costAfter - costBefore
  const improved = costDelta <= 0
  return (
    <div className="mt-1.5 flex items-center gap-2 rounded-md bg-surface px-2 py-1.5 text-xs">
      <span className="text-text-muted line-through decoration-danger/60">{costBefore.toFixed(1)}</span>
      <span aria-hidden="true">→</span>
      <span className={`font-semibold ${improved ? 'text-success' : 'text-danger'}`}>{costAfter.toFixed(1)}</span>
      <span
        className={`ml-auto rounded-full px-1.5 py-0.5 text-[10px] font-bold ${improved ? 'bg-success/15 text-success' : 'bg-danger/15 text-danger'}`}
      >
        {improved ? '▼' : '▲'} {Math.abs(costDelta).toFixed(1)}
      </span>
    </div>
  )
}

function summarize(message: LiveSimulationMessage): string {
  switch (message?.type) {
    case 'event':
      return `Disruption injected at simulated minute ${message.trigger_minute ?? 0}.`
    case 'reoptimization':
      return `${message.trigger_description ?? 'Re-optimization'} · ${message.iterations_completed ?? 0} iterations in ${(message.elapsed_seconds ?? 0).toFixed(2)}s (${message.feasible_after ? 'feasible' : 'INFEASIBLE'}).`
    case 'finished':
      return `Final cost ${(message.final_cost ?? 0).toFixed(1)} (${message.is_feasible ? 'feasible' : 'infeasible'}).`
    case 'error':
      return message.detail ?? 'Unknown live simulation error.'
    case 'snapshot':
    case 'tick':
      return `Telemetry tick at minute ${message.clock?.current_minute ?? 0}.`
    default:
      return 'Unknown activity.'
  }
}

/** Visible timeline stamp uses the simulated depot clock, never the browser wall clock. */
function simulationTimestamp(message: LiveSimulationMessage): string {
  switch (message?.type) {
    case 'event':
      return formatWorkdayMinutes(message.trigger_minute ?? 0)
    case 'reoptimization':
      return formatWorkdayMinutes(message.triggered_at_minute ?? 0)
    case 'finished':
      return formatWorkdayMinutes(WORKDAY_DURATION_MINUTES)
    case 'error':
    case 'snapshot':
    case 'tick':
    default:
      return '--:--'
  }
}

function TimelineEntry({ entry }: { entry: LiveLogEntry }) {
  if (entry?.message == null) {
    return null
  }
  const { label, dotColor } = badgeVariant(entry.message)
  return (
    <li className="relative pb-4 pl-5 last:pb-0">
      <span className={`absolute left-0 top-1 h-2.5 w-2.5 rounded-full ring-4 ring-surface-panel ${dotColor}`} />
      <span className="absolute left-[4.5px] top-3.5 bottom-0 w-px bg-border last:hidden" />
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-text-heading">{label}</span>
        <time className="font-mono text-[11px] text-text-muted">{simulationTimestamp(entry.message)}</time>
      </div>
      <p className="mt-0.5 text-xs text-text-muted">{summarize(entry.message)}</p>
      {entry.message.type === 'reoptimization' && <ReoptimizationCostComparison message={entry.message} />}
    </li>
  )
}

/** Rolling, timeline-style audit feed of disruption/re-optimization/lifecycle messages observed live. */
export function EventLogPanel() {
  const eventLog = useSimulationStore((state) => state.eventLog) ?? []
  const clearEventLog = useSimulationStore((state) => state.clearEventLog)

  return (
    <section className="panel">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Live Activity Timeline</h2>
        <button type="button" className="btn-ghost" onClick={clearEventLog} disabled={eventLog.length === 0}>
          Clear
        </button>
      </div>

      {eventLog.length === 0 && (
        <p className="text-sm text-text-muted">No disruptions recorded for this plan yet.</p>
      )}

      <ol className="max-h-96 overflow-y-auto">
        {eventLog.map((entry) => (
          <TimelineEntry key={entry.id} entry={entry} />
        ))}
      </ol>
    </section>
  )
}
