import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, Copy, KeyRound, RefreshCw, Settings, ShieldOff, Users, X } from 'lucide-react'

interface Role { id: string; profile: string; active: number }
interface Device { id: string; role_id: string; revoked: number }
interface Code { id: number; role_id: string; expires: number; used: number }
interface Snapshot { roles: Role[]; devices: Device[]; codes: Code[]; profiles: string[]; server_time: number }
interface Issued { code: string; profile_id: string; expires_in: number }
interface SpeechSettings { provider?: string; enabled?: boolean; language?: string; [key: string]: string | boolean | number | Record<string, string | number | boolean> | undefined }
interface ProfileConfig { profile: string; model: string; provider: string; context_length: number; stt: SpeechSettings; tts: SpeechSettings }
interface ConfigOptions { providers: { slug: string; name: string; models: string[] }[]; stt: SpeechSettings; tts: SpeechSettings }

function SpeechFields({ title, value, defaults, onChange }: { title: string; value: SpeechSettings; defaults: SpeechSettings; onChange: (value: SpeechSettings) => void }) {
  const providers = [...new Set([...Object.keys(defaults).filter(key => typeof defaults[key] === 'object'), value.provider || ''])]
  const provider = value.provider || ''
  const fields = { ...(defaults[provider] as Record<string, string | number | boolean> || {}), ...(value[provider] as Record<string, string | number | boolean> || {}) }
  const edgeVoices = [
    ['zh-CN-XiaoxiaoNeural', '中文（普通话） · 晓晓'],
    ['zh-CN-XiaoyiNeural', '中文（普通话） · 晓伊'],
    ['zh-CN-YunxiNeural', '中文（普通话） · 云希'],
    ['zh-CN-YunjianNeural', '中文（普通话） · 云健'],
    ['zh-TW-HsiaoChenNeural', '中文（台湾） · 曉臻'],
    ['zh-HK-HiuMaanNeural', '中文（粤语） · 曉曼'],
    ['en-US-AriaNeural', 'English · Aria'],
    ['en-US-GuyNeural', 'English · Guy'],
  ]
  return <fieldset><legend>{title}</legend>
    <label>Provider<select value={provider} onChange={event => onChange({ ...value, provider: event.target.value })}><option value="">自动选择</option>{providers.filter(Boolean).map(name => <option key={name}>{name}</option>)}</select></label>
    {title === 'STT' && <><label className="pa-toggle"><input type="checkbox" checked={value.enabled !== false} onChange={event => onChange({ ...value, enabled: event.target.checked })} />启用语音识别</label><label>语言<input value={String(value.language || '')} onChange={event => onChange({ ...value, language: event.target.value })} /></label></>}
    {Object.entries(fields).map(([key, field]) => <label key={key}>{key}{title === 'TTS' && provider === 'edge' && key === 'voice' ? <select value={String(field)} onChange={event => onChange({ ...value, [provider]: { ...fields, voice: event.target.value } })}>
      {!edgeVoices.some(([name]) => name === field) && <option value={String(field)}>{String(field)}</option>}
      {edgeVoices.map(([name, label]) => <option key={name} value={name}>{label}</option>)}
    </select> : <><input list={key === 'model' || key === 'model_id' ? `pa-${title}-${provider}-${key}` : undefined} value={String(field)} onChange={event => onChange({ ...value, [provider]: { ...fields, [key]: typeof field === 'number' ? Number(event.target.value) : event.target.value } })} /><datalist id={`pa-${title}-${provider}-${key}`}><option value={String((defaults[provider] as Record<string, string> || {})[key] || '')} /></datalist></>}</label>)}
  </fieldset>
}
interface Confirmation { title: string; path: string; roleId?: string }

async function request<Result>(path: string, method = 'GET', body?: unknown): Promise<Result> {
  const response = await fetch(`/api/pocket/${path}`, {
    method, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Pocket-Admin': '1' },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  const result = await response.json()
  if (response.status === 401) throw new Error('SESSION_EXPIRED')
  if (!response.ok) throw new Error(result.detail || '操作失败')
  return result as Result
}

interface AdminProps { onLogout: () => void; onExpired: () => void }

export function Admin({ onLogout, onExpired }: AdminProps) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [data, setData] = useState<Snapshot | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState('')
  const [view, setView] = useState('roles')
  const [query, setQuery] = useState('')
  const [issuing, setIssuing] = useState(false)
  const [profile, setProfile] = useState('')
  const [createProfile, setCreateProfile] = useState(false)
  const [ttl, setTtl] = useState(600)
  const [issued, setIssued] = useState<Issued | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [typed, setTyped] = useState('')
  const [notice, setNotice] = useState('')
  const [now, setNow] = useState(Date.now())
  const [offset, setOffset] = useState(0)
  const [expires, setExpires] = useState(0)
  const [configProfile, setConfigProfile] = useState('')
  const [config, setConfig] = useState<ProfileConfig | null>(null)
  const [options, setOptions] = useState<ConfigOptions | null>(null)
  const [credentials, setCredentials] = useState<Record<string, boolean>>({})
  const [credentialKey, setCredentialKey] = useState('')
  const [credentialValue, setCredentialValue] = useState('')
  const [removeCredential, setRemoveCredential] = useState(false)

  async function reload() {
    setBusy(true); setError('')
    try {
      const snapshot = await request<Snapshot>('admin')
      setData(snapshot); setOffset(snapshot.server_time * 1000 - Date.now())
    } catch (failure) { handleFailure(failure) }
    finally { setBusy(false) }
  }
  function close() { setIssuing(false); setIssued(null); setConfirmation(null); setTyped('') }
  function handleFailure(failure: unknown) {
    const message = failure instanceof Error ? failure.message : String(failure)
    if (message === 'SESSION_EXPIRED') { close(); onExpired() }
    else setError(message)
  }
  useEffect(() => { document.title = 'Pocket 管理'; void reload() }, [])
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    if (issued && now + offset >= expires) setIssued(null)
  }, [now, offset, expires, issued])
  useEffect(() => {
    function escape(event: KeyboardEvent) { if (event.key === 'Escape' && !busy) close() }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [busy])

  async function issue() {
    setBusy(true); setError('')
    try {
      const result = await request<Issued>('codes', 'POST', { profile, ttl, create_profile: createProfile })
      setExpires(Date.now() + offset + result.expires_in * 1000); setIssued(result); setIssuing(false)
      setNotice('连接码已签发'); await reload()
    } catch (failure) { handleFailure(failure) }
    finally { setBusy(false) }
  }
  async function mutate() {
    if (!confirmation) return
    setBusy(true); setError('')
    try {
      await request(confirmation.path, 'DELETE'); close(); setNotice('操作已完成'); await reload()
    } catch (failure) { handleFailure(failure) }
    finally { setBusy(false) }
  }
  async function loadProfileConfig(name: string) {
    setConfigProfile(name); setConfig(null); setBusy(true); setError('')
    setOptions(null)
    setCredentials({}); setCredentialKey(''); setCredentialValue(''); setRemoveCredential(false)
    if (!name) { setBusy(false); return }
    try {
      setConfig(await request<ProfileConfig>(`profiles/${encodeURIComponent(name)}/config`))
      setOptions(await request<ConfigOptions>(`profiles/${encodeURIComponent(name)}/options`))
      setCredentials(await request<Record<string, boolean>>(`profiles/${encodeURIComponent(name)}/credentials`))
    }
    catch (failure) { handleFailure(failure) }
    finally { setBusy(false) }
  }
  async function saveCredential(event: React.FormEvent) {
    event.preventDefault()
    if (!configProfile || !credentialKey || !removeCredential && !credentialValue.trim()) return
    setBusy(true); setError('')
    try {
      setCredentials(await request<Record<string, boolean>>(`profiles/${encodeURIComponent(configProfile)}/credentials/${encodeURIComponent(credentialKey)}`, removeCredential ? 'DELETE' : 'PUT', removeCredential ? undefined : { value: credentialValue }))
      setCredentialValue(''); setRemoveCredential(false); setNotice('凭据已保存')
    } catch (failure) { handleFailure(failure) }
    finally { setBusy(false) }
  }
  async function saveProfileConfig(event: React.FormEvent) {
    event.preventDefault()
    if (!config) return
    setBusy(true); setError('')
    try {
      setConfig(await request<ProfileConfig>(`profiles/${encodeURIComponent(config.profile)}/config`, 'PUT', config))
      setNotice('配置已保存')
    } catch (failure) { handleFailure(failure) }
    finally { setBusy(false) }
  }
  function confirm(title: string, path: string, roleId?: string) { setTyped(''); setConfirmation({ title, path, roleId }) }
  function beginIssue(target = '') {
    setProfile(target || data?.profiles[0] || '')
    setCreateProfile(!target && !data?.profiles.length)
    setIssuing(true)
  }
  const role = data?.roles.find(item => item.id === selected)
  const modalOpen = issuing || issued || confirmation
  useEffect(() => {
    if (modalOpen && dialog.current && !dialog.current.open) dialog.current.showModal()
  }, [modalOpen])
  const rows = data?.roles.filter(item => `${item.profile} ${item.id}`.includes(query)) || []
  const codeState = (code: Code) => code.used ? '已消费 / 撤销' : code.expires * 1000 <= now + offset ? '已过期' : '有效'

  return <div className="pa-shell">
    <aside><strong>Pocket 管理</strong><nav>
      <button className={view === 'roles' ? 'current' : ''} onClick={() => { setView('roles'); setSelected('') }}><Users size={18} />角色授权</button>
      <button className={view === 'codes' ? 'current' : ''} onClick={() => { setView('codes'); setSelected('') }}><KeyRound size={18} />连接码</button>
      <button className={view === 'config' ? 'current' : ''} onClick={() => { setView('config'); setSelected(''); setConfigProfile(''); setConfig(null) }}><Settings size={18} />Profile 配置</button>
    </nav><small>单机授权服务</small></aside>
    <section className="pa-work"><header><span>Pocket / {view === 'roles' ? '角色授权' : view === 'codes' ? '连接码' : 'Profile 配置'}</span><button disabled={busy} onClick={onLogout}>退出登录</button></header>
      <main><div className="pa-heading"><div><h1>{role ? role.profile : view === 'roles' ? '角色授权' : view === 'codes' ? '连接码' : configProfile || 'Profile 配置'}</h1><p>{role ? role.id : view === 'config' ? '模型与语音配置' : `${data?.roles.length || 0} 个已登记角色`}</p></div>
        <div className="pa-actions"><button title="刷新" aria-label="刷新" disabled={busy} onClick={() => void (view === 'config' && configProfile ? loadProfileConfig(configProfile) : reload())}><RefreshCw size={18} /></button>{view !== 'config' && <button disabled={busy || !!error || role?.active === 0} onClick={() => beginIssue(role?.profile)}><KeyRound size={18} />签发连接码</button>}</div></div>
        {error && <div role="alert" className="pa-error">{error}<button disabled={busy} onClick={() => void (view === 'config' && configProfile ? loadProfileConfig(configProfile) : reload())}>重试</button></div>}
        <div role="status" className="pa-notice">{busy ? '正在处理…' : notice}</div>
        {!data && !error && <p>正在读取授权记录…</p>}
        {data && view === 'roles' && !role && <><input aria-label="搜索角色" placeholder="搜索 profile 或稳定 ID" value={query} onChange={event => setQuery(event.target.value)} /><div className="pa-list">
          {rows.map(item => <button className="pa-role" key={item.id} onClick={() => setSelected(item.id)}><Users size={28} aria-hidden="true" /><span><strong>{item.profile}</strong><small>{item.id}</small></span><span className={item.active ? 'pa-good' : 'pa-bad'}>{item.active ? '授权开放' : '已停用'}</span><span>{data.devices.filter(device => device.role_id === item.id && !device.revoked).length} 个有效凭证</span></button>)}
          {!rows.length && <p className="pa-empty">没有匹配的授权角色</p>}
        </div></>}
        {data && role && <><button onClick={() => setSelected('')}><ArrowLeft size={18} />返回列表</button><h2>访问授权</h2><p className={role.active ? 'pa-good' : 'pa-bad'}>{role.active ? '开放' : '已停用'}</p><h2>设备凭证</h2><div className="pa-list">
          {data.devices.filter(device => device.role_id === role.id).map(device => <div className="pa-row" key={device.id}><div><strong>设备凭证</strong><small>{device.id}</small></div><span>{device.revoked ? '已撤销' : '有效'}</span><button disabled={busy || !!error || !!device.revoked} onClick={() => confirm('撤销此设备凭证', `roles/${role.id}/devices/${device.id}`)}>撤销</button></div>)}
          {!data.devices.some(device => device.role_id === role.id) && <p className="pa-empty">尚无设备凭证</p>}
        </div><h2>全部授权</h2><button className="pa-danger" disabled={busy || !!error || !role.active} onClick={() => confirm('停用全部授权', `roles/${role.id}/authorization`, role.id)}><ShieldOff size={18} />停用全部授权</button></>}
        {data && view === 'codes' && <div className="pa-list">{data.codes.map(code => <div className="pa-row" key={code.id}><div><strong>{data.roles.find(item => item.id === code.role_id)?.profile || code.role_id}</strong><small>码 #{code.id} · {new Date(code.expires * 1000).toLocaleString('zh-CN')}</small></div><span>{codeState(code)}</span><button disabled={busy || !!error || codeState(code) !== '有效'} onClick={() => confirm('撤销连接码', `codes/${code.id}`)}>撤销</button></div>)}{!data.codes.length && <p className="pa-empty">尚无连接码</p>}</div>}
        {data && view === 'config' && <div className="pa-config"><label>选择 profile<select disabled={busy} value={configProfile} onChange={event => void loadProfileConfig(event.target.value)}><option value="">请选择</option>{data.profiles.map(name => <option key={name} value={name}>{name}</option>)}</select></label>{config && <form onSubmit={event => void saveProfileConfig(event)}>
          <label>Provider<select value={config.provider} onChange={event => setConfig({ ...config, provider: event.target.value })}><option value="">自动选择</option>{config.provider && !options?.providers.some(item => item.slug === config.provider) && <option value={config.provider}>{config.provider}</option>}{options?.providers.map(item => <option key={item.slug} value={item.slug}>{item.name || item.slug}</option>)}</select></label>
          <label>模型名称<input list="pa-model-options" required maxLength={512} value={config.model} onChange={event => setConfig({ ...config, model: event.target.value })} /><datalist id="pa-model-options">{options?.providers.find(item => item.slug === config.provider)?.models.map(model => <option key={model} value={model} />)}</datalist></label>
          <label>上下文长度<input type="number" min="0" max="10000000" value={config.context_length} onChange={event => setConfig({ ...config, context_length: Number(event.target.value) })} /></label>
          <SpeechFields title="STT" value={config.stt} defaults={options?.stt || {}} onChange={stt => setConfig({ ...config, stt })} />
          <SpeechFields title="TTS" value={config.tts} defaults={options?.tts || {}} onChange={tts => setConfig({ ...config, tts })} />
          <button className="pa-primary" disabled={busy}>保存配置</button></form>}{config && <form onSubmit={event => void saveCredential(event)}><fieldset disabled={busy}><legend>API 凭据</legend>
            <label>凭据槽位<select value={credentialKey} onChange={event => { setCredentialKey(event.target.value); setCredentialValue(''); setRemoveCredential(false) }}><option value="">请选择</option>{Object.keys(credentials).map(key => <option key={key} value={key}>{key}</option>)}</select></label>
            {credentialKey && <><p className={credentials[credentialKey] ? 'pa-good' : 'pa-bad'}>{credentials[credentialKey] ? '已配置' : '未配置'}</p><label>新 API Key<input type="password" autoComplete="new-password" maxLength={4096} value={credentialValue} disabled={removeCredential} onChange={event => setCredentialValue(event.target.value)} /></label>
              {credentials[credentialKey] && <label className="pa-toggle"><input type="checkbox" checked={removeCredential} onChange={event => { setRemoveCredential(event.target.checked); setCredentialValue('') }} />确认删除此 profile 的 {credentialKey}</label>}
              <button className={removeCredential ? 'pa-danger' : 'pa-primary'} disabled={!removeCredential && !credentialValue.trim()}><KeyRound size={18} />{removeCredential ? '确认删除凭据' : '保存凭据'}</button></>}
          </fieldset></form>}</div>}
      </main>
    </section>
    {modalOpen && <dialog ref={dialog} className="pa-modal" aria-labelledby="pa-dialog-title" onCancel={event => { event.preventDefault(); if (!busy) close() }}><div className="pa-dialog-header"><h2 id="pa-dialog-title">{issuing ? '签发连接码' : issued ? '交付连接码' : confirmation?.title}</h2><button aria-label="关闭" title="关闭" disabled={busy} onClick={close}><X size={20} /></button></div>
      {error && <p role="alert" className="pa-error">{error}</p>}
        {issuing && <form onSubmit={event => { event.preventDefault(); void issue() }}>
          <label className="pa-toggle"><input type="checkbox" checked={createProfile} onChange={event => setCreateProfile(event.target.checked)} />签发时新建 profile</label>
          <label>独立 profile{createProfile
            ? <input required maxLength={64} value={profile} onChange={event => setProfile(event.target.value)} placeholder="例如 assistant" />
            : <select value={profile} onChange={event => setProfile(event.target.value)}>{data?.profiles.map(name => <option key={name} disabled={data.roles.some(item => item.profile === name && !item.active)}>{name}</option>)}</select>}
          </label>
          <label>有效期<select value={ttl} onChange={event => setTtl(Number(event.target.value))}><option value={300}>5 分钟</option><option value={600}>10 分钟</option><option value={1800}>30 分钟</option></select></label>
          <p>{createProfile ? '新 profile 使用 default 的模型与语音设置快照，不复制密钥。' : '一次兑换绑定一个设备。新增设备需要单独签发；旧码不会自动撤销。'}</p>
          <button className="pa-primary" disabled={busy || !profile.trim()}>确认签发</button>
        </form>}
      {issued && <><p>目标：{issued.profile_id}</p><output className="pa-secret">{issued.code}</output><p>剩余 {Math.max(0, Math.ceil((expires - now - offset) / 1000))} 秒 · 关闭后不再展示</p><button onClick={() => void navigator.clipboard.writeText(issued.code).then(() => setNotice('连接码已复制')).catch(() => setError('复制失败，请手动选择连接码'))}><Copy size={18} />复制</button><button className="pa-primary" onClick={close}>已交付，关闭</button></>}
      {confirmation && <><p>{confirmation.roleId ? '停用后，设备和未用码均失效，保留 profile 数据。不承诺主动关闭连接或取消在途任务。' : '撤销后无法再使用此凭证，其他设备不受影响。'}</p>{confirmation.roleId && <label>输入目标稳定 ID<strong className="pa-id">{confirmation.roleId}</strong><input autoFocus value={typed} onChange={event => setTyped(event.target.value)} /></label>}<button className="pa-danger" disabled={busy || !!confirmation.roleId && typed !== confirmation.roleId} onClick={() => void mutate()}>确认{confirmation.roleId ? '停用' : '撤销'}</button></>}
    </dialog>}
  </div>
}