import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Admin } from './Admin'
import './styles.css'

function LoginGate() {
  const [ready, setReady] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    void fetch('/api/session').then(response => setAuthenticated(response.ok))
      .catch(() => setError('无法连接管理服务')).finally(() => setReady(true))
  }, [])
  async function login() {
    setBusy(true); setError('')
    try {
      const response = await fetch('/api/login', { method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-Pocket-Admin': '1'
      }, body: JSON.stringify({ password }) })
      setPassword('')
      if (!response.ok) throw new Error(response.status === 429 ? '尝试过于频繁，请稍后再试' : '登录失败')
      setAuthenticated(true)
    } catch (failure) { setError((failure as Error).message) }
    finally { setBusy(false) }
  }
  async function logout() {
    const response = await fetch('/api/logout', { method: 'POST', headers: { 'X-Pocket-Admin': '1' } })
    if (response.ok || response.status === 401) setAuthenticated(false)
  }
  if (!ready) return <p role="status">正在验证登录…</p>
  if (authenticated) return <Admin onLogout={() => void logout()} onExpired={() => setAuthenticated(false)} />
  return <main className="pa-login"><form onSubmit={event => { event.preventDefault(); void login() }}>
    <h1>Pocket 管理</h1><label>管理员密码<input type="password" autoComplete="current-password" required maxLength={1024} value={password} onChange={event => setPassword(event.target.value)} /></label>
    {error && <p role="alert" className="pa-error">{error}</p>}
    <button className="pa-primary" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
  </form></main>
}

createRoot(document.getElementById('root')!).render(<StrictMode><LoginGate /></StrictMode>)