/** 批量查询收益核验：计划阈值结论、数据范围、两轮执行版本核对。 */

import { request } from './client'

export type VerificationScope = {
  covered_query_classes: string[]
  missing_query_classes: string[]
  covered_input_scales: string[]
  paired_samples: number
  total_samples: number
  coverage_ratio: number
  excluded_samples: number
  scope_text: string
  plan_version: string
}

export type PlanRow = {
  rule_no: string
  api: string
  plan_version: string
  estimated_benefit: string
  measured_benefit: number | null
  passed: boolean
  threshold: number | null
  data_scope: VerificationScope | Record<string, never>
  verified_at: string | null
}

export type VerificationReport = {
  ok: boolean
  rule_no: string
  api: string
  plan_version: string
  baseline_plan_versions: string[]
  threshold: number
  measured_benefit: number | null
  measured_benefit_text: string
  passed: boolean
  pair_count: number
  sample_count: number
  excluded: { query_key?: string; query_class?: string; input_scale?: unknown; reason: string }[]
  data_scope: VerificationScope
  message: string
  verified_at: string
}

export type RoundSummary = {
  request_id: string
  plan_version: string
  baseline_plan_version: string
  rule_no: string
  timeout_count: number
}

export type RoundsResult = {
  batch_id: number
  rounds: RoundSummary[]
  versions: string[]
  has_contrast: boolean
  message: string
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function listPlans(api?: string): Promise<{ plans: PlanRow[]; reached_threshold: string[] }> {
  const query = api ? `?api=${encodeURIComponent(api)}` : ''
  return json(await request(`/api/benefit/plans${query}`))
}

export async function verifyPlan(ruleNo: string, threshold?: number): Promise<VerificationReport> {
  return json(
    await request('/api/benefit/verify', {
      method: 'POST',
      body: JSON.stringify({ rule_no: ruleNo, threshold: threshold ?? null }),
    }),
  )
}

export async function fetchRounds(batchId: number): Promise<RoundsResult> {
  return json(await request(`/api/benefit/batches/${batchId}/rounds`))
}
