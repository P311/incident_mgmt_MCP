import { useState } from 'react'
import { postDuration } from '../api'
import ResultTable from './ResultTable'
import ErrorBanner from './ErrorBanner'
import FilterFields from './FilterFields'
import DateRangeFields from './DateRangeFields'

const GROUP_OPTIONS = ['priority', 'issue_type', 'report_channel']

export default function DurationPanel({ options }) {
  const [groupBy, setGroupBy] = useState(['priority'])
  const [filters, setFilters] = useState({ priority: '', issue_type: '', report_channel: '' })
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function toggleGroup(value) {
    setGroupBy((current) => (current.includes(value) ? current.filter((v) => v !== value) : [...current, value]))
  }

  async function run() {
    setLoading(true)
    setError(null)
    try {
      const data = await postDuration({
        group_by: groupBy,
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

  const columns = [...groupBy, 'count', 'mean_seconds', 'median_seconds']
  const rows = result
    ? result.rows.map((row) => [
        ...groupBy.map((g) => row.group[g] || '(missing)'),
        row.count,
        row.mean_duration_seconds,
        row.median_duration_seconds,
      ])
    : null

  return (
    <section className="panel">
      <h2>Case processing duration</h2>
      <p className="hint">
        Mean/median duration (first event to last event) per group, grouped by one or more dimensions.
      </p>
      <div className="row">
        <div className="field checks">
          {GROUP_OPTIONS.map((g) => (
            <label key={g}>
              <input type="checkbox" checked={groupBy.includes(g)} onChange={() => toggleGroup(g)} />
              {g}
            </label>
          ))}
        </div>
      </div>
      <FilterFields options={options} values={filters} onChange={setFilters} />
      <div className="row">
        <DateRangeFields from={from} to={to} onFromChange={setFrom} onToChange={setTo} />
        <button onClick={run} disabled={loading || groupBy.length === 0}>
          Run
        </button>
      </div>
      <ErrorBanner error={error} />
      <ResultTable columns={columns} rows={rows} />
    </section>
  )
}
