import { afterEach, describe, expect, it, vi } from 'vitest'
import { get } from './client'

describe('API client', () => {
  afterEach(() => vi.restoreAllMocks())

  it('decodes typed JSON using the relative API base', async () => {
    vi.stubGlobal('window', { setTimeout, clearTimeout })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 })))
    await expect(get<{ items: unknown[] }>('/sessions')).resolves.toEqual({ items: [] })
    expect(fetch).toHaveBeenCalledWith('/api/v1/sessions', expect.objectContaining({ headers: { Accept: 'application/json' } }))
  })

  it('normalizes API errors', async () => {
    vi.stubGlobal('window', { setTimeout, clearTimeout })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('bad input', { status: 422 })))
    await expect(get('/events')).rejects.toThrow('API 422: bad input')
  })
})
