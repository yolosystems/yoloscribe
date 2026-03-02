import { useState } from 'react'

interface Props {
  apiBase: string
  token: string
  onClose: () => void
  onDeleted: () => void
}

export default function DeleteAccountModal({ apiBase, token, onClose, onDeleted }: Props) {
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleDelete() {
    setConfirming(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/account`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) {
        const text = await res.text()
        throw new Error(`Delete failed (${res.status}): ${text}`)
      }
      onDeleted()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An unexpected error occurred')
      setConfirming(false)
    }
  }

  return (
    <>
      <div className="modal-overlay" onClick={onClose} />
      <div className="modal-dialog">
        <div className="modal-title">Delete Account</div>
        <div className="modal-body">
          <p>Deleting your account will permanently remove:</p>
          <ul className="modal-list">
            <li>Your primary site and all child pages</li>
            <li>Your Google identity will be unlinked from AgentScribe</li>
          </ul>
          <p className="modal-body--warning">This action cannot be undone.</p>
          {error && <p className="form-error">{error}</p>}
        </div>
        <div className="modal-actions">
          <button className="btn" onClick={onClose} disabled={confirming}>
            Cancel
          </button>
          <button className="btn btn-danger" onClick={handleDelete} disabled={confirming}>
            {confirming ? 'Deleting…' : 'Delete My Account'}
          </button>
        </div>
      </div>
    </>
  )
}
