<template>
  <section
    class="review-workspace"
    :class="{
      'queue-collapsed': queueCollapsed,
      'layout-focus': documentPages.length || presentationSlides.length
    }"
    aria-label="知识审核工作区"
  >
    <aside v-show="!queueCollapsed" class="review-queue" @scroll.passive="handleQueueScroll">
      <div class="queue-heading">
        <div class="queue-heading-row">
          <div>
            <h2>审核任务</h2>
            <span>{{ packageResponse.total || 0 }} 个审核包</span>
          </div>
          <div class="queue-heading-actions">
            <a-button
              size="small"
              aria-label="刷新审核任务"
              :loading="loadingPackages"
              @click="loadPackages"
            >
              <RefreshCw :size="14" />
            </a-button>
            <button
              type="button"
              class="queue-mobile-collapse"
              title="隐藏审核任务列表"
              aria-label="隐藏审核任务列表"
              @click="queueCollapsed = true"
            >
              <PanelLeftClose :size="15" />
            </button>
          </div>
        </div>
        <div class="queue-views" role="tablist" aria-label="审核任务状态">
          <button
            v-for="view in queueViews"
            :key="view.value"
            type="button"
            role="tab"
            :aria-selected="queueView === view.value"
            :class="{ active: queueView === view.value }"
            @click="queueView = view.value"
          >
            {{ view.label }}
            <span v-if="view.countKey">{{ packageResponse.counts?.[view.countKey] || 0 }}</span>
          </button>
        </div>
        <div
          v-if="queueView === 'completed'"
          class="queue-result-filter"
          role="group"
          aria-label="已完成任务处理结果"
        >
          <button
            v-for="result in completionResultOptions"
            :key="result.value"
            type="button"
            :aria-pressed="completionResultFilter === result.value"
            :class="{ active: completionResultFilter === result.value }"
            @click="completionResultFilter = result.value"
          >
            {{ result.label }}
          </button>
        </div>
        <div class="queue-filters">
          <a-select
            v-model:value="reviewTypeFilter"
            size="small"
            :options="reviewTypeOptions"
            aria-label="审核类型筛选"
          />
          <a-select
            v-model:value="problemFilter"
            size="small"
            :options="problemOptions"
            aria-label="问题类型筛选"
          />
        </div>
      </div>

      <a-spin :spinning="loadingPackages">
        <div v-if="!packages.length && !loadingPackages" class="queue-empty">
          <a-empty :description="queueEmptyText" />
        </div>
        <button
          v-for="item in packages"
          :key="item.package_id"
          type="button"
          class="queue-item"
          :class="{ active: item.package_id === selectedPackageId }"
          @click="selectPackage(item.package_id)"
        >
          <div class="queue-title">
            <strong>{{ item.title }}</strong>
            <span class="risk-badge" :class="`risk-${String(item.risk_level).toLowerCase()}`">{{
              riskLabel(item.risk_level)
            }}</span>
          </div>
          <p :title="item.wiki_path || '未记录目录路径'">
            {{ item.wiki_path || '未记录目录路径' }}
          </p>
          <div v-if="item.knowledge_unit_count" class="queue-unit-summary">
            <span class="queue-unit-count"
              ><strong>{{ item.decided_unit_count || 0 }}</strong
              >/{{ item.knowledge_unit_count }} 已处理</span
            >
            <span class="queue-unit-attention">待处理 {{ item.remaining_unit_count || 0 }}</span>
            <span class="queue-unit-ready">已纳入 {{ item.included_unit_count || 0 }}</span>
            <span class="queue-unit-excluded">不纳入 {{ item.excluded_unit_count || 0 }}</span>
            <span v-if="item.duplicate_unit_count" class="queue-unit-duplicate"
              >重复来源 {{ item.duplicate_unit_count }}</span
            >
          </div>
          <div v-else class="queue-type-counts">
            <span v-for="(count, type) in item.review_type_counts" :key="type"
              >{{ reviewTypeLabel(type) }} · {{ count }}项</span
            >
          </div>
          <div class="queue-meta">
            <span
              class="status-dot"
              :class="`status-${String(item.workflow_status).toLowerCase()}`"
            />
            <span>{{ workflowStatusLabel(item.workflow_status) }}</span>
            <span
              v-if="item.workflow_status === 'COMPLETED' && item.completion_result"
              class="completion-result-badge"
              :class="`completion-${item.completion_result}`"
              >{{ completionResultLabel(item.completion_result) }}</span
            >
            <span
              v-if="item.review_type_counts?.UPDATE && item.trigger_type !== 'FEEDBACK'"
              class="source-update-badge"
              >原文已更新</span
            >
            <span class="queue-time">更新于 {{ formatTime(item.updated_at) }}</span>
          </div>
        </button>
        <div v-if="packages.length" class="queue-load-more" aria-live="polite">
          <a-spin v-if="loadingMorePackages" size="small" />
          <button
            v-else-if="hasMorePackages"
            type="button"
            class="queue-load-more-button"
            @click="loadMorePackages"
          >
            继续加载审核任务（已显示 {{ packages.length }} / {{ packageResponse.total }}）
          </button>
          <span v-else>已显示全部 {{ packages.length }} 个审核任务</span>
        </div>
      </a-spin>
    </aside>

    <article v-if="packageDetail" class="review-detail">
      <div class="detail-grid">
        <section
          class="evidence-panel"
          :class="{ 'layout-focus-panel': documentPages.length || presentationSlides.length }"
        >
          <div v-if="showSourceUpdateNotice" class="source-update-notice">
            <RefreshCw :size="14" />
            <span>发现 {{ sourceUpdateCount }} 份原文更新，已生成待审核任务</span>
            <button type="button" @click="showSourceUpdates">查看更新</button>
          </div>
          <section
            v-if="packageDetail.qualityGate"
            class="quality-gate-summary"
            aria-label="质量门禁"
          >
            <div
              class="quality-gate-score"
              :class="`is-${String(packageDetail.qualityGate.status).toLowerCase()}`"
            >
              <strong>{{ packageDetail.qualityScore ?? '-' }}</strong>
              <span>质量分</span>
            </div>
            <div class="quality-gate-main">
              <header>
                <strong>{{ qualityGateLabel(packageDetail.qualityGate.status) }}</strong>
                <span v-if="packageDetail.autoCloseEligible"
                  >符合自动关闭条件（观察模式，不会自动执行）</span
                >
              </header>
              <div class="quality-dimensions">
                <span v-for="dimension in packageDetail.qualityDimensions" :key="dimension.label">
                  {{ dimension.label }} {{ dimension.score }}/{{ dimension.maxScore }}
                </span>
              </div>
              <div v-if="packageDetail.qualityGate.blockers?.length" class="quality-blockers">
                <span v-for="blocker in packageDetail.qualityGate.blockers" :key="blocker.code">
                  <CircleAlert :size="13" /> {{ blocker.message }}
                </span>
              </div>
              <div v-if="packageDetail.impactSummary" class="impact-summary">
                <span
                  >受影响知识 {{ packageDetail.impactSummary.affectedKnowledgeCount || 0 }}</span
                >
                <span>正文{{ changeFlagLabel(packageDetail.impactSummary.textChanged) }}</span>
                <span>图片{{ changeFlagLabel(packageDetail.impactSummary.imageChanged) }}</span>
                <span>版式{{ changeFlagLabel(packageDetail.impactSummary.layoutChanged) }}</span>
                <span>待处理关系 {{ packageDetail.impactSummary.openRelationCount || 0 }}</span>
              </div>
            </div>
          </section>
          <div v-if="publishBlocked" class="content-quality-alert">
            <CircleAlert :size="16" />
            <div>
              <strong>未读取到可审核正文</strong><span>{{ contentQualityMessage }}</span>
            </div>
          </div>
          <div class="evidence-tabs" aria-label="审核证据与处理操作">
            <div class="evidence-tabs-main" role="tablist" aria-label="审核证据视图">
              <button
                type="button"
                class="evidence-queue-toggle"
                :title="queueCollapsed ? '展开审核任务' : '收起审核任务'"
                :aria-label="queueCollapsed ? '展开审核任务' : '收起审核任务'"
                :aria-expanded="!queueCollapsed"
                @click="queueCollapsed = !queueCollapsed"
              >
                <PanelLeftOpen v-if="queueCollapsed" :size="18" />
                <PanelLeftClose v-else :size="18" />
              </button>
              <button
                v-if="isUpdateReview"
                type="button"
                role="tab"
                :aria-selected="activeEvidenceView === 'changes'"
                :class="{ active: activeEvidenceView === 'changes' }"
                @click="activeEvidenceView = 'changes'"
              >
                <GitCompareArrows :size="14" /> 具体变更
                <span class="tab-count">{{ versionChanges.length }}</span>
              </button>
              <button
                type="button"
                role="tab"
                :aria-selected="activeEvidenceView === 'content'"
                :class="{ active: activeEvidenceView === 'content' }"
                @click="activeEvidenceView = 'content'"
              >
                <FileText :size="14" /> 当前正文
              </button>
              <button
                type="button"
                role="tab"
                :aria-selected="activeEvidenceView === 'comparisons'"
                :class="{ active: activeEvidenceView === 'comparisons' }"
                @click="showComparisons"
              >
                <GitCompareArrows :size="14" /> 跨文档证据
                <span class="tab-count">{{ selectedRelations.length }}</span>
              </button>
              <button
                type="button"
                role="tab"
                :aria-selected="activeEvidenceView === 'history'"
                :class="{ active: activeEvidenceView === 'history' }"
                @click="activeEvidenceView = 'history'"
              >
                <History :size="14" /> 处理记录
                <span class="tab-count">{{ packageDetail.audit_record_count ?? auditRecords.length }}</span>
              </button>
            </div>
            <div class="record-actions evidence-tabs-actions">
              <a
                v-if="packageDetail.source_url"
                class="source-link"
                :href="packageDetail.source_url"
                target="_blank"
                rel="noopener noreferrer"
              >
                查看飞书原文 <ExternalLink :size="14" />
              </a>
              <a-button
                v-if="canConfirmNoUpdate"
                size="small"
                class="whole-review-button"
                :loading="batchResolving"
                :disabled="writeDisabled || batchResolving"
                @click="confirmNoUpdate"
              >
                <ClipboardCheck :size="14" />确认无需更新
              </a-button>
              <a-button
                v-else-if="showWholeReviewButton"
                size="small"
                class="whole-review-button"
                :loading="batchResolving"
                :disabled="writeDisabled || !bulkActionableItems.length || batchResolving"
                :title="wholeReviewButtonTitle"
                @click="confirmWholePackage"
              >
                <ClipboardCheck :size="14" />整篇批量审核
              </a-button>
              <a-button
                v-if="currentItem?.can_reopen_exclusion && !isDocumentLayoutReview && !isPptxReview"
                size="small"
                :loading="reopening"
                :disabled="writeDisabled || reopening"
                @click="confirmReopenExcludedItem"
              >
                <RotateCcw :size="14" />重新申请纳入
              </a-button>
              <a-button
                v-else-if="
                  !currentItem?.knowledge_unit || (!isDocumentLayoutReview && !isPptxReview)
                "
                type="primary"
                size="small"
                :disabled="writeDisabled || !itemActionable"
                :title="itemActionable ? '' : itemStatusLabel(currentItem?.item_status, currentItem)"
                @click="decisionPanelOpen = true"
              >
                <ClipboardCheck :size="14" />{{
                  itemActionable
                    ? knowledgeUnitMode
                      ? '处理当前知识单元'
                      : '审核处理'
                    : itemStatusLabel(currentItem?.item_status, currentItem)
                }}
              </a-button>
              <button
                v-if="knowledgeUnitMode"
                type="button"
                class="batch-action-secondary"
                :disabled="writeDisabled || !bulkExcludeItems.length || batchResolving"
                @click="confirmBulkOutcome('EXCLUDE')"
              >
                批量不纳入
              </button>
              <button
                v-if="knowledgeUnitMode"
                type="button"
                class="batch-action-secondary"
                :disabled="writeDisabled || !bulkOutcomeItems('REQUEST_SOURCE_CHANGE').length || batchResolving"
                @click="confirmBulkOutcome('REQUEST_SOURCE_CHANGE')"
              >
                批量退回
              </button>
            </div>
          </div>

          <section
            v-if="knowledgeUnitMode"
            class="unit-overview"
            aria-label="知识单元处理进度"
          >
            <div class="unit-overview-heading">
              <strong>{{ knowledgeUnitItems.length }}</strong>
              <span>个知识单元</span>
            </div>
            <div class="unit-overview-metrics">
              <span><b>{{ decidedUnitCount }}</b> 已处理</span>
              <span class="needs-attention"><b>{{ remainingUnitCount }}</b> 待处理</span>
              <span><b>{{ includedUnitCount }}</b> 已纳入</span>
              <span><b>{{ excludedUnitCount }}</b> 不纳入</span>
            </div>
            <div class="unit-overview-actions">
              <span
                class="unit-visibility"
                title="已处理单元仅可查看，不能重复提交"
                aria-label="已处理单元仅可查看，不能重复提交"
              >
                i
              </span>
              <div class="unit-view-switch" role="group" aria-label="知识单元筛选">
                <button
                  v-for="view in unitViewOptions"
                  :key="view.value"
                  type="button"
                  class="unit-filter"
                  :class="{ active: unitView === view.value }"
                  :aria-pressed="unitView === view.value"
                  @click="setUnitView(view.value)"
                >
                  {{ view.label }} {{ unitViewCount(view.value) }}
                </button>
              </div>
            </div>
          </section>
          <div v-if="knowledgeUnitMode && !visibleItems.length" class="unit-filter-empty">
            当前筛选没有知识单元；已处理单元仍保留在“已处理”或“全部”中查看。
          </div>

          <div class="evidence-body">
            <section v-if="activeEvidenceView === 'changes'" class="version-change-review">
              <a-spin :spinning="reviewContent.loading || previousReviewContent.loading">
                <div
                  v-if="currentItem?.knowledge_unit && currentItem.change_type === 'NEW'"
                  class="content-notice compact"
                >
                  <CircleCheck :size="22" /><strong>这是新增知识单元</strong>
                  <p>上一正式版本中没有对应内容，请直接核对当前单元正文。</p>
                </div>
                <div v-else-if="!hasComparisonBaseline" class="content-notice is-error">
                  <CircleAlert :size="24" /><strong>没有找到上一正式版本</strong>
                  <p>当前资料缺少可用于对比的旧版正文，系统无法生成具体变更内容。</p>
                </div>
                <div v-else class="version-flow">
                  <div class="version-card is-previous">
                    <span>当前正式版本</span>
                    <strong>版本 {{ packageDetail.previous_version?.revision || '-' }}</strong>
                    <small>{{ packageDetail.previous_version?.chunk_count || 0 }} 个片段</small>
                  </div>
                  <ArrowRight :size="17" />
                  <div class="version-card is-current">
                    <span>本次待审版本</span>
                    <strong>版本 {{ packageDetail.revision || '-' }}</strong>
                    <small>{{ candidateIndexLabel }}</small>
                  </div>
                  <div class="version-stat">
                    <span
                      ><b>{{ versionDiffStats.added }}</b> 行新增</span
                    >
                    <span
                      ><b>{{ versionDiffStats.removed }}</b> 行删除</span
                    >
                    <span
                      ><b>{{ versionDiffStats.modified }}</b> 处修改</span
                    >
                  </div>
                </div>
                <div
                  v-if="
                    !currentItem?.knowledge_unit &&
                    hasComparisonBaseline &&
                    (previousReviewContent.error || reviewContent.error)
                  "
                  class="content-notice is-error"
                >
                  <CircleAlert :size="24" /><strong>暂时无法生成版本对照</strong>
                  <p>{{ previousReviewContent.error || reviewContent.error }}</p>
                  <a-button size="small" @click="loadVersionContents"
                    ><RefreshCw :size="13" />重新加载</a-button
                  >
                </div>
                <div
                  v-else-if="hasComparisonBaseline && versionChanges.length"
                  class="version-change-list"
                >
                  <article
                    v-for="(change, index) in versionChanges"
                    :key="`${change.type}-${index}`"
                    class="version-change-card"
                    :class="`is-${change.type}`"
                  >
                    <header>
                      <strong>{{ versionChangeLabel(change.type) }}</strong>
                      <span>{{ change.lineCount }} 行</span>
                    </header>
                    <div v-if="change.type === 'modified'" class="change-columns">
                      <div>
                        <label>修改前</label>
                        <pre>{{ change.before }}</pre>
                      </div>
                      <div>
                        <label>修改后</label>
                        <pre>{{ change.after }}</pre>
                      </div>
                    </div>
                    <div v-else class="change-single">
                      <label>{{ change.type === 'added' ? '新增内容' : '删除内容' }}</label>
                      <pre>{{ change.type === 'added' ? change.after : change.before }}</pre>
                    </div>
                  </article>
                </div>
                <div
                  v-else-if="hasComparisonBaseline && changeContentReady"
                  class="content-notice compact"
                >
                  <CircleCheck :size="22" /><strong>{{ noTextChangeTitle }}</strong>
                  <p>{{ noTextChangeDescription }}</p>
                </div>
              </a-spin>
            </section>

            <section v-else-if="activeEvidenceView === 'content'" class="content-review">
              <a-spin
                :spinning="
                  reviewContent.loading ||
                  sourceSegmentsLoading ||
                  presentationLoading ||
                  slidePreviewLoading ||
                  documentLayoutLoading ||
                  documentPagePreviewLoading
                "
              >
                <div v-if="reviewContent.error" class="content-notice is-error">
                  <CircleAlert :size="24" /><strong>解析内容加载失败</strong>
                  <p>{{ reviewContent.error }}</p>
                  <a-button size="small" @click="loadReviewContent"
                    ><RefreshCw :size="13" />重新加载</a-button
                  >
                </div>
                <div v-else-if="presentationSlides.length" class="presentation-review">
                  <header class="presentation-toolbar">
                    <div>
                      <span>第 {{ activeSlideNumber }} / {{ presentationSlides.length }} 页</span>
                    </div>
                    <div class="presentation-toolbar-actions">
                      <span>{{ activePresentationSlide?.fragment_count || 0 }} 个可定位片段</span>
                      <button
                        type="button"
                        aria-label="上一页幻灯片"
                        :disabled="activeSlideNumber <= 1"
                        @click="changePresentationSlide(-1)"
                      >
                        <ChevronLeft :size="15" />
                      </button>
                      <button
                        type="button"
                        aria-label="下一页幻灯片"
                        :disabled="activeSlideNumber >= presentationSlides.length"
                        @click="changePresentationSlide(1)"
                      >
                        <ChevronRight :size="15" />
                      </button>
                    </div>
                  </header>
                  <div class="presentation-page-strip" aria-label="幻灯片页码">
                    <button
                      v-for="slide in presentationSlides"
                      :key="slide.slide_number"
                      type="button"
                      :class="{ active: slide.slide_number === activeSlideNumber }"
                      :aria-label="`查看第 ${slide.slide_number} 页`"
                      @click="selectPresentationSlide(slide.slide_number)"
                    >
                      {{ slide.slide_number }}
                    </button>
                  </div>
                  <div class="presentation-stage-row has-side-panel">
                    <div class="presentation-canvas-shell">
                      <div
                        class="presentation-canvas"
                        :style="{ aspectRatio: presentationAspectRatio }"
                      >
                        <img
                          v-if="activeSlidePreviewUrl"
                          :key="activeSlidePreviewUrl"
                          :src="activeSlidePreviewUrl"
                          :alt="`第 ${activeSlideNumber} 页幻灯片`"
                        />
                        <div v-else class="presentation-image-state">
                          <CircleAlert v-if="slidePreviewError" :size="22" />
                          <span>{{ slidePreviewError || '正在生成整页预览…' }}</span>
                        </div>
                        <template v-if="activeSlidePreviewUrl">
                          <button
                            v-for="fragment in activePresentationSlide?.fragments || []"
                            :key="fragment.fragment_id"
                            type="button"
                            class="presentation-fragment-hotspot"
                            :class="{
                              active: fragment.fragment_id === selectedPresentationFragmentId
                            }"
                            :style="fragmentHotspotStyle(fragment)"
                            :aria-label="`定位片段 ${fragment.fragment_number}：${fragment.content}`"
                            :title="fragment.content"
                            @click="selectPresentationFragment(fragment)"
                          >
                            <span>{{ fragment.fragment_number }}</span>
                          </button>
                        </template>
                      </div>
                    </div>
                    <aside class="layout-side-panel" aria-label="版式审核信息">
                      <section class="layout-context-sidebar">
                        <header class="layout-sidebar-heading">
                          <span>审核信息</span>
                          <a-tag
                            :color="
                              statusColor(currentItem?.item_status || packageDetail.workflow_status)
                            "
                          >
                            {{
                              itemStatusLabel(currentItem?.item_status, currentItem) ||
                              workflowStatusLabel(packageDetail.workflow_status)
                            }}
                          </a-tag>
                        </header>
                        <h3 :title="packageDetail.title">{{ packageDetail.title }}</h3>
                        <p v-if="packageDetail.wiki_path" class="layout-sidebar-path">
                          {{ packageDetail.wiki_path }}
                        </p>
                        <div class="layout-sidebar-tags">
                          <span
                            class="review-type"
                            :class="`type-${String(currentItem?.review_type).toLowerCase()}`"
                          >
                            {{ reviewTypeLabel(currentItem?.review_type) }}
                          </span>
                          <span
                            v-if="currentItem?.knowledge_unit"
                            class="unit-recommendation"
                            :class="{ attention: currentItem.manual_review_required }"
                          >
                            {{
                              currentItem.manual_review_required
                                ? '需要人工确认'
                                : `建议${outcomeLabel(currentItem.recommended_outcome)}`
                            }}
                          </span>
                        </div>
                        <dl class="layout-sidebar-facts">
                          <div v-if="currentItem?.knowledge_unit">
                            <dt>当前知识单元</dt>
                            <dd>{{ currentUnitPosition }} / {{ knowledgeUnitItems.length }}</dd>
                          </div>
                          <div>
                            <dt>预览位置</dt>
                            <dd>第 {{ activeSlideNumber }} 页</dd>
                          </div>
                          <div>
                            <dt>来源片段</dt>
                            <dd>{{ currentItem?.source_segment_ids?.length || 0 }} 个</dd>
                          </div>
                        </dl>
                        <p v-if="currentItem?.summary" class="layout-sidebar-summary">
                          {{ currentItem.summary }}
                        </p>
                        <button
                          v-if="currentItem?.can_reopen_exclusion"
                          type="button"
                          class="layout-sidebar-primary is-reopen"
                          :disabled="reopening"
                          @click.stop="confirmReopenExcludedItem"
                        >
                          <RotateCcw :size="14" />重新申请纳入
                        </button>
                        <button
                          v-else-if="knowledgeUnitMode"
                          type="button"
                          class="layout-sidebar-primary"
                          :disabled="!itemActionable"
                          :title="itemActionable ? '' : itemStatusLabel(currentItem?.item_status, currentItem)"
                          @click.stop="decisionPanelOpen = true"
                        >
                          <ClipboardCheck :size="14" />{{
                            itemActionable
                              ? '处理当前知识单元'
                              : itemStatusLabel(currentItem?.item_status, currentItem)
                          }}
                        </button>
                        <div
                          v-if="itemNavigationItems.length > 1"
                          class="layout-sidebar-navigation"
                        >
                          <button
                            type="button"
                            :disabled="!hasPreviousItem"
                            @click.stop="selectRelativeItem(-1)"
                          >
                            <ChevronLeft :size="13" /> 上一项
                          </button>
                          <button
                            type="button"
                            :disabled="!hasNextItem"
                            @click.stop="selectRelativeItem(1)"
                          >
                            下一项 <ChevronRight :size="13" />
                          </button>
                        </div>
                        <button
                          v-if="knowledgeUnitMode && visibleItems.length > 1"
                          type="button"
                          class="layout-sidebar-list-toggle"
                          :aria-expanded="unitListExpanded"
                          @click="unitListExpanded = !unitListExpanded"
                        >
                          {{
                            unitListExpanded
                              ? '收起知识单元列表'
                              : `查看全部知识单元（${visibleItems.length}）`
                          }}
                        </button>
                        <div
                          v-if="knowledgeUnitMode && unitListExpanded"
                          class="layout-sidebar-unit-list"
                        >
                          <button
                            v-for="(item, index) in visibleItems"
                            :key="item.review_item_id"
                            type="button"
                            :class="{
                              active: item.review_item_id === selectedItemId,
                              attention: item.manual_review_required
                            }"
                            @click="selectItem(item.review_item_id)"
                          >
                            <span>{{ index + 1 }}</span>
                            <strong>{{ item.title || reviewTypeLabel(item.review_type) }}</strong>
                            <small>{{ itemStatusLabel(item.item_status, item) }}</small>
                          </button>
                        </div>
                      </section>
                    </aside>
                  </div>
                  <div class="presentation-fragment-strip" aria-label="本页片段">
                    <button
                      v-for="fragment in activePresentationSlide?.fragments || []"
                      :key="fragment.fragment_id"
                      type="button"
                      :class="{
                        active: fragment.fragment_id === selectedPresentationFragmentId
                      }"
                      @click="selectPresentationFragment(fragment)"
                    >
                      <span>{{ fragment.fragment_number }}</span
                      >{{ presentationFragmentLabel(fragment) }}
                    </button>
                  </div>
                  <article v-if="selectedPresentationFragment" class="presentation-fragment-focus">
                    <header>
                      <span>第 {{ activeSlideNumber }} 页 · 片段定位</span>
                      <button type="button" @click="selectedPresentationFragmentId = ''">
                        关闭
                      </button>
                    </header>
                    <p>{{ selectedPresentationFragment.content }}</p>
                  </article>
                </div>
                <div v-else-if="documentPages.length" class="document-layout-review">
                  <header class="document-layout-toolbar">
                    <div class="document-layout-toolbar-actions">
                      <span v-if="activeDocumentPage?.preview_scaled" class="preview-scale-note">
                        原图像素过大，已按安全分辨率预览
                      </span>
                      <span
                        v-if="activeDocumentPage?.render_mode === 'grid'"
                        class="spreadsheet-toolbar-note"
                      >
                        横向、纵向滚动查看 · 点击单元格编辑
                      </span>
                      <span v-else>{{ activeDocumentPage?.block_count || 0 }} 个可定位内容块</span>
                      <button
                        type="button"
                        aria-label="上一页资料"
                        :disabled="activeDocumentPageNumber <= 1"
                        @click="changeDocumentPage(-1)"
                      >
                        <ChevronLeft :size="15" />
                      </button>
                      <button
                        type="button"
                        aria-label="下一页资料"
                        :disabled="activeDocumentPageNumber >= documentPages.length"
                        @click="changeDocumentPage(1)"
                      >
                        <ChevronRight :size="15" />
                      </button>
                    </div>
                  </header>
                  <div class="document-layout-page-strip" aria-label="资料页面导航">
                    <button
                      v-for="page in documentPages"
                      :key="page.page_number"
                      type="button"
                      :class="{ active: page.page_number === activeDocumentPageNumber }"
                      @click="selectDocumentPage(page.page_number)"
                    >
                      {{ page.label || page.page_number }}
                    </button>
                  </div>
                  <div class="document-layout-stage has-side-panel">
                    <div
                      v-if="activeDocumentPage?.render_mode === 'grid'"
                      class="spreadsheet-viewport"
                    >
                      <div
                        class="document-layout-canvas spreadsheet-canvas"
                        :style="spreadsheetCanvasStyle(activeDocumentPage)"
                        role="grid"
                        :aria-label="`${activeDocumentPage.label || '工作表'}内容`"
                      >
                        <button
                          v-for="block in activeDocumentPage.blocks || []"
                          :key="block.block_id"
                          type="button"
                          class="document-layout-block spreadsheet-cell"
                          :class="{
                            active: block.block_id === selectedDocumentBlockId,
                            edited: block.edited
                          }"
                          :style="fragmentHotspotStyle(block)"
                          :title="block.content"
                          :aria-label="`${block.locator?.cell || '单元格'}：${block.content}`"
                          role="gridcell"
                          @click="selectDocumentBlock(block)"
                        >
                          {{ block.content }}
                        </button>
                      </div>
                    </div>
                    <div
                      v-else
                      class="document-layout-canvas"
                      :style="{ aspectRatio: documentPageAspectRatio }"
                    >
                      <img
                        v-if="documentPagePreviewUrl"
                        :src="documentPagePreviewUrl"
                        :alt="`${packageDetail.title} ${activeDocumentPage?.label || ''}`"
                      />
                      <div v-else class="presentation-image-state">
                        <CircleAlert v-if="documentPagePreviewError" :size="22" />
                        <span>{{ documentPagePreviewError || '正在生成页面预览…' }}</span>
                      </div>
                      <button
                        v-for="block in activeDocumentPage?.blocks || []"
                        :key="block.block_id"
                        type="button"
                        class="document-layout-block"
                        :class="{
                          active: block.block_id === selectedDocumentBlockId,
                          edited: block.edited
                        }"
                        :style="fragmentHotspotStyle(block)"
                        :title="block.content"
                        @click="selectDocumentBlock(block)"
                      >
                        <span>{{ block.edited ? '已改' : '定位' }}</span>
                      </button>
                    </div>
                    <aside class="layout-side-panel" aria-label="版式审核信息">
                      <section class="layout-context-sidebar">
                        <header class="layout-sidebar-heading">
                          <span>审核信息</span>
                          <a-tag
                            :color="
                              statusColor(currentItem?.item_status || packageDetail.workflow_status)
                            "
                          >
                            {{
                              itemStatusLabel(currentItem?.item_status, currentItem) ||
                              workflowStatusLabel(packageDetail.workflow_status)
                            }}
                          </a-tag>
                        </header>
                        <h3 :title="packageDetail.title">{{ packageDetail.title }}</h3>
                        <p v-if="packageDetail.wiki_path" class="layout-sidebar-path">
                          {{ packageDetail.wiki_path }}
                        </p>
                        <div class="layout-sidebar-tags">
                          <span
                            class="review-type"
                            :class="`type-${String(currentItem?.review_type).toLowerCase()}`"
                          >
                            {{ reviewTypeLabel(currentItem?.review_type) }}
                          </span>
                          <span
                            v-if="currentItem?.knowledge_unit"
                            class="unit-recommendation"
                            :class="{ attention: currentItem.manual_review_required }"
                          >
                            {{
                              currentItem.manual_review_required
                                ? '需要人工确认'
                                : `建议${outcomeLabel(currentItem.recommended_outcome)}`
                            }}
                          </span>
                        </div>
                        <dl class="layout-sidebar-facts">
                          <div v-if="currentItem?.knowledge_unit">
                            <dt>当前知识单元</dt>
                            <dd>{{ currentUnitPosition }} / {{ knowledgeUnitItems.length }}</dd>
                          </div>
                          <div>
                            <dt>预览位置</dt>
                            <dd>{{ activeDocumentPage?.label || '-' }}</dd>
                          </div>
                          <div>
                            <dt>来源片段</dt>
                            <dd>{{ currentItem?.source_segment_ids?.length || 0 }} 个</dd>
                          </div>
                        </dl>
                        <p v-if="currentItem?.summary" class="layout-sidebar-summary">
                          {{ currentItem.summary }}
                        </p>
                        <button
                          v-if="currentItem?.can_reopen_exclusion"
                          type="button"
                          class="layout-sidebar-primary is-reopen"
                          :disabled="reopening"
                          @click.stop="confirmReopenExcludedItem"
                        >
                          <RotateCcw :size="14" />重新申请纳入
                        </button>
                        <button
                          v-else-if="knowledgeUnitMode"
                          type="button"
                          class="layout-sidebar-primary"
                          :disabled="!itemActionable"
                          :title="itemActionable ? '' : itemStatusLabel(currentItem?.item_status, currentItem)"
                          @click.stop="decisionPanelOpen = true"
                        >
                          <ClipboardCheck :size="14" />{{
                            itemActionable
                              ? '处理当前知识单元'
                              : itemStatusLabel(currentItem?.item_status, currentItem)
                          }}
                        </button>
                        <div
                          v-if="itemNavigationItems.length > 1"
                          class="layout-sidebar-navigation"
                        >
                          <button
                            type="button"
                            :disabled="!hasPreviousItem"
                            @click.stop="selectRelativeItem(-1)"
                          >
                            <ChevronLeft :size="13" /> 上一项
                          </button>
                          <button
                            type="button"
                            :disabled="!hasNextItem"
                            @click.stop="selectRelativeItem(1)"
                          >
                            下一项 <ChevronRight :size="13" />
                          </button>
                        </div>
                        <button
                          v-if="knowledgeUnitMode && visibleItems.length > 1"
                          type="button"
                          class="layout-sidebar-list-toggle"
                          :aria-expanded="unitListExpanded"
                          @click="unitListExpanded = !unitListExpanded"
                        >
                          {{
                            unitListExpanded
                              ? '收起知识单元列表'
                              : `查看全部知识单元（${visibleItems.length}）`
                          }}
                        </button>
                        <div
                          v-if="knowledgeUnitMode && unitListExpanded"
                          class="layout-sidebar-unit-list"
                        >
                          <button
                            v-for="(item, index) in visibleItems"
                            :key="item.review_item_id"
                            type="button"
                            :class="{
                              active: item.review_item_id === selectedItemId,
                              attention: item.manual_review_required
                            }"
                            @click="selectItem(item.review_item_id)"
                          >
                            <span>{{ index + 1 }}</span>
                            <strong>{{ item.title || reviewTypeLabel(item.review_type) }}</strong>
                            <small>{{ itemStatusLabel(item.item_status, item) }}</small>
                          </button>
                        </div>
                      </section>
                      <aside v-if="selectedDocumentBlock" class="document-layout-editor">
                        <header>
                          <div>
                            <strong>内容块编辑</strong>
                            <span>{{
                              selectedDocumentBlock.locator?.cell ||
                              selectedDocumentBlock.locator?.block ||
                              activeDocumentPage?.label
                            }}</span>
                          </div>
                          <span v-if="selectedDocumentBlock.edited" class="document-edit-badge"
                            >草稿已改</span
                          >
                        </header>
                        <textarea v-model="documentBlockDraft" rows="7" aria-label="内容块编辑" />
                        <p>这里只保存审核草稿，不会改写飞书原始资料。</p>
                        <button
                          type="button"
                          class="document-layout-save"
                          :disabled="
                            writeDisabled ||
                            !itemActionable ||
                            documentEditSaving ||
                            !documentBlockDraft.trim()
                          "
                          @click="saveDocumentBlockEdit"
                        >
                          {{ documentEditSaving ? '保存中…' : '保存此处修改' }}
                        </button>
                      </aside>
                    </aside>
                  </div>
                  <div v-if="!activeDocumentPage?.blocks?.length" class="content-notice compact">
                    <FileText :size="20" /><strong>当前页面没有可定位文字</strong>
                    <p>仍可查看原始页面；如需补充文字，请在审核意见中说明。</p>
                  </div>
                </div>
                <template v-else>
                  <div v-if="documentLayoutError" class="content-notice compact is-error">
                    <CircleAlert :size="20" /><strong>暂时无法还原资料版式</strong>
                    <p>{{ documentLayoutError }}，已回退到解析正文。</p>
                    <a-button size="small" @click="loadDocumentLayout"
                      ><RefreshCw :size="13" />重试版式还原</a-button
                    >
                  </div>
                  <div v-if="presentationError" class="content-notice compact is-error">
                    <CircleAlert :size="20" /><strong>暂时无法还原幻灯片版式</strong>
                    <p>{{ presentationError }}，已回退到解析正文。</p>
                    <a-button size="small" @click="loadPresentationLayout"
                      ><RefreshCw :size="13" />重试版式还原</a-button
                    >
                  </div>
                  <div v-if="sourceSegments.length" class="source-segment-navigation">
                    <div class="source-segment-heading">
                      <span><strong>内容定位</strong>{{ sourceSegments.length }} 个来源片段</span>
                      <button
                        v-if="selectedSourceSegment"
                        type="button"
                        @click="selectedSourceSegmentId = ''"
                      >
                        返回全文
                      </button>
                    </div>
                    <div class="source-segment-list" aria-label="来源片段定位">
                      <button
                        v-for="segment in sourceSegments"
                        :key="segment.segment_id"
                        type="button"
                        :class="{ active: segment.segment_id === selectedSourceSegmentId }"
                        :title="segmentTitle(segment)"
                        @click="selectedSourceSegmentId = segment.segment_id"
                      >
                        <span>{{ segment.segment_index + 1 }}</span
                        >{{ segmentLabel(segment) }}
                      </button>
                    </div>
                  </div>
                  <article v-if="currentItem?.knowledge_unit" class="knowledge-unit-focus">
                    <header>
                      <div>
                        <span>
                          知识单元 {{ currentUnitPosition }}/{{
                            knowledgeUnitItems.length
                          }}
                          ·
                          {{ changeTypeLabel(currentItem.change_type) }}
                        </span>
                        <strong>{{ currentItem.title }}</strong>
                      </div>
                      <small>{{ currentItem.source_segment_ids?.length || 0 }} 个来源片段</small>
                    </header>
                    <MarkdownPreview
                      compact
                      class="review-markdown"
                      :content="currentItem.content"
                    />
                    <div v-if="selectedSourceSegment" class="unit-source-locator">
                      <span>当前定位</span>
                      <strong>{{ segmentTitle(selectedSourceSegment) }}</strong>
                    </div>
                  </article>
                  <article v-else-if="selectedSourceSegment" class="source-segment-focus">
                    <header>
                      <div>
                        <span>片段 {{ selectedSourceSegment.segment_index + 1 }}</span>
                        <strong>{{ segmentTitle(selectedSourceSegment) }}</strong>
                      </div>
                      <small
                        >{{ formatNumber(selectedSourceSegment.token_count || 0) }} Tokens</small
                      >
                    </header>
                    <MarkdownPreview
                      compact
                      class="review-markdown"
                      :content="selectedSourceSegment.content"
                    />
                  </article>
                  <MarkdownPreview
                    v-else-if="reviewMarkdown && !reviewContent.error"
                    compact
                    class="review-markdown"
                    :content="reviewMarkdown"
                  />
                  <div v-else-if="!reviewContent.loading" class="content-notice">
                    <FileText :size="24" /><strong>尚未生成可预览正文</strong>
                    <p>请查看飞书原文，或返回“资料与扫描”检查解析状态。</p>
                  </div>
                </template>
              </a-spin>
            </section>

            <section
              v-else-if="activeEvidenceView === 'comparisons'"
              class="comparison-review"
              :class="{ 'is-fullscreen': comparisonFullscreen }"
            >
              <div v-if="!selectedRelations.length" class="content-notice">
                <GitCompareArrows :size="24" /><strong>当前审核项没有跨文档问题</strong>
                <p>没有发现需要人工裁决的重复、重叠或冲突证据。</p>
              </div>
              <template v-else>
                <nav class="comparison-navigator" aria-label="跨文档关系导航">
                  <div class="comparison-position">
                    <span>关系 {{ activeRelationIndex + 1 }} / {{ selectedRelations.length }}</span>
                    <strong>{{ relationLabel(activeRelation?.relation_type) }}</strong>
                  </div>
                  <div class="comparison-nav-title">
                    <span>{{ activeRelation?.source_title }}</span>
                    <ArrowRight :size="13" />
                    <span>{{ activeRelation?.target_title }}</span>
                  </div>
                  <div class="comparison-nav-actions">
                    <button
                      type="button"
                      title="上一条关系"
                      aria-label="上一条关系"
                      :disabled="selectedRelations.length <= 1"
                      @click="changeRelation(-1)"
                    >
                      <ChevronLeft :size="16" />
                    </button>
                    <button
                      type="button"
                      title="下一条关系"
                      aria-label="下一条关系"
                      :disabled="selectedRelations.length <= 1"
                      @click="changeRelation(1)"
                    >
                      <ChevronRight :size="16" />
                    </button>
                  </div>
                </nav>
                <div class="comparison-evidence-layout">
                  <div class="comparison-layout-main">
                    <a-spin :spinning="relationLayoutLoading">
                      <section
                        v-if="comparisonLayoutReady"
                        class="comparison-layout-review"
                        aria-label="跨文档版式对比"
                      >
                        <header class="comparison-layout-toolbar">
                          <div class="comparison-layout-heading">
                            <strong>版式对比</strong>
                            <span v-if="comparisonMatchCount"
                              >已定位 {{ comparisonMatchCount }} 组匹配片段</span
                            >
                            <span v-else>暂未生成可定位片段</span>
                          </div>
                          <div class="comparison-view-toolbar">
                            <label class="comparison-sync-toggle">
                              <input
                                v-model="comparisonSyncPages"
                                type="checkbox"
                                :disabled="!comparisonMatchPagesAligned"
                              />
                              <span>同步翻页</span>
                            </label>
                            <label class="comparison-sync-toggle">
                              <input
                                v-model="comparisonSyncView"
                                type="checkbox"
                                @change="syncComparisonViews"
                              />
                              <span>同步视图</span>
                            </label>
                            <div class="comparison-zoom-controls" aria-label="版式缩放控制">
                              <button
                                type="button"
                                title="缩小"
                                aria-label="缩小版式"
                                :disabled="activeComparisonView.scale <= COMPARISON_MIN_SCALE"
                                @click="changeComparisonZoom(-COMPARISON_SCALE_STEP)"
                              >
                                <ZoomOut :size="14" />
                              </button>
                              <output :aria-label="`当前缩放 ${comparisonZoomPercentage}%`">
                                {{ comparisonZoomPercentage }}%
                              </output>
                              <button
                                type="button"
                                title="放大"
                                aria-label="放大版式"
                                :disabled="activeComparisonView.scale >= COMPARISON_MAX_SCALE"
                                @click="changeComparisonZoom(COMPARISON_SCALE_STEP)"
                              >
                                <ZoomIn :size="14" />
                              </button>
                              <button
                                type="button"
                                title="恢复适配大小"
                                aria-label="恢复版式大小"
                                @click="resetComparisonView()"
                              >
                                <RotateCcw :size="13" />
                              </button>
                            </div>
                            <button
                              type="button"
                              class="comparison-fullscreen-toggle"
                              :title="comparisonFullscreen ? '退出全屏' : '全屏查看'"
                              :aria-label="comparisonFullscreen ? '退出版式全屏' : '版式全屏'"
                              :aria-pressed="comparisonFullscreen"
                              @click="toggleComparisonFullscreen"
                            >
                              <Minimize2 v-if="comparisonFullscreen" :size="15" />
                              <Maximize2 v-else :size="15" />
                            </button>
                          </div>
                        </header>
                        <div
                          v-if="relationLayoutComparison.matches?.length"
                          class="comparison-match-panel"
                          aria-label="匹配片段导航"
                        >
                          <div class="comparison-match-strip">
                            <button
                              v-for="(match, index) in relationLayoutComparison.matches"
                              :key="match.match_id"
                              type="button"
                              :class="{ active: match.match_id === selectedComparisonMatchId }"
                              @click="selectComparisonMatch(match)"
                            >
                              <span>{{ index + 1 }}</span>
                              <strong>匹配片段 {{ index + 1 }}</strong>
                              <small
                                >{{ percentage(match.similarity) }} ·
                                {{ comparisonMatchLocator(match) }}</small
                              >
                            </button>
                          </div>
                        </div>
                        <p v-else class="comparison-layout-fallback">
                          <CircleAlert :size="15" />{{
                            relationLayoutComparison.message || '当前仅提供文字证据。'
                          }}
                        </p>
                        <div class="comparison-layout-columns">
                          <section class="comparison-layout-pane">
                            <header>
                              <div>
                                <strong>{{ relationLayoutComparison.source.title }}</strong>
                                <span
                                  >版本 {{ relationLayoutComparison.source.revision || '-' }} · 第
                                  {{ comparisonPageNumbers.source }} /
                                  {{ comparisonSourcePages.length }} 页</span
                                >
                              </div>
                              <div class="comparison-page-actions">
                                <button
                                  type="button"
                                  aria-label="来源一上一页"
                                  :disabled="comparisonPageNumbers.source <= 1"
                                  @click="changeComparisonPage('source', -1)"
                                >
                                  <ChevronLeft :size="14" />
                                </button>
                                <button
                                  type="button"
                                  aria-label="来源一下一页"
                                  :disabled="
                                    comparisonPageNumbers.source >= comparisonSourcePages.length
                                  "
                                  @click="changeComparisonPage('source', 1)"
                                >
                                  <ChevronRight :size="14" />
                                </button>
                              </div>
                            </header>
                            <div
                              class="comparison-layout-stage"
                              :class="{
                                'has-grid': activeComparisonSourcePage?.render_mode === 'grid',
                                'has-markdown':
                                  activeComparisonSourcePage?.render_mode === 'markdown'
                              }"
                            >
                              <div
                                v-if="activeComparisonSourceGridBlock"
                                class="comparison-grid-cell-detail"
                              >
                                <div class="comparison-grid-cell-summary">
                                  <span>匹配单元格</span>
                                  <strong>{{ comparisonGridMatchCells('source') }}</strong>
                                </div>
                                <div class="comparison-grid-cell-current">
                                  <strong>{{
                                    activeComparisonSourceGridBlock.locator?.cell
                                  }}</strong>
                                  <span>{{ activeComparisonSourceGridBlock.content }}</span>
                                </div>
                              </div>
                              <div
                                :ref="(element) => setComparisonCanvasRef('source', element)"
                                class="comparison-layout-canvas"
                                :class="{
                                  'is-grid': activeComparisonSourcePage?.render_mode === 'grid',
                                  'is-markdown':
                                    activeComparisonSourcePage?.render_mode === 'markdown',
                                  'is-pannable':
                                    !['grid', 'markdown'].includes(
                                      activeComparisonSourcePage?.render_mode
                                    ) &&
                                    comparisonViews.source.scale > COMPARISON_MIN_SCALE
                                }"
                                :style="comparisonCanvasStyle(activeComparisonSourcePage)"
                                @pointerenter="activeComparisonViewSide = 'source'"
                                @pointerdown="startComparisonPan('source', $event)"
                                @pointermove="moveComparisonPan($event)"
                                @pointerup="endComparisonPan($event)"
                                @pointercancel="endComparisonPan($event)"
                                @wheel="zoomComparisonFromWheel('source', $event)"
                              >
                                <div
                                  class="comparison-layout-content"
                                  :class="{
                                    'is-grid': activeComparisonSourcePage?.render_mode === 'grid',
                                    'is-markdown':
                                      activeComparisonSourcePage?.render_mode === 'markdown'
                                  }"
                                  :style="comparisonViewStyle('source', activeComparisonSourcePage)"
                                >
                                  <div
                                    v-if="activeComparisonSourcePage?.render_mode === 'grid'"
                                    class="comparison-layout-grid-hint"
                                  >
                                    表格版式
                                  </div>
                                  <div
                                    v-else-if="
                                      activeComparisonSourcePage?.render_mode === 'markdown'
                                    "
                                    class="comparison-markdown-document"
                                  >
                                    <article
                                      v-for="block in activeComparisonSourcePage?.blocks || []"
                                      :key="`source-markdown-${block.block_id}`"
                                      class="comparison-markdown-block"
                                      :class="comparisonBlockClass('source', block)"
                                      role="button"
                                      tabindex="0"
                                      @click="selectComparisonBlock('source', block)"
                                      @keydown.enter.prevent="selectComparisonBlock('source', block)"
                                      @keydown.space.prevent="selectComparisonBlock('source', block)"
                                    >
                                      <span
                                        v-if="
                                          comparisonBlockClass('source', block)[
                                            'comparison-block-match'
                                          ]
                                        "
                                        class="comparison-markdown-block-number"
                                        >{{ comparisonBlockMarker('source', block) }}</span
                                      >
                                      <MarkdownPreview :content="block.content" />
                                    </article>
                                    <div
                                      v-if="!activeComparisonSourcePage?.blocks?.length"
                                      class="comparison-layout-state"
                                    >
                                      <CircleAlert :size="18" /><span>Markdown 正文为空</span>
                                    </div>
                                  </div>
                                  <img
                                    v-else-if="comparisonPagePreviewUrls.source"
                                    :src="comparisonPagePreviewUrls.source"
                                    :alt="`${relationLayoutComparison.source.title} 第 ${comparisonPageNumbers.source} 页`"
                                    draggable="false"
                                  />
                                  <div v-else class="comparison-layout-state">
                                    <CircleAlert :size="18" /><span>{{
                                      comparisonPagePreviewErrors.source || '正在生成页面预览…'
                                    }}</span>
                                  </div>
                                  <template
                                    v-if="activeComparisonSourcePage?.render_mode !== 'markdown'"
                                  >
                                    <button
                                      v-for="block in activeComparisonSourcePage?.blocks || []"
                                      :key="`source-${block.block_id}`"
                                      type="button"
                                      class="comparison-layout-block"
                                      :class="comparisonBlockClass('source', block)"
                                      :style="comparisonBlockStyle(block)"
                                      :title="block.content"
                                      @click="selectComparisonBlock('source', block)"
                                    >
                                      <span
                                        v-if="
                                          comparisonBlockClass('source', block)[
                                            'comparison-block-grid'
                                          ]
                                        "
                                        class="comparison-block-content"
                                        >{{ block.content }}</span
                                      ><span
                                        v-if="
                                          comparisonBlockClass('source', block)[
                                            'comparison-block-grid'
                                          ] &&
                                          comparisonBlockClass('source', block)[
                                            'comparison-block-match'
                                          ]
                                        "
                                        class="comparison-layout-block-number"
                                        >{{ comparisonBlockMarker('source', block) }}</span
                                      ><span
                                        v-else-if="
                                          comparisonBlockClass('source', block)[
                                            'comparison-block-match'
                                          ]
                                        "
                                        class="comparison-layout-block-number"
                                        >{{ comparisonBlockMarker('source', block) }}</span
                                      >
                                    </button>
                                  </template>
                                </div>
                              </div>
                            </div>
                          </section>
                          <section class="comparison-layout-pane">
                            <header>
                              <div>
                                <strong>{{ relationLayoutComparison.target.title }}</strong>
                                <span
                                  >版本 {{ relationLayoutComparison.target.revision || '-' }} · 第
                                  {{ comparisonPageNumbers.target }} /
                                  {{ comparisonTargetPages.length }} 页</span
                                >
                              </div>
                              <div class="comparison-page-actions">
                                <button
                                  type="button"
                                  aria-label="来源二上一页"
                                  :disabled="comparisonPageNumbers.target <= 1"
                                  @click="changeComparisonPage('target', -1)"
                                >
                                  <ChevronLeft :size="14" />
                                </button>
                                <button
                                  type="button"
                                  aria-label="来源二下一页"
                                  :disabled="
                                    comparisonPageNumbers.target >= comparisonTargetPages.length
                                  "
                                  @click="changeComparisonPage('target', 1)"
                                >
                                  <ChevronRight :size="14" />
                                </button>
                              </div>
                            </header>
                            <div
                              class="comparison-layout-stage"
                              :class="{
                                'has-grid': activeComparisonTargetPage?.render_mode === 'grid',
                                'has-markdown':
                                  activeComparisonTargetPage?.render_mode === 'markdown'
                              }"
                            >
                              <div
                                v-if="activeComparisonTargetGridBlock"
                                class="comparison-grid-cell-detail"
                              >
                                <div class="comparison-grid-cell-summary">
                                  <span>匹配单元格</span>
                                  <strong>{{ comparisonGridMatchCells('target') }}</strong>
                                </div>
                                <div class="comparison-grid-cell-current">
                                  <strong>{{
                                    activeComparisonTargetGridBlock.locator?.cell
                                  }}</strong>
                                  <span>{{ activeComparisonTargetGridBlock.content }}</span>
                                </div>
                              </div>
                              <div
                                :ref="(element) => setComparisonCanvasRef('target', element)"
                                class="comparison-layout-canvas"
                                :class="{
                                  'is-grid': activeComparisonTargetPage?.render_mode === 'grid',
                                  'is-markdown':
                                    activeComparisonTargetPage?.render_mode === 'markdown',
                                  'is-pannable':
                                    !['grid', 'markdown'].includes(
                                      activeComparisonTargetPage?.render_mode
                                    ) &&
                                    comparisonViews.target.scale > COMPARISON_MIN_SCALE
                                }"
                                :style="comparisonCanvasStyle(activeComparisonTargetPage)"
                                @pointerenter="activeComparisonViewSide = 'target'"
                                @pointerdown="startComparisonPan('target', $event)"
                                @pointermove="moveComparisonPan($event)"
                                @pointerup="endComparisonPan($event)"
                                @pointercancel="endComparisonPan($event)"
                                @wheel="zoomComparisonFromWheel('target', $event)"
                              >
                                <div
                                  class="comparison-layout-content"
                                  :class="{
                                    'is-grid': activeComparisonTargetPage?.render_mode === 'grid',
                                    'is-markdown':
                                      activeComparisonTargetPage?.render_mode === 'markdown'
                                  }"
                                  :style="comparisonViewStyle('target', activeComparisonTargetPage)"
                                >
                                  <div
                                    v-if="activeComparisonTargetPage?.render_mode === 'grid'"
                                    class="comparison-layout-grid-hint"
                                  >
                                    表格版式
                                  </div>
                                  <div
                                    v-else-if="
                                      activeComparisonTargetPage?.render_mode === 'markdown'
                                    "
                                    class="comparison-markdown-document"
                                  >
                                    <article
                                      v-for="block in activeComparisonTargetPage?.blocks || []"
                                      :key="`target-markdown-${block.block_id}`"
                                      class="comparison-markdown-block"
                                      :class="comparisonBlockClass('target', block)"
                                      role="button"
                                      tabindex="0"
                                      @click="selectComparisonBlock('target', block)"
                                      @keydown.enter.prevent="selectComparisonBlock('target', block)"
                                      @keydown.space.prevent="selectComparisonBlock('target', block)"
                                    >
                                      <span
                                        v-if="
                                          comparisonBlockClass('target', block)[
                                            'comparison-block-match'
                                          ]
                                        "
                                        class="comparison-markdown-block-number"
                                        >{{ comparisonBlockMarker('target', block) }}</span
                                      >
                                      <MarkdownPreview :content="block.content" />
                                    </article>
                                    <div
                                      v-if="!activeComparisonTargetPage?.blocks?.length"
                                      class="comparison-layout-state"
                                    >
                                      <CircleAlert :size="18" /><span>Markdown 正文为空</span>
                                    </div>
                                  </div>
                                  <img
                                    v-else-if="comparisonPagePreviewUrls.target"
                                    :src="comparisonPagePreviewUrls.target"
                                    :alt="`${relationLayoutComparison.target.title} 第 ${comparisonPageNumbers.target} 页`"
                                    draggable="false"
                                  />
                                  <div v-else class="comparison-layout-state">
                                    <CircleAlert :size="18" /><span>{{
                                      comparisonPagePreviewErrors.target || '正在生成页面预览…'
                                    }}</span>
                                  </div>
                                  <template
                                    v-if="activeComparisonTargetPage?.render_mode !== 'markdown'"
                                  >
                                    <button
                                      v-for="block in activeComparisonTargetPage?.blocks || []"
                                      :key="`target-${block.block_id}`"
                                      type="button"
                                      class="comparison-layout-block"
                                      :class="comparisonBlockClass('target', block)"
                                      :style="comparisonBlockStyle(block)"
                                      :title="block.content"
                                      @click="selectComparisonBlock('target', block)"
                                    >
                                      <span
                                        v-if="
                                          comparisonBlockClass('target', block)[
                                            'comparison-block-grid'
                                          ]
                                        "
                                        class="comparison-block-content"
                                        >{{ block.content }}</span
                                      ><span
                                        v-if="
                                          comparisonBlockClass('target', block)[
                                            'comparison-block-grid'
                                          ] &&
                                          comparisonBlockClass('target', block)[
                                            'comparison-block-match'
                                          ]
                                        "
                                        class="comparison-layout-block-number"
                                        >{{ comparisonBlockMarker('target', block) }}</span
                                      ><span
                                        v-else-if="
                                          comparisonBlockClass('target', block)[
                                            'comparison-block-match'
                                          ]
                                        "
                                        class="comparison-layout-block-number"
                                        >{{ comparisonBlockMarker('target', block) }}</span
                                      >
                                    </button>
                                  </template>
                                </div>
                              </div>
                            </div>
                          </section>
                        </div>
                      </section>
                      <div v-else-if="relationLayoutError" class="content-notice compact is-error">
                        <CircleAlert :size="20" /><strong>版式对比暂不可用</strong>
                        <p>{{ relationLayoutError }}，已保留文字证据。</p>
                      </div>
                    </a-spin>
                  </div>
                  <article
                    v-if="activeRelation"
                    class="comparison-card"
                    :class="comparisonClass(activeRelation.relation_type)"
                  >
                    <header class="comparison-card-header">
                      <div>
                        <a-tag :color="relationColor(activeRelation.relation_type)">{{
                          relationLabel(activeRelation.relation_type)
                        }}</a-tag
                        ><strong
                          >{{ activeRelation.source_title }} ↔
                          {{ activeRelation.target_title }}</strong
                        >
                      </div>
                      <span>置信度 {{ percentage(activeRelation.confidence) }}</span>
                    </header>
                    <div
                      v-if="activeRelation.same_content?.length"
                      class="evidence-block evidence-same"
                    >
                      <strong>相同内容</strong>
                      <ul>
                        <li
                          v-for="(fact, index) in activeRelation.same_content"
                          :key="`same-${index}`"
                        >
                          {{ formatFactValue(fact) }}
                        </li>
                      </ul>
                    </div>
                    <div
                      v-if="activeRelation.different_content?.length"
                      class="evidence-block evidence-diff"
                    >
                      <strong>差异内容</strong>
                      <div
                        v-for="(fact, index) in activeRelation.different_content"
                        :key="`diff-${index}`"
                        class="difference-row"
                      >
                        <span>{{ fact.field || '内容差异' }}</span>
                        <p><b>来源一</b>{{ fact.current ?? formatFactValue(fact) }}</p>
                        <p><b>来源二</b>{{ fact.candidate ?? '-' }}</p>
                      </div>
                    </div>
                    <p v-if="activeRelation.reasoning" class="comparison-reasoning">
                      <span>判断理由</span>{{ activeRelation.reasoning }}
                    </p>
                    <section
                      v-if="duplicateRelation(activeRelation)"
                      class="duplicate-governance"
                      aria-label="重复片段处理"
                    >
                      <div class="duplicate-governance-heading">
                        <div>
                          <Link2 :size="14" />
                          <span>
                            <strong>文字证据</strong>
                            <template v-if="activeDuplicateMatch">
                              当前对应上方的匹配片段 {{ activeDuplicateMatchIndex + 1 }} /
                              {{ activeDuplicateMatches.length }}
                            </template>
                            <template v-else>用于核对版式高亮的具体正文</template>
                          </span>
                        </div>
                        <a-button
                          v-if="!duplicateCandidates[activeRelation.relation_id]"
                          size="small"
                          :loading="duplicateLoading[activeRelation.relation_id]"
                          @click="loadDuplicateCandidates(activeRelation)"
                          >{{
                            duplicateLoading[activeRelation.relation_id]
                              ? '正在加载文字证据'
                              : '加载文字证据'
                          }}</a-button
                        >
                      </div>
                      <a-spin :spinning="duplicateLoading[activeRelation.relation_id]">
                        <template v-if="duplicateCandidates[activeRelation.relation_id]">
                          <div
                            v-if="duplicateCandidates[activeRelation.relation_id].decision"
                            class="duplicate-decision-result"
                          >
                            <CircleCheck :size="15" />
                            <div>
                              <strong>{{
                                duplicateDecisionTitle(
                                  duplicateCandidates[activeRelation.relation_id]
                                )
                              }}</strong>
                              <span>{{
                                duplicateResolutionSummary(
                                  duplicateCandidates[activeRelation.relation_id]
                                )
                              }}</span>
                            </div>
                          </div>
                          <template v-else-if="activeDuplicateMatch">
                            <div class="duplicate-match-list">
                              <article :key="activeDuplicateMatch.match_id" class="duplicate-match">
                                <header>
                                  <span
                                    >匹配片段 {{ activeDuplicateMatchIndex + 1 }} /
                                    {{ activeDuplicateMatches.length }}</span
                                  >
                                  <b>相似度 {{ percentage(activeDuplicateMatch.similarity) }}</b>
                                </header>
                                <div>
                                  <section>
                                    <label
                                      >来源一 · 重叠部分
                                      <span
                                        v-if="
                                          formatSegmentLocator(activeDuplicateMatch.source_locator)
                                        "
                                        >{{
                                          formatSegmentLocator(activeDuplicateMatch.source_locator)
                                        }}</span
                                      ></label
                                    >
                                    <p class="overlap-snippet">
                                      {{
                                        activeDuplicateMatch.source_overlap_excerpt ||
                                        activeDuplicateMatch.source_excerpt
                                      }}
                                    </p>
                                  </section>
                                  <section>
                                    <label
                                      >来源二 · 重叠部分
                                      <span
                                        v-if="
                                          formatSegmentLocator(activeDuplicateMatch.target_locator)
                                        "
                                        >{{
                                          formatSegmentLocator(activeDuplicateMatch.target_locator)
                                        }}</span
                                      ></label
                                    >
                                    <p class="overlap-snippet">
                                      {{
                                        activeDuplicateMatch.target_overlap_excerpt ||
                                        activeDuplicateMatch.target_excerpt
                                      }}
                                    </p>
                                  </section>
                                </div>
                              </article>
                            </div>
                            <div class="duplicate-actions">
                              <a-tooltip
                                :title="duplicateActionHelp(activeDuplicateMatches.length)"
                                placement="top"
                              >
                                <button
                                  type="button"
                                  class="unit-visibility duplicate-action-help"
                                  :aria-label="duplicateActionHelp(activeDuplicateMatches.length)"
                                >
                                  ?
                                </button>
                              </a-tooltip>
                              <div>
                                <a-button
                                  size="small"
                                  :loading="duplicateResolving === activeRelation.relation_id"
                                  :disabled="writeDisabled"
                                  @click="confirmDuplicateResolution(activeRelation, 'USE_SOURCE')"
                                  >保留来源一</a-button
                                >
                                <a-button
                                  size="small"
                                  :loading="duplicateResolving === activeRelation.relation_id"
                                  :disabled="writeDisabled"
                                  @click="confirmDuplicateResolution(activeRelation, 'USE_TARGET')"
                                  >保留来源二</a-button
                                >
                                <a-button
                                  size="small"
                                  :loading="duplicateResolving === activeRelation.relation_id"
                                  :disabled="writeDisabled"
                                  @click="
                                    confirmDuplicateResolution(activeRelation, 'KEEP_SEPARATE')
                                  "
                                  >分别保留</a-button
                                >
                              </div>
                            </div>
                          </template>
                          <div v-else class="duplicate-empty">
                            没有提取到可核对的重叠正文，本条不能作为内容重叠依据，请重新运行跨文档检查。
                          </div>
                        </template>
                      </a-spin>
                    </section>
                  </article>
                </div>
              </template>
            </section>

            <section v-else class="history-review">
              <div v-if="auditRecords.length" class="audit-record-list">
                <article
                  v-for="(record, index) in auditRecords"
                  :key="record.id || `${record.record_type}-${record.event_type}-${record.created_at}-${index}`"
                  class="audit-record"
                >
                  <div class="audit-record-heading">
                    <a-tag :color="auditRecordCategoryColor(record.category)">
                      {{ auditRecordCategoryLabel(record.category) }}
                    </a-tag>
                    <strong>{{ auditRecordTitle(record) }}</strong>
                    <a-tag
                      v-if="record.record_type === 'CHANGE_REQUEST'"
                      :color="changeRequestColor(auditRecordRequest(record).status)"
                    >
                      {{ changeRequestStatusLabel(auditRecordRequest(record).status) }}
                    </a-tag>
                    <span>{{ formatTime(record.created_at) }}</span>
                  </div>
                  <p class="audit-record-meta">
                    {{ record.scope === 'related' ? '关联资料' : '当前资料' }}
                    <template v-if="record.material?.title">
                      · {{ record.material.title }}
                    </template>
                    <template v-if="record.material?.revision">
                      · 版本 {{ record.material.revision }}
                    </template>
                    ·
                    {{ record.operator_id || '系统' }}
                  </p>
                  <p v-if="record.material?.path" class="audit-record-material">
                    位置：{{ record.material.path }}
                  </p>
                  <p v-if="record.from_status || record.to_status" class="audit-record-status">
                    状态：{{ auditStatusLabel(record.from_status) }} → {{ auditStatusLabel(record.to_status) }}
                  </p>
                  <small v-if="record.message">{{ record.message }}</small>
                  <small v-if="auditRecordDetail(record)" class="audit-record-detail">
                    {{ auditRecordDetail(record) }}
                  </small>
                  <a-button
                    v-if="record.record_type === 'CHANGE_REQUEST' && ['OPEN', 'NEW_VERSION_RECEIVED'].includes(auditRecordRequest(record).status)"
                    size="small"
                    danger
                    class="cancel-change-request"
                    :disabled="writeDisabled"
                    @click="confirmCancelChangeRequest(auditRecordRequest(record))"
                    >取消修改任务</a-button
                  >
                </article>
              </div>
              <div v-else class="content-notice compact">
                <History :size="22" /><strong>暂无业务处理记录</strong>
              </div>
            </section>
          </div>
        </section>

        <aside
          class="decision-panel"
          :class="{ open: decisionPanelOpen }"
          :aria-hidden="!decisionPanelOpen"
          aria-label="审核处理"
        >
          <template v-if="currentItem">
            <div class="decision-heading">
              <div>
                <h3>
                  {{
                    knowledgeUnitMode
                      ? '处理当前知识单元'
                      : `${reviewTypeLabel(currentItem.review_type)}处理`
                  }}
                </h3>
                <p>
                  {{
                    knowledgeUnitMode
                      ? `当前为知识单元 ${currentUnitPosition} / ${knowledgeUnitItems.length}，提交后只会处理这一项。`
                      : currentItem.review_type === 'STALE'
                        ? '内容未变化，请确认是否继续有效。'
                        : '选择业务结果，系统自动执行对应的知识处理动作。'
                  }}
                </p>
              </div>
              <div class="decision-heading-actions">
                <a-button
                  v-if="itemActionable"
                  size="small"
                  :disabled="writeDisabled || !itemActionable"
                  @click="transferOpen = !transferOpen"
                  ><UserRoundCog :size="14" />转交</a-button
                >
                <button
                  type="button"
                  class="icon-tool"
                  title="关闭审核处理"
                  aria-label="关闭审核处理"
                  @click="decisionPanelOpen = false"
                >
                  <X :size="17" />
                </button>
              </div>
            </div>
            <div v-if="transferOpen && itemActionable" class="transfer-box">
              <label>转交给知识管理员</label
              ><a-select
                v-model:value="transferForm.assignee_id"
                class="field-control"
                placeholder="选择知识管理员"
                :options="reviewerOptions"
                :disabled="writeDisabled"
              /><a-textarea
                v-model:value="transferForm.comment"
                :rows="2"
                placeholder="说明转交原因"
                :disabled="writeDisabled"
              />
              <div>
                <a-button size="small" @click="transferOpen = false">取消</a-button
                ><a-button
                  size="small"
                  type="primary"
                  :loading="transferring"
                  :disabled="writeDisabled"
                  @click="transferPackage"
                  >确认转交</a-button
                >
              </div>
            </div>
            <div v-if="!itemActionable" class="decision-readonly">
              <CircleCheck :size="20" /><strong>{{
                itemStatusLabel(currentItem.item_status, currentItem)
              }}</strong>
              <p>{{ currentItem.decision_comment || '当前审核项无需继续处理。' }}</p>
            </div>
            <template v-else>
              <div class="outcome-list">
                <button
                  v-for="outcome in outcomeOptions"
                  :key="outcome.value"
                  type="button"
                  :class="{ active: form.outcome === outcome.value }"
                  :disabled="
                    writeDisabled || (publishOutcome(outcome.value) && publishUnavailable)
                  "
                  @click="form.outcome = outcome.value"
                >
                  <span>{{ outcome.label }}</span
                  ><small>{{ outcome.description }}</small>
                </button>
              </div>
              <div class="field">
                <div class="field-label-row">
                  <label>{{ problemTagLabel }}</label>
                  <a-tooltip :title="problemTagHelp" placement="topLeft">
                    <button type="button" class="problem-help" :aria-label="problemTagHelp">
                      ?
                    </button>
                  </a-tooltip>
                </div>
                <div class="problem-tags">
                  <button
                    v-for="tag in allProblemTags"
                    :key="tag.value"
                    type="button"
                    class="problem-tag"
                    :class="{ active: form.problem_tags.includes(tag.value) }"
                    :disabled="writeDisabled"
                    @click="toggleProblemTag(tag.value)"
                  >
                    {{ tag.label }}
                  </button>
                </div>
              </div>
              <div v-if="sourceChangeOutcome" class="field source-owner-field">
                <label>资料责任人</label
                ><a-input
                  v-model:value="form.responsible_user_name"
                  placeholder="填写飞书原文修改负责人"
                  :disabled="writeDisabled"
                />
                <p>后台不会修改飞书正文；责任人完成修改后，由下一次扫描自动重新打开审核。</p>
              </div>
              <div v-if="form.outcome === 'SPLIT_SCOPE'" class="field applicability-scope-field">
                <label>适用范围</label>
                <p class="field-help">填写当前版本适用的范围；另一侧知识会保留，检索时按范围区分。</p>
                <div class="scope-grid">
                  <a-input
                    v-for="field in scopeFields"
                    :key="field.key"
                    v-model:value="form.applicability_scope[field.key]"
                    :placeholder="field.placeholder"
                    :aria-label="field.label"
                    :disabled="writeDisabled"
                  />
                </div>
              </div>
              <div class="field">
                <label>{{ decisionCommentLabel }}</label
                ><a-textarea
                  v-model:value="form.decision_comment"
                  :rows="4"
                  :placeholder="decisionCommentPlaceholder"
                  :disabled="writeDisabled"
                />
              </div>
              <div class="decision-footer">
                <a-button
                  size="small"
                  :loading="savingDraft"
                  :disabled="writeDisabled"
                  @click="saveDraft"
                  >保存草稿</a-button
                ><a-button
                  type="primary"
                  size="small"
                  :loading="resolving"
                  :disabled="
                    writeDisabled ||
                    !form.outcome ||
                    (publishOutcome(form.outcome) && publishUnavailable)
                  "
                  @click="resolveItem"
                  >提交审核结果</a-button
                >
              </div>
            </template>
          </template>
        </aside>
      </div>
    </article>
    <div v-else class="detail-empty">
      <a-spin v-if="loadingDetail" /><a-empty v-else description="从左侧选择一个审核包" />
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { diffLines } from 'diff'
import {
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  ClipboardCheck,
  ExternalLink,
  FileText,
  GitCompareArrows,
  History,
  Link2,
  Maximize2,
  Minimize2,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  RotateCcw,
  UserRoundCog,
  X,
  ZoomIn,
  ZoomOut
} from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'
import { documentApi } from '@/apis/knowledge_api'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { mergeChunks } from '@/utils/chunkUtils'

const props = defineProps({
  sourceId: { type: String, default: '' },
  targetReviewId: { type: [String, Object], default: '' },
  writeDisabled: { type: Boolean, default: false }
})
const emit = defineEmits(['count-change', 'target-consumed', 'knowledge-change'])
const packages = ref([])
const packageResponse = ref({ total: 0, counts: {} })
const packagePage = ref(1)
const loadingMorePackages = ref(false)
const packageDetail = ref(null)
const reviewers = ref([])
const selectedPackageId = ref('')
const selectedItemId = ref('')
const queueView = ref('mine')
const completionResultFilter = ref('all')
const reviewTypeFilter = ref('')
const problemFilter = ref('')
const activeEvidenceView = ref('content')
const queueCollapsed = ref(false)
const decisionPanelOpen = ref(false)
const unitView = ref('pending')
const unitListExpanded = ref(false)
const selectedRelationId = ref('')
const loadingPackages = ref(false)
const loadingDetail = ref(false)
const resolving = ref(false)
const batchResolving = ref(false)
const savingDraft = ref(false)
const transferring = ref(false)
const reopening = ref(false)
const transferOpen = ref(false)
const duplicateCandidates = reactive({})
const duplicateLoading = reactive({})
const duplicateResolving = ref('')
const relationLayoutComparison = ref(null)
const relationLayoutLoading = ref(false)
const relationLayoutError = ref('')
const comparisonPageNumbers = reactive({ source: 1, target: 1 })
const comparisonPagePreviewUrls = reactive({ source: '', target: '' })
const comparisonPagePreviewLoading = reactive({ source: false, target: false })
const comparisonPagePreviewErrors = reactive({ source: '', target: '' })
const comparisonPagePreviewCache = new Map()
const comparisonPagePreviewRequests = new Map()
const comparisonSyncPages = ref(true)
const comparisonSyncView = ref(true)
const comparisonFullscreen = ref(false)
const activeComparisonViewSide = ref('source')
const comparisonViews = reactive({
  source: { scale: 1, x: 0, y: 0 },
  target: { scale: 1, x: 0, y: 0 }
})
const comparisonGridBlockIds = reactive({ source: '', target: '' })
const comparisonCanvasRefs = { source: null, target: null }
const comparisonPan = {
  side: '',
  pointerId: null,
  startX: 0,
  startY: 0,
  originX: 0,
  originY: 0
}
const selectedComparisonMatchId = ref('')
const reviewContent = ref(emptyReviewContent())
const previousReviewContent = ref(emptyReviewContent())
const sourceSegments = ref([])
const sourceSegmentsLoading = ref(false)
const selectedSourceSegmentId = ref('')
const presentationLayout = ref(null)
const presentationLoading = ref(false)
const presentationError = ref('')
const activeSlideNumber = ref(1)
const selectedPresentationFragmentId = ref('')
const activeSlidePreviewUrl = ref('')
const slidePreviewLoading = ref(false)
const slidePreviewError = ref('')
const slidePreviewCache = new Map()
const slidePreviewRequests = new Map()
const SLIDE_PREVIEW_CACHE_LIMIT = 12
const documentLayout = ref(null)
const documentLayoutLoading = ref(false)
const documentLayoutError = ref('')
const activeDocumentPageNumber = ref(1)
const selectedDocumentBlockId = ref('')
const documentBlockDraft = ref('')
const documentPagePreviewUrl = ref('')
const documentPagePreviewLoading = ref(false)
const documentPagePreviewError = ref('')
const documentPagePreviewCache = new Map()
const documentPagePreviewRequests = new Map()
const documentEditSaving = ref(false)
const DOCUMENT_PAGE_PREVIEW_CACHE_LIMIT = 12
const COMPARISON_PAGE_PREVIEW_CACHE_LIMIT = 24
const COMPARISON_MIN_SCALE = 1
const COMPARISON_MAX_SCALE = 3
const COMPARISON_SCALE_STEP = 0.25
let slidePreviewCacheEpoch = 0
let slidePreviewRequestSeq = 0
let documentPagePreviewRequestSeq = 0
const comparisonPagePreviewRequestSeq = { source: 0, target: 0 }
let detailRequestSeq = 0
let contentRequestSeq = 0
let previousContentRequestSeq = 0
const form = reactive({
  outcome: '',
  problem_tags: [],
  decision_comment: '',
  responsible_user_name: '',
  applicability_scope: {}
})
const transferForm = reactive({ assignee_id: undefined, comment: '' })

const scopeFields = [
  { key: 'industry', label: '行业', placeholder: '适用行业' },
  { key: 'product', label: '产品', placeholder: '适用产品' },
  { key: 'product_version', label: '产品版本', placeholder: '产品版本' },
  { key: 'deployment_mode', label: '部署方式', placeholder: '部署方式' },
  { key: 'customer_type', label: '客户类型', placeholder: '客户类型' },
  { key: 'region_language', label: '地区/语言', placeholder: '地区或语言' }
]

const queueViews = [
  { value: 'mine', label: '待我处理', countKey: 'mine' },
  { value: 'waiting_source', label: '等资料', countKey: 'waiting_source_change' },
  { value: 'transferred', label: '已转交' },
  { value: 'completed', label: '已完成', countKey: 'completed' }
]
const completionResultOptions = [
  { value: 'all', label: '全部' },
  { value: 'all_included', label: '全部纳入' },
  { value: 'partial', label: '部分纳入' },
  { value: 'all_excluded', label: '全部不纳入' },
  { value: 'all_duplicate', label: '全部为重复来源' }
]
const reviewTypeOptions = [
  { label: '全部审核类型', value: '' },
  { label: '新增知识', value: 'NEW' },
  { label: '知识变更', value: 'UPDATE' },
  { label: '冲突裁决', value: 'CONFLICT' },
  { label: '确认现有知识', value: 'STALE' }
]
const allProblemTags = [
  { value: 'CONFLICT', label: '内容冲突' },
  { value: 'DUPLICATE', label: '完全重复' },
  { value: 'OVERLAP', label: '内容重叠' },
  { value: 'INSUFFICIENT_EVIDENCE', label: '证据不足' },
  { value: 'OUTDATED', label: '内容过期' }
]
const problemOptions = [{ label: '全部问题', value: '' }, ...allProblemTags]
const outcomeCopy = {
  PUBLISH: ['发布', '创建为正式知识'],
  REQUEST_SOURCE_CHANGE: ['退回飞书修改', '原文修改后自动重新审核'],
  EXCLUDE: ['不纳入知识库', '保留来源记录但不发布'],
  ADOPT_NEW_VERSION: ['采用新版', '发布后替换当前版本'],
  KEEP_CURRENT: ['保留当前版本', '本次候选不发布'],
  SPLIT_SCOPE: ['按适用范围拆分', '保留两侧知识并按适用范围区分'],
  WAIT_BUSINESS_CONFIRMATION: ['等待业务确认', '暂缓裁决并保留任务'],
  CONFIRM_VALID: ['确认仍有效', '继续保留当前正式知识'],
  REQUEST_SUPPORTING_SOURCE: ['补充来源', '补充可靠资料后重新判断'],
  ARCHIVE: ['归档知识', '从正式索引中移除'],
  DISMISS: ['关闭复核', '不是知识问题，无需调整']
}
const commentRequiredOutcomes = new Set([
  'REQUEST_SOURCE_CHANGE',
  'REQUEST_SUPPORTING_SOURCE',
  'WAIT_BUSINESS_CONFIRMATION'
])

const currentItem = computed(() =>
  packageDetail.value?.items.find((item) => item.review_item_id === selectedItemId.value)
)
const sourceUpdateCount = computed(() => Number(packageResponse.value.counts?.source_updates || 0))
const showSourceUpdateNotice = computed(
  () => sourceUpdateCount.value > 0 && reviewTypeFilter.value !== 'UPDATE'
)
const knowledgeUnitItems = computed(() =>
  (packageDetail.value?.items || []).filter((item) => item.knowledge_unit)
)
const knowledgeUnitMode = computed(() =>
  Boolean(packageDetail.value?.knowledge_unit_count || knowledgeUnitItems.value.length)
)
const unitViewOptions = [
  { value: 'pending', label: '待处理' },
  { value: 'decided', label: '已处理' },
  { value: 'all', label: '全部' }
]
const visibleItems = computed(() => {
  const items = packageDetail.value?.items || []
  if (!knowledgeUnitMode.value) return items
  const units = knowledgeUnitItems.value
  if (unitView.value === 'all') return units
  if (unitView.value === 'decided') {
    return units.filter((item) =>
      ['DECIDED', 'SOURCE_UPDATED', 'INVALIDATED'].includes(item.item_status)
    )
  }
  return units.filter((item) =>
    ['PENDING', 'WAITING_SOURCE_CHANGE', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
  )
})
// The selected unit can temporarily fall outside the active status filter (for
// example, immediately after deciding it). Keep the pager usable by falling
// back to the complete unit list in that case.
const itemNavigationItems = computed(() => {
  if (!knowledgeUnitMode.value) return visibleItems.value
  if (visibleItems.value.some((item) => item.review_item_id === selectedItemId.value)) {
    return visibleItems.value
  }
  return (packageDetail.value?.items || []).filter((item) => item.knowledge_unit)
})
const currentUnitPosition = computed(() => {
  if (!currentItem.value?.knowledge_unit) return 0
  const index = knowledgeUnitItems.value.findIndex(
    (item) => item.review_item_id === currentItem.value.review_item_id
  )
  return index >= 0 ? index + 1 : 0
})
const decidedUnitCount = computed(() =>
  knowledgeUnitItems.value.filter((item) =>
    ['DECIDED', 'SOURCE_UPDATED', 'INVALIDATED'].includes(item.item_status)
  ).length
)
const remainingUnitCount = computed(() => Math.max(knowledgeUnitItems.value.length - decidedUnitCount.value, 0))
const includedUnitCount = computed(() =>
  knowledgeUnitItems.value.filter((item) =>
    ['PUBLISH', 'ADOPT_NEW_VERSION', 'SPLIT_SCOPE', 'CONFIRM_VALID'].includes(item.outcome)
  ).length
)
const excludedUnitCount = computed(() =>
  knowledgeUnitItems.value.filter((item) =>
    ['EXCLUDE', 'KEEP_CURRENT', 'ARCHIVE', 'DUPLICATE_SOURCE'].includes(item.outcome)
  ).length
)
function unitViewCount(view) {
  if (view === 'all') return knowledgeUnitItems.value.length
  if (view === 'decided') return decidedUnitCount.value
  return remainingUnitCount.value
}
const currentVisibleIndex = computed(() =>
  itemNavigationItems.value.findIndex((item) => item.review_item_id === selectedItemId.value)
)
const hasPreviousItem = computed(() => currentVisibleIndex.value > 0)
const hasNextItem = computed(
  () =>
    currentVisibleIndex.value >= 0 &&
    currentVisibleIndex.value < itemNavigationItems.value.length - 1
)
const selectedSourceSegment = computed(() =>
  sourceSegments.value.find((segment) => segment.segment_id === selectedSourceSegmentId.value)
)
const isPptxReview = computed(() => {
  const type = String(packageDetail.value?.item_type || '').toLowerCase()
  const title = String(packageDetail.value?.title || '').toLowerCase()
  return type === 'pptx' || title.endsWith('.pptx')
})
const presentationSlides = computed(() => presentationLayout.value?.slides || [])
const activePresentationSlide = computed(
  () =>
    presentationSlides.value.find((slide) => slide.slide_number === activeSlideNumber.value) ||
    presentationSlides.value[0]
)
const selectedPresentationFragment = computed(() =>
  (activePresentationSlide.value?.fragments || []).find(
    (fragment) => fragment.fragment_id === selectedPresentationFragmentId.value
  )
)
const presentationAspectRatio = computed(() =>
  String(presentationLayout.value?.aspect_ratio || 16 / 9)
)
const isDocumentLayoutReview = computed(() => {
  if (isPptxReview.value) return false
  const type = String(packageDetail.value?.item_type || '').toLowerCase()
  const title = String(packageDetail.value?.title || '').toLowerCase()
  return ['docx', 'xlsx', 'pdf', 'image', 'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg'].some(
    (suffix) => type === suffix || title.endsWith(`.${suffix}`)
  )
})
const documentPages = computed(() => documentLayout.value?.pages || [])
const activeDocumentPage = computed(
  () =>
    documentPages.value.find((page) => page.page_number === activeDocumentPageNumber.value) ||
    documentPages.value[0]
)
const selectedDocumentBlock = computed(() =>
  (activeDocumentPage.value?.blocks || []).find(
    (block) => block.block_id === selectedDocumentBlockId.value
  )
)
const documentPageAspectRatio = computed(() =>
  String(activeDocumentPage.value?.aspect_ratio || 16 / 9)
)
const itemActionable = computed(() =>
  ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(currentItem.value?.item_status)
)
const outcomeOptions = computed(() =>
  (currentItem.value?.allowed_outcomes || [])
    .map((value) => ({
      value,
      label: outcomeCopy[value]?.[0] || value,
      description: outcomeCopy[value]?.[1] || '记录本次处理结果'
    }))
)
const selectedRelations = computed(() => {
  const relationIds = new Set(currentItem.value?.relation_ids || [])
  return (packageDetail.value?.relations || []).filter((relation) =>
    relationIds.has(relation.relation_id)
  )
})
const auditRecords = computed(() => {
  const detail = packageDetail.value
  if (Array.isArray(detail?.audit_records)) return detail.audit_records
  const requests = (detail?.change_requests || []).map((request) => ({
    record_type: 'CHANGE_REQUEST',
    category: 'SOURCE_CHANGE',
    event_type: 'source_change_requested',
    scope: 'current',
    created_at: request.updated_at || request.created_at,
    operator_id: request.created_by,
    message: request.request_text,
    request
  }))
  const events = (detail?.events || []).map((event) => ({
    record_type: 'EVENT',
    category: event.category || auditEventCategory(event.event_type),
    scope: event.scope || 'current',
    ...event
  }))
  return [...requests, ...events].sort((left, right) =>
    String(right.created_at || '').localeCompare(String(left.created_at || ''))
  )
})
const activeRelation = computed(
  () =>
    selectedRelations.value.find((relation) => relation.relation_id === selectedRelationId.value) ||
    selectedRelations.value[0]
)
const activeRelationIndex = computed(() =>
  activeRelation.value
    ? selectedRelations.value.findIndex(
        (relation) => relation.relation_id === activeRelation.value.relation_id
      )
    : -1
)
const activeComparisonMatch = computed(
  () =>
    (relationLayoutComparison.value?.matches || []).find(
      (match) => match.match_id === selectedComparisonMatchId.value
    ) || relationLayoutComparison.value?.matches?.[0]
)
const comparisonMatchPagesAligned = computed(() => {
  const match = activeComparisonMatch.value
  if (!match?.source_page_number || !match?.target_page_number) return true
  return match.source_page_number === match.target_page_number
})
const activeDuplicateCandidate = computed(
  () => duplicateCandidates[activeRelation.value?.relation_id] || null
)
const activeDuplicateMatches = computed(
  () => activeDuplicateCandidate.value?.fragment_matches || []
)
const activeDuplicateMatch = computed(() => {
  const selectedId = selectedComparisonMatchId.value
  return (
    activeDuplicateMatches.value.find((match) => match.match_id === selectedId) ||
    activeDuplicateMatches.value[0] ||
    null
  )
})
const activeDuplicateMatchIndex = computed(() => {
  if (!activeDuplicateMatch.value) return -1
  return activeDuplicateMatches.value.findIndex(
    (match) => match.match_id === activeDuplicateMatch.value.match_id
  )
})
const comparisonSourcePages = computed(() => relationLayoutComparison.value?.source?.pages || [])
const comparisonTargetPages = computed(() => relationLayoutComparison.value?.target?.pages || [])
const activeComparisonSourcePage = computed(
  () =>
    comparisonSourcePages.value.find((page) => page.page_number === comparisonPageNumbers.source) ||
    comparisonSourcePages.value[0]
)
const activeComparisonTargetPage = computed(
  () =>
    comparisonTargetPages.value.find((page) => page.page_number === comparisonPageNumbers.target) ||
    comparisonTargetPages.value[0]
)
const activeComparisonSourceGridBlock = computed(() => comparisonGridFocusBlock('source'))
const activeComparisonTargetGridBlock = computed(() => comparisonGridFocusBlock('target'))
const comparisonLayoutReady = computed(() =>
  Boolean(
    relationLayoutComparison.value?.supported &&
    activeRelation.value &&
    relationLayoutComparison.value.source?.pages?.length &&
    relationLayoutComparison.value.target?.pages?.length
  )
)
const comparisonMatchCount = computed(() => relationLayoutComparison.value?.matches?.length || 0)
const activeComparisonView = computed(() => comparisonViews[activeComparisonViewSide.value])
const comparisonZoomPercentage = computed(() => Math.round(activeComparisonView.value.scale * 100))
const mergedReviewContent = computed(() => mergeChunks(reviewContent.value.lines || []))
const reviewMarkdown = computed(
  () => reviewContent.value.content || mergedReviewContent.value.content || ''
)
const mergedPreviousReviewContent = computed(() =>
  mergeChunks(previousReviewContent.value.lines || [])
)
const previousReviewMarkdown = computed(
  () => previousReviewContent.value.content || mergedPreviousReviewContent.value.content || ''
)
const hasPreviousVersion = computed(() => Boolean(packageDetail.value?.previous_version))
const hasComparisonBaseline = computed(() =>
  currentItem.value?.knowledge_unit
    ? Boolean(currentItem.value.previous_content)
    : hasPreviousVersion.value
)
const isUpdateReview = computed(() => currentItem.value?.review_type === 'UPDATE')
const versionChanges = computed(() => {
  if (currentItem.value?.knowledge_unit) {
    return buildVersionChanges(currentItem.value.previous_content, currentItem.value.content)
  }
  return buildVersionChanges(previousReviewMarkdown.value, reviewMarkdown.value)
})
const changeContentReady = computed(() =>
  currentItem.value?.knowledge_unit
    ? Boolean(currentItem.value.content)
    : reviewContent.value.loaded && previousReviewContent.value.loaded
)
const versionDiffStats = computed(() => ({
  added: versionChanges.value.reduce(
    (total, change) => total + (change.type === 'deleted' ? 0 : countLines(change.after)),
    0
  ),
  removed: versionChanges.value.reduce(
    (total, change) => total + (change.type === 'added' ? 0 : countLines(change.before)),
    0
  ),
  modified: versionChanges.value.filter((change) => change.type === 'modified').length
}))
const reviewerOptions = computed(() =>
  reviewers.value.map((item) => ({ label: `${item.name} · ${item.role}`, value: item.user_id }))
)
const publishBlocked = computed(() => {
  const quality = packageDetail.value?.content_quality || {}
  const explicitlyMissing = quality.checked === true && quality.has_body === false
  const loadedWithoutBody =
    reviewContent.value.loaded && !reviewContent.value.error && !reviewMarkdown.value.trim()
  return explicitlyMissing || Boolean(reviewContent.value.error) || loadedWithoutBody
})
const publishUnavailable = computed(
  () => publishBlocked.value || reviewContent.value.loading || !reviewContent.value.loaded
)
const showWholeReviewButton = computed(() =>
  Boolean(packageDetail.value?.item_count || packageDetail.value?.items?.length)
)
const bulkActionableItems = computed(() =>
  (packageDetail.value?.items || []).filter((item) =>
    ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
  )
)
const allActionableUnitsUnchanged = computed(
  () =>
    bulkActionableItems.value.length > 0 &&
    bulkActionableItems.value.every(
      (item) =>
        item.knowledge_unit &&
        item.review_type === 'UPDATE' &&
        item.change_type === 'UNCHANGED' &&
        item.allowed_outcomes?.includes('KEEP_CURRENT') &&
        !item.manual_review_required &&
        !item.problem_tags?.includes('CONFLICT')
    )
)
const canConfirmNoUpdate = computed(
  () => allActionableUnitsUnchanged.value && !publishUnavailable.value
)
const candidateIndexLabel = computed(() => {
  const chunkCount = Number(packageDetail.value?.chunk_count || 0)
  return chunkCount > 0 ? `${chunkCount} 个索引片段` : '待发布，尚未生成正式索引'
})
const noTextChangeTitle = computed(() => {
  if (allActionableUnitsUnchanged.value) return '仅检测到文件版本变化'
  if (currentItem.value?.knowledge_unit) return '当前知识单元未检测到文字变化'
  return '未检测到正文文字变化'
})
const noTextChangeDescription = computed(() => {
  if (allActionableUnitsUnchanged.value) {
    return '本次解析出的全部知识单元文字与当前正式版本一致，但图片或版式变化仍可能导致文件版本发生变化。'
  }
  if (currentItem.value?.knowledge_unit) {
    return '当前知识单元的文字与正式版本一致；请继续核对其他知识单元，以及可能存在的图片或版式变化。'
  }
  return '本次解析出的正文文字与当前正式版本一致，但图片、版式或文件属性仍可能发生变化。'
})
const bulkExcludeItems = computed(() =>
  (packageDetail.value?.items || []).filter((item) => {
    const cancelledSourceChange =
      item.item_status === 'INVALIDATED' && item.outcome === 'REQUEST_SOURCE_CHANGE'
    return (
      item.knowledge_unit &&
      item.allowed_outcomes?.includes('EXCLUDE') &&
      (['PENDING', 'WAITING_SOURCE_CHANGE', 'WAITING_BUSINESS_CONFIRMATION'].includes(
        item.item_status
      ) ||
        cancelledSourceChange)
    )
  })
)
const wholeReviewButtonTitle = computed(() => {
  if (batchResolving.value) return '正在批量处理整篇资料'
  if (!bulkActionableItems.value.length) {
    return '当前没有可批量处理的知识单元，请在右侧逐条处理'
  }
  return '按系统建议批量处理整篇资料；存在风险的知识单元仍需逐条审核'
})
const bulkSafeItems = computed(() =>
  bulkActionableItems.value.filter((item) => {
    const recommended = bulkRecommendedOutcome(item)
    return (
      !item.manual_review_required &&
      !item.problem_tags?.includes('CONFLICT') &&
      Boolean(recommended) &&
      item.allowed_outcomes?.includes(recommended) &&
      (!publishOutcome(recommended) || !publishUnavailable.value)
    )
  })
)
function bulkRecommendedOutcome(item) {
  return (
    item?.recommended_outcome ||
    (!item?.knowledge_unit && item?.review_type !== 'CONFLICT'
      ? item.allowed_outcomes?.find((value) =>
          ['PUBLISH', 'ADOPT_NEW_VERSION', 'CONFIRM_VALID', 'KEEP_CURRENT', 'DISMISS'].includes(
            value
          )
        )
      : '')
  )
}
function bulkOutcomeItems(outcome) {
  return bulkActionableItems.value.filter((item) => item.allowed_outcomes?.includes(outcome))
}
function bulkOutcomeLabel(outcome) {
  if (outcome === 'EXCLUDE') return '不纳入知识库'
  if (outcome === 'REQUEST_SOURCE_CHANGE') return '退回资料修改'
  return outcomeLabel(outcome)
}
const bulkRiskCount = computed(() =>
  Math.max(bulkActionableItems.value.length - bulkSafeItems.value.length, 0)
)
const contentQualityMessage = computed(() => {
  const quality = packageDetail.value?.content_quality || {}
  if (reviewContent.value.error) return `${reviewContent.value.error} 成功读取正文后才能发布。`
  return (
    quality.reason || '系统没有读取到正文内容，请重新解析；如果飞书原文确实为空，请补充后重新扫描。'
  )
})
const sourceChangeOutcome = computed(() =>
  ['REQUEST_SOURCE_CHANGE', 'REQUEST_SUPPORTING_SOURCE'].includes(form.outcome)
)
const problemTagLabel = computed(() =>
  sourceChangeOutcome.value ? '退回原因（可多选）' : '问题记录（可选）'
)
const problemTagHelp = computed(() =>
  sourceChangeOutcome.value
    ? '用于说明需要资料提供人修改的问题；提交退回后会作为修改依据。'
    : '用于记录审核中发现的问题，不等同于审核结果；退回资料时会作为修改依据。'
)
const decisionCommentLabel = computed(() =>
  sourceChangeOutcome.value
    ? '修改要求'
    : form.outcome === 'WAIT_BUSINESS_CONFIRMATION'
      ? '待确认问题'
      : '审核意见（选填）'
)
const decisionCommentPlaceholder = computed(() =>
  sourceChangeOutcome.value ? '具体说明飞书原文需要修改或补充什么' : '记录判断依据，便于后续追溯'
)
const queueEmptyText = computed(() =>
  queueView.value === 'mine' ? '当前没有待你处理的审核包' : '当前筛选下没有审核包'
)
const hasMorePackages = computed(
  () => packages.value.length < Number(packageResponse.value.total || 0)
)

watch(
  () => [
    props.sourceId,
    queueView.value,
    completionResultFilter.value,
    reviewTypeFilter.value,
    problemFilter.value
  ],
  loadPackages,
  { immediate: true }
)
watch(
  () => props.targetReviewId,
  (value) => value && loadPackages()
)
watch(
  selectedRelations,
  (relations) => {
    if (!relations.some((relation) => relation.relation_id === selectedRelationId.value)) {
      selectedRelationId.value = relations[0]?.relation_id || ''
    }
  },
  { immediate: true }
)
watch(activeEvidenceView, (view) => {
  if (view !== 'comparisons' && comparisonFullscreen.value) toggleComparisonFullscreen(false)
})

async function loadPackages(options = {}) {
  if (!props.sourceId) return
  loadingPackages.value = true
  packagePage.value = 1
  try {
    const response = await governanceApi.listReviewPackages(props.sourceId, packageQuery())
    packages.value = response.items || []
    packageResponse.value = response
    emit('count-change', response.counts?.mine || 0)
    if (
      options?.preserveDetail === true &&
      packages.value.some((item) => item.package_id === selectedPackageId.value)
    ) {
      return
    }
    const target = normalizedReviewTarget()
    const hasRequestedTarget = target.packageIds.length > 0 || target.versionIds.length > 0
    let requested = findTargetPackage(packages.value, target)
    if (hasRequestedTarget && !requested) {
      const expandedResponse = await governanceApi.listReviewPackages(props.sourceId, {
        ...packageQuery(),
        page_size: 100
      })
      requested = findTargetPackage(expandedResponse.items || [], target)
      if (requested) {
        packages.value = [
          requested,
          ...packages.value.filter((item) => item.package_id !== requested.package_id)
        ]
      }
    }
    if (hasRequestedTarget && !requested) {
      await selectPackage('')
      message.warning('该关系两侧资料暂无可处理审核任务')
      return
    }
    const selected = packages.value.find((item) => item.package_id === selectedPackageId.value)
    await selectPackage(
      requested?.package_id || selected?.package_id || packages.value[0]?.package_id || '',
      requested ? target.relationId : '',
      options.preferredItemId || ''
    )
    if (requested) emit('target-consumed')
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载审核任务失败'))
  } finally {
    loadingPackages.value = false
  }
}

async function loadMorePackages() {
  if (loadingPackages.value || loadingMorePackages.value || !hasMorePackages.value) return
  loadingMorePackages.value = true
  const nextPage = packagePage.value + 1
  try {
    const response = await governanceApi.listReviewPackages(props.sourceId, {
      ...packageQuery(),
      page: nextPage,
      page_size: 20
    })
    const existingIds = new Set(packages.value.map((item) => item.package_id))
    packages.value = [
      ...packages.value,
      ...(response.items || []).filter((item) => !existingIds.has(item.package_id))
    ]
    packagePage.value = nextPage
    packageResponse.value = {
      ...packageResponse.value,
      total: response.total ?? packageResponse.value.total,
      counts: response.counts ?? packageResponse.value.counts
    }
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载更多审核任务失败'))
  } finally {
    loadingMorePackages.value = false
  }
}

function handleQueueScroll(event) {
  const element = event.currentTarget
  if (!element || element.scrollHeight - element.scrollTop - element.clientHeight > 120) return
  void loadMorePackages()
}
function findTargetPackage(packageList, target) {
  return packageList.find(
    (item) =>
      target.packageIds.includes(item.package_id) ||
      target.versionIds.includes(item.source_version_id)
  )
}
function normalizedReviewTarget() {
  if (!props.targetReviewId) return { packageIds: [], versionIds: [], relationId: '' }
  if (typeof props.targetReviewId === 'string') {
    return {
      packageIds: [props.targetReviewId],
      versionIds: [props.targetReviewId],
      relationId: ''
    }
  }
  return {
    packageIds: [props.targetReviewId.packageId].filter(Boolean),
    versionIds: [props.targetReviewId.sourceVersionId, props.targetReviewId.targetVersionId].filter(
      Boolean
    ),
    relationId: props.targetReviewId.relationId || ''
  }
}
function packageQuery() {
  const params = {
    review_type: reviewTypeFilter.value || undefined,
    problem_tag: problemFilter.value || undefined
  }
  if (queueView.value === 'waiting_source')
    return { ...params, view: 'all', workflow_status: 'WAITING_SOURCE_CHANGE' }
  if (queueView.value === 'transferred') return { ...params, view: 'transferred_by_me' }
  if (queueView.value === 'completed')
    return {
      ...params,
      view: 'all',
      workflow_status: 'COMPLETED',
      completion_result:
        completionResultFilter.value === 'all' ? undefined : completionResultFilter.value
    }
  return { ...params, view: 'mine' }
}
function showSourceUpdates() {
  queueView.value = 'mine'
  reviewTypeFilter.value = 'UPDATE'
  problemFilter.value = ''
}
async function selectPackage(packageId, relationId = '', preferredItemId = '') {
  selectedPackageId.value = packageId
  packageDetail.value = null
  selectedItemId.value = ''
  unitView.value = 'pending'
  unitListExpanded.value = false
  reviewContent.value = emptyReviewContent()
  previousReviewContent.value = emptyReviewContent()
  sourceSegments.value = []
  selectedSourceSegmentId.value = ''
  presentationLayout.value = null
  presentationError.value = ''
  activeSlideNumber.value = 1
  selectedPresentationFragmentId.value = ''
  resetSlidePreviewView()
  documentLayout.value = null
  documentLayoutError.value = ''
  activeDocumentPageNumber.value = 1
  selectedDocumentBlockId.value = ''
  documentBlockDraft.value = ''
  resetDocumentPagePreviewView()
  clearRelationLayoutComparison({ preserveCache: true })
  if (!packageId) return
  const requestId = ++detailRequestSeq
  loadingDetail.value = true
  try {
    const response = await governanceApi.getReviewPackage(packageId)
    if (requestId !== detailRequestSeq) return
    packageDetail.value = response
    const relationItem = relationId
      ? response.items?.find((item) => item.relation_ids?.includes(relationId))
      : null
    const attentionItem = response.items?.find(
      (item) =>
        item.manual_review_required &&
        ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
    )
    const actionable = response.items?.find((item) =>
      ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
    )
    const preferred = response.items?.find(
      (item) =>
        item.review_item_id === preferredItemId &&
        ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
    )
    const waitingSourceItem = response.items?.find(
      (item) => item.item_status === 'WAITING_SOURCE_CHANGE'
    )
    selectItem(
      relationItem?.review_item_id ||
        preferred?.review_item_id ||
        waitingSourceItem?.review_item_id ||
        attentionItem?.review_item_id ||
        actionable?.review_item_id ||
        response.items?.[0]?.review_item_id ||
        ''
    )
    await Promise.all([
      loadVersionContents(),
      loadSourceSegments(),
      loadPresentationLayout(),
      loadDocumentLayout()
    ])
    if (relationItem) await showComparisons(relationId)
  } catch (error) {
    if (requestId === detailRequestSeq)
      message.error(governanceApi.getErrorMessage(error, '加载审核详情失败'))
  } finally {
    if (requestId === detailRequestSeq) loadingDetail.value = false
  }
}
function selectItem(itemId) {
  selectedItemId.value = itemId
  transferOpen.value = false
  decisionPanelOpen.value = false
  const item = packageDetail.value?.items.find((candidate) => candidate.review_item_id === itemId)
  if (!item) return
  activeEvidenceView.value = item.review_type === 'UPDATE' ? 'changes' : 'content'
  const draft =
    packageDetail.value?.draft?.review_item_id === itemId ? packageDetail.value.draft : {}
  const visibleOutcomes = item.allowed_outcomes || []
  const recommendedOutcome = visibleOutcomes.includes(item.recommended_outcome)
    ? item.recommended_outcome
    : ''
  form.outcome =
    draft.outcome && draft.outcome !== 'SPLIT_SCOPE'
      ? draft.outcome
      : recommendedOutcome || visibleOutcomes[0] || ''
  form.problem_tags = [...(draft.problem_tags || item.problem_tags || [])]
  form.decision_comment = draft.decision_comment || item.decision_comment || ''
  form.responsible_user_name = draft.responsible_user_name || ''
  form.applicability_scope = {
    ...(item.applicability_scope || {}),
    ...(draft.applicability_scope || {})
  }
  transferForm.assignee_id = undefined
  transferForm.comment = ''
  focusKnowledgeUnit(item)
}
function setUnitView(view) {
  if (!unitViewOptions.some((option) => option.value === view)) return
  unitView.value = view
  unitListExpanded.value = false
  const selectedVisible = visibleItems.value.some(
    (item) => item.review_item_id === selectedItemId.value
  )
  if (!selectedVisible && visibleItems.value[0]) {
    selectItem(visibleItems.value[0].review_item_id)
  }
}
async function loadReviewContent() {
  const detail = packageDetail.value
  const requestId = ++contentRequestSeq
  reviewContent.value = emptyReviewContent()
  if (!detail?.target_kb_id || !detail?.yuxi_file_id) {
    reviewContent.value = {
      ...emptyReviewContent(),
      loaded: true,
      error: '当前资料尚未生成可审核正文，请返回“资料与扫描”检查解析结果。'
    }
    return
  }
  reviewContent.value = { ...emptyReviewContent(), loading: true }
  try {
    const response = await documentApi.getDocumentContent(detail.target_kb_id, detail.yuxi_file_id)
    if (requestId !== contentRequestSeq) return
    if (response?.status === 'failed') throw new Error(response.message || '解析内容读取失败')
    reviewContent.value = {
      content: response?.content || '',
      lines: response?.lines || [],
      loading: false,
      loaded: true,
      error: ''
    }
  } catch (error) {
    if (requestId === contentRequestSeq)
      reviewContent.value = {
        ...emptyReviewContent(),
        loaded: true,
        error: /[\u3400-\u9fff]/.test(error?.message || '')
          ? error.message
          : '暂时无法读取解析正文，请稍后重试。'
      }
  }
}
async function loadPreviousReviewContent() {
  const detail = packageDetail.value
  const requestId = ++previousContentRequestSeq
  previousReviewContent.value = emptyReviewContent()
  if (!detail?.previous_version) return
  if (!detail.target_kb_id || !detail.previous_version.yuxi_file_id) {
    previousReviewContent.value = {
      ...emptyReviewContent(),
      loaded: true,
      error: '当前正式版本没有可读取的解析正文，暂时无法生成变更对照。'
    }
    return
  }
  previousReviewContent.value = { ...emptyReviewContent(), loading: true }
  try {
    const response = await documentApi.getDocumentContent(
      detail.target_kb_id,
      detail.previous_version.yuxi_file_id
    )
    if (requestId !== previousContentRequestSeq) return
    if (response?.status === 'failed') throw new Error(response.message || '旧版正文读取失败')
    previousReviewContent.value = {
      content: response?.content || '',
      lines: response?.lines || [],
      loading: false,
      loaded: true,
      error: ''
    }
  } catch (error) {
    if (requestId === previousContentRequestSeq)
      previousReviewContent.value = {
        ...emptyReviewContent(),
        loaded: true,
        error: /[\u3400-\u9fff]/.test(error?.message || '')
          ? error.message
          : '暂时无法读取当前正式版本正文，请稍后重试。'
      }
  }
}
async function loadVersionContents() {
  await Promise.all([loadReviewContent(), loadPreviousReviewContent()])
}
async function loadSourceSegments() {
  if (!selectedPackageId.value) return
  sourceSegmentsLoading.value = true
  selectedSourceSegmentId.value = ''
  try {
    const response = await governanceApi.listReviewPackageSegments(selectedPackageId.value)
    sourceSegments.value = response?.items || []
    focusKnowledgeUnit(currentItem.value, { loadPreview: false })
  } catch {
    sourceSegments.value = []
  } finally {
    sourceSegmentsLoading.value = false
  }
}
async function loadPresentationLayout() {
  presentationLayout.value = null
  presentationError.value = ''
  activeSlideNumber.value = 1
  selectedPresentationFragmentId.value = ''
  if (!selectedPackageId.value || !isPptxReview.value) return
  presentationLoading.value = true
  try {
    const response = await governanceApi.getReviewPackagePresentation(selectedPackageId.value)
    presentationLayout.value = response?.supported ? response : null
    if (!presentationLayout.value?.slides?.length) presentationError.value = '没有读取到幻灯片页面'
    else {
      focusKnowledgeUnit(currentItem.value, { loadPreview: false })
      await loadSlidePreview()
    }
  } catch (error) {
    presentationError.value = governanceApi.getErrorMessage(error, '版式读取失败')
  } finally {
    presentationLoading.value = false
  }
}
function mergeDocumentLayoutEdits(layout, edits) {
  const safeEdits = edits || {}
  return {
    ...layout,
    pages: (layout.pages || []).map((page) => ({
      ...page,
      blocks: (page.blocks || []).map((block) => ({
        ...block,
        original_content: block.content,
        content: safeEdits[block.block_id]?.content ?? block.content,
        edited: Boolean(safeEdits[block.block_id])
      }))
    }))
  }
}
async function loadDocumentLayout() {
  documentLayout.value = null
  documentLayoutError.value = ''
  activeDocumentPageNumber.value = 1
  selectedDocumentBlockId.value = ''
  documentBlockDraft.value = ''
  resetDocumentPagePreviewView()
  if (!selectedPackageId.value || !isDocumentLayoutReview.value) return
  documentLayoutLoading.value = true
  try {
    const response = await governanceApi.getReviewPackageLayout(selectedPackageId.value)
    if (!response?.supported || !response?.pages?.length) {
      documentLayoutError.value = response?.message || '没有读取到可还原的页面'
      return
    }
    documentLayout.value = mergeDocumentLayoutEdits(response, response.edits)
    focusKnowledgeUnit(currentItem.value, { loadPreview: false })
    await loadDocumentPagePreview()
  } catch (error) {
    documentLayoutError.value = governanceApi.getErrorMessage(error, '版式读取失败')
  } finally {
    documentLayoutLoading.value = false
  }
}
function documentPagePreviewCacheKey(packageId, pageNumber) {
  return `${packageId}:${pageNumber}`
}
function resetDocumentPagePreviewView() {
  documentPagePreviewRequestSeq += 1
  documentPagePreviewUrl.value = ''
  documentPagePreviewLoading.value = false
  documentPagePreviewError.value = ''
}
function clearDocumentPagePreviewCache() {
  for (const url of documentPagePreviewCache.values()) URL.revokeObjectURL(url)
  documentPagePreviewCache.clear()
  documentPagePreviewRequests.clear()
  resetDocumentPagePreviewView()
}
function rememberDocumentPagePreview(key, url) {
  documentPagePreviewCache.delete(key)
  documentPagePreviewCache.set(key, url)
  while (documentPagePreviewCache.size > DOCUMENT_PAGE_PREVIEW_CACHE_LIMIT) {
    const oldestKey = documentPagePreviewCache.keys().next().value
    const oldestUrl = documentPagePreviewCache.get(oldestKey)
    documentPagePreviewCache.delete(oldestKey)
    if (oldestUrl) URL.revokeObjectURL(oldestUrl)
  }
}
async function fetchDocumentPagePreview(packageId, pageNumber) {
  const key = documentPagePreviewCacheKey(packageId, pageNumber)
  const cachedUrl = documentPagePreviewCache.get(key)
  if (cachedUrl) {
    documentPagePreviewCache.delete(key)
    documentPagePreviewCache.set(key, cachedUrl)
    return cachedUrl
  }
  if (documentPagePreviewRequests.has(key)) return documentPagePreviewRequests.get(key)
  const request = governanceApi
    .getReviewPackageLayoutPage(packageId, pageNumber)
    .then((response) => response.blob())
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      if (packageId !== selectedPackageId.value) {
        URL.revokeObjectURL(url)
        return ''
      }
      rememberDocumentPagePreview(key, url)
      return url
    })
    .finally(() => documentPagePreviewRequests.delete(key))
  documentPagePreviewRequests.set(key, request)
  return request
}
async function loadDocumentPagePreview() {
  const packageId = selectedPackageId.value
  const pageNumber = activeDocumentPageNumber.value
  const requestId = ++documentPagePreviewRequestSeq
  if (!packageId || !activeDocumentPage.value || activeDocumentPage.value.render_mode === 'grid') {
    documentPagePreviewUrl.value = ''
    return
  }
  documentPagePreviewLoading.value = true
  documentPagePreviewError.value = ''
  try {
    const url = await fetchDocumentPagePreview(packageId, pageNumber)
    if (
      requestId !== documentPagePreviewRequestSeq ||
      packageId !== selectedPackageId.value ||
      pageNumber !== activeDocumentPageNumber.value
    )
      return
    documentPagePreviewUrl.value = url
    void preloadDocumentPagePreview(pageNumber - 1)
    void preloadDocumentPagePreview(pageNumber + 1)
  } catch (error) {
    if (requestId === documentPagePreviewRequestSeq)
      documentPagePreviewError.value = governanceApi.getErrorMessage(error, '页面预览生成失败')
  } finally {
    if (requestId === documentPagePreviewRequestSeq) documentPagePreviewLoading.value = false
  }
}
async function preloadDocumentPagePreview(pageNumber) {
  const packageId = selectedPackageId.value
  const page = documentPages.value.find((item) => item.page_number === pageNumber)
  if (
    !packageId ||
    !page ||
    page.render_mode === 'grid' ||
    documentPagePreviewCache.has(documentPagePreviewCacheKey(packageId, pageNumber))
  )
    return
  try {
    await fetchDocumentPagePreview(packageId, pageNumber)
  } catch {
    // 相邻页预取失败不影响当前页，实际翻页时会重试。
  }
}
async function selectDocumentPage(pageNumber) {
  activeDocumentPageNumber.value = pageNumber
  selectedDocumentBlockId.value = ''
  documentBlockDraft.value = ''
  await loadDocumentPagePreview()
}
function changeDocumentPage(offset) {
  const next = Math.max(
    1,
    Math.min(documentPages.value.length, activeDocumentPageNumber.value + offset)
  )
  if (next === activeDocumentPageNumber.value) return
  void selectDocumentPage(next)
}
function selectDocumentBlock(block) {
  selectedDocumentBlockId.value = block.block_id
  selectedSourceSegmentId.value = block.source_segment_ids?.[0] || ''
  documentBlockDraft.value = block.content || ''
}
async function saveDocumentBlockEdit() {
  const block = selectedDocumentBlock.value
  if (props.writeDisabled || !block || !packageDetail.value || !documentBlockDraft.value.trim()) return
  documentEditSaving.value = true
  try {
    const response = await governanceApi.saveReviewPackageLayoutEdit(selectedPackageId.value, {
      lock_version: packageDetail.value.lock_version,
      block_id: block.block_id,
      page_number: activeDocumentPageNumber.value,
      content: documentBlockDraft.value.trim(),
      source_segment_ids: block.source_segment_ids || []
    })
    packageDetail.value.draft = response.draft
    packageDetail.value.lock_version = response.lock_version
    block.content = documentBlockDraft.value.trim()
    block.edited = true
    message.success('版式编辑草稿已保存，飞书原文未被修改')
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '保存版式编辑失败'))
  } finally {
    documentEditSaving.value = false
  }
}
async function selectPresentationSlide(slideNumber) {
  activeSlideNumber.value = slideNumber
  selectedPresentationFragmentId.value = ''
  selectedSourceSegmentId.value = ''
  await loadSlidePreview()
}
function changePresentationSlide(offset) {
  const next = Math.max(
    1,
    Math.min(presentationSlides.value.length, activeSlideNumber.value + offset)
  )
  selectPresentationSlide(next)
}
function clearSlidePreviewCache() {
  for (const url of slidePreviewCache.values()) URL.revokeObjectURL(url)
  slidePreviewCache.clear()
  slidePreviewRequests.clear()
  resetSlidePreviewView()
}
function resetSlidePreviewView() {
  slidePreviewCacheEpoch += 1
  slidePreviewRequestSeq += 1
  activeSlidePreviewUrl.value = ''
  slidePreviewLoading.value = false
  slidePreviewError.value = ''
}

function slidePreviewCacheKey(packageId, slideNumber) {
  return `${packageId}:${slideNumber}`
}

function rememberSlidePreview(key, url) {
  slidePreviewCache.set(key, url)
  slidePreviewCache.delete(key)
  slidePreviewCache.set(key, url)
  while (slidePreviewCache.size > SLIDE_PREVIEW_CACHE_LIMIT) {
    const oldestKey = slidePreviewCache.keys().next().value
    const oldestUrl = slidePreviewCache.get(oldestKey)
    slidePreviewCache.delete(oldestKey)
    if (oldestUrl) URL.revokeObjectURL(oldestUrl)
  }
}

async function fetchSlidePreview(packageId, slideNumber) {
  const key = slidePreviewCacheKey(packageId, slideNumber)
  const cacheEpoch = slidePreviewCacheEpoch
  const cachedUrl = slidePreviewCache.get(key)
  if (cachedUrl) {
    slidePreviewCache.delete(key)
    slidePreviewCache.set(key, cachedUrl)
    return cachedUrl
  }
  if (slidePreviewRequests.has(key)) return slidePreviewRequests.get(key)

  const request = governanceApi
    .getReviewPackageSlidePreview(packageId, slideNumber)
    .then((response) => response.blob())
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      if (cacheEpoch !== slidePreviewCacheEpoch || packageId !== selectedPackageId.value) {
        URL.revokeObjectURL(url)
        return ''
      }
      rememberSlidePreview(key, url)
      return url
    })
    .finally(() => {
      slidePreviewRequests.delete(key)
    })
  slidePreviewRequests.set(key, request)
  return request
}

async function preloadSlidePreview(slideNumber) {
  const packageId = selectedPackageId.value
  if (
    !packageId ||
    slideNumber < 1 ||
    slideNumber > presentationSlides.value.length ||
    slidePreviewCache.has(slidePreviewCacheKey(packageId, slideNumber))
  )
    return
  try {
    await fetchSlidePreview(packageId, slideNumber)
  } catch {
    // 预加载失败不影响当前页，用户切换到该页时再重试。
  }
}

async function loadSlidePreview() {
  const packageId = selectedPackageId.value
  const slideNumber = activeSlideNumber.value
  const requestId = ++slidePreviewRequestSeq
  if (!packageId || !presentationSlides.value.length) return
  slidePreviewLoading.value = true
  slidePreviewError.value = ''
  try {
    const url = await fetchSlidePreview(packageId, slideNumber)
    if (requestId !== slidePreviewRequestSeq) return
    activeSlidePreviewUrl.value = url
    void preloadSlidePreview(slideNumber - 1)
    void preloadSlidePreview(slideNumber + 1)
  } catch (error) {
    if (requestId === slidePreviewRequestSeq)
      slidePreviewError.value = governanceApi.getErrorMessage(error, '整页预览生成失败')
  } finally {
    if (requestId === slidePreviewRequestSeq) slidePreviewLoading.value = false
  }
}
function selectPresentationFragment(fragment) {
  selectedPresentationFragmentId.value = fragment.fragment_id
  selectedSourceSegmentId.value = fragment.source_segment_ids?.[0] || ''
}
function fragmentHotspotStyle(fragment) {
  const left = clampHotspotValue(fragment?.left, 0, 99)
  const top = clampHotspotValue(fragment?.top, 0, 99)
  const width = clampHotspotValue(fragment?.width, 1, 100 - left)
  const height = clampHotspotValue(fragment?.height, 1, 100 - top)
  return {
    left: `${left}%`,
    top: `${top}%`,
    width: `${width}%`,
    height: `${height}%`
  }
}
function clampHotspotValue(value, min, max) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return min
  return Math.min(Math.max(numeric, min), Math.max(min, max))
}
function spreadsheetCanvasStyle(page) {
  const columns = Math.max(Number(page?.width) || 1, 1)
  const rows = Math.max(Number(page?.height) || 1, 1)
  const columnWidth = columns > 16 ? 128 : 156
  const rowHeight = rows > 60 ? 34 : 38
  return {
    '--sheet-columns': columns,
    '--sheet-rows': rows,
    '--sheet-column-width': `${columnWidth}px`,
    '--sheet-row-height': `${rowHeight}px`,
    '--sheet-width': `${columns * columnWidth}px`,
    '--sheet-height': `${rows * rowHeight}px`
  }
}
function presentationFragmentLabel(fragment) {
  const text = String(fragment.content || '')
    .replace(/\s+/g, ' ')
    .trim()
  return text.length > 30 ? `${text.slice(0, 30)}…` : text || '未命名片段'
}
onMounted(() => window.addEventListener('keydown', handleComparisonKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleComparisonKeydown)
  document.body.classList.remove('comparison-workspace-fullscreen')
  clearSlidePreviewCache()
  clearDocumentPagePreviewCache()
  clearRelationLayoutComparison()
})
function segmentLabel(segment) {
  return segment.locator_label || segment.title_path?.at(-1) || `片段 ${segment.segment_index + 1}`
}
function segmentTitle(segment) {
  const path = (segment.title_path || []).filter(Boolean).join(' > ')
  return [path, segment.locator_label].filter(Boolean).join(' · ') || segmentLabel(segment)
}
function formatSegmentLocator(locator) {
  if (!locator || typeof locator !== 'object') return ''
  if (locator.page) return '第 ' + locator.page + ' 页'
  if (locator.slide) return '第 ' + locator.slide + ' 页幻灯片'
  if (locator.sheet) {
    if (locator.row_start && locator.row_end)
      return locator.sheet + ' · 第 ' + locator.row_start + '-' + locator.row_end + ' 行'
    return '工作表 ' + locator.sheet
  }
  return locator.block ? '片段 ' + locator.block : ''
}
function toggleProblemTag(tag) {
  form.problem_tags = form.problem_tags.includes(tag)
    ? form.problem_tags.filter((item) => item !== tag)
    : [...form.problem_tags, tag]
}
function knowledgeUnitSourceSegmentIds(item) {
  return new Set([
    ...(item?.source_segment_ids || []),
    ...(item?.subject_locator?.source_segment_ids || [])
  ])
}
function findPresentationTarget(item) {
  const sourceSegmentIds = knowledgeUnitSourceSegmentIds(item)
  if (!sourceSegmentIds.size) return null
  for (const slide of presentationSlides.value) {
    const fragment = (slide.fragments || []).find((candidate) =>
      (candidate.source_segment_ids || []).some((segmentId) => sourceSegmentIds.has(segmentId))
    )
    if (fragment) return { slide, fragment }
  }
  return null
}
function findDocumentTarget(item) {
  const sourceSegmentIds = knowledgeUnitSourceSegmentIds(item)
  if (!sourceSegmentIds.size) return null
  for (const page of documentPages.value) {
    const block = (page.blocks || []).find((candidate) =>
      (candidate.source_segment_ids || []).some((segmentId) => sourceSegmentIds.has(segmentId))
    )
    if (block) return { page, block }
  }
  return null
}
function focusKnowledgeUnit(item, { loadPreview = true } = {}) {
  if (!item?.knowledge_unit) return
  selectedSourceSegmentId.value = item.source_segment_ids?.[0] || ''
  const presentationTarget = findPresentationTarget(item)
  const slide = Number(presentationTarget?.slide.slide_number || item.subject_locator?.slide || 0)
  if (slide && presentationSlides.value.length) {
    activeSlideNumber.value = slide
    selectedPresentationFragmentId.value = presentationTarget?.fragment.fragment_id || ''
    if (loadPreview) void loadSlidePreview()
  }
  const documentTarget = findDocumentTarget(item)
  const targetSheetPage = item.subject_locator?.sheet
    ? documentPages.value.find((page) => page.label === item.subject_locator.sheet)?.page_number
    : 0
  const page = Number(
    documentTarget?.page.page_number ||
      item.subject_locator?.page ||
      item.subject_locator?.sheet_page ||
      targetSheetPage ||
      0
  )
  if (page && documentPages.value.length) {
    activeDocumentPageNumber.value = Math.max(1, Math.min(documentPages.value.length, page))
    selectedDocumentBlockId.value = documentTarget?.block.block_id || ''
    documentBlockDraft.value = documentTarget?.block.content || ''
    if (loadPreview) void loadDocumentPagePreview()
  }
}
function selectRelativeItem(offset) {
  const nextItem = itemNavigationItems.value[currentVisibleIndex.value + offset]
  if (nextItem) selectItem(nextItem.review_item_id)
}
function decisionPayload() {
  return {
    review_item_id: currentItem.value.review_item_id,
    outcome: form.outcome,
    problem_tags: form.problem_tags,
    decision_comment: form.decision_comment.trim() || undefined,
    applicability_scope: cleanApplicabilityScope(form.applicability_scope),
    responsible_user_name: form.responsible_user_name.trim() || undefined,
    layout_edits: packageDetail.value?.draft?.layout_edits || {}
  }
}
function cleanApplicabilityScope(scope) {
  return Object.fromEntries(
    Object.entries(scope || {}).filter(([, value]) => String(value || '').trim())
  )
}
async function saveDraft() {
  if (props.writeDisabled || !packageDetail.value || !currentItem.value || !itemActionable.value) return
  savingDraft.value = true
  try {
    const response = await governanceApi.saveReviewPackageDraft(packageDetail.value.package_id, {
      lock_version: packageDetail.value.lock_version,
      draft: decisionPayload()
    })
    packageDetail.value.draft = response.draft
    packageDetail.value.lock_version = response.lock_version
    message.success('草稿已保存')
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '保存草稿失败'))
  } finally {
    savingDraft.value = false
  }
}
async function resolveItem() {
  if (props.writeDisabled || !packageDetail.value || !currentItem.value) return
  if (!itemActionable.value) {
    message.warning('该知识单元已处理，不能重复提交')
    decisionPanelOpen.value = false
    return
  }
  if (publishOutcome(form.outcome) && publishUnavailable.value) {
    message.warning(
      reviewContent.value.loading
        ? '正文仍在加载，请稍候'
        : '当前资料正文不可发布，请先处理解析或正文问题'
    )
    return
  }
  if (commentRequiredOutcomes.has(form.outcome) && !form.decision_comment.trim()) {
    message.warning(`请填写${decisionCommentLabel.value}`)
    return
  }
  if (form.outcome === 'SPLIT_SCOPE' && !Object.keys(cleanApplicabilityScope(form.applicability_scope)).length) {
    message.warning('请至少填写一项适用范围')
    return
  }
  resolving.value = true
  try {
    const itemTitle = currentItem.value.title || '该知识单元'
    const outcome = form.outcome
    const currentReviewItemId = currentItem.value.review_item_id
    const nextActionableItem = itemNavigationItems.value.find(
      (item) =>
        item.review_item_id !== currentReviewItemId &&
        ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
    )
    const response = await governanceApi.resolveReviewPackage(packageDetail.value.package_id, {
      request_id: newRequestId(),
      lock_version: packageDetail.value.lock_version,
      decisions: [decisionPayload()]
    })
    let successText = ''
    if (response.unit_publish_version_ids?.length) {
      successText =
        `“${itemTitle}”已确认纳入，正在加入正式知识；本材料还有 ${response.remaining_unit_count || 0} 个知识单元待处理`
      emit('knowledge-change')
    } else if (currentItem.value.knowledge_unit) {
      successText = `已记录“${outcomeLabel(outcome)}”；剩余 ${response.remaining_unit_count || 0} 个知识单元待处理`
    } else {
      successText = `已记录“${outcomeLabel(outcome)}”`
    }
    const counterpartNotice = (response.counterpart_actions || [])
      .map((action) => action.message || action.title)
      .filter(Boolean)
      .join('；')
    message.success(counterpartNotice ? `${successText}；${counterpartNotice}` : successText)
    await loadPackages({ preferredItemId: nextActionableItem?.review_item_id || '' })
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '提交审核结果失败'))
  } finally {
    resolving.value = false
  }
}
function confirmReopenExcludedItem() {
  if (props.writeDisabled || !currentItem.value?.can_reopen_exclusion || reopening.value) return
  const itemTitle = currentItem.value.title || '该知识单元'
  Modal.confirm({
    title: '重新申请纳入',
    content: `将为“${itemTitle}”创建新的待审核任务，原“不纳入”记录仍会保留。本操作不会直接发布到正式知识。`,
    okText: '创建待审核任务',
    cancelText: '取消',
    async onOk() {
      await reopenExcludedItem()
    }
  })
}
async function reopenExcludedItem() {
  if (props.writeDisabled || !currentItem.value?.review_item_id || reopening.value) return
  const reviewItemId = currentItem.value.review_item_id
  const itemTitle = currentItem.value.title || '该知识单元'
  const filtersChanged =
    queueView.value !== 'mine' || reviewTypeFilter.value !== '' || problemFilter.value !== ''
  reopening.value = true
  try {
    const response = await governanceApi.reopenExcludedReviewItem(reviewItemId)
    selectedPackageId.value = response.package_id
    queueView.value = 'mine'
    reviewTypeFilter.value = ''
    problemFilter.value = ''
    message.success(
      response.idempotent_replay
        ? `“${itemTitle}”已有待审核任务，已为你定位`
        : `“${itemTitle}”已重新提交审核`
    )
    if (!filtersChanged) await loadPackages()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '重新申请纳入失败'))
  } finally {
    reopening.value = false
  }
}
function confirmWholePackage() {
  if (props.writeDisabled || !bulkActionableItems.value.length || batchResolving.value) return
  const safeCount = bulkSafeItems.value.length
  const riskCount = bulkRiskCount.value
  if (!safeCount) {
    Modal.confirm({
      title: '整篇资料暂不能批量处理',
      content: `共 ${bulkActionableItems.value.length} 个待处理项，其中 ${riskCount} 个存在风险或需要人工确认。请在右侧逐条处理。`,
      okText: '知道了',
      cancelText: '关闭'
    })
    return
  }
  const actionText =
    riskCount > 0
      ? `系统将按建议处理 ${safeCount} 个低风险项，另外 ${riskCount} 个风险项保留在待审核列表中。`
      : `系统将按建议处理全部 ${safeCount} 个知识单元，完成后这份资料将进入正式知识或对应的后续状态。`
  Modal.confirm({
    title: '整篇批量审核',
    content: actionText,
    okText: riskCount > 0 ? `处理 ${safeCount} 个安全项` : '确认批量处理整篇资料',
    cancelText: '返回逐条审核',
    async onOk() {
      await resolveBulkItems(bulkSafeItems.value)
    }
  })
}
function confirmNoUpdate() {
  if (props.writeDisabled || !canConfirmNoUpdate.value || batchResolving.value) return
  Modal.confirm({
    title: '确认无需更新？',
    content:
      '确认后将保留当前正式知识并关闭本次候选版本，不会重新发布或重建索引。图片或版式如有变化，请先返回原文核对。',
    okText: '保留当前正式版本',
    cancelText: '继续核对',
    async onOk() {
      await resolveBulkItems(bulkActionableItems.value, 'KEEP_CURRENT')
    }
  })
}
function confirmBulkOutcome(outcome) {
  if (props.writeDisabled) return
  const items = outcome === 'EXCLUDE' ? bulkExcludeItems.value : bulkOutcomeItems(outcome)
  if (!items.length || batchResolving.value) return
  const actionLabel = bulkOutcomeLabel(outcome)
  const needsComment = commentRequiredOutcomes.has(outcome)
  const sourceChangeCount = items.filter(
    (item) =>
      item.item_status === 'WAITING_SOURCE_CHANGE' ||
      (item.item_status === 'INVALIDATED' && item.outcome === 'REQUEST_SOURCE_CHANGE')
  ).length
  Modal.confirm({
    title: `批量${actionLabel}`,
    content: needsComment
      ? `将对 ${items.length} 个允许该结果的知识单元批量退回资料修改。系统会保留每个单元的原审核依据，资料提供人修改后将重新进入审核。`
      : sourceChangeCount
        ? `将对 ${items.length} 个知识单元标记为“不纳入知识库”，其中 ${sourceChangeCount} 个来自资料修改流程。仍在进行的修改任务会一并结束，且这些内容不会发布到正式知识。`
        : `将对 ${items.length} 个允许该结果的知识单元标记为“不纳入知识库”。这些内容会保留来源记录，但不会发布到正式知识。`,
    okText: `确认${actionLabel}`,
    cancelText: '取消',
    async onOk() {
      await resolveBulkItems(items, outcome)
    }
  })
}
async function resolveBulkItems(items, outcomeOverride = '') {
  if (props.writeDisabled || !packageDetail.value || !items?.length) return
  batchResolving.value = true
  try {
    const response =
      outcomeOverride === 'EXCLUDE'
        ? await governanceApi.bulkExcludeReviewPackage(packageDetail.value.package_id, {
            request_id: newRequestId(),
            lock_version: packageDetail.value.lock_version,
            review_item_ids: items.map((item) => item.review_item_id),
            decision_comment: '整篇资料批量标记为不纳入知识库。'
          })
        : await governanceApi.resolveReviewPackage(packageDetail.value.package_id, {
            request_id: newRequestId(),
            lock_version: packageDetail.value.lock_version,
            decisions: items.map((item) => ({
              review_item_id: item.review_item_id,
              outcome: outcomeOverride || bulkRecommendedOutcome(item),
              problem_tags: item.problem_tags || [],
              decision_comment:
                outcomeOverride === 'REQUEST_SOURCE_CHANGE'
                  ? '整篇资料批量退回，等待资料修改后重新审核。'
                  : outcomeOverride === 'KEEP_CURRENT'
                    ? '已确认正文文字无需更新，保留当前正式知识并关闭候选版本。'
                    : item.recommendation_reason || undefined,
              applicability_scope: {}
            }))
          })
    const remaining = response.remaining_unit_count || 0
    const itemCount = items.length
    if (!outcomeOverride) {
      message.success(
        remaining
          ? `已批量处理 ${itemCount} 个安全项；仍有 ${remaining} 个知识单元待逐条审核`
          : `已批量处理整篇资料，共 ${itemCount} 个知识单元`
      )
    } else if (outcomeOverride === 'KEEP_CURRENT') {
      message.success(
        remaining
          ? `已确认 ${itemCount} 个知识单元无需更新；仍有 ${remaining} 个知识单元待逐条审核`
          : '已确认无需更新，保留当前正式知识并关闭候选版本'
      )
    } else {
      const actionText = `批量${bulkOutcomeLabel(outcomeOverride)}`
      message.success(
        remaining
          ? `已${actionText} ${itemCount} 个知识单元；仍有 ${remaining} 个知识单元待逐条审核`
          : `已${actionText}整篇资料，共 ${itemCount} 个知识单元`
      )
    }
    if (response.unit_publish_version_ids?.length || response.publish_version_ids?.length) {
      emit('knowledge-change')
    }
    await loadPackages()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '整篇批量审核失败'))
  } finally {
    batchResolving.value = false
  }
}
async function transferPackage() {
  if (props.writeDisabled || !packageDetail.value || !itemActionable.value) return
  if (!transferForm.assignee_id || !transferForm.comment.trim()) {
    message.warning('请选择转交对象并填写转交原因')
    return
  }
  transferring.value = true
  try {
    await governanceApi.transferReviewPackage(packageDetail.value.package_id, {
      lock_version: packageDetail.value.lock_version,
      assignee_id: transferForm.assignee_id,
      comment: transferForm.comment.trim()
    })
    message.success('审核包已转交')
    transferOpen.value = false
    await loadPackages()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '转交审核包失败'))
  } finally {
    transferring.value = false
  }
}
function confirmCancelChangeRequest(request) {
  if (props.writeDisabled) return
  Modal.confirm({
    title: '取消资料修改任务？',
    content: '取消后该轮修改任务将关闭，审核记录仍会保留。',
    okText: '确认取消',
    cancelText: '返回',
    okType: 'danger',
    async onOk() {
      try {
        await governanceApi.cancelSourceChangeRequest(
          request.change_request_id,
          '人工取消资料修改任务'
        )
        message.success('资料修改任务已取消')
        await loadPackages()
      } catch (error) {
        message.error(governanceApi.getErrorMessage(error, '取消资料修改任务失败'))
      }
    }
  })
}
async function loadReviewers() {
  try {
    const response = await governanceApi.listReviewers()
    reviewers.value = response.items || []
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载审核人失败'))
  }
}

function setComparisonCanvasRef(side, element) {
  comparisonCanvasRefs[side] = element || null
}

function comparisonCanvasStyle(page) {
  if (['grid', 'markdown'].includes(page?.render_mode)) return {}
  return { aspectRatio: String(page?.aspect_ratio || 16 / 9) }
}

function comparisonViewStyle(side, page) {
  const view = comparisonViews[side]
  if (page?.render_mode === 'markdown') {
    return {
      width: `${view.scale * 100}%`,
      minHeight: '100%'
    }
  }
  if (page?.render_mode === 'grid') {
    const columns = Math.max(Number(page.width) || 1, 1)
    const rows = Math.max(Number(page.height) || 1, 1)
    const columnWidth = columns > 16 ? 128 : 156
    const rowHeight = rows > 60 ? 34 : rows > 20 ? 40 : 56
    const scaledColumnWidth = Math.round(columnWidth * view.scale)
    const scaledRowHeight = Math.round(rowHeight * view.scale)
    return {
      '--sheet-column-width': `${scaledColumnWidth}px`,
      '--sheet-row-height': `${scaledRowHeight}px`,
      width: `max(100%, ${columns * scaledColumnWidth}px)`,
      height: `max(100%, ${Math.max(rows * scaledRowHeight, 320)}px)`
    }
  }
  return {
    width: `${view.scale * 100}%`,
    height: `${view.scale * 100}%`,
    left: `calc(50% + ${view.x}px)`,
    top: `calc(50% + ${view.y}px)`
  }
}

function clampComparisonOffset(side, x, y, scale) {
  const rect = comparisonCanvasRefs[side]?.getBoundingClientRect?.()
  if (!rect?.width || !rect?.height) return { x, y }
  const maxX = (rect.width * (scale - 1)) / 2
  const maxY = (rect.height * (scale - 1)) / 2
  return {
    x: Math.max(-maxX, Math.min(maxX, x)),
    y: Math.max(-maxY, Math.min(maxY, y))
  }
}

function applyComparisonView(side, nextView) {
  const scale = Math.max(
    COMPARISON_MIN_SCALE,
    Math.min(COMPARISON_MAX_SCALE, Number(nextView.scale) || COMPARISON_MIN_SCALE)
  )
  const offset =
    scale === COMPARISON_MIN_SCALE
      ? { x: 0, y: 0 }
      : clampComparisonOffset(side, Number(nextView.x) || 0, Number(nextView.y) || 0, scale)
  Object.assign(comparisonViews[side], { scale, ...offset })
  activeComparisonViewSide.value = side
  if (comparisonSyncView.value) {
    const other = side === 'source' ? 'target' : 'source'
    Object.assign(comparisonViews[other], { scale, ...offset })
  }
}

function setComparisonZoom(side, nextScale, event = null) {
  const view = comparisonViews[side]
  const previousDensity = comparisonPreviewDensity(side)
  const scale = Math.max(COMPARISON_MIN_SCALE, Math.min(COMPARISON_MAX_SCALE, nextScale))
  if (scale === view.scale) return
  const rect = comparisonCanvasRefs[side]?.getBoundingClientRect?.()
  const centerX = (rect?.left || 0) + (rect?.width || 0) / 2
  const centerY = (rect?.top || 0) + (rect?.height || 0) / 2
  const anchorX = Number.isFinite(event?.clientX) ? event.clientX : centerX
  const anchorY = Number.isFinite(event?.clientY) ? event.clientY : centerY
  const ratio = scale / view.scale
  applyComparisonView(side, {
    scale,
    x: anchorX - centerX - (anchorX - centerX - view.x) * ratio,
    y: anchorY - centerY - (anchorY - centerY - view.y) * ratio
  })
  if (comparisonPreviewDensity(side) !== previousDensity) {
    void loadComparisonPagePreview(side)
    if (comparisonSyncView.value)
      void loadComparisonPagePreview(side === 'source' ? 'target' : 'source')
  }
}

function changeComparisonZoom(offset) {
  const side = activeComparisonViewSide.value
  setComparisonZoom(side, comparisonViews[side].scale + offset)
}

function zoomComparisonFromWheel(side, event) {
  if (!event.ctrlKey && !event.metaKey) return
  event.preventDefault()
  activeComparisonViewSide.value = side
  const direction = event.deltaY < 0 ? 0.1 : -0.1
  setComparisonZoom(side, comparisonViews[side].scale + direction, event)
}

function resetComparisonView(side = activeComparisonViewSide.value) {
  setComparisonZoom(side, COMPARISON_MIN_SCALE)
}

function resetAllComparisonViews() {
  Object.assign(comparisonViews.source, { scale: COMPARISON_MIN_SCALE, x: 0, y: 0 })
  Object.assign(comparisonViews.target, { scale: COMPARISON_MIN_SCALE, x: 0, y: 0 })
}

function syncComparisonViews() {
  if (!comparisonSyncView.value) return
  const side = activeComparisonViewSide.value
  const other = side === 'source' ? 'target' : 'source'
  const previousDensity = comparisonPreviewDensity(other)
  Object.assign(comparisonViews[other], comparisonViews[side])
  if (comparisonPreviewDensity(other) !== previousDensity) void loadComparisonPagePreview(other)
}

function startComparisonPan(side, event) {
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  if (
    event.button !== 0 ||
    ['grid', 'markdown'].includes(page?.render_mode) ||
    comparisonViews[side].scale <= COMPARISON_MIN_SCALE ||
    event.target?.closest?.('.comparison-layout-block')
  )
    return
  activeComparisonViewSide.value = side
  Object.assign(comparisonPan, {
    side,
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originX: comparisonViews[side].x,
    originY: comparisonViews[side].y
  })
  event.currentTarget?.setPointerCapture?.(event.pointerId)
  event.preventDefault()
}

function moveComparisonPan(event) {
  if (!comparisonPan.side || comparisonPan.pointerId !== event.pointerId) return
  applyComparisonView(comparisonPan.side, {
    scale: comparisonViews[comparisonPan.side].scale,
    x: comparisonPan.originX + event.clientX - comparisonPan.startX,
    y: comparisonPan.originY + event.clientY - comparisonPan.startY
  })
  event.preventDefault()
}

function endComparisonPan(event) {
  if (!comparisonPan.side || comparisonPan.pointerId !== event.pointerId) return
  event.currentTarget?.releasePointerCapture?.(event.pointerId)
  comparisonPan.side = ''
  comparisonPan.pointerId = null
}

function toggleComparisonFullscreen(force) {
  comparisonFullscreen.value = typeof force === 'boolean' ? force : !comparisonFullscreen.value
  document.body.classList.toggle('comparison-workspace-fullscreen', comparisonFullscreen.value)
}

function handleComparisonKeydown(event) {
  if (event.key === 'Escape' && comparisonFullscreen.value) toggleComparisonFullscreen(false)
}

async function showComparisons(relationId = '') {
  activeEvidenceView.value = 'comparisons'
  const requestedId = typeof relationId === 'string' ? relationId : ''
  const comparison =
    selectedRelations.value.find((relation) => relation.relation_id === requestedId) ||
    activeRelation.value
  await focusRelation(comparison)
}

async function focusRelation(comparison) {
  if (!comparison) return
  resetAllComparisonViews()
  comparisonGridBlockIds.source = ''
  comparisonGridBlockIds.target = ''
  selectedRelationId.value = comparison.relation_id
  await loadRelationLayoutComparison(comparison)
  if (duplicateRelation(comparison)) await loadDuplicateCandidates(comparison)
}

function clearRelationLayoutComparison({ preserveCache = false } = {}) {
  comparisonPagePreviewRequestSeq.source += 1
  comparisonPagePreviewRequestSeq.target += 1
  if (!preserveCache) {
    for (const url of comparisonPagePreviewCache.values()) URL.revokeObjectURL(url)
    comparisonPagePreviewCache.clear()
    comparisonPagePreviewRequests.clear()
  }
  relationLayoutComparison.value = null
  relationLayoutError.value = ''
  selectedComparisonMatchId.value = ''
  comparisonPageNumbers.source = 1
  comparisonPageNumbers.target = 1
  comparisonPagePreviewUrls.source = ''
  comparisonPagePreviewUrls.target = ''
  comparisonPagePreviewErrors.source = ''
  comparisonPagePreviewErrors.target = ''
  comparisonGridBlockIds.source = ''
  comparisonGridBlockIds.target = ''
}

function comparisonPreviewDensity(side) {
  const scale = comparisonViews[side].scale
  if (scale > 2) return 3
  if (scale > 1) return 2
  return 1
}

function comparisonPageCacheKey(relationId, side, pageNumber, density = 1) {
  return `${relationId}:${side}:${pageNumber}:${density}`
}

function rememberComparisonPagePreview(key, url) {
  comparisonPagePreviewCache.set(key, url)
  comparisonPagePreviewCache.delete(key)
  comparisonPagePreviewCache.set(key, url)
  while (comparisonPagePreviewCache.size > COMPARISON_PAGE_PREVIEW_CACHE_LIMIT) {
    const oldestKey = comparisonPagePreviewCache.keys().next().value
    const oldestUrl = comparisonPagePreviewCache.get(oldestKey)
    comparisonPagePreviewCache.delete(oldestKey)
    if (oldestUrl) URL.revokeObjectURL(oldestUrl)
  }
}

async function fetchComparisonPagePreview(relationId, side, pageNumber, density = 1) {
  const key = comparisonPageCacheKey(relationId, side, pageNumber, density)
  const cachedUrl = comparisonPagePreviewCache.get(key)
  if (cachedUrl) {
    comparisonPagePreviewCache.delete(key)
    comparisonPagePreviewCache.set(key, cachedUrl)
    return cachedUrl
  }
  if (comparisonPagePreviewRequests.has(key)) return comparisonPagePreviewRequests.get(key)
  const request = governanceApi
    .getRelationLayoutComparisonPage(relationId, side, pageNumber, density)
    .then((response) => response.blob())
    .then((blob) => {
      const url = URL.createObjectURL(blob)
      if (relationLayoutComparison.value?.relation_id !== relationId) {
        URL.revokeObjectURL(url)
        return ''
      }
      rememberComparisonPagePreview(key, url)
      return url
    })
    .finally(() => comparisonPagePreviewRequests.delete(key))
  comparisonPagePreviewRequests.set(key, request)
  return request
}

async function loadComparisonPagePreview(side) {
  const relationId = activeRelation.value?.relation_id
  const pageNumber = comparisonPageNumbers[side]
  const density = comparisonPreviewDensity(side)
  const requestId = ++comparisonPagePreviewRequestSeq[side]
  if (!relationId || !pageNumber) return
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  if (['grid', 'markdown'].includes(page?.render_mode)) {
    comparisonPagePreviewUrls[side] = ''
    comparisonPagePreviewErrors[side] = ''
    return
  }
  comparisonPagePreviewLoading[side] = true
  comparisonPagePreviewErrors[side] = ''
  try {
    const url = await fetchComparisonPagePreview(relationId, side, pageNumber, density)
    if (
      requestId !== comparisonPagePreviewRequestSeq[side] ||
      relationId !== activeRelation.value?.relation_id ||
      pageNumber !== comparisonPageNumbers[side] ||
      density !== comparisonPreviewDensity(side)
    )
      return
    comparisonPagePreviewUrls[side] = url
    void preloadComparisonPagePreview(side, pageNumber - 1)
    void preloadComparisonPagePreview(side, pageNumber + 1)
  } catch (error) {
    if (requestId === comparisonPagePreviewRequestSeq[side])
      comparisonPagePreviewErrors[side] = governanceApi.getErrorMessage(error, '对比页面预览失败')
  } finally {
    if (requestId === comparisonPagePreviewRequestSeq[side])
      comparisonPagePreviewLoading[side] = false
  }
}

async function preloadComparisonPagePreview(side, pageNumber) {
  const relationId = activeRelation.value?.relation_id
  const pages = side === 'source' ? comparisonSourcePages.value : comparisonTargetPages.value
  const page = pages.find((item) => item.page_number === pageNumber)
  if (
    !relationId ||
    !page ||
    ['grid', 'markdown'].includes(page.render_mode) ||
    comparisonPagePreviewCache.has(comparisonPageCacheKey(relationId, side, pageNumber, 1))
  )
    return
  try {
    await fetchComparisonPagePreview(relationId, side, pageNumber, 1)
  } catch {
    // 相邻页预取失败不影响当前证据页，实际翻页时会重试。
  }
}

async function loadRelationLayoutComparison(comparison) {
  if (!comparison?.relation_id) return
  relationLayoutLoading.value = true
  relationLayoutError.value = ''
  relationLayoutComparison.value = null
  comparisonPagePreviewUrls.source = ''
  comparisonPagePreviewUrls.target = ''
  try {
    const response = await governanceApi.getRelationLayoutComparison(comparison.relation_id)
    if (selectedRelationId.value !== comparison.relation_id) return
    relationLayoutComparison.value = response
    const match = response.matches?.[0]
    selectedComparisonMatchId.value = match?.match_id || ''
    comparisonPageNumbers.source = match?.source_page_number || 1
    comparisonPageNumbers.target = match?.target_page_number || 1
    if (!comparisonMatchPagesAligned.value) comparisonSyncPages.value = false
    if (response.supported) {
      const previewRequests = []
      if (!['grid', 'markdown'].includes(activeComparisonSourcePage.value?.render_mode)) {
        previewRequests.push(loadComparisonPagePreview('source'))
      }
      if (!['grid', 'markdown'].includes(activeComparisonTargetPage.value?.render_mode)) {
        previewRequests.push(loadComparisonPagePreview('target'))
      }
      await Promise.all(previewRequests)
    }
  } catch (error) {
    relationLayoutError.value = governanceApi.getErrorMessage(error, '跨文档版式对比加载失败')
  } finally {
    relationLayoutLoading.value = false
  }
}

async function selectComparisonMatch(match) {
  if (!match) return
  selectedComparisonMatchId.value = match.match_id
  comparisonGridBlockIds.source = ''
  comparisonGridBlockIds.target = ''
  comparisonPageNumbers.source = match.source_page_number || comparisonPageNumbers.source
  comparisonPageNumbers.target = match.target_page_number || comparisonPageNumbers.target
  if (!comparisonMatchPagesAligned.value) comparisonSyncPages.value = false
  await Promise.all([loadComparisonPagePreview('source'), loadComparisonPagePreview('target')])
}

async function changeComparisonPage(side, offset) {
  const pages = side === 'source' ? comparisonSourcePages.value : comparisonTargetPages.value
  const current = comparisonPageNumbers[side]
  const next = Math.max(1, Math.min(pages.length, current + offset))
  if (next === current) return
  comparisonPageNumbers[side] = next
  comparisonGridBlockIds[side] = ''
  if (comparisonSyncPages.value && comparisonMatchPagesAligned.value) {
    const other = side === 'source' ? 'target' : 'source'
    const otherPages =
      other === 'source' ? comparisonSourcePages.value : comparisonTargetPages.value
    comparisonPageNumbers[other] = Math.max(
      1,
      Math.min(otherPages.length, comparisonPageNumbers[other] + offset)
    )
    await Promise.all([loadComparisonPagePreview(side), loadComparisonPagePreview(other)])
    return
  }
  await loadComparisonPagePreview(side)
}

function comparisonBlockClass(side, block) {
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  const isGrid = page?.render_mode === 'grid'
  const isActiveMatch = comparisonMatchBlockIds(
    activeComparisonMatch.value,
    side,
    page?.page_number
  ).includes(block.block_id)
  const isSelected =
    isActiveMatch && (!isGrid || comparisonGridFocusBlock(side)?.block_id === block.block_id)
  return {
    'comparison-block-match': isActiveMatch,
    'comparison-block-selected': isSelected,
    'comparison-block-grid': isGrid
  }
}

function comparisonMatchIndex(match) {
  return (relationLayoutComparison.value?.matches || []).findIndex(
    (item) => item.match_id === match?.match_id
  )
}

function comparisonMatchNumber(side, block) {
  const match = comparisonMatchForBlock(side, block)
  const index = comparisonMatchIndex(match)
  return index >= 0 ? index + 1 : ''
}

function comparisonBlockMarker(side, block) {
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  if (page?.render_mode === 'grid') return block.locator?.cell || ''
  return comparisonMatchNumber(side, block)
}

function comparisonMatchLocator(match) {
  const sourcePages = comparisonMatchPages(match, 'source')
  const targetPages = comparisonMatchPages(match, 'target')
  if (sourcePages.length || targetPages.length)
    return `来源一第 ${sourcePages.join('、') || '-'} 页 ↔ 来源二第 ${targetPages.join('、') || '-'} 页`
  return match?.source_overlap_excerpt || match?.source_excerpt || '已定位片段'
}

function comparisonMatchPages(match, side) {
  const pages = match?.[`${side}_page_numbers`]
  if (Array.isArray(pages) && pages.length) return pages
  const byPage = match?.[`${side}_block_ids_by_page`]
  if (byPage && typeof byPage === 'object') {
    const values = Object.keys(byPage)
      .map(Number)
      .filter((value) => Number.isFinite(value))
    if (values.length) return values
  }
  const page = match?.[`${side}_page_number`] || match?.[`${side}_locator`]?.page
  return page ? [page] : []
}

function comparisonMatchBlockIds(match, side, pageNumber) {
  if (!match || !pageNumber) return []
  const byPage = match[`${side}_block_ids_by_page`]
  const pageBlockIds = byPage?.[pageNumber] || byPage?.[String(pageNumber)]
  if (Array.isArray(pageBlockIds)) return pageBlockIds
  return Number(match[`${side}_page_number`]) === Number(pageNumber)
    ? match[`${side}_block_ids`] || []
    : []
}

function comparisonMatchForBlock(side, block) {
  const matches = relationLayoutComparison.value?.matches || []
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  const pageNumber = page?.page_number
  const activeMatch = activeComparisonMatch.value
  if (comparisonMatchBlockIds(activeMatch, side, pageNumber).includes(block.block_id))
    return activeMatch
  return matches.find((match) =>
    comparisonMatchBlockIds(match, side, pageNumber).includes(block.block_id)
  )
}

function comparisonGridMatchBlocks(side) {
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  if (page?.render_mode !== 'grid') return []
  const matchBlockIds = new Set(
    comparisonMatchBlockIds(activeComparisonMatch.value, side, page.page_number)
  )
  return (page.blocks || []).filter((block) => matchBlockIds.has(block.block_id))
}

function comparisonGridMatchCells(side) {
  return (
    comparisonGridMatchBlocks(side)
      .map((block) => block.locator?.cell)
      .filter(Boolean)
      .join('、') || '-'
  )
}

function comparisonGridFocusBlock(side) {
  const page =
    side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value
  if (page?.render_mode !== 'grid') return null
  const blocks = page.blocks || []
  const selected = blocks.find((block) => block.block_id === comparisonGridBlockIds[side])
  if (selected) return selected
  return comparisonGridMatchBlocks(side).reduce(
    (longest, block) =>
      String(block.content || '').length > String(longest?.content || '').length ? block : longest,
    null
  )
}

async function selectComparisonBlock(side, block) {
  if (!block) return
  const isGrid =
    (side === 'source' ? activeComparisonSourcePage.value : activeComparisonTargetPage.value)
      ?.render_mode === 'grid'
  const match = comparisonMatchForBlock(side, block)
  if (match && match.match_id !== activeComparisonMatch.value?.match_id) {
    await selectComparisonMatch(match)
  }
  if (isGrid) comparisonGridBlockIds[side] = block.block_id
}

function comparisonBlockStyle(block) {
  return fragmentHotspotStyle(block)
}

async function changeRelation(offset) {
  if (!selectedRelations.value.length) return
  const nextIndex =
    (activeRelationIndex.value + offset + selectedRelations.value.length) %
    selectedRelations.value.length
  await focusRelation(selectedRelations.value[nextIndex])
}

async function loadDuplicateCandidates(comparison) {
  const relationId = comparison?.relation_id
  if (!relationId || duplicateLoading[relationId]) return
  duplicateLoading[relationId] = true
  try {
    const candidate = await governanceApi.getDuplicateCandidates(relationId)
    duplicateCandidates[relationId] = candidate
    if (!selectedComparisonMatchId.value && candidate.fragment_matches?.length) {
      selectedComparisonMatchId.value = candidate.fragment_matches[0].match_id
    }
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载重复片段失败'))
  } finally {
    duplicateLoading[relationId] = false
  }
}
function confirmDuplicateResolution(comparison, strategy) {
  if (props.writeDisabled) return
  const candidate = duplicateCandidates[comparison.relation_id]
  if (!candidate || duplicateResolving.value) return
  const primaryTitle =
    strategy === 'USE_SOURCE'
      ? candidate.source.title
      : strategy === 'USE_TARGET'
        ? candidate.target.title
        : ''
  Modal.confirm({
    title: strategy === 'KEEP_SEPARATE' ? '确认分别保留？' : `将“${primaryTitle}”作为规范内容？`,
    content:
      strategy === 'KEEP_SEPARATE'
        ? '两边片段将继续作为不同知识处理，不建立重复来源关系；两边知识单元仍需分别完成审批。'
        : '匹配到的另一边片段会记录为重复来源，不会重复创建正式知识。完全由重复片段组成、且没有其他未解决关系的知识单元会自动结束审核；含独有内容的知识单元继续待审，所选规范来源仍需正常审批。两篇文档的独有内容不受影响。',
    okText: '确认处理',
    cancelText: '返回',
    async onOk() {
      await resolveDuplicateRelation(comparison.relation_id, strategy)
    }
  })
}
function duplicateActionHelp(matchCount) {
  return `以下处理会作用于全部 ${matchCount || 0} 个匹配片段；两篇文档的独有内容继续正常审核，重复内容变化时再重新判断。`
}
async function resolveDuplicateRelation(relationId, strategy) {
  if (props.writeDisabled) return
  duplicateResolving.value = relationId
  try {
    const response = await governanceApi.resolveDuplicateRelation(relationId, {
      request_id: newRequestId(),
      strategy
    })
    duplicateCandidates[relationId] = response
    const automation = response.review_automation
    if (strategy === 'KEEP_SEPARATE') {
      message.success('已分别保留两边内容，两边知识单元继续独立审批')
    } else if (automation?.source_review_closed) {
      message.success('已处理重复来源，另一来源无需再次审批')
    } else {
      message.success(
        `已处理重复来源${automation ? `，另一来源还剩 ${automation.remaining_unit_count || 0} 个知识单元待审核` : ''}`
      )
    }
    await loadPackages({ preserveDetail: true })
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '处理重复片段失败'))
  } finally {
    duplicateResolving.value = ''
  }
}

function emptyReviewContent() {
  return { content: '', lines: [], loading: false, loaded: false, error: '' }
}
function newRequestId() {
  return (
    globalThis.crypto?.randomUUID?.() ||
    `review-${Date.now()}-${Math.random().toString(16).slice(2)}`
  )
}
function publishOutcome(value) {
  return ['PUBLISH', 'ADOPT_NEW_VERSION'].includes(value)
}
function outcomeLabel(value) {
  return outcomeCopy[value]?.[0] || '处理'
}
function formatTime(value) {
  if (!value) return '暂无时间'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(value))
}
function formatNumber(value) {
  return new Intl.NumberFormat('zh-CN').format(value || 0)
}
function formatFactValue(value) {
  return typeof value === 'string'
    ? value
    : Object.entries(value || {})
        .map(([key, item]) => `${key}: ${item}`)
        .join('，')
}
function reviewTypeLabel(value) {
  return (
    {
      NEW: '新增知识',
      UPDATE: '更新已有知识',
      CONFLICT: '冲突裁决',
      STALE: '确认现有知识'
    }[value] || '知识审核'
  )
}
function changeTypeLabel(value) {
  return (
    {
      NEW: '新增单元',
      UPDATED: '内容有变化',
      UNCHANGED: '内容未变化'
    }[value] || '知识单元'
  )
}
function itemStatusLabel(value, item = null) {
  if (value === 'INVALIDATED' && item?.decision_payload?.replaced_by_knowledge_units) {
    return '已拆分为知识单元'
  }
  if (value === 'INVALIDATED' && item?.outcome === 'REQUEST_SOURCE_CHANGE') {
    return '修改任务已结束'
  }
  if (value === 'DECIDED') {
    if (['PUBLISH', 'ADOPT_NEW_VERSION', 'SPLIT_SCOPE', 'CONFIRM_VALID'].includes(item?.outcome)) {
      return '已提交发布'
    }
    if (['EXCLUDE', 'KEEP_CURRENT', 'ARCHIVE', 'DUPLICATE_SOURCE'].includes(item?.outcome)) {
      return '已处理（未发布）'
    }
  }
  return (
    {
      PENDING: '待审核',
      WAITING_SOURCE_CHANGE: '等待资料修改',
      WAITING_BUSINESS_CONFIRMATION: '等待业务确认',
      DECIDED: '已完成',
      SOURCE_UPDATED: '已收到新版本',
      INVALIDATED: '已失效'
    }[value] || value
  )
}
function workflowStatusLabel(value) {
  return (
    {
      OPEN: '待审核',
      WAITING_SOURCE_CHANGE: '等待资料修改',
      WAITING_BUSINESS_CONFIRMATION: '等待业务确认',
      COMPLETED: '已完成',
      INVALIDATED: '已失效'
    }[value] || value
  )
}
function completionResultLabel(value) {
  return (
    {
      all_included: '全部纳入',
      partial: '部分纳入',
      all_excluded: '全部不纳入',
      all_duplicate: '全部为重复来源'
    }[value] || ''
  )
}
function statusColor(value) {
  return (
    {
      OPEN: 'processing',
      PENDING: 'processing',
      DECIDED: 'success',
      WAITING_SOURCE_CHANGE: 'warning',
      WAITING_BUSINESS_CONFIRMATION: 'warning',
      COMPLETED: 'success'
    }[value] || 'default'
  )
}
function riskLabel(value) {
  return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[value] || '待评估'
}

function qualityGateLabel(value) {
  return (
    {
      BLOCKED: '质量门禁阻断',
      RECOMMENDED: '建议通过，仍需人工确认',
      REVIEW: '需要重点人工复核',
      RETURN: '建议退回处理'
    }[value] || '质量门禁待计算'
  )
}

function changeFlagLabel(value) {
  if (value === true) return '有变化'
  if (value === false) return '无变化'
  return '待核验'
}
function buildVersionChanges(previousContent, currentContent) {
  if (!previousContent || !currentContent) return []
  const parts = diffLines(previousContent, currentContent)
  const changes = []
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index]
    if (!part.added && !part.removed) continue
    const next = parts[index + 1]
    if (part.removed && next?.added) {
      changes.push({
        type: 'modified',
        before: part.value.trimEnd(),
        after: next.value.trimEnd(),
        lineCount: Math.max(countLines(part.value), countLines(next.value))
      })
      index += 1
      continue
    }
    if (part.added && next?.removed) {
      changes.push({
        type: 'modified',
        before: next.value.trimEnd(),
        after: part.value.trimEnd(),
        lineCount: Math.max(countLines(part.value), countLines(next.value))
      })
      index += 1
      continue
    }
    changes.push({
      type: part.added ? 'added' : 'deleted',
      before: part.removed ? part.value.trimEnd() : '',
      after: part.added ? part.value.trimEnd() : '',
      lineCount: countLines(part.value)
    })
  }
  return changes
}
function countLines(value) {
  const normalized = String(value || '')
    .replace(/\r\n/g, '\n')
    .replace(/\n$/, '')
  return normalized ? normalized.split('\n').length : 0
}
function versionChangeLabel(value) {
  return { modified: '内容修改', added: '新增内容', deleted: '删除内容' }[value] || '内容变化'
}
function relationLabel(value) {
  return (
    {
      CONFLICT: '内容冲突',
      EXACT_DUPLICATE: '完全重复',
      OVERLAP: '内容重叠',
      CONDITIONAL_VARIANT: '条件变体',
      COMPLEMENTARY: '互补内容',
      INSUFFICIENT: '证据不足'
    }[value] || '待比较'
  )
}
function relationColor(value) {
  return (
    {
      CONFLICT: 'error',
      EXACT_DUPLICATE: 'success',
      OVERLAP: 'warning',
      CONDITIONAL_VARIANT: 'processing',
      INSUFFICIENT: 'warning'
    }[value] || 'default'
  )
}
function duplicateRelation(comparison) {
  return ['EXACT_DUPLICATE', 'OVERLAP'].includes(comparison?.relation_type)
}
function duplicateDecisionTitle(candidate) {
  const decision = candidate?.decision
  if (!decision) return ''
  if (decision.strategy === 'KEEP_SEPARATE') return '已决定分别保留'
  const primary =
    decision.primary_version_id === candidate.source?.version_id
      ? candidate.source
      : candidate.target
  return `已将“${primary?.title || '所选来源'}”设为规范内容`
}
function duplicateResolutionSummary(candidate) {
  const matchCount = candidate?.decision?.fragment_match_ids?.length || 0
  if (candidate?.decision?.strategy === 'KEEP_SEPARATE') {
    return `已核对 ${matchCount} 组重复片段，两边知识单元继续独立审批。`
  }
  const automation = candidate?.review_automation
  if (!automation) return `已处理 ${matchCount} 组重复片段，其他独有内容继续审核。`
  const prefix = `已处理 ${matchCount} 组重复片段，自动标记 ${automation.auto_decided_unit_count || 0} 个重复来源知识单元。`
  if (automation.source_review_closed) return `${prefix} 另一来源无需再次审批。`
  return `${prefix} 另一来源还剩 ${automation.remaining_unit_count || 0} 个知识单元待审核。`
}
function comparisonClass(value) {
  return `comparison-${String(value || '').toLowerCase()}`
}
function percentage(value) {
  return value == null ? '-' : `${Math.round(value * 100)}%`
}
function changeRequestStatusLabel(value) {
  return (
    {
      OPEN: '等待修改',
      NEW_VERSION_RECEIVED: '已收到新版本',
      FULFILLED: '已完成',
      CANCELLED: '已取消'
    }[value] || value
  )
}
function changeRequestColor(value) {
  return (
    { OPEN: 'warning', NEW_VERSION_RECEIVED: 'processing', FULFILLED: 'success' }[value] ||
    'default'
  )
}
function auditEventCategory(value) {
  if (String(value).startsWith('source_change_') || value === 'review_item_reopened') {
    return 'SOURCE_CHANGE'
  }
  if (
    String(value).includes('conflict') ||
    String(value).includes('duplicate') ||
    String(value).includes('relation')
  ) {
    return 'CROSS_DOCUMENT'
  }
  if (String(value).includes('transfer')) return 'ASSIGNMENT'
  if (
    [
      'material_discovered',
      'parsed',
      'parse_failed',
      'processing_queued',
      'processing_enqueue_failed',
      'retry_queued',
      'scan_failed',
      'startup_reconciled'
    ].includes(value) ||
    String(value).includes('scan') ||
    String(value).includes('processing') ||
    String(value).includes('startup')
  ) {
    return 'PROCESSING'
  }
  if (
    String(value).includes('publish') ||
    String(value).includes('lifecycle') ||
    [
      'approved',
      'rejected',
      'review_package_completed',
      'review_item_decided',
      'knowledge_unit_metadata_updated',
      'reindex_queued',
      'source_offline_started',
      'source_offline_failed',
      'source_offline_completed',
      'source_restore_queued',
      'source_rollback_queued',
      'removal_started',
      'removal_confirmed',
      'removal_failed',
      'replacement_cleanup_failed'
    ].includes(value)
  ) {
    return 'KNOWLEDGE'
  }
  return 'REVIEW'
}
function auditRecordCategoryLabel(value) {
  return (
    {
      SOURCE_CHANGE: '资料变更',
      CROSS_DOCUMENT: '跨文档',
      ASSIGNMENT: '责任归属',
      PROCESSING: '系统加工',
      KNOWLEDGE: '知识处理',
      REVIEW: '审核'
    }[value] || '业务操作'
  )
}
function auditRecordCategoryColor(value) {
  return (
    {
      SOURCE_CHANGE: 'orange',
      CROSS_DOCUMENT: 'purple',
      ASSIGNMENT: 'blue',
      PROCESSING: 'default',
      KNOWLEDGE: 'green',
      REVIEW: 'cyan'
    }[value] || 'default'
  )
}
function auditRecordRequest(record) {
  return record.request || record.payload?.change_request || {}
}
function auditStatusLabel(value) {
  if (!value) return '—'
  return (
    {
      discovered: '已发现',
      processing_queued: '待加工',
      processing: '加工中',
      parsed: '已解析',
      awaiting_review: '待审核',
      pending: '待处理',
      resolved: '已解决',
      changes_requested: '已要求修改',
      publish_queued: '待发布',
      publishing: '发布中',
      published: '已发布',
      publish_failed: '发布失败',
      parse_failed: '解析失败',
      replaced: '已被替代',
      removal_pending: '移除中',
      removed: '已移除',
      removal_failed: '移除失败',
      approved: '已通过',
      rejected: '已驳回',
      OPEN: '待处理',
      NEW_VERSION_RECEIVED: '已收到新版本',
      FULFILLED: '已完成',
      CANCELLED: '已取消'
    }[value] || value
  )
}
function auditFieldLabel(value) {
  return (
    {
      owner_id: '负责人 ID',
      owner_name: '负责人',
      valid_from: '生效时间',
      valid_until: '失效时间',
      review_due_at: '复核时间'
    }[value] || value
  )
}
function auditValueLabel(value) {
  if (value === null || value === undefined || value === '') return '未设置'
  return String(value)
}
function auditRecordDetail(record) {
  const payload = record.payload || {}
  const changedFields = payload.changed_fields
  if (!Array.isArray(changedFields)) return ''
  if (!changedFields.length) return '治理信息未发生变化（仅确认操作）'
  const before = payload.before || {}
  const after = payload.after || {}
  return `变更：${changedFields
    .map(
      (field) =>
        `${auditFieldLabel(field)} ${auditValueLabel(before[field])} → ${auditValueLabel(after[field])}`
    )
    .join('；')}`
}
function auditRecordTitle(record) {
  if (record.record_type === 'CHANGE_REQUEST') {
    const request = auditRecordRequest(record)
    return `第 ${request.round_number || 1} 轮资料修改`
  }
  return eventLabel(record.event_type)
}
function eventLabel(value) {
  return (
    {
      source_change_requested: '已发起资料修改',
      source_change_version_received: '已收到飞书新版本',
      review_item_reopened: '已重新打开审核',
      source_change_request_fulfilled: '资料修改已验证',
      source_change_request_cancelled: '资料修改任务已取消',
      review_item_decided: '已记录审核结果',
      review_transferred: '审核任务已转交',
      changes_requested: '已要求修改资料',
      review_package_transferred: '审核包已转交',
      review_package_completed: '审核包已完成',
      review_draft_saved: '已保存审核草稿',
      review_layout_edit_saved: '已保存版式编辑',
      cross_document_relation_resolved: '跨文档关系已处理',
      conflict_counterpart_superseded: '冲突另一侧已移出检索',
      conflict_review_superseded: '冲突审核项已被替代',
      duplicate_review_items_auto_decided: '重复来源审核已自动处理',
      knowledge_unit_lifecycle_queued: '知识生命周期任务已排队',
      knowledge_unit_metadata_updated: '已更新知识单元治理信息',
      approved: '资料审核通过',
      rejected: '资料已驳回',
      retry_queued: '资料重试已排队',
      processing_started: '资料开始加工',
      reindex_queued: '索引重建已排队',
      unit_publish_queued: '知识单元发布已排队',
      publishing: '资料正在发布',
      published: '资料已发布',
      publish_failed: '资料发布失败',
      publish_obsolete: '候选版本已过期',
      publish_obsolete_cleanup_failed: '过期候选清理失败',
      replacement_cleanup_failed: '旧索引清理失败',
      source_offline_started: '资料下架已开始',
      source_offline_failed: '资料下架失败',
      source_offline_completed: '资料已下架',
      source_restore_queued: '资料恢复已排队',
      source_rollback_queued: '资料版本回滚已排队',
      removal_started: '失效资料移除已开始',
      removal_confirmed: '失效资料已移除',
      removal_failed: '失效资料移除失败',
      parsed: '资料解析完成',
      parse_failed: '资料解析失败',
      processing_enqueue_failed: '资料加工入队失败',
      material_discovered: '发现新资料',
      processing_queued: '资料已进入加工队列',
      scan_failed: '扫描失败',
      startup_reconciled: '服务恢复时已处理异常任务',
      user_oauth_authorized: '已完成飞书授权'
    }[value] || '加工状态已更新'
  )
}
loadReviewers()
</script>

<style scoped lang="less">
.review-workspace {
  display: grid;
  position: relative;
  height: clamp(700px, calc(100vh - 274px), 900px);
  min-height: 680px;
  margin: 10px var(--page-padding) 24px;
  overflow: hidden;
  grid-template-columns: 248px minmax(0, 1fr);
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
}
.review-workspace.queue-collapsed {
  grid-template-columns: minmax(0, 1fr);
}
.review-queue {
  min-width: 0;
  overflow-y: auto;
  border-right: 1px solid var(--gray-150);
  background: var(--gray-10);
}
.queue-heading {
  position: sticky;
  z-index: 3;
  top: 0;
  padding: 12px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-10);
}
.queue-heading-row,
.queue-heading-row > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.queue-heading-row > div:first-child {
  align-items: baseline;
}
.queue-heading-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.queue-mobile-collapse {
  display: none;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
  place-items: center;
}
.queue-mobile-collapse:hover {
  border-color: var(--main-200);
  color: var(--main-700);
}
.queue-heading h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.queue-heading span {
  color: var(--color-text-tertiary);
  font-size: 11px;
}
.queue-views {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 4px;
  margin-top: 11px;
}
.queue-views button {
  display: flex;
  min-height: 28px;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 0 7px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 11px;
}
.queue-views button:hover {
  background: var(--gray-25);
}
.queue-views button.active {
  border-color: var(--main-100);
  background: var(--main-30);
  color: var(--main-700);
  font-weight: 600;
}
.queue-views button span {
  color: inherit;
}
.queue-result-filter {
  display: flex;
  gap: 3px;
  margin-top: 7px;
  overflow-x: auto;
  scrollbar-width: none;
}
.queue-result-filter::-webkit-scrollbar {
  display: none;
}
.queue-result-filter button {
  min-height: 24px;
  flex: 0 0 auto;
  padding: 0 6px;
  border: 1px solid var(--gray-150);
  border-radius: 4px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 10px;
}
.queue-result-filter button:hover {
  border-color: var(--main-200);
  color: var(--main-700);
}
.queue-result-filter button.active {
  border-color: var(--main-100);
  background: var(--main-30);
  color: var(--main-700);
  font-weight: 600;
}
.queue-filters {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 8px;
}
.source-update-notice {
  display: flex;
  min-height: 32px;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-warning-100);
  background: var(--color-warning-50);
  color: var(--color-warning-900);
  font-size: 11px;
}
.source-update-notice span {
  flex: 1;
}
.source-update-notice button {
  padding: 2px 5px;
  border: 0;
  background: transparent;
  color: var(--main-700);
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}
.source-update-notice button:hover {
  text-decoration: underline;
}
.queue-empty {
  padding: 54px 10px;
}
.queue-item {
  display: block;
  width: 100%;
  padding: 11px 12px;
  border: 0;
  border-bottom: 1px solid var(--gray-100);
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.queue-item:hover {
  background: var(--gray-25);
}
.queue-item.active {
  background: var(--main-30);
  box-shadow: inset 3px 0 0 var(--main-color);
}
.queue-load-more {
  display: flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  padding: 8px 10px 12px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  text-align: center;
}
.queue-load-more-button {
  width: 100%;
  padding: 7px 8px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--main-700);
  cursor: pointer;
  font-size: 10px;
}
.queue-load-more-button:hover {
  border-color: var(--main-200);
  background: var(--main-30);
}
.queue-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.queue-title strong {
  overflow: hidden;
  color: var(--color-text);
  font-size: 12px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.risk-badge {
  flex: 0 0 auto;
  padding: 2px 5px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.risk-high {
  background: var(--color-error-50);
  color: var(--color-error-700);
}
.risk-medium {
  background: var(--color-warning-50);
  color: var(--color-warning-700);
}
.queue-item > p {
  overflow: hidden;
  margin: 5px 0 7px;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-type-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.queue-type-counts span {
  padding: 2px 5px;
  border-radius: 3px;
  background: var(--gray-50);
  color: var(--color-text-secondary);
  font-size: 10px;
}
.queue-meta .source-update-badge {
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--color-warning-50);
  color: var(--color-warning-700);
  font-size: 9px;
}
.queue-unit-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.queue-unit-summary span {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--gray-50);
}
.queue-unit-summary .queue-unit-count {
  color: var(--color-text-secondary);
}
.queue-unit-count strong {
  color: var(--color-text-primary);
  font-size: 12px;
}
.queue-unit-summary .queue-unit-attention {
  background: var(--color-warning-50);
  color: var(--color-warning-700);
}
.queue-unit-summary .queue-unit-ready {
  color: var(--color-success-700);
}
.queue-unit-summary .queue-unit-excluded {
  background: var(--gray-100);
  color: var(--color-text-secondary);
}
.queue-unit-summary .queue-unit-duplicate {
  background: var(--color-info-50);
  color: var(--color-info-700);
}
.queue-meta .completion-result-badge {
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--gray-100);
  color: var(--color-text-secondary);
  font-size: 9px;
}
.queue-meta .completion-all_included {
  background: var(--color-success-50);
  color: var(--color-success-700);
}
.queue-meta .completion-partial {
  background: var(--color-warning-50);
  color: var(--color-warning-700);
}
.queue-meta .completion-all_duplicate {
  background: var(--color-info-50);
  color: var(--color-info-700);
}
.queue-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 8px;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--gray-300);
}
.status-open {
  background: var(--main-color);
}
.status-waiting_source_change,
.status-waiting_business_confirmation {
  background: var(--color-warning-500);
}
.status-completed {
  background: var(--color-success-500);
}
.queue-time {
  margin-left: auto;
}
.review-detail {
  display: flex;
  position: relative;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
}
.record-heading {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  min-height: 46px;
  padding: 5px 18px;
  border-bottom: 1px solid var(--gray-100);
}
.record-heading-main {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}
.icon-tool {
  display: inline-grid;
  width: 30px;
  height: 30px;
  flex: 0 0 30px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition:
    border-color 180ms ease,
    background-color 180ms ease,
    color 180ms ease;
}
.icon-tool:hover {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.icon-tool:focus-visible {
  outline: 2px solid var(--main-300);
  outline-offset: 2px;
}
.record-title {
  min-width: 0;
}
.record-title-line {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
}
.record-title h2 {
  overflow: hidden;
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.record-title p {
  overflow: hidden;
  margin: 2px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.record-count {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
  font-size: 10px;
  white-space: nowrap;
}
.record-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
  color: var(--color-text-tertiary);
  font-size: 11px;
}
.record-actions :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.record-actions .whole-review-button {
  border-color: var(--main-200);
  color: var(--main-700);
  background: var(--main-10);
}
.record-actions .whole-review-button:disabled {
  border-color: var(--gray-150);
  color: var(--color-text-tertiary);
  background: var(--gray-25);
  cursor: not-allowed;
}
.record-actions .whole-review-button:hover {
  border-color: var(--main-400);
  color: var(--main-800);
  background: var(--main-30);
}
.batch-action-secondary {
  min-height: 28px;
  padding: 0 9px;
  border: 1px solid var(--gray-200);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 11px;
  white-space: nowrap;
}
.batch-action-secondary:hover:not(:disabled) {
  border-color: var(--main-200);
  background: var(--main-20);
  color: var(--main-700);
}
.batch-action-secondary:disabled {
  color: var(--color-text-tertiary);
  background: var(--gray-25);
  cursor: not-allowed;
  opacity: 0.62;
}
.source-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--main-700);
  white-space: nowrap;
}
.reopen-trail {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-start;
  gap: 7px;
  padding: 6px 18px;
  border-bottom: 1px solid var(--main-100);
  background: var(--main-10);
  color: var(--main-700);
}
.reopen-trail svg {
  margin-top: 1px;
}
.reopen-trail div {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 7px;
}
.reopen-trail strong {
  font-size: 11px;
}
.reopen-trail span {
  overflow: hidden;
  color: var(--color-text-secondary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.unit-overview {
  display: flex;
  min-height: 42px;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
  padding: 3px 18px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-0);
}
.unit-overview-heading {
  display: flex;
  flex: 0 0 auto;
  align-items: baseline;
  gap: 5px;
  padding-right: 12px;
  border-right: 1px solid var(--gray-150);
  color: var(--color-text-secondary);
  font-size: 10px;
}
.unit-overview-heading strong {
  color: var(--color-text-primary);
  font-size: 13px;
}
.unit-overview-metrics {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 11px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  white-space: nowrap;
}
.unit-overview-metrics span {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
}
.unit-overview-metrics b {
  color: var(--color-text-primary);
  font-size: 12px;
}
.unit-overview-metrics .needs-attention {
  color: var(--color-warning-700);
}
.unit-overview-metrics .needs-attention b {
  color: var(--color-warning-700);
}
.unit-overview-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 7px;
}
.unit-visibility {
  display: inline-grid;
  width: 17px;
  height: 17px;
  flex: 0 0 17px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-200);
  border-radius: 50%;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  font-size: 10px;
  font-weight: 600;
  cursor: help;
}
.unit-visibility:hover {
  border-color: var(--main-200);
  color: var(--main-700);
}
.unit-filter {
  flex: 0 0 auto;
  padding: 3px 7px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  font-size: 10px;
  white-space: nowrap;
  cursor: pointer;
}
.unit-view-switch {
  display: inline-flex;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
}
.unit-view-switch .unit-filter {
  border: 0;
  border-right: 1px solid var(--gray-150);
  border-radius: 0;
}
.unit-view-switch .unit-filter:last-child {
  border-right: 0;
}
.unit-overview-actions :deep(.ant-btn) {
  flex: 0 0 auto;
  white-space: nowrap;
}
.unit-filter:hover,
.unit-filter.active {
  border-color: var(--main-200);
  background: var(--main-10);
  color: var(--main-700);
}
.unit-filter-empty {
  flex: 0 0 auto;
  padding: 7px 18px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--main-10);
  color: var(--color-text-secondary);
  font-size: 10px;
}
.item-navigation {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 0;
  padding: 0 18px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-10);
}
.item-navigation-compact {
  display: flex;
  min-height: 35px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.item-navigation-position {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 7px;
}
.item-navigation-position > span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.item-navigation-position > strong {
  color: var(--color-text-primary);
  font-size: 12px;
  white-space: nowrap;
}
.item-navigation-position > small {
  overflow: hidden;
  max-width: min(340px, 38vw);
  color: var(--color-text-secondary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item-navigation-controls {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
}
.item-navigation-step,
.item-navigation-expand {
  display: inline-flex;
  height: 24px;
  align-items: center;
  gap: 3px;
  padding: 0 7px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  font-size: 10px;
  white-space: nowrap;
  cursor: pointer;
}
.item-navigation-step:hover:not(:disabled),
.item-navigation-expand:hover {
  border-color: var(--main-200);
  background: var(--main-10);
  color: var(--main-700);
}
.item-navigation-step:disabled {
  color: var(--gray-300);
  cursor: not-allowed;
}
.item-navigation-expand {
  border-color: transparent;
  background: transparent;
  color: var(--main-700);
}
.item-navigation-list {
  display: grid;
  max-height: 170px;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 5px;
  padding: 0 0 8px;
  overflow-y: auto;
}
.item-navigation-list > button {
  display: flex;
  min-width: 150px;
  align-items: center;
  gap: 7px;
  padding: 6px 8px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  text-align: left;
  cursor: pointer;
}
.item-navigation-list button {
  min-width: 0;
}
.item-navigation-list > button:hover,
.item-navigation-list > button.active {
  border-color: var(--main-200);
  background: var(--main-10);
}
.item-navigation-list > button.attention {
  border-left: 3px solid var(--color-warning-500);
}
.item-navigation-list > button > span {
  display: grid;
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  place-items: center;
  border-radius: 50%;
  background: var(--gray-100);
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.item-navigation-list > button.active > span {
  background: var(--main-color);
  color: var(--gray-0);
}
.item-navigation-list > button div {
  display: grid;
  min-width: 0;
  gap: 1px;
}
.item-navigation-list strong,
.item-navigation-list small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item-navigation-list strong {
  font-size: 11px;
}
.item-navigation-list small {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.knowledge-lineage {
  display: flex;
  min-height: 29px;
  flex: 0 0 auto;
  align-items: center;
  gap: 7px;
  padding: 2px 22px;
  overflow-x: auto;
  border-bottom: 1px solid var(--gray-100);
  background: color-mix(in srgb, var(--main-10) 55%, var(--gray-0));
  color: var(--gray-300);
}
.lineage-step {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 5px;
}
.lineage-step span {
  color: var(--color-text-tertiary);
  font-size: 8px;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
.lineage-step strong {
  overflow: hidden;
  max-width: 220px;
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lineage-step.active {
  position: relative;
  padding-left: 9px;
}
.lineage-step.active::before {
  position: absolute;
  top: 2px;
  bottom: 2px;
  left: 0;
  width: 3px;
  border-radius: 2px;
  background: var(--main-color);
  content: '';
}
.lineage-step.active span,
.lineage-step.active strong {
  color: var(--main-700);
}
.lineage-step.segment-step strong {
  max-width: 180px;
}
.detail-grid {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: minmax(0, 1fr);
}
.evidence-panel {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}
.evidence-context {
  display: flex;
  flex: 0 0 auto;
  min-height: 48px;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 6px 22px 5px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-10);
}
.item-summary {
  min-width: 0;
}
.item-summary > div {
  display: flex;
  align-items: center;
  gap: 8px;
}
.item-summary strong {
  font-size: 14px;
}
.item-summary > p {
  overflow: hidden;
  margin: 2px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.unit-recommendation {
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--color-success-50);
  color: var(--color-success-700);
  font-size: 9px;
  font-weight: 600;
  white-space: nowrap;
}
.unit-recommendation.attention {
  background: var(--color-warning-50);
  color: var(--color-warning-700);
}
.review-type {
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--main-30);
  color: var(--main-700);
  font-size: 10px;
  font-weight: 600;
}
.type-conflict {
  background: var(--color-error-50);
  color: var(--color-error-700);
}
.type-stale {
  background: var(--color-warning-50);
  color: var(--color-warning-700);
}
.record-meta {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  margin-top: 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.record-meta span + span::before {
  display: inline-block;
  width: 3px;
  height: 3px;
  margin: 0 7px;
  border-radius: 50%;
  background: var(--gray-300);
  vertical-align: middle;
  content: '';
}
.content-quality-alert {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 10px 22px 0;
  padding: 8px 9px;
  border: 1px solid var(--color-warning-100);
  border-radius: 5px;
  background: var(--color-warning-10);
  color: var(--color-warning-700);
}
.quality-gate-summary {
  display: flex;
  gap: 12px;
  margin: 10px 14px 0;
  padding: 10px 12px;
  border: 1px solid #dce4ee;
  border-radius: 6px;
  background: #f8fafc;
}
.quality-gate-score {
  display: grid;
  flex: 0 0 62px;
  place-content: center;
  border-right: 1px solid #dce4ee;
  text-align: center;
}
.quality-gate-score strong {
  color: #245dab;
  font-size: 24px;
  line-height: 1;
}
.quality-gate-score span {
  margin-top: 4px;
  color: #718096;
  font-size: 11px;
}
.quality-gate-score.is-blocked strong,
.quality-gate-score.is-return strong {
  color: #b42318;
}
.quality-gate-score.is-review strong {
  color: #a15c00;
}
.quality-gate-main {
  min-width: 0;
  flex: 1;
}
.quality-gate-main header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.quality-gate-main header strong {
  color: #26364a;
  font-size: 13px;
}
.quality-gate-main header span {
  color: #247548;
  font-size: 11px;
}
.quality-dimensions,
.impact-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 12px;
  margin-top: 6px;
  color: #64748b;
  font-size: 11px;
}
.quality-blockers {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 10px;
  margin-top: 7px;
}
.quality-blockers span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #b42318;
  font-size: 11px;
}
.content-quality-alert div {
  display: grid;
  gap: 1px;
}
.content-quality-alert strong {
  font-size: 11px;
}
.content-quality-alert span {
  color: var(--color-text-secondary);
  font-size: 10px;
}
.evidence-tabs {
  display: flex;
  position: relative;
  z-index: 30;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 3px;
  margin-top: 0;
  padding: 0 14px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-0);
}
.evidence-tabs-main {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 3px;
}
.evidence-tabs-actions {
  flex: 0 0 auto;
  gap: 8px;
  margin-left: auto;
  padding-left: 12px;
  border-left: 1px solid var(--gray-150);
}
.evidence-tabs-main > button {
  position: relative;
  display: inline-flex;
  min-height: 30px;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  border: 0;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 11px;
}
.evidence-tabs-main > button:hover,
.evidence-tabs-main > button.active {
  color: var(--main-700);
}
.evidence-tabs-main > button.active {
  font-weight: 600;
  box-shadow: inset 0 -2px 0 var(--main-color);
}
.evidence-tabs-main > .evidence-queue-toggle {
  width: 32px;
  min-height: 32px;
  flex: 0 0 32px;
  justify-content: center;
  margin-right: 4px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--color-text-secondary) !important;
}
.evidence-queue-toggle svg {
  flex: 0 0 18px;
}
.evidence-queue-toggle:hover,
.evidence-queue-toggle:focus-visible {
  border-color: var(--main-50) !important;
  background: var(--main-20) !important;
  color: var(--main-color) !important;
  outline: none;
}
.tab-count {
  display: inline-grid;
  min-width: 17px;
  height: 17px;
  place-items: center;
  padding: 0 4px;
  border-radius: 9px;
  background: var(--gray-100);
  font-size: 9px;
}
.evidence-body {
  min-height: 0;
  flex: 1;
  position: relative;
  overflow-x: hidden;
  overflow-y: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.content-review {
  min-height: 100%;
  padding: 8px 28px 36px;
}
.presentation-review {
  display: grid;
  max-width: 1120px;
  gap: 10px;
  margin: 0 auto;
}
.presentation-toolbar {
  display: flex;
  position: sticky;
  z-index: 4;
  top: 0;
  min-height: 36px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 2px;
  background: var(--gray-0);
}
.presentation-toolbar > div,
.presentation-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.presentation-toolbar strong {
  color: var(--color-text-primary);
  font-size: 12px;
}
.presentation-toolbar span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.presentation-toolbar-actions button {
  display: inline-grid;
  width: 26px;
  height: 26px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 50%;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.presentation-toolbar-actions button:hover:not(:disabled) {
  border-color: var(--main-200);
  color: var(--main-700);
}
.presentation-toolbar-actions button:disabled {
  cursor: default;
  opacity: 0.36;
}
.presentation-stage-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
  gap: 10px;
}
.presentation-stage-row.has-side-panel {
  grid-template-columns: minmax(0, 1fr) minmax(220px, 260px);
}
.presentation-pages {
  display: grid;
  max-height: 610px;
  gap: 4px;
  padding-right: 4px;
  overflow-y: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.presentation-pages button {
  width: 32px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 10px;
}
.presentation-pages button:hover,
.presentation-pages button.active {
  background: var(--main-50);
  color: var(--main-700);
}
.presentation-pages button.active {
  font-weight: 700;
  box-shadow: inset 2px 0 0 var(--main-color);
}
.presentation-page-strip,
.document-layout-page-strip {
  display: flex;
  gap: 4px;
  padding: 2px 0;
  overflow-x: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.presentation-page-strip button,
.document-layout-page-strip button {
  min-width: 42px;
  max-width: 150px;
  padding: 5px 8px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.presentation-page-strip button:hover,
.presentation-page-strip button.active,
.document-layout-page-strip button:hover,
.document-layout-page-strip button.active {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.presentation-page-strip button.active,
.document-layout-page-strip button.active {
  font-weight: 650;
}
.presentation-canvas-shell {
  min-width: 0;
  padding: 8px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: color-mix(in srgb, var(--gray-900) 4%, var(--gray-0));
  box-shadow: 0 12px 28px color-mix(in srgb, var(--gray-900) 9%, transparent);
}
.presentation-canvas {
  position: relative;
  width: 100%;
  overflow: hidden;
  background: var(--gray-0);
}
.presentation-canvas > img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.presentation-image-state {
  display: grid;
  position: absolute;
  inset: 0;
  place-items: center;
  align-content: center;
  gap: 7px;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.presentation-fragment-hotspot {
  position: absolute;
  min-width: 8px;
  min-height: 8px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--main-color) 24%, transparent);
  border-radius: 2px;
  background: transparent;
  color: var(--main-800);
  cursor: pointer;
  transition:
    border-color 140ms ease,
    background-color 140ms ease,
    box-shadow 140ms ease;
}
.presentation-fragment-hotspot > span {
  display: grid;
  position: absolute;
  top: -8px;
  right: -8px;
  width: 17px;
  height: 17px;
  place-items: center;
  border: 1px solid var(--main-100);
  border-radius: 50%;
  background: var(--gray-0);
  box-shadow: 0 2px 6px color-mix(in srgb, var(--gray-900) 12%, transparent);
  font-size: 8px;
  opacity: 0;
  transition: opacity 140ms ease;
}
.presentation-fragment-hotspot:hover,
.presentation-fragment-hotspot:focus-visible,
.presentation-fragment-hotspot.active {
  z-index: 2;
  border-color: var(--main-color);
  background: color-mix(in srgb, var(--main-color) 12%, transparent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--main-color) 13%, transparent);
  outline: none;
}
.presentation-fragment-hotspot:hover > span,
.presentation-fragment-hotspot:focus-visible > span,
.presentation-fragment-hotspot.active > span {
  opacity: 1;
}
.presentation-fragment-strip {
  display: flex;
  gap: 5px;
  padding: 2px 0 4px;
  overflow-x: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.presentation-fragment-strip button {
  display: inline-flex;
  max-width: 220px;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  padding: 4px 7px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 999px;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.presentation-fragment-strip button:hover,
.presentation-fragment-strip button.active {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.presentation-fragment-strip button > span {
  display: inline-grid;
  width: 15px;
  height: 15px;
  flex: 0 0 15px;
  place-items: center;
  border-radius: 50%;
  background: var(--gray-100);
  font-size: 8px;
}
.presentation-fragment-strip button.active > span {
  background: var(--main-color);
  color: var(--gray-0);
}
.presentation-fragment-focus {
  padding: 9px 11px;
  border-left: 3px solid var(--main-color);
  border-radius: 0 5px 5px 0;
  background: var(--main-20);
}
.presentation-fragment-focus header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: var(--main-700);
  font-size: 9px;
}
.presentation-fragment-focus header button {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 9px;
}
.presentation-fragment-focus p {
  margin: 5px 0 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.65;
  white-space: pre-wrap;
}
.document-layout-review {
  display: grid;
  max-width: 1120px;
  gap: 10px;
  margin: 0 auto;
}
.document-layout-toolbar,
.document-layout-toolbar > div,
.document-layout-toolbar-actions {
  display: flex;
  align-items: center;
}
.document-layout-toolbar {
  min-height: 36px;
  position: sticky;
  z-index: 4;
  top: 0;
  justify-content: space-between;
  gap: 12px;
  padding: 0 2px;
  background: var(--gray-0);
}
.document-layout-toolbar > div,
.document-layout-toolbar-actions {
  gap: 8px;
}
.document-layout-toolbar strong {
  color: var(--color-text-primary);
  font-size: 12px;
}
.document-layout-toolbar span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.document-layout-toolbar-actions button {
  display: inline-grid;
  width: 26px;
  height: 26px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 50%;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.document-layout-toolbar-actions button:hover:not(:disabled) {
  border-color: var(--main-200);
  color: var(--main-700);
}
.document-layout-toolbar-actions button:disabled {
  cursor: default;
  opacity: 0.36;
}
.document-layout-stage {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
  gap: 10px;
}
.document-layout-stage.has-side-panel {
  grid-template-columns: minmax(0, 1fr) minmax(220px, 260px);
}
.layout-side-panel {
  display: grid;
  position: sticky;
  z-index: 5;
  top: 8px;
  align-self: start;
  min-width: 0;
  max-height: min(calc(100vh - 190px), 680px);
  align-content: start;
  gap: 8px;
  overflow-y: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.layout-context-sidebar {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  box-shadow: 0 8px 20px color-mix(in srgb, var(--gray-900) 6%, transparent);
}
.layout-sidebar-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.layout-sidebar-heading :deep(.ant-tag) {
  margin: 0;
  font-size: 9px;
  line-height: 18px;
}
.layout-context-sidebar h3 {
  margin: 0;
  overflow: hidden;
  color: var(--color-text-primary);
  font-size: 13px;
  font-weight: 650;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.layout-sidebar-path {
  margin: -3px 0 0;
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.layout-sidebar-tags {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 5px;
}
.layout-sidebar-facts {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 8px 0;
  border-top: 1px solid var(--gray-100);
  border-bottom: 1px solid var(--gray-100);
}
.layout-sidebar-facts div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.layout-sidebar-facts dt {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.layout-sidebar-facts dd {
  max-width: 150px;
  margin: 0;
  overflow: hidden;
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.layout-sidebar-summary {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.55;
}
.layout-sidebar-navigation {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 5px;
}
.layout-sidebar-navigation button,
.layout-sidebar-list-toggle {
  display: inline-flex;
  min-height: 26px;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 0 6px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 9px;
}
.layout-sidebar-navigation button:hover:not(:disabled),
.layout-sidebar-list-toggle:hover {
  border-color: var(--main-200);
  background: var(--main-10);
  color: var(--main-700);
}
.layout-sidebar-navigation button:disabled {
  color: var(--gray-300);
  cursor: not-allowed;
}
.layout-sidebar-primary {
  display: inline-flex;
  min-height: 30px;
  align-items: center;
  justify-content: center;
  gap: 5px;
  width: 100%;
  padding: 0 10px;
  border: 1px solid var(--main-300);
  border-radius: 5px;
  background: var(--main-color);
  color: var(--gray-0);
  cursor: pointer;
  font-size: 10px;
  font-weight: 600;
  transition:
    border-color 140ms ease,
    background-color 140ms ease,
    box-shadow 140ms ease;
}
.layout-sidebar-primary:hover:not(:disabled) {
  border-color: var(--main-700);
  background: var(--main-700);
  box-shadow: 0 3px 10px color-mix(in srgb, var(--main-color) 22%, transparent);
}
.layout-sidebar-primary.is-reopen {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.layout-sidebar-primary:focus-visible {
  outline: 2px solid var(--main-300);
  outline-offset: 2px;
}
.layout-sidebar-primary:disabled {
  border-color: var(--gray-150);
  background: var(--gray-100);
  color: var(--color-text-tertiary);
  cursor: not-allowed;
}
.layout-sidebar-list-toggle {
  width: 100%;
  border-color: transparent;
  background: transparent;
  color: var(--main-700);
}
.layout-sidebar-unit-list {
  display: grid;
  max-height: 180px;
  gap: 4px;
  overflow-y: auto;
  padding-top: 3px;
  border-top: 1px solid var(--gray-100);
}
.layout-sidebar-unit-list button {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  padding: 5px 6px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
  text-align: left;
}
.layout-sidebar-unit-list button:hover,
.layout-sidebar-unit-list button.active {
  border-color: var(--main-200);
  background: var(--main-10);
  color: var(--main-700);
}
.layout-sidebar-unit-list button.attention {
  border-left: 3px solid var(--color-warning-500);
}
.layout-sidebar-unit-list button > span {
  display: inline-grid;
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  place-items: center;
  border-radius: 50%;
  background: var(--gray-100);
  font-size: 9px;
}
.layout-sidebar-unit-list button.active > span {
  background: var(--main-color);
  color: var(--gray-0);
}
.layout-sidebar-unit-list strong {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  font-size: 10px;
  font-weight: 550;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.layout-sidebar-unit-list small {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
  font-size: 9px;
  white-space: nowrap;
}
.document-layout-canvas {
  position: relative;
  min-width: 0;
  min-height: 360px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  box-shadow: 0 12px 28px color-mix(in srgb, var(--gray-900) 8%, transparent);
}
.document-layout-canvas > img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.spreadsheet-viewport {
  min-width: 0;
  max-height: min(64vh, 680px);
  overflow: auto;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-50);
  box-shadow: 0 12px 28px color-mix(in srgb, var(--gray-900) 8%, transparent);
  scrollbar-color: var(--gray-300) transparent;
  scrollbar-width: thin;
}
.document-layout-canvas.spreadsheet-canvas {
  width: max(100%, var(--sheet-width, 760px));
  height: max(520px, var(--sheet-height, 520px));
  min-width: 760px;
  min-height: 0;
  overflow: visible;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  background-color: var(--gray-0);
  background-image:
    linear-gradient(var(--gray-100) 1px, transparent 1px),
    linear-gradient(90deg, var(--gray-100) 1px, transparent 1px);
  background-size: var(--sheet-column-width, 156px) var(--sheet-row-height, 38px);
}
.document-layout-block {
  position: absolute;
  min-width: 10px;
  min-height: 10px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--main-color) 25%, transparent);
  border-radius: 3px;
  background: color-mix(in srgb, var(--main-color) 4%, transparent);
  color: var(--main-800);
  cursor: pointer;
  font-size: 9px;
  overflow: hidden;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition:
    border-color 140ms ease,
    background-color 140ms ease,
    box-shadow 140ms ease;
}
.document-layout-block > span {
  display: inline-flex;
  position: absolute;
  top: -1px;
  right: -1px;
  padding: 1px 3px;
  border-radius: 0 3px 0 3px;
  background: var(--main-color);
  color: var(--gray-0);
  font-size: 8px;
  opacity: 0;
}
.document-layout-block:hover,
.document-layout-block:focus-visible,
.document-layout-block.active {
  z-index: 2;
  border-color: var(--main-color);
  background: color-mix(in srgb, var(--main-color) 13%, transparent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--main-color) 13%, transparent);
  outline: none;
}
.document-layout-block:hover > span,
.document-layout-block:focus-visible > span,
.document-layout-block.active > span {
  opacity: 1;
}
.document-layout-block.edited {
  border-color: var(--color-warning-500);
  background: color-mix(in srgb, var(--color-warning-500) 10%, transparent);
}
.spreadsheet-cell {
  min-width: 0;
  min-height: 0;
  padding: 7px 9px;
  border-color: var(--gray-150);
  border-radius: 0;
  background: color-mix(in srgb, var(--gray-0) 80%, transparent);
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  text-overflow: clip;
  white-space: normal;
}
.spreadsheet-cell:hover,
.spreadsheet-cell.active {
  background: var(--main-10);
}
.spreadsheet-cell.edited {
  background: color-mix(in srgb, var(--color-warning-500) 12%, var(--gray-0));
}
.spreadsheet-toolbar-note {
  color: var(--color-text-tertiary);
}
.preview-scale-note {
  color: var(--color-warning-700) !important;
}
.document-layout-editor {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  box-shadow: 0 8px 20px color-mix(in srgb, var(--gray-900) 6%, transparent);
}
.document-layout-editor header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.document-layout-editor header > div {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.document-layout-editor header strong {
  color: var(--color-text-primary);
  font-size: 12px;
}
.document-layout-editor header span {
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.document-edit-badge {
  flex: 0 0 auto;
  padding: 2px 5px;
  border-radius: 999px;
  background: var(--color-warning-50);
  color: var(--color-warning-700) !important;
}
.document-layout-editor textarea {
  width: 100%;
  min-height: 150px;
  resize: vertical;
  padding: 8px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-10);
  color: var(--color-text-primary);
  font: inherit;
  font-size: 11px;
  line-height: 1.55;
}
.document-layout-editor textarea:focus {
  border-color: var(--main-300);
  outline: 2px solid color-mix(in srgb, var(--main-color) 16%, transparent);
}
.document-layout-editor p {
  margin: 0;
  color: var(--color-text-tertiary);
  font-size: 9px;
  line-height: 1.5;
}
.document-layout-save {
  justify-self: start;
  min-height: 28px;
  padding: 0 10px;
  border: 0;
  border-radius: 5px;
  background: var(--main-color);
  color: var(--gray-0);
  cursor: pointer;
  font-size: 10px;
}
.document-layout-save:hover:not(:disabled) {
  background: var(--main-700);
}
.document-layout-save:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.source-segment-navigation {
  max-width: 920px;
  margin: 0 auto 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--gray-100);
}
.source-segment-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 7px;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.source-segment-heading span {
  display: flex;
  align-items: baseline;
  gap: 7px;
}
.source-segment-heading strong {
  color: var(--color-text-secondary);
  font-size: 11px;
}
.source-segment-heading button {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--main-700);
  cursor: pointer;
  font-size: 10px;
}
.source-segment-list {
  display: flex;
  gap: 5px;
  padding-bottom: 2px;
  overflow-x: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.source-segment-list button {
  display: inline-flex;
  min-width: 0;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  max-width: 180px;
  padding: 4px 7px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-segment-list button:hover,
.source-segment-list button.active {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.source-segment-list button > span {
  display: inline-grid;
  width: 16px;
  height: 16px;
  flex: 0 0 16px;
  place-items: center;
  border-radius: 50%;
  background: var(--gray-100);
  font-size: 9px;
}
.source-segment-list button.active > span {
  background: var(--main-color);
  color: var(--gray-0);
}
.source-segment-focus {
  max-width: 920px;
  margin: 0 auto;
  overflow: hidden;
  border: 1px solid var(--main-100);
  border-radius: 6px;
  background: var(--main-10);
}
.knowledge-unit-focus {
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
}
.knowledge-unit-focus > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 12px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-10);
}
.knowledge-unit-focus > header div {
  display: grid;
  min-width: 0;
  gap: 2px;
}
.knowledge-unit-focus > header span,
.knowledge-unit-focus > header small {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.knowledge-unit-focus > header strong {
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.knowledge-unit-focus .review-markdown {
  padding: 14px 16px;
}
.unit-source-locator {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 12px;
  border-top: 1px solid var(--gray-100);
  background: var(--main-10);
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.unit-source-locator strong {
  overflow: hidden;
  color: var(--main-700);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-segment-focus > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--main-100);
}
.source-segment-focus > header div {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
}
.source-segment-focus > header span,
.source-segment-focus > header small {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.source-segment-focus > header strong {
  overflow: hidden;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-segment-focus .review-markdown {
  padding: 16px 20px 22px;
}
.version-change-review {
  min-height: 100%;
  padding: 18px 22px 40px;
}
.version-flow {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) auto minmax(120px, 1fr) minmax(190px, auto);
  align-items: center;
  gap: 9px;
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-10);
}
.version-flow > svg {
  color: var(--color-text-tertiary);
}
.version-card {
  display: grid;
  gap: 2px;
  min-width: 0;
  padding: 7px 9px;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
}
.version-card span,
.version-card small {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.version-card strong {
  overflow: hidden;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.version-card.is-current {
  border-color: var(--main-200);
  background: var(--main-10);
}
.version-stat {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  color: var(--color-text-tertiary);
  font-size: 9px;
  white-space: nowrap;
}
.version-stat b {
  margin-right: 2px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.version-change-list {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}
.version-change-card {
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
}
.version-change-card > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 9px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-10);
}
.version-change-card > header strong {
  font-size: 10px;
}
.version-change-card > header span,
.version-change-card label {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.version-change-card.is-modified {
  border-color: var(--color-warning-100);
}
.version-change-card.is-added {
  border-color: var(--color-success-100);
}
.version-change-card.is-deleted {
  border-color: var(--color-error-100);
}
.change-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
}
.change-columns > div,
.change-single {
  min-width: 0;
  padding: 9px;
}
.change-columns > div + div {
  border-left: 1px solid var(--gray-100);
}
.change-columns > div:first-child {
  background: var(--color-error-10);
}
.change-columns > div:last-child,
.version-change-card.is-added .change-single {
  background: var(--color-success-10);
}
.version-change-card.is-deleted .change-single {
  background: var(--color-error-10);
}
.version-change-card pre {
  margin: 5px 0 0;
  overflow: auto;
  color: var(--color-text-secondary);
  font-family: inherit;
  font-size: 10px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.review-markdown {
  max-width: 920px;
  margin: 0 auto;
  color: var(--color-text-secondary);
  font-size: 14px;
  line-height: 1.78;
}
.content-notice {
  display: grid;
  min-height: 230px;
  place-items: center;
  align-content: center;
  gap: 7px;
  padding: 24px;
  color: var(--color-text-tertiary);
  text-align: center;
}
.content-notice.compact {
  min-height: 120px;
}
.content-notice strong {
  color: var(--color-text-secondary);
  font-size: 12px;
}
.content-notice p {
  max-width: 410px;
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
}
.content-notice.is-error svg,
.content-notice.is-error strong {
  color: var(--color-error-700);
}
.comparison-review,
.history-review {
  padding: 16px 22px 36px;
}
.comparison-review {
  min-width: 0;
}
:global(body.comparison-workspace-fullscreen) {
  overflow: hidden;
}
.comparison-review.is-fullscreen {
  display: grid;
  position: fixed;
  z-index: 1000;
  inset: 0;
  grid-template-rows: auto minmax(0, 1fr);
  min-width: 0;
  height: 100dvh;
  padding: 10px 12px 12px;
  overflow: hidden;
  background: var(--gray-10);
}
.comparison-review.is-fullscreen .comparison-navigator {
  position: relative;
  top: auto;
}
.comparison-review.is-fullscreen .comparison-evidence-layout {
  min-height: 0;
  height: 100%;
  align-items: stretch;
  overflow: hidden;
}
.comparison-review.is-fullscreen .comparison-layout-main,
.comparison-review.is-fullscreen :deep(.ant-spin-nested-loading),
.comparison-review.is-fullscreen :deep(.ant-spin-container) {
  min-height: 0;
  height: 100%;
}
.comparison-review.is-fullscreen .comparison-layout-review {
  grid-template-rows: auto auto minmax(0, 1fr);
  height: 100%;
  margin-top: 0;
  overflow: hidden;
}
.comparison-review.is-fullscreen .comparison-match-panel {
  position: relative;
  top: auto;
}
.comparison-review.is-fullscreen .comparison-layout-columns {
  min-height: 0;
  height: 100%;
}
.comparison-review.is-fullscreen .comparison-layout-pane {
  min-height: 0;
  height: 100%;
  overflow: hidden;
}
.comparison-review.is-fullscreen .comparison-layout-stage {
  min-height: 0;
  height: 100%;
}
.comparison-review.is-fullscreen .comparison-layout-canvas {
  width: 100%;
  height: auto;
  min-height: 0;
  max-height: 100%;
  place-self: center;
}
.comparison-review.is-fullscreen .comparison-layout-canvas.is-grid,
.comparison-review.is-fullscreen .comparison-layout-canvas.is-markdown {
  height: 100%;
}
.comparison-review.is-fullscreen .comparison-evidence-layout > .comparison-card {
  position: static;
  top: auto;
  max-height: none;
  height: 100%;
  overflow-y: auto;
}
.comparison-navigator {
  display: grid;
  position: sticky;
  z-index: 20;
  top: 0;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  min-height: 48px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-0);
  overflow: hidden;
}
.comparison-position {
  display: grid;
  gap: 1px;
  min-width: 84px;
}
.comparison-position span {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.comparison-position strong {
  color: var(--color-text-secondary);
  font-size: 11px;
}
.comparison-nav-title {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.comparison-nav-title span {
  min-width: 0;
  flex: 1 1 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.comparison-nav-title span:first-child {
  text-align: right;
}
.comparison-nav-title svg {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
}
.comparison-nav-actions {
  display: flex;
  gap: 4px;
}
.comparison-nav-actions button {
  display: grid;
  width: 29px;
  height: 29px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.comparison-nav-actions button:hover:not(:disabled) {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.comparison-nav-actions button:disabled {
  color: var(--gray-300);
  cursor: not-allowed;
}
.comparison-layout-review {
  display: grid;
  position: relative;
  gap: 8px;
  isolation: isolate;
  min-width: 0;
  overflow: visible;
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 7px;
  background: var(--gray-10);
}
.comparison-evidence-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(286px, 340px);
  align-items: start;
  gap: 10px;
}
.comparison-layout-main {
  min-width: 0;
}
.comparison-evidence-layout > .comparison-card {
  position: sticky;
  top: 56px;
  max-height: calc(100vh - 76px);
  margin-top: 10px;
  overflow-y: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.comparison-evidence-layout > .comparison-card .duplicate-match > div {
  grid-template-columns: 1fr;
}
.comparison-evidence-layout > .comparison-card .duplicate-match section + section {
  padding-top: 7px;
  border-top: 1px solid var(--gray-100);
  border-left: 0;
}
.comparison-evidence-layout > .comparison-card .difference-row {
  grid-template-columns: 1fr;
  gap: 3px;
}
.comparison-layout-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.comparison-layout-heading {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: 8px;
}
.comparison-layout-toolbar strong {
  color: var(--color-text-primary);
  font-size: 12px;
}
.comparison-layout-toolbar span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.comparison-sync-toggle {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  color: var(--color-text-secondary);
  font-size: 10px;
  cursor: pointer;
  user-select: none;
}
.comparison-sync-toggle input {
  accent-color: var(--main-color);
}
.comparison-view-toolbar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
}
.comparison-zoom-controls {
  display: inline-grid;
  grid-template-columns: 27px 46px 27px 27px;
  align-items: center;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 5px;
  background: var(--gray-0);
}
.comparison-zoom-controls button,
.comparison-fullscreen-toggle {
  display: grid;
  width: 27px;
  height: 27px;
  place-items: center;
  padding: 0;
  border: 0;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.comparison-zoom-controls button + button,
.comparison-zoom-controls button:last-child {
  border-left: 1px solid var(--gray-100);
}
.comparison-zoom-controls button:hover:not(:disabled),
.comparison-fullscreen-toggle:hover {
  background: var(--main-30);
  color: var(--main-700);
}
.comparison-zoom-controls button:disabled {
  color: var(--gray-300);
  cursor: not-allowed;
}
.comparison-zoom-controls output {
  display: grid;
  height: 27px;
  place-items: center;
  border-right: 1px solid var(--gray-100);
  border-left: 1px solid var(--gray-100);
  color: var(--color-text-secondary);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.comparison-fullscreen-toggle {
  border: 1px solid var(--gray-150);
  border-radius: 5px;
}
.comparison-match-panel {
  display: grid;
  position: sticky;
  z-index: 10;
  top: 56px;
  gap: 4px;
  margin: 0 0 8px;
  padding: 5px 0 6px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--gray-10);
}
.comparison-match-panel .comparison-match-strip {
  padding: 0 0 7px;
}
.comparison-layout-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  min-width: 0;
  overflow: hidden;
}
.comparison-layout-pane {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 6px;
  min-width: 0;
  padding: 7px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
}
.comparison-layout-pane > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}
.comparison-layout-pane > header > div:first-child {
  display: grid;
  min-width: 0;
  gap: 2px;
}
.comparison-layout-pane > header strong {
  overflow: hidden;
  color: var(--color-text-secondary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.comparison-layout-pane > header span {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.comparison-layout-stage {
  display: grid;
  min-width: 0;
  min-height: 0;
}
.comparison-layout-stage.has-grid {
  grid-template-rows: auto minmax(0, 1fr);
  gap: 5px;
}
.comparison-layout-stage.has-markdown {
  min-height: 320px;
}
.comparison-grid-cell-detail {
  display: grid;
  gap: 5px;
  max-height: 90px;
  padding: 6px 8px;
  overflow: auto;
  border: 1px solid var(--main-100);
  border-radius: 4px;
  background: var(--main-10);
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.5;
  scrollbar-color: var(--gray-300) transparent;
  scrollbar-width: thin;
}
.comparison-grid-cell-summary,
.comparison-grid-cell-current {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 7px;
}
.comparison-grid-cell-summary {
  padding-bottom: 4px;
  border-bottom: 1px solid var(--main-100);
}
.comparison-grid-cell-summary > span {
  color: var(--color-text-tertiary);
  white-space: nowrap;
}
.comparison-grid-cell-detail strong {
  color: var(--main-700);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.comparison-grid-cell-current > span {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.comparison-page-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 3px;
}
.comparison-page-actions button {
  display: grid;
  width: 24px;
  height: 24px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 4px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.comparison-page-actions button:hover:not(:disabled) {
  border-color: var(--main-200);
  background: var(--main-30);
  color: var(--main-700);
}
.comparison-page-actions button:disabled {
  color: var(--gray-300);
  cursor: not-allowed;
}
.comparison-layout-canvas {
  position: relative;
  min-width: 0;
  min-height: 260px;
  overflow: hidden;
  border: 1px solid var(--gray-100);
  border-radius: 5px;
  background: var(--gray-25);
  touch-action: none;
}
.comparison-layout-canvas.is-grid {
  width: 100%;
  height: min(52vh, 560px);
  min-height: 320px;
  overflow: auto;
  touch-action: auto;
  scrollbar-color: var(--gray-300) transparent;
  scrollbar-width: thin;
}
.comparison-layout-canvas.is-markdown {
  width: 100%;
  height: min(58vh, 640px);
  min-height: 320px;
  overflow: auto;
  background: var(--gray-0);
  touch-action: auto;
  scrollbar-color: var(--gray-300) transparent;
  scrollbar-width: thin;
}
.comparison-layout-canvas.is-pannable {
  cursor: grab;
}
.comparison-layout-canvas.is-pannable:active {
  cursor: grabbing;
}
.comparison-layout-content {
  position: absolute;
  transform: translate(-50%, -50%);
}
.comparison-layout-content.is-grid {
  position: relative;
  top: auto;
  left: auto;
  transform: none;
}
.comparison-layout-content.is-markdown {
  position: relative;
  top: auto;
  left: auto;
  transform: none;
}
.comparison-markdown-document {
  display: grid;
  gap: 8px;
  min-height: 100%;
  padding: 18px 20px 36px;
  background: var(--gray-0);
}
.comparison-markdown-block {
  position: relative;
  padding: 5px 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  cursor: pointer;
}
.comparison-markdown-block:hover,
.comparison-markdown-block:focus-visible {
  border-color: var(--main-200);
  background: var(--main-20);
  outline: none;
}
.comparison-markdown-block.comparison-block-match {
  border-color: var(--color-warning-500);
  background: var(--color-warning-50);
  box-shadow: inset 3px 0 0 var(--color-warning-500);
}
.comparison-markdown-block.comparison-block-selected {
  border-color: var(--main-color);
  background: var(--main-30);
  box-shadow: inset 3px 0 0 var(--main-color);
}
.comparison-markdown-block-number {
  display: inline-grid;
  position: absolute;
  z-index: 1;
  top: -7px;
  left: -7px;
  min-width: 16px;
  height: 16px;
  place-items: center;
  border-radius: 8px;
  background: var(--main-700);
  color: var(--gray-0);
  font-size: 9px;
  line-height: 1;
}
.comparison-markdown-block :deep(.yk-markdown-preview) {
  font-size: 12px;
  line-height: 1.65;
}
.comparison-layout-content > img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  user-select: none;
}
.comparison-layout-grid-hint {
  position: absolute;
  inset: 0;
  padding: 7px;
  background-color: var(--gray-0);
  background-image:
    linear-gradient(var(--gray-100) 1px, transparent 1px),
    linear-gradient(90deg, var(--gray-100) 1px, transparent 1px);
  background-size: var(--sheet-column-width, 156px) var(--sheet-row-height, 56px);
  color: var(--color-text-tertiary);
  font-size: 9px;
  pointer-events: none;
}
.comparison-layout-state {
  display: flex;
  position: absolute;
  inset: 0;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  text-align: center;
}
.comparison-layout-block {
  position: absolute;
  z-index: 1;
  min-width: 8px;
  min-height: 8px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 3px;
  background: transparent;
  cursor: pointer;
}
.comparison-layout-block:hover {
  border-color: var(--main-400);
  background: color-mix(in srgb, var(--main-200) 18%, transparent);
}
.comparison-layout-block.comparison-block-match {
  z-index: 2;
  border-color: var(--color-warning-500);
  background: color-mix(in srgb, var(--color-warning-100) 22%, transparent);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-warning-500) 24%, transparent);
}
.comparison-layout-block.comparison-block-selected {
  z-index: 3;
  border-width: 2px;
  border-color: var(--main-color);
  background: color-mix(in srgb, var(--main-200) 22%, transparent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--main-color) 18%, transparent);
}
.comparison-layout-block.comparison-block-grid {
  overflow: hidden;
  border-color: var(--gray-150);
  background: color-mix(in srgb, var(--gray-0) 92%, var(--main-30));
  color: var(--color-text-secondary);
  font-size: 9px;
  line-height: 1.25;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.comparison-layout-block.comparison-block-grid > .comparison-block-content {
  display: block;
  position: static;
  overflow: hidden;
  padding: 2px 3px;
  background: transparent;
  color: inherit;
  font-size: inherit;
  line-height: inherit;
  overflow-wrap: anywhere;
  text-overflow: clip;
  white-space: normal;
}
.comparison-layout-block.comparison-block-grid.comparison-block-match {
  border-color: var(--color-warning-500);
  background: color-mix(in srgb, var(--color-warning-50) 88%, var(--gray-0));
}
.comparison-layout-block > span {
  display: none;
  position: absolute;
  top: -17px;
  left: 0;
  padding: 2px 4px;
  border-radius: 3px;
  background: var(--main-700);
  color: var(--gray-0);
  font-size: 9px;
  line-height: 1.2;
  white-space: nowrap;
  pointer-events: none;
}
.comparison-layout-block-number {
  display: inline-grid;
  position: absolute;
  top: -18px;
  left: -1px;
  min-width: 16px;
  height: 16px;
  place-items: center;
  padding: 0;
  border-radius: 8px;
  background: var(--main-700);
  color: var(--gray-0);
  font-size: 9px;
  line-height: 1;
  pointer-events: none;
}
.comparison-layout-block.comparison-block-match > .comparison-layout-block-number {
  display: inline-grid;
}
.comparison-layout-block.comparison-block-selected > span,
.comparison-layout-block:hover > span {
  display: block;
}
.comparison-match-strip {
  display: flex;
  gap: 5px;
  min-width: 0;
  overflow-x: auto;
  padding: 1px 0 2px;
}
.comparison-match-strip button {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
  max-width: 300px;
  min-width: 0;
  padding: 4px 7px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 4px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  font-size: 10px;
  text-align: left;
  cursor: pointer;
}
.comparison-match-strip button strong,
.comparison-match-strip button small {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.comparison-match-strip button strong {
  color: inherit;
  font-size: 10px;
  font-weight: 600;
}
.comparison-match-strip button small {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.comparison-match-strip button > span {
  display: inline-grid;
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 50%;
  background: var(--color-warning-100);
  color: var(--color-warning-700);
  font-size: 9px;
}
.comparison-match-strip button:hover,
.comparison-match-strip button.active {
  border-color: var(--main-300);
  background: var(--main-30);
  color: var(--main-700);
}
.comparison-layout-fallback {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  margin: 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.5;
}
.comparison-card {
  margin-top: 12px;
  padding: 0 16px 16px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
}
.comparison-card.comparison-conflict {
  border-color: var(--color-error-100);
  box-shadow: inset 3px 0 0 var(--color-error-500);
}
.comparison-card.comparison-overlap {
  border-color: var(--color-warning-100);
  box-shadow: inset 3px 0 0 var(--color-warning-500);
}
.comparison-card.comparison-exact_duplicate {
  border-color: var(--color-success-100);
  box-shadow: inset 3px 0 0 var(--color-success-500);
}
.comparison-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 0;
}
.comparison-card-header > div {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
}
.comparison-card-header strong {
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.comparison-card-header > span {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.evidence-block {
  margin-top: 9px;
  padding: 8px 9px;
  border-radius: 5px;
  font-size: 10px;
}
.evidence-same {
  border: 1px solid var(--color-warning-100);
  background: var(--color-warning-10);
}
.evidence-diff {
  border: 1px solid var(--color-error-100);
  background: var(--color-error-10);
}
.evidence-block > strong {
  font-size: 10px;
}
.evidence-block ul {
  margin: 5px 0 0;
  padding-left: 16px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.difference-row {
  display: grid;
  grid-template-columns: 68px 1fr 1fr;
  gap: 6px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--color-error-100);
}
.difference-row > span {
  color: var(--color-error-700);
  font-weight: 600;
}
.difference-row p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.difference-row b {
  display: block;
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.comparison-reasoning {
  margin: 9px 0 0;
  padding-top: 7px;
  border-top: 1px solid var(--gray-100);
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.5;
}
.comparison-reasoning span {
  margin-right: 7px;
  color: var(--color-text-tertiary);
}
.duplicate-governance {
  margin-top: 9px;
  padding-top: 9px;
  border-top: 1px solid var(--gray-100);
}
.duplicate-governance-heading,
.duplicate-governance-heading > div,
.duplicate-actions > div,
.duplicate-decision-result {
  display: flex;
  align-items: center;
}
.duplicate-governance-heading {
  justify-content: space-between;
  gap: 10px;
}
.duplicate-governance-heading > div {
  min-width: 0;
  gap: 6px;
  color: var(--main-color);
}
.duplicate-governance-heading span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.duplicate-governance-heading strong {
  margin-right: 7px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.duplicate-match-list {
  display: grid;
  max-height: 310px;
  gap: 7px;
  margin-top: 9px;
  overflow-y: auto;
}
.duplicate-match {
  overflow: hidden;
  border: 1px solid var(--main-100);
  border-radius: 6px;
  background: var(--main-10);
}
.duplicate-match > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 8px;
  border-bottom: 1px solid var(--main-100);
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.duplicate-match > header b {
  color: var(--main-color);
  font-weight: 600;
}
.duplicate-match > div {
  display: grid;
  grid-template-columns: 1fr 1fr;
}
.duplicate-match section {
  min-width: 0;
  padding: 7px 8px;
}
.duplicate-match section label span {
  margin-left: 6px;
  color: var(--main-700);
  font-weight: 500;
}
.duplicate-match section + section {
  border-left: 1px solid var(--main-100);
}
.duplicate-match label {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.duplicate-match p {
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.5;
  white-space: pre-wrap;
}
.duplicate-match .overlap-snippet {
  padding: 5px 6px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--main-100) 58%, transparent);
  color: var(--color-text-primary);
  box-shadow: inset 2px 0 0 var(--main-color);
}
.duplicate-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}
.duplicate-action-help {
  margin-right: auto;
}
.duplicate-actions > div {
  flex: 0 0 auto;
  gap: 5px;
}
.duplicate-decision-result {
  gap: 7px;
  margin-top: 9px;
  padding: 8px 9px;
  border: 1px solid var(--color-success-100);
  border-radius: 6px;
  background: var(--color-success-10);
  color: var(--color-success-700);
}
.duplicate-decision-result strong,
.duplicate-decision-result span {
  display: block;
}
.duplicate-decision-result strong {
  color: var(--color-text-secondary);
  font-size: 10px;
}
.duplicate-decision-result span,
.duplicate-empty {
  margin-top: 2px;
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.duplicate-empty {
  padding: 12px 0 3px;
  text-align: center;
}
.audit-record-list {
  display: grid;
  gap: 8px;
  margin-top: 8px;
}
.audit-record {
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-10);
}
.audit-record-heading {
  display: flex;
  align-items: center;
  gap: 8px;
}
.audit-record-heading strong {
  flex: 1;
  min-width: 0;
  font-size: 11px;
}
.audit-record-heading > span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.audit-record-meta {
  margin: 4px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.audit-record-material,
.audit-record-status {
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.audit-record small {
  display: block;
  margin-top: 5px;
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.audit-record-detail {
  color: var(--color-text-tertiary);
}
.cancel-change-request {
  margin-top: 8px;
}
.event-timeline {
  display: grid;
  gap: 0;
  margin: 14px 3px 0;
}
.event-timeline > div {
  position: relative;
  padding: 0 0 12px 17px;
  border-left: 1px solid var(--gray-150);
}
.event-timeline i {
  position: absolute;
  top: 3px;
  left: -4px;
  width: 7px;
  height: 7px;
  border: 2px solid var(--gray-0);
  border-radius: 50%;
  background: var(--main-color);
}
.event-timeline p {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin: 0;
  font-size: 10px;
}
.event-timeline p span,
.event-timeline small {
  color: var(--color-text-tertiary);
}
.event-timeline small {
  display: block;
  margin-top: 2px;
  font-size: 10px;
}
.decision-panel {
  position: absolute;
  z-index: 40;
  top: 64px;
  right: 0;
  bottom: 0;
  width: min(380px, calc(100% - 24px));
  min-height: 0;
  padding: 18px;
  overflow-y: auto;
  border-left: 1px solid var(--gray-150);
  background: var(--gray-10);
  box-shadow: -12px 0 28px color-mix(in srgb, var(--gray-900) 10%, transparent);
  visibility: hidden;
  transform: translateX(100%);
  transition:
    transform 200ms ease,
    visibility 200ms ease;
}
.decision-panel.open {
  visibility: visible;
  transform: translateX(0);
}
.decision-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.decision-heading h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.decision-heading p {
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.45;
}
.decision-heading-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
}
.decision-heading-actions :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.transfer-box {
  display: grid;
  gap: 7px;
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--main-100);
  border-radius: 6px;
  background: var(--main-10);
}
.transfer-box label {
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 600;
}
.transfer-box > div {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.decision-readonly {
  display: grid;
  place-items: center;
  gap: 6px;
  margin-top: 30px;
  padding: 24px;
  color: var(--color-success-700);
  text-align: center;
}
.decision-readonly p {
  margin: 0;
  color: var(--color-text-tertiary);
  font-size: 11px;
  line-height: 1.5;
}
.outcome-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 12px;
}
.outcome-list button {
  display: grid;
  gap: 2px;
  min-height: 46px;
  padding: 7px 9px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  text-align: left;
  cursor: pointer;
}
.outcome-list button:hover {
  border-color: var(--main-200);
}
.outcome-list button.active {
  border-color: var(--main-300);
  background: var(--main-30);
  color: var(--main-700);
}
.outcome-list button:disabled {
  background: var(--gray-50);
  color: var(--color-text-tertiary);
  cursor: not-allowed;
}
.outcome-list span {
  font-size: 11px;
  font-weight: 600;
}
.outcome-list small {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.field {
  margin-top: 12px;
}
.field > label {
  display: block;
  margin-bottom: 5px;
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 600;
}
.field-label-row {
  display: flex;
  min-height: 18px;
  align-items: center;
  gap: 5px;
  margin-bottom: 5px;
}
.field-label-row > label {
  margin-bottom: 0;
}
.problem-help {
  display: inline-grid;
  width: 15px;
  height: 15px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--gray-200);
  border-radius: 50%;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  cursor: help;
  font-size: 9px;
  font-weight: 650;
  line-height: 1;
}
.problem-help:hover,
.problem-help:focus-visible {
  border-color: var(--main-200);
  color: var(--main-700);
}
.problem-help:focus-visible {
  outline: 2px solid var(--main-200);
  outline-offset: 1px;
}
.problem-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.problem-tag {
  min-height: 25px;
  padding: 2px 7px;
  border: 1px solid var(--gray-150);
  border-radius: 13px;
  background: var(--gray-0);
  color: var(--color-text-tertiary);
  font-size: 9px;
  cursor: pointer;
}
.problem-tag.active {
  border-color: var(--color-error-100);
  background: var(--color-error-50);
  color: var(--color-error-700);
}
.source-owner-field > p {
  margin: 5px 0 0;
  color: var(--color-text-tertiary);
  font-size: 9px;
  line-height: 1.5;
}
.applicability-scope-field .field-help {
  margin: -1px 0 7px;
  color: var(--color-text-tertiary);
  font-size: 9px;
  line-height: 1.5;
}
.scope-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.field-control {
  width: 100%;
}
.decision-footer {
  display: flex;
  position: sticky;
  bottom: -18px;
  justify-content: flex-end;
  gap: 7px;
  margin: 14px -18px -18px;
  padding: 12px 18px;
  border-top: 1px solid var(--gray-150);
  background: var(--gray-10);
}
.detail-empty {
  display: grid;
  min-height: 600px;
  place-items: center;
}
@media (max-width: 1180px) {
  .review-workspace {
    grid-template-columns: 220px minmax(0, 1fr);
  }
  .review-workspace.queue-collapsed {
    grid-template-columns: minmax(0, 1fr);
  }
  .detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .version-flow {
    grid-template-columns: minmax(120px, 1fr) auto minmax(120px, 1fr);
  }
  .version-stat {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
}
@media (max-width: 900px) {
  .review-workspace {
    height: auto;
    grid-template-columns: 1fr;
  }
  .review-queue {
    max-height: 330px;
    border-right: 0;
    border-bottom: 1px solid var(--gray-150);
  }
  .queue-mobile-collapse {
    display: inline-grid;
  }
  .review-detail {
    min-height: 720px;
  }
  .detail-grid {
    grid-template-columns: 1fr;
  }
  .evidence-panel {
    min-height: 640px;
  }
  .change-columns {
    grid-template-columns: 1fr;
  }
  .change-columns > div + div {
    border-top: 1px solid var(--gray-100);
    border-left: 0;
  }
  .evidence-context {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }
  .evidence-tabs {
    align-items: flex-start;
    flex-wrap: wrap;
    padding-inline: 10px;
  }
  .evidence-tabs-actions {
    width: 100%;
    justify-content: flex-end;
    margin-left: 0;
    padding: 5px 0;
    border-top: 1px solid var(--gray-100);
    border-left: 0;
  }
  .record-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
  }
  .record-meta {
    justify-content: flex-start;
  }
  .unit-overview {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 7px 12px;
  }
  .unit-overview-metrics {
    order: 3;
    width: 100%;
    overflow-x: auto;
  }
  .unit-overview-actions {
    width: 100%;
    justify-content: flex-end;
  }
  .unit-visibility {
    margin-right: auto;
  }
  .comparison-layout-columns {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 680px) {
  .record-count {
    display: none;
  }
  .unit-overview-actions {
    flex-wrap: wrap;
    justify-content: flex-start;
  }
  .unit-visibility {
    margin-right: 0;
  }
  .item-navigation-compact {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
    padding: 7px 0;
  }
  .item-navigation-controls {
    width: 100%;
  }
  .item-navigation-step,
  .item-navigation-expand {
    flex: 1;
    justify-content: center;
  }
  .record-actions > span,
  .source-link {
    display: none;
  }
  .comparison-nav-title {
    justify-content: flex-start;
  }
  .comparison-layout-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .comparison-view-toolbar {
    width: 100%;
    justify-content: flex-start;
  }
  .comparison-evidence-layout {
    grid-template-columns: 1fr;
  }
  .comparison-evidence-layout > .comparison-card {
    position: static;
    max-height: none;
    margin-top: 0;
  }
  .comparison-layout-canvas {
    min-height: 220px;
  }
  .duplicate-match > div {
    grid-template-columns: 1fr;
  }
  .duplicate-match section + section {
    padding-top: 9px;
    border-top: 1px solid var(--gray-100);
    border-left: 0;
  }
  .outcome-list {
    grid-template-columns: 1fr;
  }
  .content-review {
    padding-inline: 12px;
  }
  .presentation-toolbar-actions > span {
    display: none;
  }
  .presentation-stage-row {
    grid-template-columns: 1fr;
  }
  .presentation-stage-row.has-side-panel {
    grid-template-columns: 1fr;
  }
  .document-layout-stage,
  .document-layout-stage.has-side-panel {
    grid-template-columns: 1fr;
  }
  .layout-side-panel,
  .document-layout-editor {
    order: 2;
  }
  .layout-side-panel {
    position: sticky;
    top: 8px;
    max-height: none;
    overflow: visible;
  }
  .presentation-page-strip,
  .document-layout-page-strip {
    display: flex;
    padding: 0 0 3px;
    overflow-x: auto;
  }
  .presentation-fragment-strip {
    padding-left: 0;
  }
  .presentation-fragment-focus {
    margin-left: 0;
  }
}
</style>
