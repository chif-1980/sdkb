<template>
  <section class="review-workspace" aria-label="待审核工作区">
    <aside class="review-queue">
      <div class="queue-heading">
        <div class="queue-heading-row">
          <h2>待审核</h2>
          <span>按风险优先</span>
        </div>
        <div class="queue-filters">
          <a-select v-model:value="statusFilter" size="small" :options="statusOptions" />
          <a-select v-model:value="problemFilter" size="small" :options="problemOptions" />
        </div>
      </div>
      <a-spin :spinning="loading">
        <div v-if="!reviews.length && !loading" class="queue-empty">
          <a-empty description="暂无待审核素材" />
        </div>
        <button
          v-for="review in reviews"
          :key="review.review_id"
          type="button"
          class="queue-item"
          :class="{ active: review.review_id === selectedReviewId }"
          @click="selectReview(review.review_id)"
        >
          <div class="queue-title">
            <strong>{{ review.title }}</strong>
            <a-tag :color="riskColor(review.risk_level)">{{ riskLabel(review.risk_level) }}</a-tag>
          </div>
          <p>{{ review.wiki_path || '未记录目录路径' }}</p>
          <div class="queue-meta">
            <a-tag v-for="tag in review.problem_tags" :key="tag" :color="problemColor(tag)">
              {{ problemLabel(tag) }}
            </a-tag>
            <span>{{ review.comparison_count }} 个比较</span>
          </div>
        </button>
      </a-spin>
    </aside>

    <article v-if="selectedReview" class="review-detail">
      <div class="process-rail" aria-label="知识加工流程">
        <div class="process-step done"><span>✓</span><div><strong>扫描</strong><small>已发现</small></div></div>
        <div class="process-step done"><span>✓</span><div><strong>解析</strong><small>已完成</small></div></div>
        <div class="process-step done"><span>✓</span><div><strong>跨文档比较</strong><small>{{ selectedReview.comparison_count }} 个比较</small></div></div>
        <div class="process-step current"><span>4</span><div><strong>人工审核</strong><small>处理中</small></div></div>
        <div class="process-step"><span>5</span><div><strong>发布</strong><small>尚未开始</small></div></div>
      </div>

      <div class="detail-grid">
        <section class="evidence-panel">
          <div class="record-heading">
            <div>
              <h2>{{ selectedReview.title }}</h2>
              <p>{{ selectedReview.wiki_path || '未记录目录路径' }} · {{ formatTime(selectedReview.source_updated_at) }}</p>
            </div>
            <a-tag :color="selectedReview.risk_level === 'HIGH' ? 'error' : 'warning'">
              {{ relationLabel(selectedReview.relation_types?.[0]) }}
            </a-tag>
          </div>
          <div class="scope-line">
            <a-tag v-for="entry in selectedScopeEntries" :key="entry.key">
              {{ scopeLabel(entry.key) }}：{{ entry.value }}
            </a-tag>
            <a-tag v-if="!selectedScopeEntries.length">适用范围待确认</a-tag>
          </div>

          <div class="comparison-heading">
            <strong>跨文档证据对照</strong>
            <span>AI 比较结果仅作建议，最终由人工裁决</span>
          </div>
          <a-spin :spinning="loadingComparisons">
            <div v-if="!comparisons.length" class="comparison-empty">当前没有可展示的跨文档证据。</div>
            <div v-for="comparison in comparisons" :key="comparison.relation_id" class="comparison-card">
              <div class="comparison-card-heading">
                <a-tag :color="relationColor(comparison.relation_type)">{{ relationLabel(comparison.relation_type) }}</a-tag>
                <span>置信度 {{ percentage(comparison.confidence) }}</span>
              </div>
              <div class="source-columns">
                <div><label>来源一</label><strong>{{ comparison.source_title }}</strong><p>{{ comparison.source_path || '-' }}</p></div>
                <div><label>来源二</label><strong>{{ comparison.target_title }}</strong><p>{{ comparison.target_path || '-' }}</p></div>
              </div>
              <dl class="comparison-facts">
                <div v-if="comparison.same_content?.length"><dt>相同内容</dt><dd>{{ formatFacts(comparison.same_content) }}</dd></div>
                <div v-if="comparison.different_content?.length"><dt>差异内容</dt><dd>{{ formatFacts(comparison.different_content) }}</dd></div>
                <div v-if="comparison.reasoning"><dt>判断理由</dt><dd>{{ comparison.reasoning }}</dd></div>
              </dl>
            </div>
          </a-spin>
        </section>

        <aside class="decision-panel">
          <h3>审核决定</h3>
          <p>审核记录会保留来源、证据和历史处理人。</p>
          <div class="decision-grid">
            <button v-for="item in decisions" :key="item.value" type="button" class="decision-button" :class="{ active: form.decision === item.value }" @click="form.decision = item.value">
              {{ item.label }}
            </button>
          </div>
          <div class="field">
            <label>问题标签</label>
            <div class="problem-tags">
              <button v-for="tag in allProblemTags" :key="tag.value" type="button" class="problem-tag" :class="{ active: form.problem_tags.includes(tag.value) }" @click="toggleProblemTag(tag.value)">
                {{ tag.label }}
              </button>
            </div>
          </div>
          <div v-if="form.decision === 'TRANSFER'" class="field">
            <label>转交给</label>
            <a-select v-model:value="form.assignee_id" class="field-control" placeholder="选择知识管理员" :options="reviewerOptions" />
          </div>
          <div class="field">
            <label>处理方式</label>
            <a-select v-model:value="form.action" class="field-control" :options="actionOptions" />
          </div>
          <div class="field scope-fields">
            <label>适用范围</label>
            <div class="scope-grid">
              <a-input v-model:value="form.applicability_scope.industry" placeholder="行业" />
              <a-input v-model:value="form.applicability_scope.product" placeholder="产品" />
              <a-input v-model:value="form.applicability_scope.product_version" placeholder="版本" />
              <a-input v-model:value="form.applicability_scope.deployment_mode" placeholder="部署模式" />
            </div>
          </div>
          <div class="field">
            <label>审核意见</label>
            <a-textarea v-model:value="form.decision_comment" :rows="3" placeholder="请记录裁决依据或需要补充的内容" />
          </div>
          <div class="decision-footer">
            <a-button size="small" @click="saveDraft">保存草稿</a-button>
            <a-button type="primary" size="small" :loading="resolving" @click="resolve">确认{{ decisionLabel }}</a-button>
          </div>
        </aside>
      </div>
    </article>
    <div v-else class="detail-empty"><a-empty description="从左侧选择一条审核任务" /></div>
  </section>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { governanceApi } from '@/apis/governance_api'

const props = defineProps({
  sourceId: { type: String, default: '' },
  targetReviewId: { type: String, default: '' }
})
const emit = defineEmits(['count-change', 'target-consumed'])

const reviews = ref([])
const reviewers = ref([])
const comparisons = ref([])
const selectedReviewId = ref('')
const loading = ref(false)
const loadingComparisons = ref(false)
const resolving = ref(false)
const statusFilter = ref('')
const problemFilter = ref('')

const form = reactive({
  decision: 'REQUEST_CHANGES',
  action: 'MARK_INSUFFICIENT',
  problem_tags: [],
  decision_comment: '',
  assignee_id: undefined,
  applicability_scope: { industry: '', product: '', product_version: '', deployment_mode: '' }
})

const selectedReview = computed(() => reviews.value.find((item) => item.review_id === selectedReviewId.value))
const selectedScopeEntries = computed(() =>
  Object.entries(selectedReview.value?.applicability_scope || {})
    .filter(([, value]) => value)
    .map(([key, value]) => ({ key, value }))
)
const decisionLabel = computed(() => decisions.find((item) => item.value === form.decision)?.label || '处理')
const reviewerOptions = computed(() => reviewers.value.map((item) => ({ label: `${item.name} · ${item.role}`, value: item.user_id })))

const decisions = [
  { value: 'PUBLISH', label: '发布' },
  { value: 'REQUEST_CHANGES', label: '需要修改' },
  { value: 'REJECT', label: '驳回' },
  { value: 'TRANSFER', label: '转交' }
]
const actionOptions = [
  { label: '创建新知识', value: 'CREATE' },
  { label: '更新当前知识', value: 'UPDATE' },
  { label: '保留当前版本', value: 'KEEP_CURRENT' },
  { label: '按适用范围拆分', value: 'SPLIT_BY_SCOPE' },
  { label: '标记为重复来源', value: 'MARK_DUPLICATE' },
  { label: '标记证据不足', value: 'MARK_INSUFFICIENT' },
  { label: '归档候选版本', value: 'ARCHIVE' }
]
const allProblemTags = [
  { value: 'CONFLICT', label: '内容冲突' },
  { value: 'DUPLICATE', label: '完全重复' },
  { value: 'OVERLAP', label: '内容重叠' },
  { value: 'MISSING_SCOPE', label: '缺少范围' },
  { value: 'INSUFFICIENT_EVIDENCE', label: '证据不足' },
  { value: 'OUTDATED', label: '内容过期' }
]
const statusOptions = [
  { label: '未完成任务', value: '' },
  { label: '待审核', value: 'pending' },
  { label: '待补充', value: 'changes_requested' }
]
const problemOptions = [{ label: '全部问题', value: '' }, ...allProblemTags]

watch(
  () => [props.sourceId, statusFilter.value, problemFilter.value],
  () => loadReviews(),
  { immediate: true }
)

watch(selectedReviewId, () => loadComparisons())

watch(
  () => props.targetReviewId,
  (reviewId) => selectRequestedReview(reviewId)
)

async function loadReviews() {
  if (!props.sourceId) return
  loading.value = true
  try {
    const response = await governanceApi.listReviews(props.sourceId, {
      status: statusFilter.value || undefined,
      problem_tag: problemFilter.value || undefined
    })
    reviews.value = response.items || []
    emit('count-change', reviews.value.length)
    if (!selectRequestedReview(props.targetReviewId)) {
      const selected = reviews.value.find((item) => item.review_id === selectedReviewId.value)
      selectReview(selected?.review_id || reviews.value[0]?.review_id || '')
    }
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载审核任务失败'))
  } finally {
    loading.value = false
  }
}

function selectRequestedReview(reviewId) {
  if (!reviewId || !reviews.value.length) return false
  const requested = reviews.value.find(
    (item) => item.review_id === reviewId || item.version_id === reviewId
  )
  if (!requested) return false
  selectReview(requested.review_id)
  emit('target-consumed')
  return true
}

async function loadComparisons() {
  if (!selectedReviewId.value) return
  loadingComparisons.value = true
  try {
    const response = await governanceApi.listReviewComparisons(selectedReviewId.value)
    comparisons.value = response.items || []
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载跨文档证据失败'))
  } finally {
    loadingComparisons.value = false
  }
}

async function loadReviewers() {
  try {
    const response = await governanceApi.listReviewers()
    reviewers.value = response.items || []
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载审核人失败'))
  }
}

function selectReview(reviewId) {
  selectedReviewId.value = reviewId
  const review = reviews.value.find((item) => item.review_id === reviewId)
  if (!review) {
    comparisons.value = []
    return
  }
  form.problem_tags = [...(review.problem_tags || [])]
  form.decision_comment = review.decision_comment || ''
  form.assignee_id = review.assignee_id || undefined
  form.applicability_scope = {
    industry: review.applicability_scope?.industry || '',
    product: review.applicability_scope?.product || '',
    product_version: review.applicability_scope?.product_version || '',
    deployment_mode: review.applicability_scope?.deployment_mode || ''
  }
  form.decision = 'REQUEST_CHANGES'
  form.action = review.relation_types?.includes('CONFLICT') ? 'UPDATE' : 'MARK_INSUFFICIENT'
}

function toggleProblemTag(tag) {
  form.problem_tags = form.problem_tags.includes(tag) ? form.problem_tags.filter((item) => item !== tag) : [...form.problem_tags, tag]
}

function payload() {
  return {
    decision: form.decision,
    action: form.action,
    problem_tags: form.problem_tags,
    decision_comment: form.decision_comment || undefined,
    assignee_id: form.assignee_id || undefined,
    applicability_scope: Object.fromEntries(Object.entries(form.applicability_scope).filter(([, value]) => value))
  }
}

async function resolve() {
  if (!selectedReviewId.value) return
  if (form.decision === 'TRANSFER' && !form.assignee_id) {
    message.warning('请选择转交对象')
    return
  }
  if (['REQUEST_CHANGES', 'REJECT'].includes(form.decision) && !form.decision_comment.trim()) {
    message.warning('请填写审核意见')
    return
  }
  resolving.value = true
  try {
    await governanceApi.resolveReview(selectedReviewId.value, payload())
    message.success(`已记录“${decisionLabel.value}”`)
    await loadReviews()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '提交审核决定失败'))
  } finally {
    resolving.value = false
  }
}

function saveDraft() {
  message.info('审核草稿仅保存在当前页面，确认后才会写入系统')
}

function formatTime(value) {
  if (!value) return '暂无时间'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}
function percentage(value) { return value == null ? '-' : `${Math.round(value * 100)}%` }
function formatFacts(values) { return values.map((value) => (typeof value === 'string' ? value : Object.entries(value).map(([key, item]) => `${key}: ${item}`).join('，'))).join('；') }
function scopeLabel(key) { return { industry: '行业', product: '产品', product_version: '版本', deployment_mode: '部署模式' }[key] || key }
function relationLabel(value) { return { CONFLICT: '内容冲突', EXACT_DUPLICATE: '完全重复', OVERLAP: '内容重叠', CONDITIONAL_VARIANT: '条件变体', COMPLEMENTARY: '互补内容', INSUFFICIENT: '证据不足' }[value] || '待比较' }
function relationColor(value) { return { CONFLICT: 'error', EXACT_DUPLICATE: 'success', OVERLAP: 'warning', CONDITIONAL_VARIANT: 'processing', INSUFFICIENT: 'warning' }[value] || 'default' }
function riskLabel(value) { return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[value] || '待评估' }
function riskColor(value) { return { HIGH: 'error', MEDIUM: 'warning', LOW: 'default' }[value] || 'default' }
function problemLabel(value) { return allProblemTags.find((tag) => tag.value === value)?.label || value }
function problemColor(value) { return value === 'CONFLICT' ? 'error' : value === 'INSUFFICIENT_EVIDENCE' ? 'warning' : 'processing' }

loadReviewers()
</script>

<style scoped lang="less">
.review-workspace { display: grid; grid-template-columns: 330px minmax(0, 1fr); min-height: 570px; margin: 10px var(--page-padding) 0; overflow: hidden; border: 1px solid color-mix(in srgb, var(--gray-150) 55%, transparent); border-radius: 8px; background: var(--gray-0); }
.review-queue { min-width: 0; border-right: 1px solid color-mix(in srgb, var(--gray-150) 50%, transparent); background: color-mix(in srgb, var(--gray-25) 70%, var(--gray-0)); }
.queue-heading { padding: 14px; border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent); }
.queue-heading-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.queue-heading h2 { margin: 0; font-size: 16px; font-weight: 600; }
.queue-heading-row span { color: var(--color-text-tertiary); font-size: 12px; }
.queue-filters { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin-top: 10px; }
.queue-empty { padding: 55px 10px; }
.queue-item { display: block; width: 100%; padding: 13px 14px; border: 0; border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 30%, transparent); background: transparent; text-align: left; cursor: pointer; }
.queue-item:hover { background: var(--main-10); }
.queue-item.active { background: var(--main-30); box-shadow: inset 3px 0 0 var(--main-color); }
.queue-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.queue-title strong { font-size: 13px; line-height: 1.5; }
.queue-item p { margin: 6px 0 8px; color: var(--color-text-tertiary); font-size: 12px; line-height: 1.5; }
.queue-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; color: var(--color-text-tertiary); font-size: 11px; }
.review-detail { min-width: 0; }
.process-rail { display: grid; grid-template-columns: repeat(5, 1fr); padding: 12px 18px; border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent); background: color-mix(in srgb, var(--gray-25) 55%, var(--gray-0)); }
.process-step { position: relative; display: flex; align-items: center; min-width: 0; gap: 7px; }
.process-step:not(:last-child)::after { position: absolute; top: 12px; left: 28px; right: 8px; height: 1px; background: var(--gray-150); content: ''; }
.process-step > span { position: relative; z-index: 1; display: grid; width: 24px; height: 24px; flex: 0 0 24px; place-items: center; border: 1px solid var(--gray-200); border-radius: 50%; background: var(--gray-0); color: var(--color-text-tertiary); font-size: 11px; }
.process-step.done > span { border-color: var(--color-success-300); background: var(--color-success-50); color: var(--color-success-700); }
.process-step.current > span { border-color: var(--main-color); background: var(--main-color); color: var(--gray-0); }
.process-step div { position: relative; z-index: 2; min-width: 0; padding-right: 4px; background: color-mix(in srgb, var(--gray-25) 55%, var(--gray-0)); }
.process-step strong, .process-step small { display: block; white-space: nowrap; }
.process-step strong { font-size: 11px; }
.process-step small { margin-top: 1px; color: var(--color-text-tertiary); font-size: 10px; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1.55fr) 310px; min-height: 508px; }
.evidence-panel { min-width: 0; padding: 17px 18px 20px; border-right: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent); }
.record-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.record-heading h2 { margin: 0; font-size: 18px; font-weight: 600; line-height: 1.45; }
.record-heading p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 12px; }
.scope-line { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px; }
.comparison-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 15px; padding: 9px 11px; background: var(--gray-25); }
.comparison-heading strong { font-size: 12px; }
.comparison-heading span { color: var(--color-text-tertiary); font-size: 11px; }
.comparison-empty { padding: 35px 10px; color: var(--color-text-tertiary); text-align: center; }
.comparison-card { margin-top: 8px; padding: 11px; border: 1px solid color-mix(in srgb, var(--gray-150) 65%, transparent); border-radius: 7px; }
.comparison-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.comparison-card-heading > span { color: var(--color-text-tertiary); font-size: 11px; }
.source-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.source-columns > div + div { padding-left: 10px; border-left: 1px solid var(--gray-150); }
.source-columns label { display: block; color: var(--color-text-tertiary); font-size: 10px; }
.source-columns strong { display: block; margin-top: 4px; font-size: 13px; }
.source-columns p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 11px; }
.comparison-facts { display: grid; gap: 5px; margin: 11px 0 0; }
.comparison-facts div { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: 8px; padding-top: 6px; border-top: 1px solid var(--gray-100); }
.comparison-facts dt { color: var(--color-text-tertiary); font-size: 11px; }
.comparison-facts dd { margin: 0; color: var(--color-text-secondary); font-size: 11px; line-height: 1.55; }
.decision-panel { padding: 17px 15px; background: color-mix(in srgb, var(--gray-25) 40%, var(--gray-0)); }
.decision-panel h3 { margin: 0; font-size: 15px; font-weight: 600; }
.decision-panel > p { margin: 4px 0 12px; color: var(--color-text-tertiary); font-size: 11px; line-height: 1.5; }
.decision-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.decision-button { min-height: 36px; border: 1px solid var(--gray-200); border-radius: 6px; background: var(--gray-0); color: var(--color-text-secondary); cursor: pointer; }
.decision-button:hover { border-color: var(--main-300); color: var(--main-700); }
.decision-button.active { border-color: var(--main-300); background: var(--main-30); color: var(--main-700); font-weight: 600; }
.field { margin-top: 13px; }
.field > label { display: block; margin-bottom: 6px; color: var(--color-text-secondary); font-size: 11px; font-weight: 550; }
.problem-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.problem-tag { min-height: 27px; padding: 3px 8px; border: 1px solid var(--gray-200); border-radius: 14px; background: var(--gray-0); color: var(--color-text-tertiary); font-size: 10px; cursor: pointer; }
.problem-tag.active { border-color: var(--color-error-300); background: var(--color-error-50); color: var(--color-error-700); }
.field-control { width: 100%; }
.scope-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.decision-footer { display: flex; justify-content: flex-end; gap: 7px; margin-top: 14px; }
.detail-empty { display: grid; min-height: 570px; place-items: center; }

@media (max-width: 1100px) {
  .review-workspace { grid-template-columns: 280px minmax(0, 1fr); }
  .detail-grid { grid-template-columns: minmax(0, 1fr) 275px; }
}
</style>
