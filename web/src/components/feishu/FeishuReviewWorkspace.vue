<template>
  <section
    class="review-workspace"
    :class="{ 'queue-collapsed': queueCollapsed }"
    aria-label="知识审核工作区"
  >
    <aside v-show="!queueCollapsed" class="review-queue">
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
          <div class="queue-type-counts">
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
            <span class="queue-time">更新于 {{ formatTime(item.updated_at) }}</span>
          </div>
        </button>
      </a-spin>
    </aside>

    <article v-if="packageDetail" class="review-detail">
      <header class="record-heading">
        <div class="record-heading-main">
          <button
            type="button"
            class="icon-tool"
            :title="queueCollapsed ? '展开审核任务' : '收起审核任务'"
            :aria-label="queueCollapsed ? '展开审核任务' : '收起审核任务'"
            :aria-expanded="!queueCollapsed"
            @click="queueCollapsed = !queueCollapsed"
          >
            <PanelLeftOpen v-if="queueCollapsed" :size="17" />
            <PanelLeftClose v-else :size="17" />
          </button>
          <div class="record-title">
            <div class="record-title-line">
              <h2>{{ packageDetail.title }}</h2>
              <a-tag :color="statusColor(packageDetail.workflow_status)">{{
                workflowStatusLabel(packageDetail.workflow_status)
              }}</a-tag>
            </div>
            <p>{{ packageDetail.wiki_path || '未记录目录路径' }}</p>
          </div>
        </div>
        <div class="record-actions">
          <span>共 {{ packageDetail.item_count }} 项</span>
          <a
            v-if="packageDetail.source_url"
            class="source-link"
            :href="packageDetail.source_url"
            target="_blank"
            rel="noopener noreferrer"
          >
            查看飞书原文 <ExternalLink :size="14" />
          </a>
          <a-button type="primary" size="small" @click="decisionPanelOpen = true">
            <ClipboardCheck :size="14" />审核处理
          </a-button>
        </div>
      </header>

      <div v-if="currentItem?.reopened_from_item_id" class="reopen-trail">
        <History :size="15" />
        <div>
          <strong>资料修改后重新审核</strong
          ><span>已关联上一轮审核记录，本轮只判断新版本是否满足修改要求。</span>
        </div>
      </div>

      <nav v-if="packageDetail.items.length > 1" class="item-navigation" aria-label="审核项导航">
        <button
          v-for="(item, index) in packageDetail.items"
          :key="item.review_item_id"
          type="button"
          :class="{ active: item.review_item_id === selectedItemId }"
          @click="selectItem(item.review_item_id)"
        >
          <span>{{ index + 1 }}</span>
          <div>
            <strong>{{ item.title || reviewTypeLabel(item.review_type) }}</strong
            ><small
              >{{ reviewTypeLabel(item.review_type) }} ·
              {{ itemStatusLabel(item.item_status) }}</small
            >
          </div>
        </button>
      </nav>

      <div class="detail-grid">
        <section class="evidence-panel">
          <div class="evidence-context">
            <div class="item-summary">
              <div>
                <span
                  class="review-type"
                  :class="`type-${String(currentItem?.review_type).toLowerCase()}`"
                  >{{ reviewTypeLabel(currentItem?.review_type) }}</span
                >
                <strong>{{ currentItem?.title || packageDetail.title }}</strong>
              </div>
              <p>
                {{ currentItem?.summary || '请核对资料正文、版本变化和相关证据后形成审核结论。' }}
              </p>
            </div>
            <div class="record-meta" aria-label="来源版本信息">
              <span>{{ itemTypeLabel(packageDetail.item_type) }}</span
              ><span>版本 {{ packageDetail.revision || '-' }}</span
              ><span>{{ packageDetail.chunk_count || 0 }} 个片段</span
              ><span v-if="packageDetail.token_count"
                >{{ formatNumber(packageDetail.token_count) }} Tokens</span
              >
            </div>
          </div>
          <div v-if="publishBlocked" class="content-quality-alert">
            <CircleAlert :size="16" />
            <div>
              <strong>未读取到可审核正文</strong><span>{{ contentQualityMessage }}</span>
            </div>
          </div>
          <div class="evidence-tabs" role="tablist" aria-label="审核证据视图">
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
              <span class="tab-count">{{ packageDetail.change_requests.length }}</span>
            </button>
          </div>

          <div class="evidence-body">
            <section v-if="activeEvidenceView === 'changes'" class="version-change-review">
              <a-spin :spinning="reviewContent.loading || previousReviewContent.loading">
                <div v-if="!hasPreviousVersion" class="content-notice is-error">
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
                    <small>{{ packageDetail.chunk_count || 0 }} 个片段</small>
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
                  v-if="hasPreviousVersion && (previousReviewContent.error || reviewContent.error)"
                  class="content-notice is-error"
                >
                  <CircleAlert :size="24" /><strong>暂时无法生成版本对照</strong>
                  <p>{{ previousReviewContent.error || reviewContent.error }}</p>
                  <a-button size="small" @click="loadVersionContents"
                    ><RefreshCw :size="13" />重新加载</a-button
                  >
                </div>
                <div
                  v-else-if="hasPreviousVersion && versionChanges.length"
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
                  v-else-if="
                    hasPreviousVersion && reviewContent.loaded && previousReviewContent.loaded
                  "
                  class="content-notice compact"
                >
                  <CircleCheck :size="22" /><strong>正文内容没有发现变化</strong>
                  <p>飞书版本号发生了变化，但本次解析出的正文与当前正式版本一致。</p>
                </div>
              </a-spin>
            </section>

            <section v-else-if="activeEvidenceView === 'content'" class="content-review">
              <a-spin
                :spinning="
                  reviewContent.loading ||
                  sourceSegmentsLoading ||
                  presentationLoading ||
                  slidePreviewLoading
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
                      <strong>整页查看</strong>
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
                  <div class="presentation-stage-row">
                    <nav class="presentation-pages" aria-label="幻灯片页码">
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
                    </nav>
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
                <template v-else>
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
                  <article v-if="selectedSourceSegment" class="source-segment-focus">
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

            <section v-else-if="activeEvidenceView === 'comparisons'" class="comparison-review">
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
                  <div class="source-columns">
                    <div>
                      <label>来源一 · 版本 {{ activeRelation.source_revision || '-' }}</label
                      ><strong>{{ activeRelation.source_title }}</strong>
                      <p>{{ activeRelation.source_path || '未记录目录' }}</p>
                    </div>
                    <div>
                      <label>来源二 · 版本 {{ activeRelation.target_revision || '-' }}</label
                      ><strong>{{ activeRelation.target_title }}</strong>
                      <p>{{ activeRelation.target_path || '未记录目录' }}</p>
                    </div>
                  </div>
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
                        <span><strong>重复片段处理</strong>只处理相同片段，不影响文档其他内容</span>
                      </div>
                      <a-button
                        v-if="!duplicateCandidates[activeRelation.relation_id]"
                        size="small"
                        :loading="duplicateLoading[activeRelation.relation_id]"
                        @click="loadDuplicateCandidates(activeRelation)"
                        >{{
                          duplicateLoading[activeRelation.relation_id]
                            ? '正在定位重叠部分'
                            : '显示重叠部分'
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
                            <span
                              >已处理
                              {{
                                duplicateCandidates[activeRelation.relation_id].decision
                                  .fragment_match_ids?.length || 0
                              }}
                              组重复片段，其他独有内容继续审核。</span
                            >
                          </div>
                        </div>
                        <template
                          v-else-if="
                            duplicateCandidates[activeRelation.relation_id].fragment_matches?.length
                          "
                        >
                          <div class="duplicate-match-list">
                            <article
                              v-for="(match, index) in duplicateCandidates[
                                activeRelation.relation_id
                              ].fragment_matches"
                              :key="match.match_id"
                              class="duplicate-match"
                            >
                              <header>
                                <span>匹配片段 {{ index + 1 }}</span>
                                <b>相似度 {{ percentage(match.similarity) }}</b>
                              </header>
                              <div>
                                <section>
                                  <label
                                    >来源一 · 重叠部分
                                    <span v-if="formatSegmentLocator(match.source_locator)">{{
                                      formatSegmentLocator(match.source_locator)
                                    }}</span></label
                                  >
                                  <p class="overlap-snippet">
                                    {{ match.source_overlap_excerpt || match.source_excerpt }}
                                  </p>
                                </section>
                                <section>
                                  <label
                                    >来源二 · 重叠部分
                                    <span v-if="formatSegmentLocator(match.target_locator)">{{
                                      formatSegmentLocator(match.target_locator)
                                    }}</span></label
                                  >
                                  <p class="overlap-snippet">
                                    {{ match.target_overlap_excerpt || match.target_excerpt }}
                                  </p>
                                </section>
                              </div>
                            </article>
                          </div>
                          <div class="duplicate-actions">
                            <p>
                              选择规范内容后，另一边仅作为重复来源保留；两篇文档的独有内容继续正常审核，重复内容变化时再重新判断。
                            </p>
                            <div>
                              <a-button
                                size="small"
                                :loading="duplicateResolving === activeRelation.relation_id"
                                @click="confirmDuplicateResolution(activeRelation, 'USE_SOURCE')"
                                >保留来源一</a-button
                              >
                              <a-button
                                size="small"
                                :loading="duplicateResolving === activeRelation.relation_id"
                                @click="confirmDuplicateResolution(activeRelation, 'USE_TARGET')"
                                >保留来源二</a-button
                              >
                              <a-button
                                size="small"
                                :loading="duplicateResolving === activeRelation.relation_id"
                                @click="confirmDuplicateResolution(activeRelation, 'KEEP_SEPARATE')"
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
              </template>
            </section>

            <section v-else class="history-review">
              <div v-if="packageDetail.change_requests.length" class="change-request-list">
                <article
                  v-for="request in packageDetail.change_requests"
                  :key="request.change_request_id"
                  class="change-request-card"
                >
                  <div>
                    <span>第 {{ request.round_number }} 轮资料修改</span
                    ><a-tag :color="changeRequestColor(request.status)">{{
                      changeRequestStatusLabel(request.status)
                    }}</a-tag>
                  </div>
                  <strong>{{ request.request_text }}</strong>
                  <p>
                    {{ request.responsible_user_name || '未指定资料责任人' }} ·
                    {{ formatTime(request.updated_at) }}
                  </p>
                  <a-button
                    v-if="['OPEN', 'NEW_VERSION_RECEIVED'].includes(request.status)"
                    size="small"
                    danger
                    class="cancel-change-request"
                    @click="confirmCancelChangeRequest(request)"
                    >取消修改任务</a-button
                  >
                </article>
              </div>
              <div v-else class="content-notice compact">
                <History :size="22" /><strong>暂无资料修改记录</strong>
              </div>
              <div v-if="packageDetail.events.length" class="event-timeline">
                <div
                  v-for="(event, index) in packageDetail.events"
                  :key="`${event.event_type}-${index}`"
                >
                  <i />
                  <p>
                    <strong>{{ eventLabel(event.event_type) }}</strong
                    ><span>{{ formatTime(event.created_at) }}</span>
                  </p>
                  <small v-if="event.message">{{ event.message }}</small>
                </div>
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
                <h3>{{ reviewTypeLabel(currentItem.review_type) }}处理</h3>
                <p>
                  {{
                    currentItem.review_type === 'STALE'
                      ? '内容未变化，请确认是否继续有效。'
                      : '选择业务结果，系统自动执行对应的知识处理动作。'
                  }}
                </p>
              </div>
              <div class="decision-heading-actions">
                <a-button size="small" @click="transferOpen = !transferOpen"
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
            <div v-if="transferOpen" class="transfer-box">
              <label>转交给知识管理员</label
              ><a-select
                v-model:value="transferForm.assignee_id"
                class="field-control"
                placeholder="选择知识管理员"
                :options="reviewerOptions"
              /><a-textarea
                v-model:value="transferForm.comment"
                :rows="2"
                placeholder="说明转交原因"
              />
              <div>
                <a-button size="small" @click="transferOpen = false">取消</a-button
                ><a-button
                  size="small"
                  type="primary"
                  :loading="transferring"
                  @click="transferPackage"
                  >确认转交</a-button
                >
              </div>
            </div>
            <div v-if="!itemActionable" class="decision-readonly">
              <CircleCheck :size="20" /><strong>{{
                itemStatusLabel(currentItem.item_status)
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
                  :disabled="publishOutcome(outcome.value) && publishUnavailable"
                  @click="form.outcome = outcome.value"
                >
                  <span>{{ outcome.label }}</span
                  ><small>{{ outcome.description }}</small>
                </button>
              </div>
              <div class="field">
                <label>问题标签</label>
                <div class="problem-tags">
                  <button
                    v-for="tag in allProblemTags"
                    :key="tag.value"
                    type="button"
                    class="problem-tag"
                    :class="{ active: form.problem_tags.includes(tag.value) }"
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
                />
                <p>后台不会修改飞书正文；责任人完成修改后，由下一次扫描自动重新打开审核。</p>
              </div>
              <div class="field">
                <label>{{ decisionCommentLabel }}</label
                ><a-textarea
                  v-model:value="form.decision_comment"
                  :rows="4"
                  :placeholder="decisionCommentPlaceholder"
                />
              </div>
              <div class="decision-footer">
                <a-button size="small" :loading="savingDraft" @click="saveDraft">保存草稿</a-button
                ><a-button
                  type="primary"
                  size="small"
                  :loading="resolving"
                  :disabled="!form.outcome || (publishOutcome(form.outcome) && publishUnavailable)"
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
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
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
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  UserRoundCog,
  X
} from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'
import { documentApi } from '@/apis/knowledge_api'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { mergeChunks } from '@/utils/chunkUtils'

const props = defineProps({
  sourceId: { type: String, default: '' },
  targetReviewId: { type: [String, Object], default: '' }
})
const emit = defineEmits(['count-change', 'target-consumed'])
const packages = ref([])
const packageResponse = ref({ total: 0, counts: {} })
const packageDetail = ref(null)
const reviewers = ref([])
const selectedPackageId = ref('')
const selectedItemId = ref('')
const queueView = ref('mine')
const reviewTypeFilter = ref('')
const problemFilter = ref('')
const activeEvidenceView = ref('content')
const queueCollapsed = ref(false)
const decisionPanelOpen = ref(false)
const selectedRelationId = ref('')
const loadingPackages = ref(false)
const loadingDetail = ref(false)
const resolving = ref(false)
const savingDraft = ref(false)
const transferring = ref(false)
const transferOpen = ref(false)
const duplicateCandidates = reactive({})
const duplicateLoading = reactive({})
const duplicateResolving = ref('')
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
let slidePreviewRequestSeq = 0
let detailRequestSeq = 0
let contentRequestSeq = 0
let previousContentRequestSeq = 0
const form = reactive({
  outcome: '',
  problem_tags: [],
  decision_comment: '',
  responsible_user_name: ''
})
const transferForm = reactive({ assignee_id: undefined, comment: '' })

const queueViews = [
  { value: 'mine', label: '待我处理', countKey: 'mine' },
  { value: 'waiting_source', label: '等资料', countKey: 'waiting_source_change' },
  { value: 'transferred', label: '已转交' },
  { value: 'completed', label: '已完成', countKey: 'completed' }
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
const itemActionable = computed(() =>
  ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(currentItem.value?.item_status)
)
const outcomeOptions = computed(() =>
  (currentItem.value?.allowed_outcomes || [])
    .filter((value) => value !== 'SPLIT_SCOPE')
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
const isUpdateReview = computed(() => currentItem.value?.review_type === 'UPDATE')
const versionChanges = computed(() =>
  buildVersionChanges(previousReviewMarkdown.value, reviewMarkdown.value)
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

watch(
  () => [props.sourceId, queueView.value, reviewTypeFilter.value, problemFilter.value],
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

async function loadPackages() {
  if (!props.sourceId) return
  loadingPackages.value = true
  try {
    const response = await governanceApi.listReviewPackages(props.sourceId, packageQuery())
    packages.value = response.items || []
    packageResponse.value = response
    emit('count-change', response.counts?.mine || 0)
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
      requested ? target.relationId : ''
    )
    if (requested) emit('target-consumed')
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载审核任务失败'))
  } finally {
    loadingPackages.value = false
  }
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
    return { ...params, view: 'all', workflow_status: 'COMPLETED' }
  return { ...params, view: 'mine' }
}
async function selectPackage(packageId, relationId = '') {
  selectedPackageId.value = packageId
  packageDetail.value = null
  selectedItemId.value = ''
  reviewContent.value = emptyReviewContent()
  previousReviewContent.value = emptyReviewContent()
  sourceSegments.value = []
  selectedSourceSegmentId.value = ''
  presentationLayout.value = null
  presentationError.value = ''
  activeSlideNumber.value = 1
  selectedPresentationFragmentId.value = ''
  revokeSlidePreview()
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
    const actionable = response.items?.find((item) =>
      ['PENDING', 'WAITING_BUSINESS_CONFIRMATION'].includes(item.item_status)
    )
    selectItem(
      relationItem?.review_item_id ||
        actionable?.review_item_id ||
        response.items?.[0]?.review_item_id ||
        ''
    )
    await Promise.all([loadVersionContents(), loadSourceSegments(), loadPresentationLayout()])
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
  const visibleOutcomes = (item.allowed_outcomes || []).filter((value) => value !== 'SPLIT_SCOPE')
  form.outcome =
    draft.outcome && draft.outcome !== 'SPLIT_SCOPE' ? draft.outcome : visibleOutcomes[0] || ''
  form.problem_tags = [...(draft.problem_tags || item.problem_tags || [])]
  form.decision_comment = draft.decision_comment || item.decision_comment || ''
  form.responsible_user_name = draft.responsible_user_name || ''
  transferForm.assignee_id = undefined
  transferForm.comment = ''
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
    else await loadSlidePreview()
  } catch (error) {
    presentationError.value = governanceApi.getErrorMessage(error, '版式读取失败')
  } finally {
    presentationLoading.value = false
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
function revokeSlidePreview() {
  if (activeSlidePreviewUrl.value) URL.revokeObjectURL(activeSlidePreviewUrl.value)
  activeSlidePreviewUrl.value = ''
  slidePreviewError.value = ''
}
async function loadSlidePreview() {
  const packageId = selectedPackageId.value
  const slideNumber = activeSlideNumber.value
  const requestId = ++slidePreviewRequestSeq
  revokeSlidePreview()
  if (!packageId || !presentationSlides.value.length) return
  slidePreviewLoading.value = true
  try {
    const response = await governanceApi.getReviewPackageSlidePreview(packageId, slideNumber)
    const blob = await response.blob()
    if (requestId !== slidePreviewRequestSeq) return
    activeSlidePreviewUrl.value = URL.createObjectURL(blob)
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
  return {
    left: `${fragment.left || 0}%`,
    top: `${fragment.top || 0}%`,
    width: `${fragment.width || 1}%`,
    height: `${fragment.height || 1}%`
  }
}
function presentationFragmentLabel(fragment) {
  const text = String(fragment.content || '')
    .replace(/\s+/g, ' ')
    .trim()
  return text.length > 30 ? `${text.slice(0, 30)}…` : text || '未命名片段'
}
onBeforeUnmount(revokeSlidePreview)
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
function decisionPayload() {
  return {
    review_item_id: currentItem.value.review_item_id,
    outcome: form.outcome,
    problem_tags: form.problem_tags,
    decision_comment: form.decision_comment.trim() || undefined,
    applicability_scope: {},
    responsible_user_name: form.responsible_user_name.trim() || undefined
  }
}
async function saveDraft() {
  if (!packageDetail.value || !currentItem.value) return
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
  if (!packageDetail.value || !currentItem.value) return
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
  resolving.value = true
  try {
    await governanceApi.resolveReviewPackage(packageDetail.value.package_id, {
      request_id: newRequestId(),
      lock_version: packageDetail.value.lock_version,
      decisions: [decisionPayload()]
    })
    message.success(`已记录“${outcomeLabel(form.outcome)}”`)
    await loadPackages()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '提交审核结果失败'))
  } finally {
    resolving.value = false
  }
}
async function transferPackage() {
  if (!packageDetail.value) return
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
  selectedRelationId.value = comparison.relation_id
  if (duplicateRelation(comparison)) await loadDuplicateCandidates(comparison)
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
    duplicateCandidates[relationId] = await governanceApi.getDuplicateCandidates(relationId)
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载重复片段失败'))
  } finally {
    duplicateLoading[relationId] = false
  }
}
function confirmDuplicateResolution(comparison, strategy) {
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
        ? '两边片段将继续作为不同知识处理，不建立重复来源关系。'
        : '匹配到的另一边片段会记录为重复来源，不会重复创建正式知识；两篇文档的独有内容不受影响。',
    okText: '确认处理',
    cancelText: '返回',
    async onOk() {
      await resolveDuplicateRelation(comparison.relation_id, strategy)
    }
  })
}
async function resolveDuplicateRelation(relationId, strategy) {
  duplicateResolving.value = relationId
  try {
    const response = await governanceApi.resolveDuplicateRelation(relationId, {
      request_id: newRequestId(),
      strategy
    })
    duplicateCandidates[relationId] = response
    message.success(
      strategy === 'KEEP_SEPARATE' ? '已分别保留两边内容' : '已建立规范内容和重复来源关系'
    )
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
function itemStatusLabel(value) {
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
function statusColor(value) {
  return (
    {
      OPEN: 'processing',
      WAITING_SOURCE_CHANGE: 'warning',
      WAITING_BUSINESS_CONFIRMATION: 'warning',
      COMPLETED: 'success'
    }[value] || 'default'
  )
}
function riskLabel(value) {
  return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[value] || '待评估'
}
function itemTypeLabel(value) {
  return (
    {
      page: '飞书文档',
      attachment: '附件',
      docx: 'Word',
      pdf: 'PDF',
      pptx: '演示文稿',
      xlsx: '电子表格',
      image: '图片 OCR'
    }[value] || String(value || '资料').toUpperCase()
  )
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
function eventLabel(value) {
  return (
    {
      source_change_requested: '已发起资料修改',
      source_change_version_received: '已收到飞书新版本',
      review_item_reopened: '已重新打开审核',
      source_change_request_fulfilled: '资料修改已验证',
      source_change_request_cancelled: '资料修改任务已取消',
      review_item_decided: '已记录审核结果',
      review_package_transferred: '审核包已转交',
      review_package_completed: '审核包已完成',
      review_draft_saved: '已保存审核草稿'
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
.queue-filters {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 8px;
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
  min-height: 64px;
  padding: 10px 18px;
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
  align-items: center;
  gap: 8px;
}
.record-title h2 {
  overflow: hidden;
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.record-title p {
  overflow: hidden;
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
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
  gap: 8px;
  padding: 8px 18px;
  border-bottom: 1px solid var(--main-100);
  background: var(--main-10);
  color: var(--main-700);
}
.reopen-trail svg {
  margin-top: 1px;
}
.reopen-trail div {
  display: grid;
  gap: 1px;
}
.reopen-trail strong {
  font-size: 11px;
}
.reopen-trail span {
  color: var(--color-text-secondary);
  font-size: 10px;
}
.item-navigation {
  display: flex;
  flex: 0 0 auto;
  gap: 5px;
  padding: 8px 18px;
  overflow-x: auto;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-10);
}
.item-navigation button {
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
.item-navigation button:hover,
.item-navigation button.active {
  border-color: var(--main-200);
  background: var(--main-10);
}
.item-navigation button > span {
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
.item-navigation button.active > span {
  background: var(--main-color);
  color: var(--gray-0);
}
.item-navigation div {
  display: grid;
  min-width: 0;
  gap: 1px;
}
.item-navigation strong,
.item-navigation small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item-navigation strong {
  font-size: 11px;
}
.item-navigation small {
  color: var(--color-text-tertiary);
  font-size: 9px;
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
  min-height: 58px;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 10px 22px 8px;
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
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 11px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  flex: 0 0 auto;
  gap: 3px;
  margin-top: 0;
  padding: 0 14px;
  border-bottom: 1px solid var(--gray-150);
}
.evidence-tabs button {
  position: relative;
  display: inline-flex;
  min-height: 34px;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  border: 0;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 11px;
}
.evidence-tabs button:hover,
.evidence-tabs button.active {
  color: var(--main-700);
}
.evidence-tabs button.active {
  font-weight: 600;
  box-shadow: inset 0 -2px 0 var(--main-color);
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
  overflow-y: auto;
  scrollbar-color: var(--gray-200) transparent;
  scrollbar-width: thin;
}
.content-review {
  min-height: 100%;
  padding: 20px 28px 44px;
}
.presentation-review {
  display: grid;
  max-width: 1120px;
  gap: 10px;
  margin: 0 auto;
}
.presentation-toolbar {
  display: flex;
  min-height: 36px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 2px;
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
  grid-template-columns: 42px minmax(0, 1fr);
  align-items: start;
  gap: 10px;
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
  padding: 2px 0 4px 52px;
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
  margin-left: 52px;
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
.comparison-navigator {
  display: grid;
  position: sticky;
  z-index: 2;
  top: 0;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  min-height: 48px;
  padding: 8px 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: color-mix(in srgb, var(--gray-0) 94%, transparent);
  box-shadow: 0 5px 15px color-mix(in srgb, var(--gray-900) 6%, transparent);
  backdrop-filter: blur(8px);
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
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
.source-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  padding-top: 9px;
  border-top: 1px solid var(--gray-100);
}
.source-columns > div + div {
  padding-left: 10px;
  border-left: 1px solid var(--gray-100);
}
.source-columns label,
.source-columns p {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.source-columns strong {
  display: block;
  margin-top: 2px;
  font-size: 11px;
}
.source-columns p {
  margin: 2px 0 0;
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
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
}
.duplicate-actions p {
  max-width: 460px;
  margin: 0;
  color: var(--color-text-tertiary);
  font-size: 9px;
  line-height: 1.5;
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
.change-request-list {
  display: grid;
  gap: 8px;
  margin-top: 8px;
}
.change-request-card {
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-10);
}
.change-request-card > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.change-request-card > div > span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.change-request-card > strong {
  display: block;
  margin-top: 7px;
  font-size: 11px;
  line-height: 1.55;
}
.change-request-card > p {
  margin: 4px 0 0;
  color: var(--color-text-tertiary);
  font-size: 10px;
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
  z-index: 8;
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
  .record-meta {
    justify-content: flex-start;
  }
}
@media (max-width: 680px) {
  .record-actions > span,
  .source-link {
    display: none;
  }
  .comparison-nav-title {
    justify-content: flex-start;
  }
  .source-columns,
  .duplicate-match > div {
    grid-template-columns: 1fr;
  }
  .source-columns > div + div,
  .duplicate-match section + section {
    padding-top: 9px;
    padding-left: 0;
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
  .presentation-pages {
    display: flex;
    max-height: none;
    padding: 0 0 3px;
    overflow-x: auto;
  }
  .presentation-pages button.active {
    box-shadow: inset 0 -2px 0 var(--main-color);
  }
  .presentation-fragment-strip {
    padding-left: 0;
  }
  .presentation-fragment-focus {
    margin-left: 0;
  }
}
</style>
