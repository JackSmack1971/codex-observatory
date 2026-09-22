export type Evidence = { source_class: string; fact_type: string; stability?: string | null; source_event?: string | null; correlation_confidence?: string | null }
export type Page<T> = { items: T[]; next_cursor: string | null; has_more: boolean }
export type Metric = { value: number | null; coverage: string; evidence?: Evidence | null }
export type Health = { status: string; components: { component: string; status: string; reason?: string | null }[] }
export type Overview = { sessions: Metric; threads: Metric; turns: Metric; events: Metric; tool_calls: Metric; tool_failures: Metric; approvals: Metric; agents: Metric; tokens: Metric; repositories: Metric; health: Health; archive: { status: string; batches: number; files: number; rows: number; verification_failures: number; small_files: number; duckdb_status: string } }
export type Session = { session_id: string | null; thread_id: string; name: string | null; cwd: string | null; model: string | null; started_at: string | null; updated_at: string | null; status: string | null; archived: boolean | null; turns: number; tokens: number | null; tool_calls: number; evidence: Evidence }
export type Row = Record<string, unknown> & { evidence?: Evidence }
export type AdminHealth = { collector: string; status: string; reason?: string | null; last_success?: string | null; last_error?: string | null }
export type AdminGroup = { dimension: 'model' | 'project' | 'service_tier' | 'batch' | 'line_item'; value: string | boolean | null; input_tokens?: number; output_tokens?: number; model_requests?: number; amount?: string; currency?: string | null; result_count: number }
export type AdminBucket = { start_time: number | null; end_time: number | null }
export type AdminInterval = { key: '24h' | '7d' | '30d'; start_time: number; end_time: number; timezone: 'UTC' }
export type AdminProvenance = { source: 'openai_admin_api'; scope: 'organization'; attribution: 'unavailable' }
export type AdminUsageSummary = { provenance: AdminProvenance; interval: AdminInterval; health: AdminHealth; input_tokens: number | null; output_tokens: number | null; model_requests: number | null; result_count: number; latest_bucket: AdminBucket; groups: AdminGroup[] }
export type AdminCostTotal = { currency: string | null; amount: string }
export type AdminCostSummary = { provenance: AdminProvenance; interval: AdminInterval; health: AdminHealth; totals: AdminCostTotal[]; result_count: number; latest_bucket: AdminBucket; groups: AdminGroup[]; groups_omitted: number }
export type AdminSummary = { provenance: AdminProvenance; interval: AdminInterval; usage: AdminUsageSummary; costs: AdminCostSummary }
export async function get<T>(path: string, signal?: AbortSignal): Promise<T> { const controller = new AbortController(); const timeout = window.setTimeout(() => controller.abort(), 8000); if (signal) signal.addEventListener('abort', () => controller.abort(), { once: true }); try { const response = await fetch(`/api/v1${path}`, { signal: controller.signal, headers: { Accept: 'application/json' } }); if (!response.ok) throw new Error(`API ${response.status}: ${await response.text()}`); return await response.json() as T } finally { window.clearTimeout(timeout) } }
