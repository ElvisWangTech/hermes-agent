import { useState } from 'react'
import { destroyRole, rememberName } from './api'
import { stepFor, type PocketProfile } from './flow'
import { Connect } from './screens/Connect'
import { Home } from './screens/Home'
import { Name } from './screens/Name'
import { Pick } from './screens/Pick'
import { clearPocketStorage, saveProfile } from './storage'

export function App({ profile: initial }: { profile: PocketProfile }) {
  const [profile, setProfile] = useState(initial)

  const save = (next: PocketProfile) => {
    saveProfile(next)
    setProfile(next)
  }

  const step = stepFor(profile)

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
      profile={profile}
      onReset={() => save({ token: '', characterId: profile.characterId, name: profile.name })}
      onDestroy={async () => {
        await destroyRole(profile.token, profile.name)
        clearPocketStorage()
        setProfile({ token: '', characterId: '', name: '' })
      }}
    />
  )
}
