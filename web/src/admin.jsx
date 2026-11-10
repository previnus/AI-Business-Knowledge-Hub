import { useCallback, useEffect, useState } from 'react'
import { Plus } from 'lucide-react'
import { api, kindLabel } from './api.js'
import { Dialog } from './dialog.jsx'

const statusNames = { ready: 'Active', queued: 'Queued', running: 'Indexing', failed: 'Failed' }
const jobErrors = {
  attempts_exhausted: 'The worker could not finish after three attempts.',
  provider_unavailable: 'The embedding provider is unavailable.',
  provider_rejected: 'The embedding provider rejected the request. Check its configuration.',
  no_extractable_text: 'No selectable text was found.',
  invalid_pdf: 'The PDF could not be read.',
  encrypted_pdf: 'Password-protected PDFs are unsupported.',
  expected_utf8: 'Save the file as UTF-8 and try again.',
  non_public_address: 'The website resolved to a restricted address.',
  host_not_allowed: 'A website hostname was outside the allowlist.',
  website_too_large: 'The webpage exceeded the download limit.',
}

function GroupPicker({ groups, selected, setSelected }) {
  return <fieldset className="group-picker">
    <legend>Knowledge groups</legend>
    {groups.length === 0 && <p className="muted">Create a group in Manage access first.</p>}
    {groups.map(group => <label key={group.id} className="check">
      <input type="checkbox" checked={selected.includes(group.id)} onChange={event =>
        setSelected(event.target.checked ? [...selected, group.id] : selected.filter(id => id !== group.id))} />
      {group.name}
    </label>)}
  </fieldset>
}

function SourceEditor({ mode, source, groups, close, saved }) {
  const [type, setType] = useState('file')
  const [selected, setSelected] = useState(source?.group_ids || [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const title = { add: 'Add source', groups: 'Change source groups', replace: 'Replace file', delete: 'Delete source' }[mode]
  const needsGroups = mode === 'groups' || mode === 'add'

  async function save(event) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      if (mode === 'groups') {
        await api('/admin/sources/' + source.id + '/groups', { method: 'PUT', body: { group_ids: selected } })
      } else if (mode === 'delete') {
        await api('/admin/sources/' + source.id, { method: 'DELETE' })
      } else if (mode === 'replace') {
        await api('/admin/sources/' + source.id + '/file', { method: 'PUT', body: form })
      } else if (type === 'web') {
        await api('/admin/sources/web', { method: 'POST', body: {
          url: form.get('url'), title: form.get('title'), group_ids: selected,
        } })
      } else {
        form.set('group_ids', JSON.stringify(selected))
        await api('/admin/sources/file', { method: 'POST', body: form })
      }
      saved()
    } catch (error) {
      setError(error.message)
    } finally {
      setBusy(false)
    }
  }

  return <Dialog title={title} close={close}>
    <form className="stack" onSubmit={save}>
      {mode === 'add' && <>
        <label>Source type<select value={type} onChange={event => setType(event.target.value)}>
          <option value="file">Upload a file</option><option value="web">Add a public webpage</option>
        </select></label>
        <label>Title<input name="title" required maxLength={160} /></label>
      </>}
      {source && <p className="muted">{source.title}</p>}
      {mode === 'delete' && <p>Delete this source, its indexed versions, and its citations from the library?</p>}
      {(mode === 'replace' || (mode === 'add' && type === 'file')) && <label>
        File<input type="file" name="file" required accept=".pdf,.md,.markdown,.txt" />
        <small className="muted">Text-based PDF, Markdown, or UTF-8 text. Up to 25 MB.</small>
      </label>}
      {mode === 'add' && type === 'web' && <label>Public HTTPS URL
        <input name="url" type="url" required maxLength={2048} placeholder="https://" />
      </label>}
      {needsGroups && <GroupPicker groups={groups} selected={selected} setSelected={setSelected} />}
      {error && <p className="notice error" role="alert">{error}</p>}
      <div className="form-actions">
        <button type="button" className="button secondary" onClick={close}>Cancel</button>
        <button className={'button ' + (mode === 'delete' ? 'danger' : 'primary')}
          disabled={busy || (needsGroups && !selected.length)}>
          {busy ? 'Saving…' : { add: 'Queue ingestion', groups: 'Save groups', replace: 'Queue replacement', delete: 'Delete source' }[mode]}
        </button>
      </div>
    </form>
  </Dialog>
}

function MemberEditor({ member, groups, saved, back }) {
  const [selected, setSelected] = useState(member.group_ids)
  const [enabled, setEnabled] = useState(member.enabled)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function save(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await api('/admin/users/' + member.id, { method: 'PUT', body: { group_ids: selected, enabled } })
      saved()
    } catch (error) {
      setError(error.message)
    } finally {
      setBusy(false)
    }
  }
  return <form className="stack" onSubmit={save}>
    <h3>{member.display_name || member.subject}</h3>
    <GroupPicker groups={groups} selected={selected} setSelected={setSelected} />
    <label className="check"><input type="checkbox" checked={enabled} disabled={member.is_admin}
      onChange={event => setEnabled(event.target.checked)} /> Account enabled</label>
    {error && <p className="notice error" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="button secondary" onClick={back}>Back</button>
      <button className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save access'}</button></div>
  </form>
}

function AccessEditor({ access, reload, close }) {
  const [member, setMember] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function create(event) {
    event.preventDefault()
    const form = event.currentTarget
    const name = new FormData(form).get('name')
    setBusy(true)
    setError('')
    try {
      await api('/admin/groups', { method: 'POST', body: { name } })
      form.reset()
      await reload()
    } catch (error) {
      setError(error.message)
    } finally {
      setBusy(false)
    }
  }
  return <Dialog title="Manage access" close={close} wide>
    {member ? <MemberEditor member={member} groups={access.groups} back={() => setMember(null)} saved={() => {
      setMember(null)
      reload()
    }} /> : <div className="stack">
      <form className="inline-form" onSubmit={create}>
        <label>New group name<input name="name" maxLength={100} required /></label>
        <button className="button primary" disabled={busy}>Create group</button>
      </form>
      {error && <p className="notice error" role="alert">{error}</p>}
      <p className="muted">Employees appear here after their first company sign-in.</p>
      <div className="table-wrap"><table>
        <thead><tr><th>Employee</th><th>Access</th><th>Groups</th><th>Actions</th></tr></thead>
        <tbody>{access.users.map(user => <tr key={user.id}>
          <td>{user.display_name || user.subject}</td>
          <td>{!user.enabled ? 'Disabled' : user.is_admin ? 'Administrator' : 'Employee'}</td>
          <td>{access.groups.filter(group => user.group_ids.includes(group.id)).map(group => group.name).join(', ') || 'None'}</td>
          <td><button className="button small secondary" onClick={() => setMember(user)}>Edit access</button></td>
        </tr>)}</tbody>
      </table></div>
    </div>}
  </Dialog>
}

export function Administration({ openCitation }) {
  const [sources, setSources] = useState(null)
  const [access, setAccess] = useState({ groups: [], users: [] })
  const [modal, setModal] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(null)

  const reloadSources = useCallback(signal => api('/admin/sources', { signal }).then(setSources).catch(error => {
    if (error.name !== 'AbortError') setError(error.message)
  }), [])
  const reloadAccess = useCallback(signal => api('/admin/access', { signal }).then(setAccess).catch(error => {
    if (error.name !== 'AbortError') setError(error.message)
  }), [])
  useEffect(() => {
    const controller = new AbortController()
    reloadSources(controller.signal)
    reloadAccess(controller.signal)
    return () => controller.abort()
  }, [reloadSources, reloadAccess])
  const processing = sources?.some(item => ['queued', 'running'].includes(item.status))
  useEffect(() => {
    if (!processing) return
    const controller = new AbortController()
    const timer = setInterval(() => reloadSources(controller.signal), 3500)
    return () => { clearInterval(timer); controller.abort() }
  }, [processing, reloadSources])

  async function refresh(item) {
    setBusy(item.id)
    setError('')
    try {
      await api('/admin/sources/' + item.id + '/refresh', { method: 'POST' })
      await reloadSources()
    } catch (error) {
      setError(error.message)
    } finally {
      setBusy(null)
    }
  }

  return <section>
    <div className="page-heading">
      <h1>Manage sources</h1>
      <div className="heading-actions">
        <button className="button primary" onClick={() => setModal({ mode: 'add' })}><Plus size={16} /> Add source</button>
        <button className="button secondary" onClick={() => { reloadAccess(); setModal({ mode: 'access' }) }}>Manage access</button>
      </div>
    </div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!sources && !error && <p role="status">Loading sources…</p>}
    {sources?.length === 0 && <p className="notice">No sources yet. Add a document or webpage to start indexing.</p>}
    {sources?.length > 0 && <div className="table-wrap"><table>
      <thead><tr><th>Source</th><th>Format</th><th>Groups</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>{sources.map(source => <tr key={source.id}>
        <td>{source.citation_id ? <a href={'/citation/' + source.citation_id}
          onClick={event => openCitation(event, source.citation_id)}>{source.title}</a> : source.title}</td>
        <td>{kindLabel[source.kind]}</td>
        <td>{access.groups.filter(group => source.group_ids.includes(group.id)).map(group => group.name).join(', ')}</td>
        <td><span className={'status ' + (source.status || '')}>{statusNames[source.status] || 'Pending'}</span>
          {source.status === 'failed' && <small className="job-error">
            {jobErrors[source.error_code] || 'Ingestion could not complete.'}{source.active ? ' Previous version is still active.' : ''}
          </small>}</td>
        <td><details className="row-actions"><summary aria-label={'Actions for ' + source.title}>Actions</summary>
          <div className="row-menu">
            <button className="button small secondary" onClick={() => setModal({ mode: 'groups', source })}>Change groups</button>
            {source.kind === 'web' ? <button className="button small secondary"
              disabled={busy === source.id || ['queued', 'running'].includes(source.status)}
              onClick={() => refresh(source)}>Refresh</button>
              : <button className="button small secondary" disabled={['queued', 'running'].includes(source.status)}
                onClick={() => setModal({ mode: 'replace', source })}>Replace file</button>}
            <button className="button small secondary" onClick={() => setModal({ mode: 'delete', source })}>Delete</button>
          </div>
        </details></td>
      </tr>)}</tbody>
    </table></div>}
    {modal?.mode === 'access' && <AccessEditor access={access} reload={reloadAccess} close={() => setModal(null)} />}
    {modal && modal.mode !== 'access' && <SourceEditor mode={modal.mode} source={modal.source} groups={access.groups}
      close={() => setModal(null)} saved={() => { setModal(null); reloadSources() }} />}
  </section>
}
