import { createClient } from '@supabase/supabase-js'

// In LOCAL_MODE Supabase is not used — auth is bypassed entirely on the backend.
const LOCAL_MODE = import.meta.env.VITE_LOCAL_MODE === 'true'

export const supabase = LOCAL_MODE
  ? (null as unknown as ReturnType<typeof createClient>)
  : createClient(
      import.meta.env.VITE_SUPABASE_URL as string,
      import.meta.env.VITE_SUPABASE_ANON_KEY as string,
      { auth: { flowType: 'implicit' } }
    )
