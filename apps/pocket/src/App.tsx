import { useState } from 'react'
import { disconnectDevice, rememberName } from './api'
import { BackButton } from './BackButton'
import { stepFor, type PocketProfile } from './flow'
import { Connect } from './screens/Connect'
import { Home } from './screens/Home'
import { Name } from './screens/Name'
import { Pick } from './screens/Pick'
import { clearPocketStorage, savedRoles, saveProfile, switchRole } from './storage'
import { findCharacter } from './characters'
import { newId } from './storage'

export function App({ profile: initial }: { profile: PocketProfile }) {
  const [profile, setProfile] = useState<PocketProfile>(() => {
    const next = { ...initial, localId: initial.localId || newId() }
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

  const leaveAdd = () => {
    clearPocketStorage()
    if (previous) select(previous)
    setChoosing(true)
  }

  if (choosing) return (
    <main className="overlay">
      <BackButton onClick={() => setChoosing(false)} />
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
        select({ localId: newId(), token: '', characterId: '', name: '' })
        setAdding(true)
      }}>添加角色</button>
    </main>
  )

  if (adding && step !== 'home') return (
    step === 'connect' ? <Connect onLeave={leaveAdd} onToken={token => save({ ...profile, token })} /> : step === 'age' ?
      <Pick onLeave={leaveAdd} onChoose={characterId => save({ ...profile, characterId })} /> :
      <Name onLeave={leaveAdd} token={profile.token} characterId={profile.characterId} onNamed={async name => {
        await rememberName(profile.token, name)
        save({ ...profile, name })
        setAdding(false)
      }} />
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
        save({ localId: newId(), token: '', characterId: '', name: '' })
      }}
    />
  )
}
