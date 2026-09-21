import { afterEach, describe, expect, it, vi } from 'vitest'
import { LiveConnection } from './live'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  sent: string[] = []
  url: URL
  constructor(url: URL) { this.url = url; FakeWebSocket.instances.push(this) }
  close() {}
  send(value: string) { this.sent.push(value) }
  open() { this.onopen?.() }
  message(body: object) { this.onmessage?.({ data: JSON.stringify(body) }) }
  disconnect() { this.onclose?.() }
}

describe('LiveConnection', () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); FakeWebSocket.instances = [] })

  it('sends the documented subscribe frame and handles baseline/event messages', () => {
    const states: string[] = []; const invalidations: boolean[] = []
    vi.stubGlobal('window', { location: { href: 'http://127.0.0.1:8080/sessions' }, setTimeout, clearTimeout })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const live = new LiveConnection(full => invalidations.push(full), state => states.push(state))
    live.start(); FakeWebSocket.instances[0].open()
    expect(JSON.parse(FakeWebSocket.instances[0].sent[0])).toEqual({ type: 'subscribe', resume_from_sequence: null, filters: {} })
    FakeWebSocket.instances[0].message({ type: 'subscribed', schema: 'codex.observatory.live.v1', sequence: 6 })
    FakeWebSocket.instances[0].message({ type: 'event', schema: 'codex.observatory.live.v1', sequence: 7, payload: {} })
    expect(String(FakeWebSocket.instances[0].url)).toBe('ws://127.0.0.1:8080/api/v1/live')
    expect(states).toEqual(['CONNECTING', 'LIVE'])
    expect(invalidations).toEqual([true, false])
    live.stop()
  })

  it('reconnects with its acknowledged sequence and preserves state while disconnected', () => {
    vi.useFakeTimers(); const states: string[] = []; const invalidations: boolean[] = []
    vi.stubGlobal('window', { location: { href: 'http://localhost/' }, setTimeout, clearTimeout })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const live = new LiveConnection(full => invalidations.push(full), state => states.push(state))
    live.start(); FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].message({ type: 'subscribed', schema: 'codex.observatory.live.v1', sequence: 8 })
    FakeWebSocket.instances[0].message({ type: 'event', schema: 'codex.observatory.live.v1', sequence: 9, payload: {} })
    FakeWebSocket.instances[0].disconnect()
    expect(invalidations).toEqual([true, false])
    expect(states.at(-1)).toBe('DEGRADED')
    vi.advanceTimersByTime(1000)
    expect(states.at(-1)).toBe('RECONNECTING')
    FakeWebSocket.instances[1].open()
    expect(JSON.parse(FakeWebSocket.instances[1].sent[0])).toEqual({ type: 'subscribe', resume_from_sequence: 9, filters: {} })
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
