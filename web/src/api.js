export async function api(path, { method = 'GET', body, signal } = {}) {
  const isFile = body instanceof FormData
  const response = await fetch('/api' + path, {
    method,
    signal,
    credentials: 'same-origin',
    headers: body && !isFile ? { 'Content-Type': 'application/json' } : {},
    body: body && !isFile ? JSON.stringify(body) : body,
  })
  if (!response.ok) {
    const data = await response.json().catch(() => null)
    const error = new Error(data?.detail || (response.status === 413 ? 'The file exceeds 25 MB.' : 'The request failed. Try again.'))
    error.status = response.status
    throw error
  }
  return response.status === 204 ? null : response.json()
}

export const kindLabel = { pdf: 'PDF', markdown: 'Markdown', text: 'Plain text', web: 'Website' }
export const dateLabel = value => new Date(value).toLocaleDateString(undefined, { dateStyle: 'medium' })
