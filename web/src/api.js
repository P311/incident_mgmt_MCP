// Thin client for the incident-management-gui backend (`src/incident_management/gui.py`).
// It never talks to the MCP server directly — every call goes through the backend's
// small HTTP API, which is the only thing this module depends on.

class ApiError extends Error {
  constructor(message, hint) {
    super(message)
    this.hint = hint
  }
}

async function request(path, options) {
  const res = await fetch(path, options)
  const data = await res.json()
  if (!res.ok) {
    throw new ApiError(data.error || res.statusText, data.hint)
  }
  return data
}

export function getHealth() {
  return request('/api/health')
}

export function getDimensionValues(dimension) {
  return request(`/api/dimensions/${encodeURIComponent(dimension)}`)
}

function postJson(path, body) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function postDuration(body) {
  return postJson('/api/duration', body)
}

export function postCounters(body) {
  return postJson('/api/counters', body)
}

export function postReportBrief(body) {
  return postJson('/api/report', body)
}

export function postReportWrite(body) {
  return postJson('/api/report/write', body)
}
