import { useEffect, useRef } from 'react'
import { useMap } from 'react-leaflet'

interface RecenterOnDepotProps {
  latitude: number
  longitude: number
}

/**
 * `MapContainer`'s `center` prop only applies on the very first render, but
 * the depot's coordinates are only known once the network graph finishes
 * loading (shortly after mount). This performs that one-time recenter as
 * soon as real coordinates become available.
 */
export function RecenterOnDepot({ latitude, longitude }: RecenterOnDepotProps) {
  const map = useMap()
  const hasRecenteredRef = useRef(false)

  useEffect(() => {
    if (hasRecenteredRef.current) {
      return
    }
    hasRecenteredRef.current = true
    map.setView([latitude, longitude], 15)
  }, [map, latitude, longitude])

  return null
}
