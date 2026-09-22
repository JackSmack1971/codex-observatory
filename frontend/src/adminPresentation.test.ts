import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const app = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8')

describe('Admin presentation contract', () => {
  it('keeps Admin evidence explicitly organization-scoped and unattributed', () => {
    expect(app).toContain('Admin Usage & Costs')
    expect(app).toContain('OpenAI Admin API')
    expect(app).toContain('organization/API usage and billing evidence')
    expect(app).toContain('Local Codex session attribution:</strong> unavailable / not established')
    expect(app).not.toMatch(/session_cost|thread_cost|turn_cost|agent_cost/)
    expect(app).not.toMatch(/this Codex session cost/i)
  })

  it('uses fixed UTC ranges and textual unavailable/null states', () => {
    expect(app).toContain('24h')
    expect(app).toContain('7d')
    expect(app).toContain('30d')
    expect(app).toContain('Unknown / unavailable')
    expect(app).toContain('No usage data in the selected UTC interval.')
    expect(app).toContain('values are not converted between currencies.')
  })
})
