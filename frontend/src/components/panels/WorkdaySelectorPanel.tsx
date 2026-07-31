import { useEffect } from 'react'
import { useSimulationStore } from '../../store/simulationStore'

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
      <div className="panel__header">
        <h2>Workday Plans</h2>
        <button type="button" className="button button--ghost" onClick={() => void fetchWorkdays()} disabled={isLoadingWorkdays}>
          Refresh
        </button>
      </div>

      {workdaysError && <p className="panel__error">{workdaysError}</p>}
      {isLoadingWorkdays && workdays.length === 0 && <p className="panel__hint">Loading workday plans…</p>}
      {!isLoadingWorkdays && workdays.length === 0 && !workdaysError && (
        <p className="panel__hint">No workday plans found. Run `seed_db.py` against the backend database.</p>
      )}

      <ul className="workday-list">
        {workdays.map((workday) => (
          <li key={workday.id}>
            <button
              type="button"
              className={`workday-list__item${activePlan?.id === workday.id ? ' workday-list__item--active' : ''}`}
              onClick={() => void selectWorkday(workday.id)}
            >
              <span className="workday-list__date">{workday.workday_date}</span>
              <span className={`workday-list__status workday-list__status--${workday.status.toLowerCase()}`}>
                {workday.status}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
