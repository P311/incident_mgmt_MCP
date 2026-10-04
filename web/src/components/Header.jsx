import { useEffect, useState } from 'react'
import { getHealth } from '../api'

export default function Header() {
  const [status, setStatus] = useState('connecting…')

  useEffect(() => {
    let cancelled = false
    getHealth()
      .then((data) => {
        if (!cancelled) setStatus(`${data.server_name} v${data.server_version}`)
      })
      .catch((err) => {
        if (!cancelled) setStatus(`disconnected: ${err.message}`)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <header>
      <h1>Incident Management Console</h1>
      <span id="server-info">{status}</span>
    </header>
  )
}
