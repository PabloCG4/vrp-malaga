import { useEffect, type ReactNode } from 'react'

interface ModalProps {
  title: string
  subtitle?: string
  isOpen: boolean
  onClose: () => void
  children: ReactNode
}

/**
 * Generic, professional modal dialog shell shared by every disruption
 * injection form. Closes on `Escape` or on a backdrop click, and renders
 * nothing at all while closed (rather than hiding via CSS), keeping unused
 * form state fully unmounted between openings.
 */
export function Modal({ title, subtitle, isOpen, onClose, children }: ModalProps) {
  useEffect(() => {
    if (!isOpen) {
      return
    }
    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  if (!isOpen) {
    return null
  }

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-md rounded-2xl border border-border-strong bg-surface-raised p-5 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold normal-case tracking-normal text-text-heading">{title}</h2>
            {subtitle && <p className="mt-1 text-xs text-text-muted">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-text-muted transition-colors hover:bg-surface-alt hover:text-text-heading"
            aria-label="Close dialog"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
