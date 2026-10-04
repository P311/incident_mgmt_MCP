import { useState } from 'react'
import { postCounters } from '../api'
import ResultTable from './ResultTable'
import ErrorBanner from './ErrorBanner'
import FilterFields from './FilterFields'
import DateRangeFields from './DateRangeFields'

const METRICS = ['reassignment_count', 'reopen_count']

export default function CounterPanel({ options }) {
  const [metric, setMetric] = useState(METRICS[0])
  const [filters, setFilters] = useState({ priority: '', issue_type: '', report_channel: '' })
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function run() {
    setLoading(true)
    setError(null)
    try {
      const data = await postCounters({
        metric,
        priority: filters.priority ? [filters.priority] : [],
        issue_type: filters.issue_type ? [filters.issue_type] : [],
        report_channel: filters.report_channel ? [filters.report_channel] : [],
        timestamp_from: from,
        timestamp_to: to,
      })
      setResult(data)
    } catch (err) {
      setError(err)
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const rows = result ? result.rows.map((row) => [row.value, row.count, `${(row.share * 100).toFixed(1)}%`]) : null

  return (
    <section className="panel">
      <h2>Counter distribution</h2>
      <p className="hint">How many cases were reassigned or reopened N times, for each N.</p>
      <div className="row">
        <div className="field">
          <label>metric</label>
          <select value={metric} onChange={(event) => setMetric(event.target.value)}>
            {METRICS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      </div>
      <FilterFields options={options} values={filters} onChange={setFilters} />
      <div className="row">
        <DateRangeFields from={from} to={to} onFromChange={setFrom} onToChange={setTo} />
        <button onClick={run} disabled={loading}>
          Run
        </button>
      </div>
      <ErrorBanner error={error} />
      <ResultTable columns={['value', 'count', 'share']} rows={rows} />
    </section>
  )
}
