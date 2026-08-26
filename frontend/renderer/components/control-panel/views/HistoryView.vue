<script setup lang="ts">
/**
 * 历史记录 — 按每次 session（一次完整对话）的抽屉
 * 列表：日期 + 首个输入前 20 字 + …（含消息数 / 轮数）；点击展开 →
 * 该会话的事件轨迹时间线（用户→西西→工具→结果，按时间顺序；runId 变化时按「轮」分段）。
 * 数据：history:list / history:search（分页）、history:detail（session 事件流）。
 */
import { computed, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import PageCard from '../PageCard.vue'
import type { HistoryDetail, HistoryEvent, HistoryItem } from '../../../../preload/types'
import { setPanelActions, clearPanelActions } from '../../../app/panelActions'

const PAGE_SIZE = 20

const rows = ref<HistoryItem[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const searching = ref(false)
const loading = ref(false)
const error = ref('')

// 展开状态：sessionId → detail | 'loading' | 'error'
const expanded = ref<Record<string, HistoryDetail | 'loading' | 'error'>>({})

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

function fmtTime(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function doSearch(pageNum: number): Promise<void> {
  const kw = keyword.value.trim()
  searching.value = true
  error.value = ''
  try {
    const res = kw
      ? await window.api.historySearch(kw, pageNum, PAGE_SIZE)
      : await window.api.historyList(pageNum, PAGE_SIZE)
    rows.value = res.items ?? []
    total.value = res.total ?? 0
    page.value = res.page ?? pageNum
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    rows.value = []
    total.value = 0
  } finally {
    searching.value = false
  }
}

/** 展开 / 收起抽屉；展开时拉取该 session 的事件轨迹 */
async function toggleRun(row: HistoryItem): Promise<void> {
  const key = row.sessionId ?? ''
  if (!key) return
  if (expanded.value[key]) {
    delete expanded.value[key]
    expanded.value = { ...expanded.value }
    return
  }
  expanded.value = { ...expanded.value, [key]: 'loading' }
  try {
    const detail = await window.api.historyDetail(key)
    expanded.value = { ...expanded.value, [key]: detail }
  } catch {
    expanded.value = { ...expanded.value, [key]: 'error' }
  }
}

function fmtTs(ts?: number): string {
  if (!ts) return '--:--:--'
  const d = new Date(ts * 1000)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function kindLabel(kind: string): string {
  switch (kind) {
    case 'user':
      return '用户'
    case 'assistant':
      return '西西'
    case 'tool':
      return '调用工具'
    case 'tool_result':
      return '工具结果'
    default:
      return '系统'
  }
}

function kindIcon(kind: string): string {
  switch (kind) {
    case 'user':
      return '🧑'
    case 'assistant':
      return '🐱'
    case 'tool':
      return '🔧'
    case 'tool_result':
      return '📦'
    default:
      return '⚙'
  }
}

/** 事件在会话内属于第几轮（按 runId 去重计数） */
function runNo(events: HistoryEvent[], i: number): number {
  const seen = new Set<string>()
  for (let k = 0; k <= i && k < events.length; k++) {
    if (events[k]?.runId) seen.add(events[k]!.runId as string)
  }
  return seen.size || 1
}

// 单条事件的折叠状态（默认展开；key=事件下标）
const eventFold = ref<Record<number, boolean>>({})
function toggleEvent(i: number): void {
  eventFold.value = { ...eventFold.value, [i]: !eventFold.value[i] }
}

// ── 删除会话（选择模式） ──
// 点击「删除会话」→ 会话条出现圆点（可多选）→ 右下角「确认删除」（保存按钮位置）→ 删除并刷新
const selectMode = ref(false)
const selected = ref<string[]>([])

function enterSelectMode(): void {
  selectMode.value = true
  selected.value = []
  expanded.value = {}
  registerActions()
}
function exitSelectMode(): void {
  selectMode.value = false
  selected.value = []
  registerActions()
}
function togglePick(sessionId?: string): void {
  if (!sessionId) return
  const i = selected.value.indexOf(sessionId)
  if (i >= 0) selected.value = selected.value.filter((id) => id !== sessionId)
  else selected.value = [...selected.value, sessionId]
}
async function confirmDelete(): Promise<void> {
  const ids = selected.value
  if (!ids.length) return
  for (const id of ids) {
    try {
      await window.api.historyDelete(id)
    } catch {
      /* 单条失败继续删其余 */
    }
  }
  exitSelectMode()
  expanded.value = {}
  await doSearch(1)
}

function registerActions(): void {
  if (selectMode.value) {
    setPanelActions([
      { key: 'cancel', label: '取消', onClick: () => exitSelectMode() },
      { key: 'delete', label: '确认删除', primary: true, disabled: () => selected.value.length === 0, onClick: () => void confirmDelete() },
    ])
  } else {
    clearPanelActions()
  }
}

watch(selectMode, () => registerActions())
onMounted(() => {
  void doSearch(1)
  registerActions()
})
onActivated(registerActions)
onBeforeUnmount(() => clearPanelActions())
</script>

<template>
  <div class="flex flex-col gap-4">
    <PageCard title="历史记录" description="回看与西西的每一段完整对话；展开可查看这段对话的完整过程（用户 → 西西 → 工具 → 结果），并自动按「轮」分段">
      <div class="flex items-center gap-2">
        <input
          v-model="keyword"
          type="text"
          placeholder="搜索历史消息…"
          class="h-8 min-w-0 flex-1 rounded-md border border-line-subtle bg-surface-1 px-2.5 text-[13px] text-fg-primary placeholder:text-fg-muted outline-none transition-all duration-ds-sm ease-expo-out focus:border-accent/60 focus:shadow-[0_0_0_2px_rgba(94,106,210,0.25),var(--ds-shadow-sm)]"
          @keydown.enter="doSearch(1)"
        />
        <button
          type="button"
          class="h-8 shrink-0 rounded-md border border-line-subtle bg-transparent px-3 text-[13px] font-medium text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary hover:shadow-ds-hover active:shadow-ds-active"
          :disabled="searching || loading"
          @click="doSearch(1)"
        >
          {{ searching ? '搜索中…' : '查询' }}
        </button>
        <button
          type="button"
          class="h-8 shrink-0 rounded-md border px-3 text-[13px] font-medium transition-all duration-ds-sm ease-expo-out hover:shadow-ds-hover active:shadow-ds-active"
          :class="selectMode
            ? 'border-danger/40 text-danger hover:bg-danger/10'
            : 'border-line-subtle bg-transparent text-fg-secondary hover:bg-surface-2 hover:text-fg-primary'"
          @click="selectMode ? exitSelectMode() : enterSelectMode()"
        >
          {{ selectMode ? '取消选择' : '删除会话' }}
        </button>
      </div>

      <div v-if="selectMode" class="mt-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-1.5 text-[12px] text-danger">
        已进入删除模式：点击会话旁的圆点选中，再点右下角「确认删除」
      </div>

      <div class="mt-3">
        <!-- 加载中 -->
        <div v-if="loading || searching" class="py-6 text-center text-[13px] text-fg-muted">加载中…</div>

        <!-- 错误 -->
        <div v-else-if="error" class="rounded-md border border-danger/30 bg-danger/10 px-3 py-2.5 text-[12px] text-danger">
          {{ error }}
        </div>

        <!-- 空态 -->
        <div v-else-if="rows.length === 0" class="py-6 text-center text-[13px] text-fg-muted">暂无历史记录</div>

        <!-- session 抽屉列表 -->
        <div v-else class="flex flex-col gap-1.5">
          <div
            v-for="row in rows"
            :key="row.sessionId"
            class="overflow-hidden rounded-md border border-line-subtle bg-surface-1/60"
          >
            <!-- 抽屉标题（点击展开/收起；删除模式下点击=选中） -->
            <button
              type="button"
              class="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors duration-ds-sm ease-expo-out hover:bg-surface-2"
              @click="selectMode ? togglePick(row.sessionId) : toggleRun(row)"
            >
              <span v-if="selectMode" class="flex h-4 w-4 shrink-0 items-center justify-center">
                <span
                  class="h-3.5 w-3.5 rounded-full border-2 transition-colors duration-ds-sm ease-expo-out"
                  :class="selected.includes(row.sessionId ?? '') ? 'border-accent bg-accent' : 'border-fg-muted'"
                />
              </span>
              <span class="shrink-0 font-mono text-[11px] text-fg-muted">{{ fmtTime(row.time) }}</span>
              <span class="flex-1 truncate text-[13px] text-fg-secondary" :title="row.preview">{{ row.preview }}</span>
              <span v-if="row.runCount" class="shrink-0 rounded-sm bg-accent/15 px-1.5 py-0.5 font-mono text-[10px] text-accent">{{ row.runCount }} 轮</span>
              <span v-if="row.msgCount" class="shrink-0 font-mono text-[10px] text-fg-muted">{{ row.msgCount }} 条</span>
              <span
                class="shrink-0 text-[11px] text-fg-muted transition-transform duration-ds-sm ease-expo-out"
                :class="expanded[row.sessionId ?? ''] ? 'rotate-90' : ''"
              >›</span>
            </button>

            <!-- 展开：事件轨迹时间线（按轮分段） -->
            <div
              v-if="expanded[row.sessionId ?? '']"
              class="border-t border-line-subtle bg-surface-2/40 px-3 py-2"
            >
              <div v-if="expanded[row.sessionId ?? ''] === 'loading'" class="py-2 text-center text-[12px] text-fg-muted">
                轨迹加载中…
              </div>
              <div v-else-if="expanded[row.sessionId ?? ''] === 'error'" class="py-2 text-center text-[12px] text-danger">
                轨迹加载失败
              </div>
              <template v-else>
                <div class="mb-1.5 flex items-center gap-2">
                  <span class="font-mono text-[10px] uppercase tracking-widest text-fg-muted">事件轨迹</span>
                  <span class="truncate font-mono text-[10px] text-fg-muted">
                    {{ (expanded[row.sessionId ?? ''] as HistoryDetail).title }}
                  </span>
                </div>
                <div class="flex flex-col gap-1.5">
                  <template v-for="(ev, i) in (expanded[row.sessionId ?? ''] as HistoryDetail).events" :key="i">
                    <!-- 轮次分隔 -->
                    <div
                      v-if="i > 0 && ev.runId && (expanded[row.sessionId ?? ''] as HistoryDetail).events[i - 1]?.runId && ev.runId !== (expanded[row.sessionId ?? ''] as HistoryDetail).events[i - 1]?.runId"
                      class="mx-1 flex items-center gap-2 py-0.5"
                    >
                      <span class="h-px flex-1 bg-line-subtle" />
                      <span class="font-mono text-[9px] uppercase tracking-widest text-fg-muted">第 {{ runNo((expanded[row.sessionId ?? ''] as HistoryDetail).events, i) }} 轮</span>
                      <span class="h-px flex-1 bg-line-subtle" />
                    </div>

                    <!-- 单条事件：标题行（可折叠） + 内容在其下方 -->
                    <div class="rounded-md bg-surface-1/70">
                      <button
                        type="button"
                        class="flex w-full items-center gap-2 px-2 py-1.5 text-left"
                        :title="eventFold[i] ? '展开' : '收起'"
                        @click="toggleEvent(i)"
                      >
                        <span class="shrink-0 text-[12px] leading-5">{{ kindIcon(ev.kind) }}</span>
                        <span class="shrink-0 font-mono text-[10px] leading-5 text-fg-muted">{{ fmtTs(ev.ts) }}</span>
                        <span class="shrink-0 rounded-sm bg-accent/10 px-1 py-0.5 font-mono text-[9px] text-accent">
                          {{ kindLabel(ev.kind) }}
                        </span>
                        <span class="flex-1" />
                        <span
                          class="shrink-0 text-[10px] text-fg-muted transition-transform duration-ds-sm ease-expo-out"
                          :class="eventFold[i] ? '' : 'rotate-90'"
                        >›</span>
                      </button>
                      <div v-show="!eventFold[i]" class="px-2 pb-1.5 pl-[26px]">
                        <p class="break-words text-[12px] leading-5 text-fg-secondary">
                          <template v-if="ev.kind === 'tool'">
                            <span class="font-medium text-fg-primary">{{ ev.name }}</span>
                            <code class="ml-1 break-all font-mono text-[11px] text-fg-muted">{{ JSON.stringify(ev.args ?? {}) }}</code>
                          </template>
                          <template v-else>{{ ev.text }}</template>
                        </p>
                      </div>
                    </div>
                  </template>
                  <div
                    v-if="!((expanded[row.sessionId ?? ''] as HistoryDetail).events?.length)"
                    class="py-1 text-center text-[11px] text-fg-muted"
                  >
                    本段对话暂无事件
                  </div>
                </div>
              </template>
            </div>
          </div>
        </div>

        <!-- 分页 -->
        <div class="mt-3 flex items-center justify-between">
          <span class="font-mono text-[11px] text-fg-muted">共 {{ total }} 条 · 第 {{ page }} / {{ pageCount }} 页</span>
          <div class="flex gap-1.5">
            <button
              type="button"
              class="h-7 rounded-md border border-line-subtle bg-transparent px-2.5 text-[12px] font-medium text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary disabled:opacity-40"
              :disabled="page <= 1 || loading || searching"
              @click="doSearch(page - 1)"
            >
              上一页
            </button>
            <button
              type="button"
              class="h-7 rounded-md border border-line-subtle bg-transparent px-2.5 text-[12px] font-medium text-fg-secondary transition-all duration-ds-sm ease-expo-out hover:bg-surface-2 hover:text-fg-primary disabled:opacity-40"
              :disabled="page >= pageCount || loading || searching"
              @click="doSearch(page + 1)"
            >
              下一页
            </button>
          </div>
        </div>
      </div>
    </PageCard>
  </div>
</template>