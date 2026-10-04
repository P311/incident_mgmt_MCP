import { useState } from 'react'
import { postReportBrief, postReportWrite } from '../api'
import ErrorBanner from './ErrorBanner'

const DIMENSIONS = ['priority', 'issue_type', 'report_channel']
const AUDIENCES = ['operations', 'executive']

export default function ReportPanel() {
  const [dimension, setDimension] = useState('priority')
  const [audience, setAudience] = useState('operations')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [text, setText] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function body() {
    return { dimension, audience, timestamp_from: from, timestamp_to: to }
  }

  async function loadBrief() {
    setLoading(true)
    setError(null)
    try {
      const data = await postReportBrief(body())
      setText(`${data.brief}\n\n--- data ---\n${JSON.stringify(data.payload, null, 2)}`)
    } catch (err) {
      setError(err)
      setText(null)
    } finally {
      setLoading(false)
    }
  }

  async function writeReport() {
    setLoading(true)
    setError(null)
    try {
      const data = await postReportWrite(body())
      setText(`${data.report}\n\n(model: ${data.model}, stop_reason: ${data.stop_reason})`)
    } catch (err) {
      setError(err)
      setText(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="panel">
      <h2>Incident report</h2>
      <p className="hint">
        Runs the server's aggregations for one dimension and window. The brief is the same text the server hands a
        model; "Write with client model" additionally asks the server to sample a model over MCP — this console has
        none, so that call is expected to report itself unavailable.
      </p>
      <div className="row">
        <div className="field">
          <label>dimension</label>
          <select value={dimension} onChange={(event) => setDimension(event.target.value)}>
            {DIMENSIONS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>audience</label>
          <select value={audience} onChange={(event) => setAudience(event.target.value)}>
            {AUDIENCES.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>from</label>
          <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
        </div>
        <div className="field">
          <label>to</label>
          <input type="date" value={to} onChange={(event) => setTo(event.target.value)} />
        </div>
      </div>
      <div className="row">
        <button onClick={loadBrief} disabled={loading}>
          Load brief + data
        </button>
        <button className="secondary" onClick={writeReport} disabled={loading}>
          Write with client model (sampling)
        </button>
      </div>
      <ErrorBanner error={error} />
      {text && <pre className="brief">{text}</pre>}
    </section>
  )
}
