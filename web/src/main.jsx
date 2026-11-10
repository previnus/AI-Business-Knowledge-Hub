import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ArrowRight, BookOpen, ExternalLink, FileText, Layers, LogOut, MessageSquare, Settings } from 'lucide-react'
import { api, dateLabel, kindLabel } from './api.js'
import { Administration } from './admin.jsx'
import { Dialog } from './dialog.jsx'
import './styles.css'

const evidenceTypes = [
  ['PDF pages', 'See the page that supports the answer.'],
  ['Documentation sections', 'Find the relevant section in a guide or text file.'],
  ['Web snapshots', 'Read the indexed excerpt and open the original page.'],
]

function Brand() {
  return <div className="brand"><span className="brand-mark"><BookOpen size={19} /></span>Knowledge Hub</div>
}

function Ask({ openCitation }) {
  const [question, setQuestion] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [answer, setAnswer] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef(null)
  useEffect(() => () => pending.current?.abort(), [])

  async function submit(event) {
    event.preventDefault()
    const text = question.trim()
    if (!text || busy) return
    const controller = new AbortController()
    pending.current = controller
    setBusy(true)
    setError('')
    setAnswer(null)
    setSubmitted(text)
    try {
      setAnswer(await api('/ask', { method: 'POST', body: { question: text }, signal: controller.signal }))
    } catch (error) {
      if (error.name !== 'AbortError') setError(error.message)
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return (
    <section className="ask-page" aria-labelledby="ask-title">
      <h1 id="ask-title">Ask your knowledge base</h1>
      <p className="intro">Find answers in the company sources you can access.</p>
      <form className="ask-form" onSubmit={submit}>
        <label htmlFor="question">Your question</label>
        <textarea id="question" value={question} onChange={event => setQuestion(event.target.value)}
          maxLength={2000} rows={4} required />
        <div className="form-actions"><button className="button navy" disabled={busy || !question.trim()}>
          {busy ? 'Finding evidence…' : 'Ask question'} <ArrowRight size={16} />
        </button></div>
      </form>
      <div aria-live="polite" aria-busy={busy}>
        {error && <p className="notice error" role="alert">{error}</p>}
        {busy && <p className="notice">Looking for relevant passages…</p>}
        {answer && <section className="answer">
          <h2>Answer</h2>
          <p className="muted asked-question">{submitted}</p>
          {answer.status === 'insufficient_evidence'
            ? <p>I couldn’t find enough evidence in the sources you can access to answer this question.</p>
            : answer.segments.map((segment, index) => (
              <div className="answer-segment" key={index}>
                <p>{segment.text}</p>
                <ul className="citations">
                  {segment.citations.map(citation => <li key={citation.id}>
                    <a className="citation-link" href={citation.url} title={citation.quote}
                      onClick={event => openCitation(event, citation.id)}>
                      <FileText size={14} /> {citation.title} · {citation.location}
                    </a>
                    <blockquote>{citation.quote}</blockquote>
                  </li>)}
                </ul>
              </div>
            ))}
        </section>}
      </div>
      <section className="evidence-guide">
        <h2>Answers with evidence</h2>
        <ol>{evidenceTypes.map(([title, description], index) => <li key={title}>
          <span className="step" aria-hidden="true">{index + 1}</span>
          <div><strong>{title}</strong><p>{description}</p></div>
        </li>)}</ol>
      </section>
    </section>
  )
}

function Citation({ id, close }) {
  const [item, setItem] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api('/citations/' + id, { signal: controller.signal }).then(setItem).catch(error => {
      if (error.name !== 'AbortError') setError(error.message)
    })
    return () => controller.abort()
  }, [id])

  return <Dialog title="Evidence" close={close} wide>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!item && !error && <p role="status">Opening citation…</p>}
    {item && <>
      <h3 className="source-title">{item.title}</h3>
      <p className="muted">{kindLabel[item.kind]} · {item.location} · Indexed {dateLabel(item.created_at)}</p>
      <pre className="excerpt">{item.excerpt}</pre>
      <div className="form-actions">
        {item.attachment_id && <a className="button secondary" href={'/api/attachments/' + item.attachment_id}>Download original</a>}
        {item.original_url && <a className="button secondary" href={item.original_url} target="_blank" rel="noopener noreferrer">
          Open original page <ExternalLink size={14} />
        </a>}
      </div>
    </>}
  </Dialog>
}

function Library({ openCitation }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    api('/sources', { signal: controller.signal }).then(setItems).catch(error => {
      if (error.name !== 'AbortError') setError(error.message)
    })
    return () => controller.abort()
  }, [])

  return <section>
    <h1>Sources</h1>
    <p className="intro">Browse the indexed sources shared with you.</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!items && !error && <p role="status">Loading sources…</p>}
    {items?.length === 0 && <p className="notice">No indexed sources are available to your account yet.</p>}
    {items?.length > 0 && <div className="table-wrap"><table>
      <thead><tr><th>Source</th><th>Format</th><th>Indexed</th></tr></thead>
      <tbody>{items.map(item => <tr key={item.id}>
        <td><a href={'/citation/' + item.citation_id} onClick={event => openCitation(event, item.citation_id)}>{item.title}</a></td>
        <td>{kindLabel[item.kind]}</td><td>{dateLabel(item.created_at)}</td>
      </tr>)}</tbody>
    </table></div>}
  </section>
}

function citationFromLocation() {
  return /^\/citation\/([0-9a-f-]{36})$/.exec(window.location.pathname)?.[1] || null
}

function App() {
  const [user, setUser] = useState(undefined)
  const [error, setError] = useState('')
  const [section, setSection] = useState('Ask')
  const [citation, setCitation] = useState(citationFromLocation)
  const navigation = [['Ask', MessageSquare], ['Sources', Layers], ...(user?.is_admin ? [['Administration', Settings]] : [])]

  useEffect(() => {
    const controller = new AbortController()
    api('/me', { signal: controller.signal }).then(setUser).catch(error => {
      if (error.name === 'AbortError') return
      if (error.status !== 401) setError(error.message)
      setUser(null)
    })
    const pop = () => setCitation(citationFromLocation())
    window.addEventListener('popstate', pop)
    return () => { controller.abort(); window.removeEventListener('popstate', pop) }
  }, [])

  function openCitation(event, id) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return
    event.preventDefault()
    window.history.pushState(null, '', '/citation/' + id)
    setCitation(id)
  }

  function closeCitation() {
    window.history.replaceState(null, '', '/')
    setCitation(null)
  }

  async function logout() {
    try {
      await api('/auth/logout', { method: 'POST' })
      closeCitation()
      setUser(null)
      setSection('Ask')
    } catch (error) {
      setError(error.message)
    }
  }

  if (!user) return <main className="sign-in">
    <div className="sign-card">
      <Brand />
      <h1>{user === undefined ? 'Opening Knowledge Hub' : 'Company knowledge, with sources'}</h1>
      {error && <p className="notice error" role="alert">{error}</p>}
      {user === null && <>
        <p>Sign in to ask questions and read the sources shared with your team.</p>
        <a className="button primary" href={'/api/auth/login?return_to=' + encodeURIComponent(window.location.pathname)}>
          Sign in with company SSO <ArrowRight size={16} />
        </a>
      </>}
    </div>
  </main>

  return <div className="shell">
    <aside className="sidebar">
      <Brand />
      <nav aria-label="Workspace">{navigation.map(([name, Icon]) => <button key={name}
        className={section === name ? 'nav-button selected' : 'nav-button'}
        aria-current={section === name ? 'page' : undefined}
        onClick={() => { setError(''); setSection(name) }}><Icon size={18} /> {name}</button>)}</nav>
      <div className="account">
        <div><small>Signed in as</small><strong>{user.display_name || 'Employee'}</strong></div>
        <button className="icon-button" aria-label="Sign out" onClick={logout}><LogOut size={18} /></button>
      </div>
    </aside>
    <main className="content">
      {error && <p className="notice error" role="alert">{error}</p>}
      {section === 'Ask' && <Ask openCitation={openCitation} />}
      {section === 'Sources' && <Library openCitation={openCitation} />}
      {section === 'Administration' && <Administration openCitation={openCitation} />}
    </main>
    {citation && <Citation key={citation} id={citation} close={closeCitation} />}
  </div>
}

createRoot(document.getElementById('root')).render(<App />)
