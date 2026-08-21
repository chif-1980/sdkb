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
            <span>{{ comparisonStatusLabel(review.comparison_status) }}</span>
          </div>
        </button>
      </a-spin>
    </aside>

    <article v-if="selectedReview" class="review-detail">
      <div class="process-rail" aria-label="知识加工流程">
        <div class="process-step done"><span>✓</span><div><strong>扫描</strong><small>已发现</small></div></div>
        <div class="process-step done"><span>✓</span><div><strong>解析</strong><small>已完成</small></div></div>
        <div class="process-step" :class="comparisonStepClass(selectedReview.comparison_status)">
          <span>{{ selectedReview.comparison_status === 'completed' ? '✓' : '3' }}</span>
          <div><strong>跨文档比较</strong><small>{{ comparisonStatusLabel(selectedReview.comparison_status) }} · {{ selectedReview.comparison_count }} 个关系</small></div>
        </div>
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
            <div class="record-actions">
              <a-tag :color="selectedReview.risk_level === 'HIGH' ? 'error' : 'warning'">{{ relationLabel(selectedReview.relation_types?.[0]) }}</a-tag>
              <a-tag v-if="selectedReview.content_missing" color="error">正文缺失</a-tag>
              <a-tag v-else-if="selectedReview.content_check_pending" color="warning">正文待确认</a-tag>
              <a v-if="selectedReview.source_url" class="source-link" :href="selectedReview.source_url" target="_blank" rel="noopener noreferrer">
                查看飞书原文 <ExternalLink :size="14" />
              </a>
            </div>
          </div>
          <div class="record-meta" aria-label="待审核资料信息">
            <span>{{ itemTypeLabel(selectedReview.item_type) }}</span>
            <span>版本 {{ selectedReview.revision || '-' }}</span>
            <span>{{ reviewChunkCount }} 个片段</span>
            <span v-if="selectedReview.token_count">{{ formatNumber(selectedReview.token_count) }} Tokens</span>
          </div>
          <div v-if="selectedReview.content_missing" class="content-quality-alert is-missing">
            <FileText :size="16" />
            <div><strong>正文缺失，默认不可发布</strong><span>当前解析结果只有标题，没有可审核正文。请补充飞书原文后重新扫描。</span></div>
          </div>
          <div v-else-if="selectedReview.content_check_pending" class="content-quality-alert is-pending">
            <FileText :size="16" />
            <div><strong>正文检查尚未完成，暂不可发布</strong><span>系统还没有确认这份资料存在可审核正文，请先重新解析或检查解析结果。</span></div>
          </div>
          <div class="scope-line">
            <a-tag v-for="entry in selectedScopeEntries" :key="entry.key">
              {{ scopeLabel(entry.key) }}：{{ entry.value }}
            </a-tag>
            <a-tag v-if="!selectedScopeEntries.length">适用范围待确认</a-tag>
          </div>

          <div class="evidence-tabs" role="tablist" aria-label="审核资料视图">
            <button type="button" role="tab" :aria-selected="activeEvidenceView === 'content'" :class="{ active: activeEvidenceView === 'content' }" @click="activeEvidenceView = 'content'">
              <FileText :size="14" /> 待审核内容
            </button>
            <button type="button" role="tab" :aria-selected="activeEvidenceView === 'comparisons'" :class="{ active: activeEvidenceView === 'comparisons' }" @click="activeEvidenceView = 'comparisons'">
              <GitCompareArrows :size="14" /> 跨文档证据 <span class="tab-count">{{ comparisons.length }}</span>
            </button>
          </div>

          <div class="evidence-body">
            <section v-if="activeEvidenceView === 'content'" class="content-review" aria-label="待审核内容">
              <a-spin :spinning="reviewContent.loading">
                <div v-if="reviewContent.error" class="content-notice is-error">
                  <FileText :size="24" />
                  <strong>解析内容加载失败</strong>
                  <p>{{ reviewContent.error }}</p>
                  <a-button size="small" @click="loadReviewContent"><RefreshCw :size="13" />重新加载</a-button>
                </div>
                <MarkdownPreview v-else-if="reviewMarkdown" compact class="review-markdown" :content="reviewMarkdown" />
                <div v-else-if="!reviewContent.loading" class="content-notice">
                  <FileText :size="24" />
                  <strong>尚未生成可预览正文</strong>
                  <p>当前任务只有来源和版本信息，请查看飞书原文或等待资料重新解析。</p>
                  <a v-if="selectedReview.source_url" :href="selectedReview.source_url" target="_blank" rel="noopener noreferrer">查看飞书原文 <ExternalLink :size="13" /></a>
                </div>
              </a-spin>
            </section>

            <section v-else class="comparison-review" aria-label="跨文档证据">
              <div class="comparison-heading">
                <strong>跨文档证据对照</strong>
                <span>AI 比较结果仅作建议，最终由人工裁决</span>
              </div>
              <a-spin :spinning="loadingComparisons">
                <div v-if="!comparisons.length && !loadingComparisons" class="content-notice">
                  <GitCompareArrows :size="24" />
                  <strong>{{ comparisonEmptyTitle(selectedReview.comparison_status) }}</strong>
                  <p>{{ comparisonEmptyDescription(selectedReview.comparison_status) }}</p>
                </div>
                <div v-for="comparison in comparisons" :key="comparison.relation_id" class="comparison-card" :class="comparisonClass(comparison.relation_type)">
                  <div class="comparison-card-heading">
                    <a-tag :color="relationColor(comparison.relation_type)">{{ relationLabel(comparison.relation_type) }}</a-tag>
                    <span>置信度 {{ percentage(comparison.confidence) }}</span>
                  </div>
                  <div class="source-columns">
                    <div><label>来源一</label><strong>{{ comparison.source_title }}</strong><p>{{ comparison.source_path || '-' }}</p></div>
                    <div><label>来源二</label><strong>{{ comparison.target_title }}</strong><p>{{ comparison.target_path || '-' }}</p></div>
                  </div>
                  <div v-if="comparison.same_content?.length" class="evidence-block evidence-block-same">
                    <div class="evidence-block-heading"><span class="evidence-mark">相同</span><strong>共同内容</strong><span>{{ comparison.same_content.length }} 项</span></div>
                    <ul><li v-for="(fact, index) in comparison.same_content" :key="`same-${index}`">{{ formatFactValue(fact) }}</li></ul>
                  </div>
                  <div v-if="comparison.different_content?.length" class="evidence-block evidence-block-diff">
                    <div class="evidence-block-heading"><span class="evidence-mark">差异</span><strong>需要重点核对</strong><span>{{ comparison.different_content.length }} 项</span></div>
                    <div v-for="(fact, index) in comparison.different_content" :key="`diff-${index}`" class="difference-row">
                      <span class="difference-field">{{ fact.field || '内容差异' }}</span>
                      <span class="difference-value"><b>来源一</b>{{ fact.current ?? formatFactValue(fact) }}</span>
                      <span class="difference-value"><b>来源二</b>{{ fact.candidate ?? '-' }}</span>
                    </div>
                  </div>
                  <div v-if="comparison.reasoning" class="comparison-reasoning"><span>判断理由</span>{{ comparison.reasoning }}</div>
                </div>
              </a-spin>
            </section>
          </div>
        </section>

        <aside class="decision-panel">
          <h3>审核决定</h3>
          <p>审核记录会保留来源、证据和历史处理人。</p>
          <div class="decision-grid">
            <button v-for="item in decisions" :key="item.value" type="button" class="decision-button" :class="{ active: form.decision === item.value }" :disabled="item.value === 'PUBLISH' && publishBlocked" @click="form.decision = item.value">
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
            <a-button type="primary" size="small" :loading="resolving" :disabled="publishBlocked && form.decision === 'PUBLISH'" @click="resolve">确认{{ decisionLabel }}</a-button>
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
import { ExternalLink, FileText, GitCompareArrows, RefreshCw } from 'lucide-vue-next'

import { governanceApi } from '@/apis/governance_api'
import { documentApi } from '@/apis/knowledge_api'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { mergeChunks } from '@/utils/chunkUtils'

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
const activeEvidenceView = ref('content')
const reviewContent = ref(emptyReviewContent())
let contentRequestSeq = 0

const form = reactive({
  decision: 'REQUEST_CHANGES',
  action: 'MARK_INSUFFICIENT',
  problem_tags: [],
  decision_comment: '',
  assignee_id: undefined,
  applicability_scope: { industry: '', product: '', product_version: '', deployment_mode: '' }
})

const selectedReview = computed(() => reviews.value.find((item) => item.review_id === selectedReviewId.value))
const mergedReviewContent = computed(() => mergeChunks(reviewContent.value.lines || []))
const reviewMarkdown = computed(() => reviewContent.value.content || mergedReviewContent.value.content || '')
const reviewChunkCount = computed(() => reviewContent.value.lines?.length || selectedReview.value?.chunk_count || 0)
const selectedScopeEntries = computed(() =>
  Object.entries(selectedReview.value?.applicability_scope || {})
    .filter(([, value]) => value)
    .map(([key, value]) => ({ key, value }))
)
const decisionLabel = computed(() => decisions.find((item) => item.value === form.decision)?.label || '处理')
const reviewerOptions = computed(() => reviewers.value.map((item) => ({ label: `${item.name} · ${item.role}`, value: item.user_id })))
const publishBlocked = computed(() => Boolean(selectedReview.value?.content_missing || selectedReview.value?.content_check_pending))

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
  { value: 'OUTDATED', label: '内容过期' },
  { value: 'CONTENT_MISSING', label: '正文缺失' }
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

watch(selectedReviewId, () => {
  activeEvidenceView.value = 'content'
  loadComparisons()
  loadReviewContent()
})

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

function emptyReviewContent() {
  return { content: '', lines: [], loading: false, error: '' }
}

async function loadReviewContent() {
  const review = selectedReview.value
  const requestId = ++contentRequestSeq
  reviewContent.value = emptyReviewContent()
  if (!review?.target_kb_id || !review?.yuxi_file_id) return

  reviewContent.value = { ...emptyReviewContent(), loading: true }
  try {
    const response = await documentApi.getDocumentContent(review.target_kb_id, review.yuxi_file_id)
    if (requestId !== contentRequestSeq) return
    if (response?.status === 'failed') throw new Error(response.message || '解析内容读取失败')
    reviewContent.value = { content: response?.content || '', lines: response?.lines || [], loading: false, error: '' }
  } catch (error) {
    if (requestId !== contentRequestSeq) return
    const detail = error?.message || ''
    reviewContent.value = { ...emptyReviewContent(), error: /[\u3400-\u9fff]/.test(detail) ? detail : '暂时无法读取解析正文，请稍后重试。' }
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
  if (form.decision === 'PUBLISH' && publishBlocked.value) {
    message.warning(selectedReview.value?.content_missing ? '正文缺失，不能发布' : '正文检查尚未完成，不能发布')
    return
  }
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
function formatNumber(value) { return new Intl.NumberFormat('zh-CN').format(value || 0) }
function itemTypeLabel(value) { return { docx: '飞书文档', pdf: 'PDF', pptx: '演示文稿', xlsx: '电子表格', image: '图片 OCR', video: '音视频' }[value] || String(value || '资料').toUpperCase() }
function percentage(value) { return value == null ? '-' : `${Math.round(value * 100)}%` }
function formatFactValue(value) { return typeof value === 'string' ? value : Object.entries(value || {}).map(([key, item]) => `${key}: ${item}`).join('，') }
function scopeLabel(key) { return { industry: '行业', product: '产品', product_version: '版本', deployment_mode: '部署模式' }[key] || key }
function relationLabel(value) { return { CONFLICT: '内容冲突', EXACT_DUPLICATE: '完全重复', OVERLAP: '内容重叠', CONDITIONAL_VARIANT: '条件变体', COMPLEMENTARY: '互补内容', INSUFFICIENT: '证据不足' }[value] || '待比较' }
function relationColor(value) { return { CONFLICT: 'error', EXACT_DUPLICATE: 'success', OVERLAP: 'warning', CONDITIONAL_VARIANT: 'processing', INSUFFICIENT: 'warning' }[value] || 'default' }
function comparisonClass(value) { return `comparison-${String(value || '').toLowerCase()}` }
function comparisonStatusLabel(value) { return { not_started: '尚未检查', queued: '等待检查', running: '检查中', completed: '已完成检查', failed: '检查失败' }[value] || '待检查' }
function comparisonStepClass(value) { return value === 'completed' ? 'done' : value === 'running' ? 'current' : '' }
function comparisonEmptyTitle(value) { return { not_started: '尚未执行跨文档检查', queued: '跨文档检查排队中', running: '跨文档检查进行中', failed: '跨文档检查失败' }[value] || '未发现跨文档问题' }
function comparisonEmptyDescription(value) {
  return {
    not_started: '检查尚未开始，完成后会显示重复、重叠或冲突证据。',
    queued: '任务已进入后台队列，请稍后刷新查看结果。',
    running: '系统正在比较高相关资料，结果会自动回填到这里。',
    failed: '本次检查未完成，可在“跨文档问题”页签点击补跑检查。'
  }[value] || '当前资料没有重复、重叠或冲突证据，仍需根据正文确认内容是否可以发布。'
}
function riskLabel(value) { return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[value] || '待评估' }
function riskColor(value) { return { HIGH: 'error', MEDIUM: 'warning', LOW: 'default' }[value] || 'default' }
function problemLabel(value) { return allProblemTags.find((tag) => tag.value === value)?.label || value }
function problemColor(value) {
  return ['CONFLICT', 'CONTENT_MISSING'].includes(value) ? 'error' : value === 'INSUFFICIENT_EVIDENCE' ? 'warning' : 'processing'
}

loadReviewers()
</script>

<style scoped lang="less">
.review-workspace { display: grid; grid-template-columns: 330px minmax(0, 1fr); height: clamp(620px, calc(100vh - 318px), 760px); min-height: 570px; margin: 10px var(--page-padding) 0; overflow: hidden; border: 1px solid color-mix(in srgb, var(--gray-150) 55%, transparent); border-radius: 8px; background: var(--gray-0); }
.review-queue { min-width: 0; overflow-y: auto; border-right: 1px solid color-mix(in srgb, var(--gray-150) 50%, transparent); background: color-mix(in srgb, var(--gray-25) 70%, var(--gray-0)); }
.queue-heading { position: sticky; z-index: 2; top: 0; padding: 14px; border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent); background: color-mix(in srgb, var(--gray-25) 92%, var(--gray-0)); }
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
.review-detail { display: flex; min-width: 0; min-height: 0; flex-direction: column; }
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
.detail-grid { display: grid; min-height: 0; flex: 1; grid-template-columns: minmax(0, 1.55fr) 310px; }
.evidence-panel { display: flex; min-width: 0; min-height: 0; flex-direction: column; padding: 17px 18px 0; overflow: hidden; border-right: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent); }
.record-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.record-heading h2 { margin: 0; font-size: 18px; font-weight: 600; line-height: 1.45; }
.record-heading p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 12px; }
.record-actions { display: flex; flex-shrink: 0; align-items: center; gap: 8px; }
.source-link { display: inline-flex; align-items: center; gap: 4px; color: var(--main-color); font-size: 12px; white-space: nowrap; }
.record-meta { display: flex; flex-wrap: wrap; gap: 0; margin-top: 9px; color: var(--color-text-tertiary); font-size: 11px; }
.record-meta span { display: inline-flex; align-items: center; }
.record-meta span + span::before { width: 3px; height: 3px; margin: 0 7px; border-radius: 50%; background: var(--gray-300); content: ''; }
.content-quality-alert { display: flex; align-items: flex-start; gap: 8px; margin-top: 11px; padding: 9px 10px; border-radius: 5px; font-size: 11px; line-height: 1.5; }
.content-quality-alert > svg { flex: 0 0 auto; margin-top: 1px; }
.content-quality-alert div { display: grid; gap: 2px; }
.content-quality-alert strong { font-size: 12px; }
.content-quality-alert span { color: var(--color-text-secondary); }
.content-quality-alert.is-missing { border: 1px solid #efc0c2; background: #fff0f0; color: #a63840; }
.content-quality-alert.is-pending { border: 1px solid #f0df9e; background: #fff9df; color: #8b6712; }
.scope-line { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px; }
.evidence-tabs { display: flex; flex-shrink: 0; gap: 3px; margin-top: 14px; border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 55%, transparent); }
.evidence-tabs button { position: relative; display: inline-flex; min-height: 36px; align-items: center; gap: 6px; padding: 0 10px; border: 0; background: transparent; color: var(--color-text-tertiary); cursor: pointer; font: inherit; font-size: 12px; }
.evidence-tabs button::after { position: absolute; right: 8px; bottom: -1px; left: 8px; height: 2px; border-radius: 2px; background: transparent; content: ''; }
.evidence-tabs button:hover, .evidence-tabs button.active { color: var(--main-700); }
.evidence-tabs button.active { font-weight: 600; }
.evidence-tabs button.active::after { background: var(--main-color); }
.tab-count { display: inline-grid; min-width: 18px; height: 18px; place-items: center; padding: 0 5px; border-radius: 9px; background: var(--gray-100); color: var(--color-text-tertiary); font-size: 10px; }
.evidence-tabs button.active .tab-count { background: var(--main-30); color: var(--main-700); }
.evidence-body { min-height: 0; flex: 1; overflow-y: auto; scrollbar-color: color-mix(in srgb, var(--main-color) 28%, var(--gray-150)) transparent; scrollbar-width: thin; }
.content-review { min-height: 100%; padding: 15px 2px 24px; }
.review-markdown { color: var(--color-text-secondary); font-size: 13px; line-height: 1.7; }
.content-notice { display: grid; min-height: 240px; place-items: center; align-content: center; gap: 8px; padding: 28px; color: var(--color-text-tertiary); text-align: center; }
.content-notice svg { color: var(--gray-400); }
.content-notice strong { color: var(--color-text-secondary); font-size: 13px; font-weight: 600; }
.content-notice p { max-width: 410px; margin: 0; font-size: 12px; line-height: 1.65; }
.content-notice a, .content-notice :deep(.ant-btn) { display: inline-flex; align-items: center; gap: 5px; margin-top: 3px; }
.content-notice.is-error svg, .content-notice.is-error strong { color: var(--color-error-700); }
.comparison-review { min-height: 100%; padding-bottom: 22px; }
.comparison-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 2px 9px; border-bottom: 1px solid var(--gray-100); }
.comparison-heading strong { font-size: 12px; }
.comparison-heading span { color: var(--color-text-tertiary); font-size: 11px; }
.comparison-card { margin-top: 8px; padding: 11px; border: 1px solid color-mix(in srgb, var(--gray-150) 65%, transparent); border-radius: 7px; }
.comparison-card.comparison-conflict { border-color: #e5a4a7; background: #fffafa; box-shadow: inset 3px 0 0 #d45a5f; }
.comparison-card.comparison-overlap { border-color: #e6d49a; background: #fffef8; box-shadow: inset 3px 0 0 #d6aa36; }
.comparison-card.comparison-exact_duplicate { border-color: #b7d5be; background: #fbfefb; box-shadow: inset 3px 0 0 #68a57b; }
.comparison-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.comparison-card-heading > span { color: var(--color-text-tertiary); font-size: 11px; }
.source-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.source-columns > div + div { padding-left: 10px; border-left: 1px solid var(--gray-150); }
.source-columns label { display: block; color: var(--color-text-tertiary); font-size: 10px; }
.source-columns strong { display: block; margin-top: 4px; font-size: 13px; }
.source-columns p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 11px; }
.evidence-block { margin-top: 10px; padding: 8px 9px; border-radius: 5px; }
.evidence-block-same { background: #fff9df; border: 1px solid #f0df9e; }
.evidence-block-diff { background: #fff0f0; border: 1px solid #efc0c2; }
.evidence-block-heading { display: flex; align-items: center; gap: 6px; color: var(--color-text-secondary); font-size: 11px; }
.evidence-block-heading > span:last-child { margin-left: auto; color: var(--color-text-tertiary); }
.evidence-mark { display: inline-flex; align-items: center; min-height: 18px; padding: 0 5px; border-radius: 3px; font-size: 10px; font-weight: 650; }
.evidence-block-same .evidence-mark { color: #8b6712; background: #f5dfa0; }
.evidence-block-diff .evidence-mark { color: #a63840; background: #f3c6c8; }
.evidence-block ul { display: grid; gap: 3px; margin: 6px 0 0; padding-left: 17px; color: var(--color-text-secondary); font-size: 11px; line-height: 1.55; }
.difference-row { display: grid; grid-template-columns: 74px minmax(0, 1fr) minmax(0, 1fr); gap: 7px; margin-top: 6px; padding-top: 6px; border-top: 1px solid color-mix(in srgb, #df9a9d 40%, transparent); color: var(--color-text-secondary); font-size: 11px; line-height: 1.5; }
.difference-field { color: #a63840; font-weight: 600; }
.difference-value b { display: block; margin-bottom: 2px; color: #bd3f45; font-size: 10px; font-weight: 600; }
.comparison-reasoning { margin-top: 9px; padding-top: 7px; border-top: 1px solid var(--gray-100); color: var(--color-text-secondary); font-size: 11px; line-height: 1.55; }
.comparison-reasoning > span { margin-right: 8px; color: var(--color-text-tertiary); }
.decision-panel { min-height: 0; padding: 17px 15px; overflow-y: auto; background: color-mix(in srgb, var(--gray-25) 40%, var(--gray-0)); }
.decision-panel h3 { margin: 0; font-size: 15px; font-weight: 600; }
.decision-panel > p { margin: 4px 0 12px; color: var(--color-text-tertiary); font-size: 11px; line-height: 1.5; }
.decision-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.decision-button { min-height: 36px; border: 1px solid var(--gray-200); border-radius: 6px; background: var(--gray-0); color: var(--color-text-secondary); cursor: pointer; }
.decision-button:hover { border-color: var(--main-300); color: var(--main-700); }
.decision-button.active { border-color: var(--main-300); background: var(--main-30); color: var(--main-700); font-weight: 600; }
.decision-button:disabled { border-color: var(--gray-150); background: var(--gray-50); color: var(--color-text-tertiary); cursor: not-allowed; opacity: .7; }
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

@media (max-width: 900px) {
  .review-workspace { height: auto; grid-template-columns: 1fr; }
  .review-queue { max-height: 320px; border-right: 0; border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 50%, transparent); }
  .review-detail { min-height: 760px; }
  .detail-grid { grid-template-columns: 1fr; }
  .evidence-panel { min-height: 500px; border-right: 0; }
  .decision-panel { border-top: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent); }
}
</style>
