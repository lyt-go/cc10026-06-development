<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <section v-if="benefitPanels.length" class="benefit-panel" data-testid="benefit-panel">
      <header class="benefit-head">
        <h3>批量查询收益核验</h3>
        <button class="btn" data-testid="refresh-benefit" type="button" @click="reloadBenefit">刷新核验结论</button>
      </header>
      <p class="page-desc">
        收益按同一查询、同一输入规模、同一实际计划版本的成对实测样本核算；缺少成对证据的样本不补预估值。
      </p>
      <table class="data-table">
        <thead>
          <tr>
            <th>规则编号</th>
            <th>实际版本</th>
            <th>预估收益</th>
            <th>实测收益</th>
            <th>阈值结论</th>
            <th>可用数据范围</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in benefitPlans" :key="plan.rule_no" :data-testid="`benefit-${plan.rule_no}`">
            <td>{{ plan.rule_no }}</td>
            <td>{{ plan.plan_version || '—' }}</td>
            <td>{{ plan.estimated_benefit || '—' }}</td>
            <td>{{ plan.measured_benefit === null ? '待核验' : formatPercent(plan.measured_benefit) }}</td>
            <td>{{ thresholdLabel(plan) }}</td>
            <td>{{ plan.data_scope.scope_text || '—' }}</td>
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`verify-${plan.rule_no}`"
                @click="runVerify(plan.rule_no)"
              >
                收益核验
              </button>
            </td>
          </tr>
          <tr v-if="!benefitPlans.length">
            <td colspan="7" class="empty-state">暂无待核验的优化计划</td>
          </tr>
        </tbody>
      </table>
      <p v-if="benefitMessage" class="benefit-message" data-testid="benefit-message">{{ benefitMessage }}</p>
      <div v-if="batchRounds" class="benefit-rounds">
        <strong>两轮执行计划核对：</strong>
        <span data-testid="rounds-summary">{{ batchRounds.message }}</span>
        <ul>
          <li v-for="round in batchRounds.rounds" :key="round.request_id">
            {{ round.request_id }}：计划 {{ round.plan_version }}，
            对照 {{ round.baseline_plan_version || '未设置' }}，
            超时 {{ round.timeout_count }} 个
          </li>
        </ul>
      </div>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import {
  fetchRounds,
  listPlans,
  verifyPlan,
} from '@/api/benefit-service'
import type { PlanRow, RoundsResult } from '@/api/benefit-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const benefitPlans = ref<PlanRow[]>([])
const benefitMessage = ref('')
const batchRounds = ref<RoundsResult | null>(null)
const benefitPanels = computed(() =>
  props.moduleKey === 'query_optimize' || props.moduleKey === 'batch_query'
    ? [props.moduleKey]
    : [],
)
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (benefitPanels.value.length) await reloadBenefit()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`
}

function thresholdLabel(plan: PlanRow): string {
  if (plan.measured_benefit === null) return '证据不足，未达阈值'
  const threshold = plan.threshold === null ? '—' : formatPercent(plan.threshold)
  return plan.passed ? `已达阈值 ${threshold}` : `未达阈值 ${threshold}`
}

async function reloadBenefit() {
  if (!benefitPanels.value.length) return
  try {
    const payload = await listPlans()
    benefitPlans.value = payload.plans
  } catch (error) {
    benefitMessage.value = error instanceof Error ? error.message : '核验结论读取失败'
  }
  const batchRow = rows.value[0]
  if (props.moduleKey === 'batch_query' && batchRow) {
    try {
      batchRounds.value = await fetchRounds(Number(batchRow.id))
    } catch {
      batchRounds.value = null
    }
  }
}

async function runVerify(ruleNo: string) {
  benefitMessage.value = ''
  try {
    const report = await verifyPlan(ruleNo)
    benefitMessage.value = report.message
    await reloadBenefit()
  } catch (error) {
    benefitMessage.value = error instanceof Error ? error.message : '收益核验失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
  await reloadBenefit()
}

onMounted(reload)
</script>
