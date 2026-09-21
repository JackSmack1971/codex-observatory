export type LiveState = 'CONNECTING' | 'LIVE' | 'RECONNECTING' | 'DEGRADED'

type LiveMessage =
  | { type: 'event'; schema: 'codex.observatory.live.v1'; sequence: number; payload: unknown }
  | { type: 'subscribed'; schema: 'codex.observatory.live.v1'; sequence: number }
  | { type: 'heartbeat'; schema: 'codex.observatory.live.v1'; sequence: number }
  | { type: 'reset_required'; schema: 'codex.observatory.live.v1'; oldest_available_sequence: number | null; latest_sequence: number | null }

export class LiveConnection {
  private socket?: WebSocket
  private cursor?: number
  private attempts = 0
  private timer?: number
  private stopped = false
  private invalidate: (full: boolean) => void
  private updateState: (state: LiveState) => void

  constructor(invalidate: (full: boolean) => void, updateState: (state: LiveState) => void) {
    this.invalidate = invalidate
    this.updateState = updateState
  }

  start() { this.stopped = false; this.connect(false) }
  stop() { this.stopped = true; if (this.timer !== undefined) window.clearTimeout(this.timer); this.socket?.close() }

  private connect(reconnecting: boolean) {
    this.updateState(reconnecting ? 'RECONNECTING' : 'CONNECTING')
    const url = new URL('/api/v1/live', window.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(url)
    this.socket = socket
    socket.onopen = () => {
      this.attempts = 0
      socket.send(JSON.stringify({ type: 'subscribe', resume_from_sequence: this.cursor ?? null, filters: {} }))
      this.updateState('LIVE')
    }
    socket.onmessage = message => {
      let body: LiveMessage
      try { body = JSON.parse(String(message.data)) as LiveMessage } catch { return }
      if (body.schema !== 'codex.observatory.live.v1') return
      if (body.type === 'event' && Number.isSafeInteger(body.sequence) && body.sequence > (this.cursor ?? 0)) {
        this.cursor = body.sequence
        this.invalidate(false)
      } else if (body.type === 'subscribed' && Number.isSafeInteger(body.sequence)) {
        this.cursor = Math.max(this.cursor ?? 0, body.sequence)
        this.invalidate(true)
      } else if (body.type === 'heartbeat' && Number.isSafeInteger(body.sequence)) {
        this.cursor = Math.max(this.cursor ?? 0, body.sequence)
      } else if (body.type === 'reset_required') {
        this.cursor = undefined
        this.invalidate(true)
      }
    }
    socket.onerror = () => this.updateState('DEGRADED')
    socket.onclose = () => {
      if (this.stopped || socket !== this.socket) return
      this.updateState('DEGRADED')
      const delay = Math.min(1000 * 2 ** this.attempts++, 30_000)
      this.timer = window.setTimeout(() => this.connect(true), delay)
    }
  }
}
