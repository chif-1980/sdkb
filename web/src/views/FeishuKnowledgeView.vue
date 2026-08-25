<template>
  <main class="workbench-page">
    <header class="page-heading">
      <div>
        <h1>知识加工</h1>
        <p>管理飞书素材的扫描、审核与发布状态</p>
      </div>
      <div class="heading-actions">
        <a-button :loading="checkingSource" :disabled="!currentSource" @click="checkCurrentSource">
          <Link2 :size="16" />
          检查连接
        </a-button>
        <a-button
          data-testid="scan-incremental"
          :loading="scanningMode === 'incremental'"
          :disabled="!currentSource || scanLocked"
          @click="startScan('incremental')"
        >
          <RefreshCw :size="16" />
          增量扫描
        </a-button>
        <a-button
          data-testid="scan-full"
          type="primary"
          :loading="scanningMode === 'full'"
          :disabled="!currentSource || scanLocked"
          @click="startScan('full')"
        >
          <ScanSearch :size="16" />
          全量扫描
        </a-button>
      </div>
    </header>

    <a-alert v-if="pageError" class="page-alert" type="error" show-icon :message="pageError" />

    <a-spin :spinning="loadingSources">
      <template v-if="currentSource">
        <section class="source-strip" aria-label="飞书数据源">
          <div class="source-identity">
            <div class="source-icon"><BookOpen :size="20" /></div>
            <div>
              <label>数据源</label>
              <strong>{{ currentSource.name }}</strong>
            </div>
          </div>
          <div class="source-field source-field-wide">
            <label>Wiki 根节点</label>
            <a
              v-if="currentSource.wiki_root_url"
              :href="currentSource.wiki_root_url"
              target="_blank"
              rel="noopener noreferrer"
            >
              {{ currentSource.wiki_root_url }}
            </a>
            <span v-else>{{ currentSource.wiki_root_token || '-' }}</span>
          </div>
          <div class="source-field">
            <label>目标知识库</label>
            <strong>{{ currentSource.target_kb_id || '-' }}</strong>
          </div>
          <div class="source-field">
            <label>飞书访问身份</label>
            <strong>{{ oauthStatusLabel }}</strong>
          </div>
          <div class="source-field">
            <label>最近全量 / 增量</label>
            <span>{{ formatTime(currentSource.last_full_sync_at) }}</span>
            <span>{{ formatTime(currentSource.last_incremental_sync_at) }}</span>
          </div>
        </section>

        <section class="stats-row" aria-label="素材统计">
          <div v-for="stat in stats" :key="stat.label" class="stat-item">
            <span>{{ stat.label }}</span>
            <strong :class="stat.tone">{{ stat.value }}</strong>
          </div>
        </section>

        <nav class="governance-tabs" aria-label="知识加工模块">
          <button
            type="button"
            class="governance-tab"
            :class="{ active: activeModule === 'materials' }"
            @click="activeModule = 'materials'"
          >
            资料与扫描
          </button>
          <button
            type="button"
            class="governance-tab"
            :class="{ active: activeModule === 'reviews' }"
            @click="activeModule = 'reviews'"
          >
            待审核 <span class="governance-count">{{ governanceCounts.reviews }}</span>
          </button>
          <button
            type="button"
            class="governance-tab"
            :class="{ active: activeModule === 'relations' }"
            @click="activeModule = 'relations'"
          >
            跨文档检查 <span class="governance-count">{{ governanceCounts.relations }}</span>
          </button>
          <button
            type="button"
            class="governance-tab"
            :class="{ active: activeModule === 'formal' }"
            @click="activeModule = 'formal'"
          >
            正式知识 <span class="governance-count">{{ governanceCounts.formal }}</span>
          </button>
        </nav>

        <section v-if="activeModule === 'materials'" aria-label="资料与扫描">
          <div class="workspace-grid">
            <section class="workspace-section tree-section" aria-label="飞书知识目录">
              <div class="section-heading">
                <div>
                  <div class="section-title">
                    <h2>知识目录</h2>
                    <a-tooltip title="列出当前知识空间的全部顶层节点及下级内容，仅读取目录元数据">
                      <span class="section-help" aria-label="知识目录说明" role="img">
                        <CircleHelp :size="14" />
                      </span>
                    </a-tooltip>
                  </div>
                </div>
                <div class="tree-actions">
                  <a-tooltip :title="oauthStatus.authorized ? '重新扫码授权' : '扫码授权'">
                    <a-button
                      data-testid="oauth-qr-authorize"
                      type="primary"
                      :aria-label="oauthStatus.authorized ? '重新扫码授权' : '扫码授权'"
                      :loading="qrAuthorizing"
                      :disabled="!currentSource"
                      @click="startQrOAuth"
                    >
                      <QrCode :size="16" />
                      <span class="tree-action-label">{{
                        oauthStatus.authorized ? '重新扫码授权' : '扫码授权'
                      }}</span>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="浏览器授权">
                    <a-button
                      data-testid="oauth-browser-authorize"
                      aria-label="浏览器授权"
                      :loading="authorizingUser"
                      :disabled="!currentSource"
                      @click="startBrowserOAuth"
                    >
                      <ExternalLink :size="16" />
                      <span class="tree-action-label">浏览器授权</span>
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="刷新目录">
                    <a-button
                      aria-label="刷新知识目录"
                      :loading="loadingTree"
                      :disabled="!oauthStatus.authorized"
                      @click="loadTree(true)"
                    >
                      <RefreshCw :size="16" />
                      <span class="tree-action-label">刷新目录</span>
                    </a-button>
                  </a-tooltip>
                </div>
              </div>
              <div class="panel-body tree-body">
                <a-alert
                  v-if="treeError"
                  class="tree-alert"
                  type="warning"
                  show-icon
                  :message="treeError"
                />
                <a-spin :spinning="loadingTree">
                  <a-tree
                    v-if="treeData.length && !treeError"
                    :tree-data="treeData"
                    :default-expand-all="false"
                    block-node
                    show-line
                  >
                    <template #title="{ data }">
                      <a
                        v-if="data.url"
                        :href="data.url"
                        target="_blank"
                        rel="noopener noreferrer"
                        >{{ data.title }}</a
                      >
                      <span v-else>{{ data.title }}</span>
                    </template>
                  </a-tree>
                  <a-empty
                    v-else-if="!treeError && !loadingTree"
                    description="暂未读取到知识目录"
                  />
                </a-spin>
              </div>
            </section>

            <section class="workspace-section runs-section">
              <div class="section-heading">
                <div>
                  <div class="section-title">
                    <h2>扫描批次</h2>
                    <a-tooltip title="选择批次查看该次扫描产生或变更的素材">
                      <span class="section-help" aria-label="扫描批次说明" role="img">
                        <CircleHelp :size="14" />
                      </span>
                    </a-tooltip>
                  </div>
                </div>
                <button
                  v-if="selectedRunId"
                  type="button"
                  class="clear-filter"
                  @click="selectRun('')"
                >
                  清除筛选
                </button>
              </div>
              <div class="panel-body runs-body">
                <FeishuSyncRunsTable
                  :runs="runs"
                  :loading="loadingRuns"
                  :selected-run-id="selectedRunId"
                  @select="selectRun"
                />
              </div>
            </section>
          </div>

          <section class="workspace-section material-section">
            <div class="section-heading material-heading">
              <div>
                <h2>素材队列</h2>
                <p>{{ selectedRunId ? '正在查看所选批次素材' : '显示当前数据源的全部素材版本' }}</p>
              </div>
              <a-button aria-label="刷新素材" @click="loadMaterials">
                <RefreshCw :size="16" />
                刷新
              </a-button>
            </div>

            <form class="filter-bar" @submit.prevent="loadMaterials">
              <a-select
                v-model:value="filters.processing_status"
                allow-clear
                placeholder="加工状态"
                :options="processingOptions"
              />
              <a-select
                v-model:value="filters.review_status"
                allow-clear
                placeholder="审核状态"
                :options="reviewOptions"
              />
              <a-select
                v-model:value="filters.source_validity"
                allow-clear
                placeholder="来源状态"
                :options="validityOptions"
              />
              <a-select
                v-model:value="filters.item_type"
                allow-clear
                placeholder="素材类型"
                :options="typeOptions"
              />
              <a-input v-model:value="filters.directory" allow-clear placeholder="飞书目录路径" />
              <a-range-picker v-model:value="updatedRange" show-time />
              <a-button html-type="submit" type="primary">筛选</a-button>
              <a-button @click="resetFilters">重置</a-button>
            </form>

            <FeishuMaterialTable
              ref="materialTableRef"
              :materials="materials"
              :loading="loadingMaterials"
              :max-selection="MAX_BATCH_SIZE"
              @open-detail="openMaterialDetail"
              @action="handleMaterialAction"
              @batch-action="handleBatchAction"
              @selection-limit="(limit) => message.warning(`单次批量操作最多选择 ${limit} 条素材`)"
            />
          </section>
        </section>

        <FeishuReviewWorkspace
          v-else-if="activeModule === 'reviews'"
          :source-id="currentSourceId"
          :target-review-id="governanceReviewTarget"
          @count-change="governanceCounts.reviews = $event"
          @target-consumed="governanceReviewTarget = null"
        />
        <FeishuRelationsPanel
          v-else-if="activeModule === 'relations'"
          :source-id="currentSourceId"
          @count-change="governanceCounts.relations = $event"
          @open-review="openGovernanceReview"
        />
        <FeishuFormalKnowledgePanel
          v-else-if="activeModule === 'formal'"
          :source-id="currentSourceId"
          @count-change="governanceCounts.formal = $event"
        />
      </template>

      <a-empty v-else class="empty-source" description="尚未配置飞书知识数据源" />
    </a-spin>

    <FeishuMaterialDetailDrawer
      :open="detailOpen"
      :material="detailMaterial"
      :events="detailEvents"
      :content="detailContent"
      :loading="loadingDetail"
      @close="closeDetail"
    />

    <a-modal
      :open="qrDialogOpen"
      title="扫码授权飞书知识库"
      :footer="null"
      :width="520"
      :mask-closable="qrPhase !== 'loading'"
      @cancel="closeQrAuthorization"
    >
      <div class="qr-oauth-content">
        <div class="qr-code-frame" aria-live="polite">
          <a-spin v-if="qrPhase === 'loading'" />
          <img
            v-else-if="qrImageUrl"
            data-testid="oauth-qr-image"
            :src="qrImageUrl"
            alt="飞书知识库授权二维码"
          />
          <div v-else class="qr-code-empty">
            <ShieldAlert :size="30" />
            <span>二维码生成失败</span>
          </div>
        </div>
        <div class="qr-oauth-copy">
          <h3>{{ qrPhase === 'success' ? '授权已完成' : '请使用手机飞书扫码' }}</h3>
          <p v-if="qrPhase === 'success'">正在刷新电脑端的授权身份与知识目录。</p>
          <p v-else>由能够访问“SD 知识库”的管理员扫码，并在手机上确认授权。</p>
          <div class="qr-status" :class="`is-${qrPhase}`">
            <CircleCheck v-if="qrPhase === 'success'" :size="16" />
            <RefreshCw v-else-if="qrPhase === 'waiting'" :size="16" />
            <ShieldAlert v-else-if="qrPhase === 'error' || qrPhase === 'expired'" :size="16" />
            <span>{{ qrStatusLabel }}</span>
          </div>
          <p v-if="qrError" class="qr-error">{{ qrError }}</p>
          <p class="qr-permission">二维码 5 分钟内有效，仅申请知识库只读访问与自动续期权限。</p>
        </div>
      </div>
    </a-modal>
  </main>
</template>

<script setup>
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Input, message, Modal } from 'ant-design-vue'
import {
  BookOpen,
  CircleCheck,
  CircleHelp,
  ExternalLink,
  Link2,
  QrCode,
  RefreshCw,
  ScanSearch,
  ShieldAlert
} from 'lucide-vue-next'
import QRCode from 'qrcode'

import { feishuKnowledgeApi, MAX_BATCH_SIZE } from '@/apis/feishu_knowledge_api'
import { governanceApi } from '@/apis/governance_api'
import { documentApi } from '@/apis/knowledge_api'
import FeishuMaterialDetailDrawer from '@/components/feishu/FeishuMaterialDetailDrawer.vue'
import FeishuMaterialTable from '@/components/feishu/FeishuMaterialTable.vue'
import FeishuSyncRunsTable from '@/components/feishu/FeishuSyncRunsTable.vue'
import FeishuFormalKnowledgePanel from '@/components/feishu/FeishuFormalKnowledgePanel.vue'
import FeishuRelationsPanel from '@/components/feishu/FeishuRelationsPanel.vue'
import FeishuReviewWorkspace from '@/components/feishu/FeishuReviewWorkspace.vue'

const TERMINAL_RUN_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])
const QR_AUTH_TTL_MS = 5 * 60 * 1000
const QR_POLL_INTERVAL_MS = 2000
const TREE_CACHE_KEY = 'feishu-knowledge-tree-cache-v1'
const TREE_CACHE_TTL_MS = 10 * 60 * 1000

const sources = ref([])
const currentSourceId = ref('')
const runs = ref([])
const materials = ref([])
const selectedRunId = ref('')
const activeRunId = ref('')
const scanningMode = ref('')
const checkingSource = ref(false)
const authorizingUser = ref(false)
const qrAuthorizing = ref(false)
const qrDialogOpen = ref(false)
const qrImageUrl = ref('')
const qrPhase = ref('idle')
const qrError = ref('')
const qrStartedAt = ref('')
const qrExpiresAt = ref(0)
const loadingSources = ref(false)
const loadingRuns = ref(false)
const loadingMaterials = ref(false)
const loadingTree = ref(false)
const pageError = ref('')
const treeError = ref('')
const treeData = ref([])
const oauthStatus = ref({ authorized: false, status: 'not_authorized' })
const detailOpen = ref(false)
const detailMaterial = ref(null)
const detailEvents = ref([])
const detailContent = ref(emptyDetailContent())
const loadingDetail = ref(false)
const updatedRange = ref([])
const materialTableRef = ref(null)
const activeModule = ref('materials')
const governanceCounts = reactive({ reviews: 0, relations: 0, formal: 0 })
const governanceReviewTarget = ref(null)
let pollTimer = null
let qrPollTimer = null
let qrRequestSeq = 0
let detailRequestSeq = 0
let isAlive = true

function emptyDetailContent() {
  return { content: '', lines: [], loading: false, error: '' }
}

function openGovernanceReview(relation) {
  governanceReviewTarget.value = {
    relationId: relation.relation_id || '',
    sourceVersionId: relation.source_version_id || '',
    targetVersionId: relation.target_version_id || ''
  }
  activeModule.value = 'reviews'
}

const filters = reactive({
  processing_status: undefined,
  review_status: undefined,
  source_validity: undefined,
  item_type: undefined,
  directory: ''
})

const processingOptions = [
  { label: '已发现', value: 'discovered' },
  { label: '归档中', value: 'syncing' },
  { label: '已归档', value: 'synced' },
  { label: '等待加工', value: 'processing_queued' },
  { label: '加工中', value: 'processing' },
  { label: '待审核', value: 'awaiting_review' },
  { label: '已跳过', value: 'skipped' },
  { label: '解析失败', value: 'parse_failed' },
  { label: '等待发布', value: 'publish_queued' },
  { label: '发布中', value: 'publishing' },
  { label: '已发布', value: 'published' },
  { label: '发布失败', value: 'publish_failed' },
  { label: '已替换', value: 'replaced' },
  { label: '不支持', value: 'unsupported' },
  { label: '待下架', value: 'removal_pending' },
  { label: '下架失败', value: 'removal_failed' },
  { label: '已下架', value: 'removed' }
]
const reviewOptions = [
  { label: '待审核', value: 'pending' },
  { label: '已通过', value: 'approved' },
  { label: '已驳回', value: 'rejected' },
  { label: '无需审核', value: 'not_required' }
]
const validityOptions = [
  { label: '来源有效', value: 'valid' },
  { label: '来源失效', value: 'invalid' }
]
const typeOptions = [
  { label: '飞书页面', value: 'page' },
  { label: '目录节点', value: 'directory' },
  { label: '附件', value: 'attachment' },
  { label: '音频', value: 'audio' },
  { label: '视频', value: 'video' }
]

const currentSource = computed(() =>
  sources.value.find((item) => item.source_id === currentSourceId.value)
)

const hasActiveRun = computed(() =>
  runs.value.some((run) => run.status === 'queued' || run.status === 'running')
)
const scanLocked = computed(() =>
  Boolean(
    !oauthStatus.value.authorized || scanningMode.value || activeRunId.value || hasActiveRun.value
  )
)

const oauthStatusLabel = computed(() => {
  if (oauthStatus.value.authorized) {
    return oauthStatus.value.display_name ? `已授权 · ${oauthStatus.value.display_name}` : '已授权'
  }
  if (oauthStatus.value.status === 'reauthorization_required') return '授权已失效'
  return '未授权'
})

const qrStatusLabel = computed(() => {
  return {
    idle: '等待生成二维码',
    loading: '正在生成二维码',
    waiting: '等待手机确认授权',
    success: '授权成功',
    expired: '二维码已过期',
    error: '授权未完成'
  }[qrPhase.value]
})

const stats = computed(() => [
  { label: '素材总数', value: currentSource.value?.total_count ?? 0 },
  { label: '待审核', value: currentSource.value?.awaiting_review_count ?? 0, tone: 'warning' },
  { label: '失败', value: currentSource.value?.failed_count ?? 0, tone: 'error' },
  { label: '来源失效', value: currentSource.value?.source_invalid_count ?? 0, tone: 'muted' }
])

async function loadSources() {
  const response = await feishuKnowledgeApi.listSources()
  sources.value = response.items || []
  if (!sources.value.some((item) => item.source_id === currentSourceId.value)) {
    currentSourceId.value = sources.value[0]?.source_id || ''
  }
  governanceCounts.reviews = currentSource.value?.awaiting_review_count ?? 0
}

async function loadReviewCount() {
  if (!currentSourceId.value) return
  try {
    const response = await governanceApi.listReviewPackages(currentSourceId.value, { view: 'mine' })
    governanceCounts.reviews = response.counts?.mine ?? response.total ?? 0
  } catch {
    governanceCounts.reviews = currentSource.value?.awaiting_review_count ?? 0
  }
}

async function loadRelationCount() {
  if (!currentSourceId.value) return
  try {
    const response = await governanceApi.getComparisonStatus(currentSourceId.value)
    governanceCounts.relations = response.relation_count ?? 0
  } catch {
    governanceCounts.relations = 0
  }
}

async function loadFormalKnowledgeCount() {
  if (!currentSourceId.value) return
  try {
    const response = await governanceApi.listFormalKnowledge(currentSourceId.value)
    governanceCounts.formal = response.items?.length ?? 0
  } catch {
    governanceCounts.formal = 0
  }
}

async function loadRuns() {
  if (!currentSourceId.value) return
  loadingRuns.value = true
  try {
    const response = await feishuKnowledgeApi.listRuns(currentSourceId.value)
    runs.value = response.items || []
    const activeRun = runs.value.find((run) => run.status === 'queued' || run.status === 'running')
    if (activeRun && !activeRunId.value) {
      activeRunId.value = activeRun.run_id
      schedulePoll()
    }
  } finally {
    loadingRuns.value = false
  }
}

function materialQuery() {
  return {
    ...filters,
    run_id: selectedRunId.value || undefined,
    updated_from: updatedRange.value?.[0]?.toISOString?.(),
    updated_to: updatedRange.value?.[1]?.toISOString?.()
  }
}

async function loadMaterials() {
  if (!currentSourceId.value) return
  loadingMaterials.value = true
  try {
    const response = await feishuKnowledgeApi.listMaterials(currentSourceId.value, materialQuery())
    materials.value = response.items || []
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '加载素材失败'))
  } finally {
    loadingMaterials.value = false
  }
}

function normalizeTree(nodes) {
  return (nodes || []).map((node) => ({
    key: node.node_token,
    title: node.title || node.node_token,
    url: node.url || '',
    children: normalizeTree(node.children),
    isLeaf: !node.children?.length
  }))
}

function readTreeCache(sourceId) {
  try {
    const cache = JSON.parse(sessionStorage.getItem(TREE_CACHE_KEY) || '{}')
    const entry = cache?.[sourceId]
    if (!entry || !Array.isArray(entry.tree) || Date.now() - entry.cachedAt > TREE_CACHE_TTL_MS) {
      return null
    }
    return entry.tree
  } catch {
    return null
  }
}

function writeTreeCache(sourceId, tree) {
  try {
    const cache = JSON.parse(sessionStorage.getItem(TREE_CACHE_KEY) || '{}')
    cache[sourceId] = { cachedAt: Date.now(), tree }
    sessionStorage.setItem(TREE_CACHE_KEY, JSON.stringify(cache))
  } catch {
    // 缓存不可用时不影响目录读取。
  }
}

async function loadTree(force = false) {
  if (!currentSourceId.value) return
  if (!oauthStatus.value.authorized) {
    treeData.value = []
    treeError.value = '请先授权一名能够访问该知识空间的飞书用户'
    return
  }
  if (!force) {
    const cachedTree = readTreeCache(currentSourceId.value)
    if (cachedTree) {
      treeData.value = cachedTree
      treeError.value = ''
      return
    }
  }
  loadingTree.value = true
  treeError.value = ''
  try {
    const response = await feishuKnowledgeApi.listTree(currentSourceId.value)
    treeData.value = normalizeTree(response.nodes)
    writeTreeCache(currentSourceId.value, treeData.value)
  } catch (error) {
    treeData.value = []
    treeError.value = feishuKnowledgeApi.getErrorMessage(error, '加载知识目录失败')
  } finally {
    loadingTree.value = false
  }
}

async function refreshAll({ forceTree = false } = {}) {
  await loadSources()
  if (currentSourceId.value) {
    await loadOAuthStatus()
    await Promise.all([
      loadRuns(),
      loadMaterials(),
      loadTree(forceTree),
      loadReviewCount(),
      loadRelationCount(),
      loadFormalKnowledgeCount()
    ])
  }
}

async function loadOAuthStatus() {
  if (!currentSourceId.value) return
  try {
    oauthStatus.value = await feishuKnowledgeApi.getOAuthStatus(currentSourceId.value)
  } catch (error) {
    oauthStatus.value = { authorized: false, status: 'not_authorized' }
    throw error
  }
}

function validateAuthorizationUrl(value) {
  const authorizationUrl = new URL(value)
  if (authorizationUrl.origin !== 'https://accounts.feishu.cn') {
    throw new Error('飞书授权地址无效')
  }
  return authorizationUrl.toString()
}

async function startBrowserOAuth() {
  if (!currentSource.value || authorizingUser.value) return
  authorizingUser.value = true
  try {
    const response = await feishuKnowledgeApi.startOAuth(currentSource.value.source_id, 'redirect')
    window.location.assign(validateAuthorizationUrl(response.authorization_url))
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '发起飞书用户授权失败'))
    authorizingUser.value = false
  }
}

async function startQrOAuth() {
  if (!currentSource.value || qrAuthorizing.value) return
  const requestId = ++qrRequestSeq
  clearTimeout(qrPollTimer)
  qrDialogOpen.value = true
  qrAuthorizing.value = true
  qrPhase.value = 'loading'
  qrImageUrl.value = ''
  qrError.value = ''
  try {
    const response = await feishuKnowledgeApi.startOAuth(currentSource.value.source_id, 'qr')
    const authorizationUrl = validateAuthorizationUrl(response.authorization_url)
    const imageUrl = await QRCode.toDataURL(authorizationUrl, {
      width: 232,
      margin: 1,
      errorCorrectionLevel: 'M',
      color: { dark: '#172033', light: '#ffffff' }
    })
    if (!isAlive || requestId !== qrRequestSeq) return
    qrStartedAt.value = response.started_at
    qrExpiresAt.value = Date.now() + QR_AUTH_TTL_MS
    qrImageUrl.value = imageUrl
    qrPhase.value = 'waiting'
    scheduleQrAuthorizationPoll(requestId)
  } catch (error) {
    if (requestId !== qrRequestSeq) return
    qrPhase.value = 'error'
    qrError.value = feishuKnowledgeApi.getErrorMessage(error, '生成飞书授权二维码失败')
  } finally {
    if (requestId === qrRequestSeq) qrAuthorizing.value = false
  }
}

function scheduleQrAuthorizationPoll(requestId) {
  if (!isAlive || !qrDialogOpen.value || requestId !== qrRequestSeq) return
  clearTimeout(qrPollTimer)
  qrPollTimer = setTimeout(() => pollQrAuthorization(requestId), QR_POLL_INTERVAL_MS)
}

function completedAfterQrStarted(status) {
  const refreshedAt = Date.parse(status?.last_refreshed_at || '')
  const startedAt = Date.parse(qrStartedAt.value || '')
  return (
    status?.authorized &&
    Number.isFinite(refreshedAt) &&
    Number.isFinite(startedAt) &&
    refreshedAt >= startedAt
  )
}

async function pollQrAuthorization(requestId) {
  if (!isAlive || !qrDialogOpen.value || requestId !== qrRequestSeq) return
  if (Date.now() >= qrExpiresAt.value) {
    qrPhase.value = 'expired'
    qrError.value = '二维码已过期，请关闭后重新发起授权。'
    return
  }
  try {
    const status = await feishuKnowledgeApi.getOAuthStatus(currentSourceId.value)
    if (!isAlive || requestId !== qrRequestSeq) return
    if (!completedAfterQrStarted(status)) {
      scheduleQrAuthorizationPoll(requestId)
      return
    }
    oauthStatus.value = status
    qrPhase.value = 'success'
    qrError.value = ''
    await Promise.all([loadSources(), loadTree(true)])
    if (!isAlive || requestId !== qrRequestSeq) return
    message.success('飞书用户授权成功，知识目录已刷新')
    qrPollTimer = setTimeout(() => closeQrAuthorization(), 800)
  } catch {
    if (!isAlive || requestId !== qrRequestSeq) return
    qrError.value = '暂时无法确认授权结果，系统会继续检查。'
    scheduleQrAuthorizationPoll(requestId)
  }
}

function closeQrAuthorization() {
  qrRequestSeq += 1
  clearTimeout(qrPollTimer)
  qrDialogOpen.value = false
  qrAuthorizing.value = false
  qrImageUrl.value = ''
  qrPhase.value = 'idle'
  qrError.value = ''
  qrStartedAt.value = ''
  qrExpiresAt.value = 0
}

function handleOAuthCallbackResult() {
  const params = new URLSearchParams(window.location.search)
  const status = params.get('oauth_status')
  if (!status) return
  if (status === 'success') {
    message.success('飞书用户授权成功，后续读取将使用该用户权限')
  } else {
    const code = params.get('oauth_error') || 'FEISHU_USER_OAUTH_FAILED'
    const error = { response: { data: { detail: { code } } } }
    message.error(feishuKnowledgeApi.getErrorMessage(error, '飞书用户授权失败'))
  }
  window.history.replaceState({}, '', window.location.pathname)
}

async function initialize() {
  loadingSources.value = true
  pageError.value = ''
  try {
    await refreshAll()
  } catch (error) {
    pageError.value = feishuKnowledgeApi.getErrorMessage(error, '加载知识加工工作台失败')
  } finally {
    loadingSources.value = false
  }
}

async function checkCurrentSource() {
  if (!currentSource.value) return
  checkingSource.value = true
  try {
    await feishuKnowledgeApi.checkSource(currentSource.value.source_id)
    message.success('飞书数据源连接正常')
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '连接检查失败'))
  } finally {
    checkingSource.value = false
  }
}

async function startScan(mode) {
  if (!currentSource.value || scanLocked.value) return
  scanningMode.value = mode
  try {
    const result = await feishuKnowledgeApi.scanSource(currentSource.value.source_id, mode)
    activeRunId.value = result.run_id
    if (!runs.value.some((run) => run.run_id === result.run_id)) {
      runs.value = [
        {
          run_id: result.run_id,
          source_id: currentSource.value.source_id,
          run_type: mode,
          status: result.status || 'queued'
        },
        ...runs.value
      ]
    }
    message.success(`${mode === 'full' ? '全量' : '增量'}扫描已提交`)
    schedulePoll()
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '扫描提交失败'))
  } finally {
    scanningMode.value = ''
  }
}

function schedulePoll() {
  if (!isAlive || !activeRunId.value) return
  clearTimeout(pollTimer)
  pollTimer = setTimeout(pollActiveRun, 2000)
}

async function pollActiveRun() {
  const runId = activeRunId.value
  if (!isAlive || !runId) return
  try {
    const run = await feishuKnowledgeApi.getRun(runId)
    if (!isAlive || activeRunId.value !== runId) return
    const index = runs.value.findIndex((item) => item.run_id === run.run_id)
    const runType = run.run_type || (index >= 0 ? runs.value[index].run_type : '')
    if (index >= 0) runs.value.splice(index, 1, run)
    else runs.value.unshift(run)
    if (TERMINAL_RUN_STATUSES.has(run.status)) {
      activeRunId.value = ''
      await refreshAll({ forceTree: true })
      if (!isAlive) return
      if (run.status === 'succeeded') {
        message.success(`${runType === 'full' ? '全量' : '增量'}扫描完成`)
      } else {
        message.error(run.error_summary || '扫描任务未成功完成')
      }
      return
    }
    schedulePoll()
  } catch (error) {
    if (!isAlive || activeRunId.value !== runId) return
    message.error(feishuKnowledgeApi.getErrorMessage(error, '获取扫描进度失败'))
    try {
      await loadRuns()
    } catch {
      // 仅 getRun 返回明确终态时才结束当前轮询。
    }
    if (isAlive && activeRunId.value === runId) schedulePoll()
  }
}

function selectRun(runId) {
  selectedRunId.value = runId
  loadMaterials()
}

function resetFilters() {
  Object.assign(filters, {
    processing_status: undefined,
    review_status: undefined,
    source_validity: undefined,
    item_type: undefined,
    directory: ''
  })
  updatedRange.value = []
  loadMaterials()
}

async function openMaterialDetail(material) {
  const requestId = ++detailRequestSeq
  detailOpen.value = true
  detailMaterial.value = material
  detailEvents.value = []
  detailContent.value = emptyDetailContent()
  loadingDetail.value = true
  let detail
  try {
    const [materialDetail, events] = await Promise.all([
      feishuKnowledgeApi.getMaterial(material.version_id),
      feishuKnowledgeApi.listMaterialEvents(material.version_id)
    ])
    if (requestId !== detailRequestSeq) return
    detail = materialDetail
    detailMaterial.value = detail
    detailEvents.value = events.items || []
  } catch (error) {
    if (requestId !== detailRequestSeq) return
    message.error(feishuKnowledgeApi.getErrorMessage(error, '加载素材详情失败'))
  } finally {
    if (requestId === detailRequestSeq) loadingDetail.value = false
  }

  if (!detail?.target_kb_id || !detail?.yuxi_file_id || requestId !== detailRequestSeq) return

  detailContent.value = { ...emptyDetailContent(), loading: true }
  try {
    const response = await documentApi.getDocumentContent(detail.target_kb_id, detail.yuxi_file_id)
    if (requestId !== detailRequestSeq) return
    if (response?.status === 'failed') {
      throw new Error(response.message || '加载解析内容失败')
    }
    detailContent.value = {
      content: response?.content || '',
      lines: response?.lines || [],
      loading: false,
      error: ''
    }
  } catch (error) {
    if (requestId !== detailRequestSeq) return
    detailContent.value = {
      ...emptyDetailContent(),
      error: feishuKnowledgeApi.getErrorMessage(error, '加载解析内容失败')
    }
  }
}

function closeDetail() {
  detailRequestSeq += 1
  detailOpen.value = false
  detailMaterial.value = null
  detailEvents.value = []
  detailContent.value = emptyDetailContent()
  loadingDetail.value = false
}

function askRejectReason(title, onConfirm) {
  let reason = ''
  Modal.confirm({
    title,
    content: h('div', { class: 'reject-reason' }, [
      h('label', { for: 'reject-reason-input' }, '驳回原因'),
      h(Input.TextArea, {
        id: 'reject-reason-input',
        placeholder: '请说明需要修改的内容',
        maxlength: 500,
        'onUpdate:value': (value) => (reason = value)
      })
    ]),
    okText: '确认驳回',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () => {
      if (!reason.trim()) {
        message.warning('请输入驳回原因')
        return Promise.reject(new Error('驳回原因不能为空'))
      }
      return onConfirm(reason.trim())
    }
  })
}

function confirmAction(title, content, onConfirm, danger = false) {
  Modal.confirm({
    title,
    content,
    okText: '确认',
    cancelText: '取消',
    okButtonProps: danger ? { danger: true } : {},
    onOk: onConfirm
  })
}

async function executeSingle(action, material, reason) {
  try {
    if (action === 'approve') await feishuKnowledgeApi.approveMaterial(material.version_id)
    if (action === 'reject') await feishuKnowledgeApi.rejectMaterial(material.version_id, reason)
    if (action === 'retry') await feishuKnowledgeApi.retryMaterial(material.version_id)
    if (action === 'confirm_removal') await feishuKnowledgeApi.confirmRemoval(material.version_id)
    message.success(actionSuccessLabel(action))
    await Promise.all([loadSources(), loadMaterials()])
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '素材操作失败'))
    throw error
  }
}

function handleMaterialAction({ action, material }) {
  if (action === 'reject') {
    askRejectReason(`驳回“${material.title || '未命名素材'}”？`, (reason) =>
      executeSingle(action, material, reason)
    )
    return
  }
  const titles = {
    approve: `确认审核通过“${material.title || '未命名素材'}”？`,
    retry: `确认重试“${material.title || '未命名素材'}”？`,
    confirm_removal: `确认下架“${material.title || '未命名素材'}”？`
  }
  const descriptions = {
    approve: '通过后将进入正式知识库发布流程。',
    retry: '系统将根据当前状态重新执行加工或发布。',
    confirm_removal: '确认后将从正式知识库移除该素材。'
  }
  confirmAction(
    titles[action],
    descriptions[action],
    () => executeSingle(action, material),
    action === 'confirm_removal'
  )
}

async function executeBatch(action, versionIds, reason) {
  try {
    const result = await feishuKnowledgeApi.batchAction(action, versionIds, reason)
    const failed = result.failed || 0
    if (failed) message.warning(`批量操作完成，${failed} 条处理失败`)
    else message.success(`已处理 ${result.succeeded ?? versionIds.length} 条素材`)
    materialTableRef.value?.clearSelection()
    await Promise.all([loadSources(), loadMaterials()])
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '批量操作失败'))
    throw error
  }
}

function handleBatchAction({ action, versionIds }) {
  if (!versionIds.length) return
  if (versionIds.length > MAX_BATCH_SIZE) {
    message.warning(`单次批量操作最多选择 ${MAX_BATCH_SIZE} 条素材`)
    return
  }
  if (action === 'reject') {
    askRejectReason(`驳回所选 ${versionIds.length} 条素材？`, (reason) =>
      executeBatch(action, versionIds, reason)
    )
    return
  }
  const labels = { approve: '审核通过', retry: '重试', confirm_removal: '确认下架' }
  confirmAction(
    `确认${labels[action]}所选 ${versionIds.length} 条素材？`,
    action === 'confirm_removal' ? '这些素材将从正式知识库移除。' : '操作将应用到全部所选素材。',
    () => executeBatch(action, versionIds),
    action === 'confirm_removal'
  )
}

function actionSuccessLabel(action) {
  return {
    approve: '已审核通过，等待发布',
    reject: '已驳回素材',
    retry: '已提交重试',
    confirm_removal: '已确认下架'
  }[action]
}

function formatTime(value) {
  if (!value) return '暂无记录'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(value))
}

onMounted(() => {
  handleOAuthCallbackResult()
  initialize()
})
onBeforeUnmount(() => {
  isAlive = false
  clearTimeout(pollTimer)
  clearTimeout(qrPollTimer)
})
</script>

<style scoped lang="less">
.workbench-page {
  min-width: 0;
  min-height: 100vh;
  overflow-x: hidden;
  background: var(--gray-25);
  color: var(--color-text);
}

.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  min-height: 72px;
  padding: 12px var(--page-padding);
  background: color-mix(in srgb, var(--gray-0) 94%, transparent);
}

.page-heading h1,
.section-heading h2 {
  margin: 0;
  color: var(--color-text);
  font-weight: 600;
  letter-spacing: 0;
}

.page-heading h1 {
  font-size: 22px;
}

.page-heading p,
.section-heading p {
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 13px;
}

.heading-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.heading-actions :deep(.ant-btn),
.material-heading :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.page-alert {
  margin: 12px var(--page-padding) 0;
}

.source-strip {
  display: grid;
  grid-template-columns:
    minmax(160px, 0.8fr) minmax(260px, 1.5fr) minmax(130px, 0.7fr)
    minmax(160px, 0.8fr) minmax(180px, 0.9fr);
  gap: 0;
  margin: 12px var(--page-padding) 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}

.source-identity,
.source-field {
  display: flex;
  min-width: 0;
  min-height: 78px;
  flex-direction: column;
  justify-content: center;
  padding: 13px 16px;
  border-right: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent);
}

.source-identity {
  flex-direction: row;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
}

.source-field:last-child {
  border-right: 0;
}

.source-icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  place-items: center;
  border-radius: 7px;
  background: var(--main-30);
  color: var(--main-color);
}

.source-strip label {
  display: block;
  margin-bottom: 4px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.source-strip strong,
.source-strip span,
.source-strip a {
  min-width: 0;
  overflow: hidden;
  color: var(--color-text);
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-strip a {
  color: var(--main-color);
}

.source-field span + span {
  margin-top: 2px;
  color: var(--color-text-tertiary);
  font-size: 12px;
  font-weight: 400;
}

.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  margin: 10px var(--page-padding) 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}

.governance-tabs {
  display: flex;
  align-items: flex-end;
  height: 48px;
  margin: 0 var(--page-padding);
  border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 50%, transparent);
}

.governance-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  align-self: stretch;
  gap: 6px;
  padding: 6px 16px 4px;
  border: 0;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}

.governance-tab::after {
  position: absolute;
  right: 14px;
  bottom: -1px;
  left: 14px;
  height: 2px;
  border-radius: 2px;
  background: transparent;
  content: '';
}

.governance-tab:hover,
.governance-tab.active {
  color: var(--main-700);
}

.governance-tab.active {
  font-weight: 600;
}

.governance-tab.active::after {
  background: var(--main-color);
}

.governance-count {
  display: inline-grid;
  min-width: 20px;
  height: 20px;
  place-items: center;
  padding: 0 5px;
  border-radius: 10px;
  background: var(--gray-100);
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.governance-tab.active .governance-count {
  background: var(--main-30);
  color: var(--main-700);
}

.stat-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  min-height: 56px;
  padding: 12px 16px;
  border-right: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
}

.stat-item:last-child {
  border-right: 0;
}

.stat-item span {
  color: var(--color-text-secondary);
  font-size: 13px;
}

.stat-item strong {
  color: var(--main-700);
  font-size: 22px;
  font-weight: 600;
}

.stat-item strong.warning {
  color: var(--color-warning-900);
}
.stat-item strong.error {
  color: var(--color-error-700);
}
.stat-item strong.muted {
  color: var(--gray-600);
}

.workspace-section {
  margin: 10px var(--page-padding) 0;
  padding: 14px 16px 4px;
  border: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}

.workspace-grid {
  display: grid;
  grid-template-columns: minmax(320px, 0.78fr) minmax(620px, 1.62fr);
  gap: 10px;
  margin: 10px var(--page-padding) 0;
  height: 310px;
  align-items: start;
}

.workspace-grid > .workspace-section {
  box-sizing: border-box;
  display: flex;
  height: 100%;
  flex-direction: column;
  min-width: 0;
  margin: 0;
  overflow: hidden;
}

.material-section {
  margin-bottom: 24px;
  padding-bottom: 14px;
}

.tree-section {
  container-name: knowledge-tree;
  container-type: inline-size;
  padding-bottom: 14px;
}

.runs-section {
  padding-bottom: 14px;
}

.workspace-grid .section-heading {
  flex-shrink: 0;
  min-height: 42px;
}

.section-title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.section-title h2 {
  white-space: nowrap;
}

.section-help {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-tertiary);
  cursor: help;
  transition: color 160ms ease;
}

.section-help:hover {
  color: var(--main-color);
}

.panel-body {
  min-height: 0;
  flex: 1;
  overflow: auto;
  scrollbar-color: color-mix(in srgb, var(--main-color) 30%, var(--gray-150)) transparent;
  scrollbar-width: thin;
}

.tree-body {
  padding-right: 4px;
}

.runs-body {
  padding-bottom: 2px;
}

.runs-body :deep(.ant-table-wrapper) {
  min-width: 0;
}

.tree-alert {
  margin-bottom: 12px;
}

.tree-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tree-actions :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

@container knowledge-tree (max-width: 520px) {
  .tree-actions :deep(.ant-btn) {
    width: 32px;
    min-width: 0;
    flex: 0 0 32px;
    justify-content: center;
    padding-inline: 0;
  }

  .tree-action-label {
    display: none;
  }
}

.qr-oauth-content {
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  align-items: center;
  gap: 24px;
  padding: 12px 2px 4px;
}

.qr-code-frame {
  display: grid;
  width: 232px;
  height: 232px;
  place-items: center;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--main-color) 24%, var(--gray-150));
  border-radius: 8px;
  background: var(--gray-0);
}

.qr-code-frame img {
  display: block;
  width: 232px;
  height: 232px;
}

.qr-code-empty {
  display: grid;
  place-items: center;
  gap: 8px;
  color: var(--color-text-tertiary);
  font-size: 13px;
}

.qr-oauth-copy h3 {
  margin: 0 0 7px;
  color: var(--color-text);
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0;
}

.qr-oauth-copy > p {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.65;
}

.qr-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 16px;
  color: var(--main-700);
  font-size: 13px;
  font-weight: 500;
}

.qr-status.is-waiting svg {
  animation: qr-status-spin 1.6s linear infinite;
}

.qr-status.is-success {
  color: var(--color-success-700, #287a58);
}

.qr-status.is-error,
.qr-status.is-expired,
.qr-error {
  color: var(--color-error-700);
}

.qr-oauth-copy .qr-error {
  margin-top: 8px;
}

.qr-oauth-copy .qr-permission {
  margin-top: 14px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

@keyframes qr-status-spin {
  to {
    transform: rotate(360deg);
  }
}

.tree-section :deep(.ant-tree) {
  padding: 4px 0;
  background: transparent;
}

.tree-section :deep(.ant-tree-title a) {
  color: var(--main-color);
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.section-heading h2 {
  font-size: 16px;
}

.clear-filter {
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--main-color);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.filter-bar {
  display: grid;
  grid-template-columns:
    repeat(4, minmax(112px, 0.7fr)) minmax(160px, 1fr) minmax(260px, 1.4fr)
    auto auto;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px;
  border-radius: 6px;
  background: var(--gray-25);
}

.empty-source {
  padding-top: 20vh;
}

:deep(.ant-table-wrapper .ant-table-thead > tr > th) {
  border-bottom-color: color-mix(in srgb, var(--gray-150) 45%, transparent);
  background: var(--gray-25);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

:deep(.ant-table-wrapper .ant-table-tbody > tr > td) {
  border-bottom-color: color-mix(in srgb, var(--gray-150) 34%, transparent);
}

:global(.reject-reason) {
  display: grid;
  gap: 8px;
  padding-top: 8px;
}

@media (max-width: 1180px) {
  .source-strip {
    grid-template-columns: 1fr 1.5fr;
  }

  .source-field:nth-child(2) {
    border-right: 0;
  }

  .source-identity,
  .source-field:nth-child(2) {
    border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent);
  }

  .filter-bar {
    grid-template-columns: repeat(4, minmax(110px, 1fr));
  }
}

@media (max-width: 980px) {
  .workspace-grid {
    grid-template-columns: 1fr;
    height: auto;
  }

  .workspace-grid > .workspace-section {
    height: 310px;
  }
}

@media (max-width: 760px) {
  .page-heading {
    align-items: flex-start;
    flex-direction: column;
    padding-top: 16px;
    padding-bottom: 14px;
  }

  .heading-actions {
    width: 100%;
    overflow-x: auto;
  }

  .heading-actions :deep(.ant-btn) {
    min-height: 40px;
    flex-shrink: 0;
  }

  .source-strip {
    grid-template-columns: 1fr;
  }

  .source-identity,
  .source-field {
    min-height: 66px;
    border-right: 0;
    border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent);
  }

  .source-field:last-child {
    border-bottom: 0;
  }

  .stats-row {
    grid-template-columns: 1fr 1fr;
  }

  .stat-item:nth-child(2) {
    border-right: 0;
  }

  .stat-item:nth-child(-n + 2) {
    border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
  }

  .filter-bar {
    grid-template-columns: 1fr;
  }

  .workspace-section {
    padding-inline: 10px;
  }

  .workspace-grid > .workspace-section {
    height: 280px;
  }

  .qr-oauth-content {
    grid-template-columns: 1fr;
    justify-items: center;
  }

  .qr-oauth-copy {
    text-align: center;
  }
}
</style>
