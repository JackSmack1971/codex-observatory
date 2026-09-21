import { afterEach, describe, expect, it, vi } from 'vitest'
import { LiveConnection } from './live'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  url: URL
  constructor(url: URL) { this.url = url; FakeWebSocket.instances.push(this) }
  close() {}
  open() { this.onopen?.() }
  message(body: object) { this.onmessage?.({ data: JSON.stringify(body) }) }
  disconnect() { this.onclose?.() }
}

describe('LiveConnection', () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); FakeWebSocket.instances = [] })

  it('connects same-origin and invalidates HTTP data on live events', () => {
    const states: string[] = []; const invalidations: boolean[] = []
    vi.stubGlobal('window', { location: { href: 'http://127.0.0.1:8080/sessions' }, setTimeout, clearTimeout })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const live = new LiveConnection(full => invalidations.push(full), state => states.push(state))
    live.start(); FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].message({ type: 'event', schema: 'codex.observatory.live.v1', sequence: 7, event: {} })
    expect(String(FakeWebSocket.instances[0].url)).toBe('ws://127.0.0.1:8080/api/v1/live')
    expect(states).toEqual(['CONNECTING', 'LIVE'])
    expect(invalidations).toEqual([false])
    live.stop()
  })

  it('reconnects with its acknowledged sequence and preserves state while disconnected', () => {
    vi.useFakeTimers(); const states: string[] = []; const invalidations: boolean[] = []
    vi.stubGlobal('window', { location: { href: 'http://localhost/' }, setTimeout, clearTimeout })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const live = new LiveConnection(full => invalidations.push(full), state => states.push(state))
    live.start(); FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].message({ type: 'event', schema: 'codex.observatory.live.v1', sequence: 9, event: {} })
    FakeWebSocket.instances[0].disconnect()
    expect(invalidations).toEqual([false])
    expect(states.at(-1)).toBe('DEGRADED')
    vi.advanceTimersByTime(1000)
    expect(states.at(-1)).toBe('RECONNECTING')
    expect(String(FakeWebSocket.instances[1].url)).toContain('last_sequence=9')
    live.stop()
  })

  it('requests authoritative REST recovery after reset_required', () => {
    const invalidations: boolean[] = []
    vi.stubGlobal('window', { location: { href: 'http://localhost/' }, setTimeout, clearTimeout })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const live = new LiveConnection(full => invalidations.push(full), () => {})
    live.start()
    FakeWebSocket.instances[0].message({ type: 'reset_required', schema: 'codex.observatory.live.v1', oldest_available_sequence: 5, latest_sequence: 10 })
    expect(invalidations).toEqual([true])
    live.stop()
  })
})
