import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'

export function Dialog({ title, close, children, wide = false }) {
  const ref = useRef(null)
  useEffect(() => {
    const dialog = ref.current
    dialog.showModal()
    return () => { if (dialog.open) dialog.close() }
  }, [])

  return <dialog ref={ref} className={wide ? 'wide-dialog' : ''} aria-label={title}
    onCancel={event => { event.preventDefault(); close() }}>
    <div className="dialog-head">
      <h2>{title}</h2>
      <button type="button" className="icon-button" aria-label="Close" onClick={close}><X size={20} /></button>
    </div>
    {children}
  </dialog>
}
