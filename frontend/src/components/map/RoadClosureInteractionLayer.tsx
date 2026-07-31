import { CircleMarker, Marker, Polyline, useMapEvent } from 'react-leaflet'
import { createClosureNodeIcon } from './mapIcons'
import { useNetworkStore } from '../../store/networkStore'
import { useUiStore } from '../../store/uiStore'

const PRIMARY_ICON = createClosureNodeIcon('primary')
const CANDIDATE_ICON = createClosureNodeIcon('candidate')

/**
 * Implements the "Road Closure Selection Mode" interaction (FR-4): while
 * active, the first map click snaps to the nearest street network node and
 * highlights its direct neighbors as clickable candidates; the second click
 * (on an adjacent node) draws a temporary warning line and hands the pair
 * off to the Traffic Incident modal via `store/uiStore.ts`.
 */
export function RoadClosureInteractionLayer() {
  const isRoadClosureMode = useUiStore((state) => state.isRoadClosureMode)
  const closureFirstNode = useUiStore((state) => state.closureFirstNode)
  const closureSecondNode = useUiStore((state) => state.closureSecondNode)
  const setClosureFirstNode = useUiStore((state) => state.setClosureFirstNode)
  const setClosureSecondNode = useUiStore((state) => state.setClosureSecondNode)
  const setClosureSelectionError = useUiStore((state) => state.setClosureSelectionError)
  const openTrafficModal = useUiStore((state) => state.openTrafficModal)

  const findNearestNode = useNetworkStore((state) => state.findNearestNode)
  const areNodesAdjacent = useNetworkStore((state) => state.areNodesAdjacent)
  const getNeighbors = useNetworkStore((state) => state.getNeighbors)

  useMapEvent('click', (event) => {
    if (!isRoadClosureMode) {
      return
    }
    const nearestNode = findNearestNode(event.latlng.lat, event.latlng.lng)
    if (nearestNode === null) {
      setClosureSelectionError('No street network node found near that point. Click closer to a road.')
      return
    }

    if (closureFirstNode === null) {
      setClosureFirstNode(nearestNode)
      return
    }
    if (nearestNode.node_id === closureFirstNode.node_id) {
      return
    }
    if (!areNodesAdjacent(closureFirstNode.node_id, nearestNode.node_id)) {
      setClosureSelectionError('Those nodes do not share a street. Pick one of the highlighted neighbors.')
      return
    }

    setClosureSecondNode(nearestNode)
    openTrafficModal({ firstNode: closureFirstNode.node_id, secondNode: nearestNode.node_id })
  })

  if (closureFirstNode === null) {
    return null
  }

  const neighborCandidates = getNeighbors(closureFirstNode.node_id).filter(
    (neighbor) => neighbor.node_id !== closureSecondNode?.node_id,
  )

  return (
    <>
      <Marker position={[closureFirstNode.latitude, closureFirstNode.longitude]} icon={PRIMARY_ICON} />
      {closureSecondNode === null &&
        neighborCandidates.map((neighbor) => (
          <CircleMarker
            key={neighbor.node_id}
            center={[neighbor.latitude, neighbor.longitude]}
            radius={6}
            pathOptions={{ color: '#f5b942', weight: 2, dashArray: '2,2', fillOpacity: 0.15 }}
          />
        ))}
      {closureSecondNode !== null && (
        <>
          <Marker position={[closureSecondNode.latitude, closureSecondNode.longitude]} icon={CANDIDATE_ICON} />
          <Polyline
            positions={[
              [closureFirstNode.latitude, closureFirstNode.longitude],
              [closureSecondNode.latitude, closureSecondNode.longitude],
            ]}
            pathOptions={{ color: '#f2555a', weight: 5, dashArray: '8,8', opacity: 0.9 }}
          />
        </>
      )}
    </>
  )
}
