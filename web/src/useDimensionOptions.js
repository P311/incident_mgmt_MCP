import { useEffect, useState } from 'react'
import { getDimensionValues } from './api'

const DIMENSIONS = ['priority', 'issue_type', 'report_channel']

// Fetched once and shared by every filter control, so a value a user picks is
// always one the server actually knows about — no typing, no quoting, no typos.
export default function useDimensionOptions() {
  const [options, setOptions] = useState({ priority: [], issue_type: [], report_channel: [] })
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    Promise.all(DIMENSIONS.map((dimension) => getDimensionValues(dimension)))
      .then((results) => {
        if (cancelled) return
        const next = {}
        DIMENSIONS.forEach((dimension, index) => {
          next[dimension] = results[index].values
        })
        setOptions(next)
      })
      .catch((err) => {
        if (!cancelled) setError(err)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return { options, error }
}
