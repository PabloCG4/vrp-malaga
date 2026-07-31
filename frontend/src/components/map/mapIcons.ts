/**
 * Custom Leaflet marker icons, built as `L.divIcon` HTML fragments rather
 * than image sprites.
 *
 * Avoids the well-known Leaflet + bundler "broken default marker icon"
 * problem entirely (Leaflet's default `L.Icon` resolves its PNGs via
 * relative URLs that most bundlers, Vite included, do not rewrite
 * automatically), while also giving each marker kind (depot, standard
 * customer, urgent customer, vehicle) a distinct, clean visual identity as
 * required by FR-5, styled with the same Tailwind utility classes as the
 * rest of the dashboard.
 */

import L from 'leaflet'

export function createDepotIcon(): L.DivIcon {
  return L.divIcon({
    html: `<div class="flex h-8 w-8 items-center justify-center rounded-lg border-2 border-white/90 bg-accent text-base shadow-lg shadow-black/40">🏭</div>`,
    className: '',
    iconSize: [32, 32],
    iconAnchor: [16, 16],
    popupAnchor: [0, -16],
  })
}

export function createCustomerIcon(isUrgent: boolean): L.DivIcon {
  const colorClasses = isUrgent ? 'bg-danger' : 'bg-surface-raised border-accent'
  const pulse = isUrgent
    ? '<span class="absolute inset-0 -z-10 animate-ping rounded-full bg-danger opacity-60"></span>'
    : ''
  return L.divIcon({
    html: `<div class="relative flex h-4 w-4 items-center justify-center rounded-full border-2 border-white/90 ${colorClasses} shadow-md shadow-black/40">${pulse}</div>`,
    className: '',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
    popupAnchor: [0, -8],
  })
}

export function createVehicleIcon(color: string, label: string): L.DivIcon {
  return L.divIcon({
    html: `<div class="flex h-7 w-7 items-center justify-center rounded-full border-2 border-white text-[11px] font-bold text-white shadow-lg shadow-black/50" style="background-color:${color}">${label}</div>`,
    className: '',
    iconSize: [28, 28],
    iconAnchor: [14, 14],
    popupAnchor: [0, -14],
  })
}

export function createClosureNodeIcon(kind: 'primary' | 'candidate'): L.DivIcon {
  const isPrimary = kind === 'primary'
  const size = isPrimary ? 18 : 12
  const classes = isPrimary
    ? 'bg-warning border-2 border-white shadow-lg shadow-black/40'
    : 'bg-surface border-2 border-warning border-dashed'
  return L.divIcon({
    html: `<div class="rounded-full ${classes}" style="width:${size}px;height:${size}px"></div>`,
    className: '',
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  })
}
