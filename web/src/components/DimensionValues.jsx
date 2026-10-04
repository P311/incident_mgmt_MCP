import { useState } from 'react'
import { getDimensionValues } from '../api'
import ResultTable from './ResultTable'
import ErrorBanner from './ErrorBanner'

const DIMENSIONS = ['priority', 'issue_type', 'report_channel']

export default function DimensionValues() {
  const [dimension, setDimension] = useState('priority')
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const data = await getDimensionValues(dimension)
      setRows(data.values.map((v) => [v.value, v.case_count]))
    } catch (err) {
      setError(err)
      setRows(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="panel">
      <h2>Dimension values</h2>
      <p className="hint">
        Look up the exact strings a filter must use, e.g. "High" not "1 - High".
      </p>
      <div className="row">
        <div className="field">
          <label htmlFor="dim-select">Dimension</label>
          <select id="dim-select" value={dimension} onChange={(event) => setDimension(event.target.value)}>
            {DIMENSIONS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
        <button onClick={load} disabled={loading}>
          Load values
        </button>
      </div>
      <ErrorBanner error={error} />
      <ResultTable columns={['value', 'case_count']} rows={rows} />
    </section>
  )
}
