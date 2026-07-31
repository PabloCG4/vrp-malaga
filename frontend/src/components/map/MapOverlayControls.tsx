import { useUiStore } from '../../store/uiStore'

/** Dark/Light tile theme toggle, floated over the top-right corner of the map. */
export function TileToggleControl() {
  const tileMode = useUiStore((state) => state.tileMode)
  const toggleTileMode = useUiStore((state) => state.toggleTileMode)

  return (
    <button
      type="button"
      onClick={toggleTileMode}
      className="btn-secondary absolute right-3 top-3 z-[500] bg-surface-raised/90 backdrop-blur"
    >
      {tileMode === 'dark' ? '☀️ Light Mode' : '🌙 Dark Mode'}
    </button>
  )
}

/** Instructional banner + inline error shown while the road-closure selection tool is active. */
export function RoadClosureBanner() {
  const isRoadClosureMode = useUiStore((state) => state.isRoadClosureMode)
  const closureFirstNode = useUiStore((state) => state.closureFirstNode)
  const closureSelectionError = useUiStore((state) => state.closureSelectionError)
  const setRoadClosureMode = useUiStore((state) => state.setRoadClosureMode)

  if (!isRoadClosureMode) {
    return null
  }

  return (
    <div className="absolute left-1/2 top-3 z-[500] flex -translate-x-1/2 items-center gap-3 rounded-lg border border-warning/40 bg-surface-raised/95 px-4 py-2 text-xs shadow-lg backdrop-blur">
      <span className="font-semibold text-warning">🚧 Road Closure Selection Mode</span>
      <span className="text-text-muted">
        {closureFirstNode === null
          ? 'Click a node on the map to select the first end of the closed street.'
          : `First node ${closureFirstNode.node_id} selected — click an adjacent (highlighted) node.`}
      </span>
      {closureSelectionError && <span className="font-medium text-danger">{closureSelectionError}</span>}
      <button type="button" className="btn-ghost" onClick={() => setRoadClosureMode(false)}>
        Cancel
      </button>
    </div>
  )
}
