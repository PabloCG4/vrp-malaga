import { useEffect } from 'react'
import { useSimulationStore } from '../../store/simulationStore'
import type { WorkdayStatus } from '../../types/enums'

const STATUS_BADGE_STYLES: Record<WorkdayStatus, string> = {
  DRAFT: 'bg-warning/10 text-warning',
  ACTIVE: 'bg-success/10 text-success',
  COMPLETED: 'bg-text-muted/10 text-text-muted',
}

/** Lets a dispatcher pick which workday plan to inspect/dispatch/watch live. */
export function WorkdaySelectorPanel() {
  const workdays = useSimulationStore((state) => state.workdays)
  const isLoadingWorkdays = useSimulationStore((state) => state.isLoadingWorkdays)
  const workdaysError = useSimulationStore((state) => state.workdaysError)
  const activePlan = useSimulationStore((state) => state.activePlan)
  const fetchWorkdays = useSimulationStore((state) => state.fetchWorkdays)
  const selectWorkday = useSimulationStore((state) => state.selectWorkday)

  useEffect(() => {
    void fetchWorkdays()
  }, [fetchWorkdays])

  return (
    <section className="panel">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Workday Plans</h2>
        <button type="button" className="btn-ghost" onClick={() => void fetchWorkdays()} disabled={isLoadingWorkdays}>
          Refresh
        </button>
      </div>

      {workdaysError && <p className="text-xs text-danger">{workdaysError}</p>}
      {isLoadingWorkdays && workdays.length === 0 && <p className="text-sm text-text-muted">Loading workday plans…</p>}
      {!isLoadingWorkdays && workdays.length === 0 && !workdaysError && (
        <p className="text-sm text-text-muted">No workday plans found. Run `seed_db.py` against the backend database.</p>
      )}

      <ul className="flex max-h-64 flex-col gap-1.5 overflow-y-auto">
        {workdays.map((workday) => (
          <li key={workday.id}>
            <button
              type="button"
              className={`flex w-full items-center justify-between rounded-lg border px-3 py-2 text-sm transition-colors ${
                activePlan?.id === workday.id
                  ? 'border-accent bg-accent/10 text-text-heading'
                  : 'border-border bg-surface-alt text-text hover:border-border-strong'
              }`}
              onClick={() => void selectWorkday(workday.id)}
            >
              <span>{workday.workday_date}</span>
              <span className={`badge ${STATUS_BADGE_STYLES[workday.status]}`}>{workday.status}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
