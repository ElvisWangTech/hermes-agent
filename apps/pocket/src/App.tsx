import { useState } from 'react'
import { disconnectDevice, rememberName } from './api'
import { stepFor, type PocketProfile } from './flow'
import { Connect } from './screens/Connect'
import { Home } from './screens/Home'
import { Name } from './screens/Name'
import { Pick } from './screens/Pick'
import { clearPocketStorage, savedRoles, saveProfile, switchRole } from './storage'
import { findCharacter } from './characters'

export function App({ profile: initial }: { profile: PocketProfile }) {
  const [profile, setProfile] = useState<PocketProfile>(() => {
    const next = { ...initial, localId: initial.localId || crypto.randomUUID() }
    saveProfile(next)
    return next
  })
  const [choosing, setChoosing] = useState(false)
  const [adding, setAdding] = useState(false)
  const [previous, setPrevious] = useState<PocketProfile | null>(null)

  const save = (next: PocketProfile) => {
    saveProfile(next)
    setProfile(next)
  }

  const step = stepFor(profile)

  const select = (next: PocketProfile) => {
    switchRole(next)
    setProfile(next)
    setChoosing(false)
    setAdding(false)
  }

  if (choosing) return (
    <main className="overlay">
      <button type="button" onClick={() => setChoosing(false)}>返回</button>
      <h1>切换角色</h1>
      <div className="log">
        {savedRoles().filter(role => stepFor(role) === 'home').map(role => (
          <button type="button" className="role-choice" key={role.localId} onClick={() => select(role)}>
            <img src={findCharacter(role.characterId)?.image} alt="" width={56} height={56} />
            <span>{role.name}</span>
            {role.localId === profile.localId ? <span>当前</span> : null}
          </button>
        ))}
      </div>
      <button type="button" onClick={() => {
        setPrevious(profile)
        select({ localId: crypto.randomUUID(), token: '', characterId: '', name: '' })
        setAdding(true)
      }}>添加角色</button>
    </main>
  )

  if (adding && step !== 'home') return (
    <>
      <button type="button" className="past-entry" onClick={() => {
        clearPocketStorage()
        if (previous) select(previous)
        setChoosing(true)
      }}>返回角色列表</button>
      {step === 'connect' ? <Connect onToken={token => save({ ...profile, token })} /> : step === 'age' ?
        <Pick onChoose={characterId => save({ ...profile, characterId })} /> :
        <Name token={profile.token} characterId={profile.characterId} onNamed={async name => {
          await rememberName(profile.token, name)
          save({ ...profile, name })
          setAdding(false)
        }} />}
    </>
  )
  if (step === 'connect') return <Connect onToken={token => save({ ...profile, token })} />
  if (step === 'age') return <Pick onChoose={characterId => save({ ...profile, characterId })} />
  if (step === 'name') {
    return (
      <Name
        token={profile.token}
        characterId={profile.characterId}
        onNamed={async name => {
          await rememberName(profile.token, name)
          save({ ...profile, name })
        }}
      />
    )
  }

  return (
    <Home
      key={profile.localId}
      profile={profile}
      onSwitch={() => setChoosing(true)}
      onReset={() => save({ ...profile, token: '' })}
      onDestroy={async () => {
        await disconnectDevice(profile.token)
        clearPocketStorage()
        save({ localId: crypto.randomUUID(), token: '', characterId: '', name: '' })
      }}
    />
  )
}
