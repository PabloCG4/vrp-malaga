/**
 * Ephemeral UI state store (Zustand).
 *
 * Holds view-only state that does not belong in `store/simulationStore.ts`
 * (Block 1's store, left untouched by this block) nor in
 * `store/networkStore.ts` (static server data): the map's tile theme, the
 * interactive road-closure selection tool's in-progress state, and whether
 * the two disruption-injection modals are open. Kept as a separate store so
 * a component can subscribe to just the slice it needs (e.g. the map only
 * cares about `tileMode`/`isRoadClosureMode`, the modals only care about
 * their own open/prefill flags) without extra re-renders.
 */

import { create } from 'zustand'
import type { NetworkNode } from '../types/network'

export type TileMode = 'dark' | 'light'

export interface TrafficModalPrefill {
  firstNode: number
  secondNode: number
}

interface UiStoreState {
  // -- Map tile theme -----------------------------------------------------
  tileMode: TileMode
  toggleTileMode: () => void

  // -- Interactive road-closure selection tool -----------------------------
  isRoadClosureMode: boolean
  setRoadClosureMode: (active: boolean) => void
  toggleRoadClosureMode: () => void
  closureFirstNode: NetworkNode | null
  closureSecondNode: NetworkNode | null
  closureSelectionError: string | null
  setClosureFirstNode: (node: NetworkNode | null) => void
  setClosureSecondNode: (node: NetworkNode | null) => void
  setClosureSelectionError: (message: string | null) => void
  clearClosureSelection: () => void

  // -- Traffic incident modal ------------------------------------------------
  isTrafficModalOpen: boolean
  trafficModalPrefill: TrafficModalPrefill | null
  openTrafficModal: (prefill?: TrafficModalPrefill) => void
  closeTrafficModal: () => void

  // -- Urgent order modal --------------------------------------------------------
  isUrgentOrderModalOpen: boolean
  openUrgentOrderModal: () => void
  closeUrgentOrderModal: () => void
}

export const useUiStore = create<UiStoreState>((set) => ({
  tileMode: 'dark',
  toggleTileMode: () => set((state) => ({ tileMode: state.tileMode === 'dark' ? 'light' : 'dark' })),

  isRoadClosureMode: false,
  setRoadClosureMode: (active) =>
    set({
      isRoadClosureMode: active,
      ...(active === false && { closureFirstNode: null, closureSecondNode: null, closureSelectionError: null }),
    }),
  toggleRoadClosureMode: () =>
    set((state) => {
      const nextActive = !state.isRoadClosureMode
      return {
        isRoadClosureMode: nextActive,
        ...(nextActive === false && { closureFirstNode: null, closureSecondNode: null, closureSelectionError: null }),
      }
    }),
  closureFirstNode: null,
  closureSecondNode: null,
  closureSelectionError: null,
  setClosureFirstNode: (node) => set({ closureFirstNode: node, closureSelectionError: null }),
  setClosureSecondNode: (node) => set({ closureSecondNode: node }),
  setClosureSelectionError: (message) => set({ closureSelectionError: message }),
  clearClosureSelection: () => set({ closureFirstNode: null, closureSecondNode: null, closureSelectionError: null }),

  isTrafficModalOpen: false,
  trafficModalPrefill: null,
  openTrafficModal: (prefill) =>
    set({ isTrafficModalOpen: true, trafficModalPrefill: prefill ?? null, isRoadClosureMode: false }),
  closeTrafficModal: () =>
    set({
      isTrafficModalOpen: false,
      trafficModalPrefill: null,
      closureFirstNode: null,
      closureSecondNode: null,
      closureSelectionError: null,
    }),

  isUrgentOrderModalOpen: false,
  openUrgentOrderModal: () => set({ isUrgentOrderModalOpen: true }),
  closeUrgentOrderModal: () => set({ isUrgentOrderModalOpen: false }),
}))
