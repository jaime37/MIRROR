<template>
  <div class="pt-root">

    <!-- ── Header ── -->
    <header class="pt-header">
      <div class="pt-brand" @click="$router.push('/')">MIROFISH</div>
      <div class="pt-header-title">
        <span class="pt-badge">PAPER TRADING</span>
        <span class="pt-subtitle">Simulation · No real money</span>
      </div>
      <div class="pt-header-actions">
        <button class="btn-ghost" @click="refreshAll" :disabled="loading.refresh">
          <span class="icon">⟳</span>
          {{ loading.refresh ? 'Refreshing…' : 'Refresh' }}
        </button>
        <button class="btn-danger-ghost" @click="confirmReset">Reset Portfolio</button>
      </div>
    </header>

    <!-- ── KPI Cards ── -->
    <section class="pt-kpis">
      <div class="kpi-card">
        <div class="kpi-label">Portfolio Value</div>
        <div class="kpi-value" :class="pnlClass(stats.total_pnl)">
          ${{ fmt(stats.total_value) }}
        </div>
        <div class="kpi-sub">Cash: ${{ fmt(stats.balance) }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Total P&amp;L</div>
        <div class="kpi-value" :class="pnlClass(stats.total_pnl)">
          {{ stats.total_pnl >= 0 ? '+' : '' }}${{ fmt(stats.total_pnl) }}
        </div>
        <div class="kpi-sub">{{ stats.total_return_pct >= 0 ? '+' : '' }}{{ stats.total_return_pct }}%</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Win Rate</div>
        <div class="kpi-value">{{ stats.win_rate }}%</div>
        <div class="kpi-sub">{{ stats.wins }}W / {{ stats.losses }}L</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Positions</div>
        <div class="kpi-value">{{ stats.open_positions }}</div>
        <div class="kpi-sub">{{ stats.closed_trades }} closed trades</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Positions Value</div>
        <div class="kpi-value">${{ fmt(stats.positions_value) }}</div>
        <div class="kpi-sub">{{ stats.total_trades }} total trades</div>
      </div>
    </section>

    <!-- ── Main grid ── -->
    <div class="pt-grid">

      <!-- ── Bot Control (full width) ── -->
      <div class="pt-bot-row">

        <!-- Status + controls -->
        <div class="pt-card bot-control-card">
          <div class="card-header">
            <span class="card-title">🤖 Autonomous Bot</span>
            <span class="bot-status-pill" :class="botRunning ? 'running' : 'stopped'">
              {{ botRunning ? '● RUNNING' : '○ STOPPED' }}
            </span>
          </div>
          <div class="bot-controls">
            <button class="btn-primary" @click="runOnce" :disabled="loading.bot">
              {{ loading.bot ? '⏳ Running cycle…' : '▶ Run Once' }}
            </button>
            <button v-if="!botRunning" class="btn-start" @click="startBot" :disabled="loading.bot">
              ⏱ Start Auto (every {{ botSettings.cycle_interval_minutes }}m)
            </button>
            <button v-else class="btn-stop" @click="stopBot">
              ⏹ Stop Auto
            </button>
          </div>
          <div class="bot-settings-grid">
            <div class="bs-field">
              <label>Markets / cycle</label>
              <input type="number" v-model.number="botSettings.max_markets_per_cycle" @change="saveSettings" min="1" max="50" />
            </div>
            <div class="bs-field">
              <label>Position size (USDC)</label>
              <input type="number" v-model.number="botSettings.position_size_usdc" @change="saveSettings" min="10" />
            </div>
            <div class="bs-field">
              <label>Min edge</label>
              <input type="number" v-model.number="botSettings.min_edge" @change="saveSettings" step="0.01" min="0.01" max="0.5" />
            </div>
            <div class="bs-field">
              <label>Interval (min)</label>
              <input type="number" v-model.number="botSettings.cycle_interval_minutes" @change="saveSettings" min="5" />
            </div>
            <div class="bs-field">
              <label>Take profit %</label>
              <input type="number" v-model.number="botSettings.take_profit_pct" @change="saveSettingsTP" step="1" min="1" />
            </div>
            <div class="bs-field">
              <label>Stop loss %</label>
              <input type="number" v-model.number="botSettings.stop_loss_pct" @change="saveSettingsSL" step="1" min="1" />
            </div>
            <div class="bs-field">
              <label>Max open positions</label>
              <input type="number" v-model.number="botSettings.max_open_positions" @change="saveSettings" min="1" max="20" />
            </div>
            <div class="bs-field checkbox-field">
              <label>
                <input type="checkbox" v-model="botSettings.auto_close" @change="saveSettings" />
                Auto TP/SL
              </label>
            </div>
          </div>
        </div>

        <!-- Last run log -->
        <div class="pt-card bot-log-card">
          <div class="card-header">
            <span class="card-title">Last Run Log</span>
            <span v-if="lastRun" class="card-hint">
              {{ fmtTime(lastRun.started_at) }} ·
              {{ lastRun.markets_scanned }} scanned ·
              {{ lastRun.trades_opened }} opened ·
              {{ lastRun.trades_closed }} closed
            </span>
          </div>
          <div v-if="!lastRun" class="empty-state">No runs yet — click "Run Once" to start</div>
          <div v-else class="run-log">
            <div v-for="(entry, i) in lastRun.log" :key="i"
              class="log-line" :class="entry.level">
              <span class="log-ts">{{ entry.ts }}</span>
              <span class="log-msg">{{ entry.msg }}</span>
            </div>
          </div>
        </div>

      </div>

      <!-- LEFT: Equity chart + Run pipeline -->
      <div class="pt-left">

        <!-- Equity chart -->
        <div class="pt-card chart-card">
          <div class="card-header">
            <span class="card-title">Equity Curve</span>
            <span class="chart-range-pills">
              <button v-for="r in ranges" :key="r" class="pill"
                :class="{ active: chartRange === r }" @click="chartRange = r">{{ r }}</button>
            </span>
          </div>
          <div class="chart-wrapper">
            <svg ref="chartSvg" class="equity-svg" />
            <div v-if="equityHistory.length === 0" class="chart-empty">
              No data yet — run the pipeline to start tracking
            </div>
          </div>
        </div>

        <!-- Run Pipeline -->
        <div class="pt-card pipeline-card">
          <div class="card-header">
            <span class="card-title">Run Pipeline</span>
            <span class="card-hint">Connect a MiroFish report → scan markets → paper trade</span>
          </div>
          <div class="pipeline-form">
            <div class="form-row">
              <label>Report ID</label>
              <input v-model="pipelineForm.report_id" placeholder="report_xxxx (leave blank to paste markdown)" />
            </div>
            <div class="form-row" v-if="!pipelineForm.report_id">
              <label>Or paste Report Markdown</label>
              <textarea v-model="pipelineForm.report_markdown" rows="4"
                placeholder="Paste the MiroFish report markdown here…" />
            </div>
            <div class="form-row-inline">
              <div class="form-field">
                <label>Max markets</label>
                <input v-model.number="pipelineForm.max_markets" type="number" min="1" max="100" />
              </div>
              <div class="form-field">
                <label>Position size (USDC)</label>
                <input v-model.number="pipelineForm.position_size" type="number" min="10" max="5000" />
              </div>
              <div class="form-field">
                <label>Min edge</label>
                <input v-model.number="pipelineForm.min_edge" type="number" step="0.01" min="0.01" max="0.5" />
              </div>
              <div class="form-field checkbox-field">
                <label>
                  <input type="checkbox" v-model="pipelineForm.dry_run" />
                  Dry run
                </label>
              </div>
            </div>
            <button class="btn-primary" @click="runPipeline" :disabled="loading.pipeline">
              {{ loading.pipeline ? '⏳ Running…' : '▶ Run Pipeline' }}
            </button>
          </div>

          <!-- Pipeline result -->
          <div v-if="pipelineResult" class="pipeline-result">
            <div class="result-header">
              <span>✅ Scan complete</span>
              <span class="result-meta">
                {{ pipelineResult.markets_scanned }} scanned ·
                {{ pipelineResult.opportunities_found }} opportunities ·
                {{ pipelineResult.trades_placed }} trades {{ pipelineResult.dry_run ? '(dry run)' : 'placed' }}
              </span>
            </div>
            <div class="result-table-wrap">
              <table class="result-table">
                <thead>
                  <tr>
                    <th>Question</th>
                    <th>YES price</th>
                    <th>Est. prob</th>
                    <th>Edge</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in pipelineResult.results.filter(r => r.signal?.relevant)" :key="r.market_id">
                    <td class="q-cell" :title="r.question">{{ truncate(r.question, 60) }}</td>
                    <td>{{ pct(r.yes_price) }}</td>
                    <td>{{ r.signal?.probability != null ? pct(r.signal.probability) : '—' }}</td>
                    <td :class="r.edge_info?.has_edge ? 'edge-yes' : ''">
                      {{ r.edge_info ? pct(r.edge_info.edge) : '—' }}
                    </td>
                    <td>
                      <span class="action-badge" :class="r.action">{{ r.action }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

      </div>

      <!-- RIGHT: Open positions + Trade history -->
      <div class="pt-right">

        <!-- Open positions -->
        <div class="pt-card">
          <div class="card-header">
            <span class="card-title">Open Positions ({{ openPositions.length }})</span>
            <button class="btn-ghost-sm" @click="refreshPositions" :disabled="loading.refresh">
              Refresh prices
            </button>
          </div>
          <div v-if="openPositions.length === 0" class="empty-state">No open positions</div>
          <div v-else class="positions-list">
            <div v-for="pos in openPositions" :key="pos.market_id" class="position-row">
              <div class="pos-top">
                <span class="pos-question" :title="pos.question">{{ truncate(pos.question, 55) }}</span>
                <span class="pos-side" :class="pos.side.toLowerCase()">{{ pos.side }}</span>
              </div>
              <div class="pos-metrics">
                <div class="metric">
                  <span class="metric-label">Entry</span>
                  <span>{{ pct(pos.entry_price) }}</span>
                </div>
                <div class="metric">
                  <span class="metric-label">Current</span>
                  <span>{{ pct(pos.current_price) }}</span>
                </div>
                <div class="metric">
                  <span class="metric-label">Cost</span>
                  <span>${{ fmt(pos.cost_basis) }}</span>
                </div>
                <div class="metric">
                  <span class="metric-label">Value</span>
                  <span>${{ fmt(pos.current_value) }}</span>
                </div>
                <div class="metric">
                  <span class="metric-label">Unreal. P&amp;L</span>
                  <span :class="pnlClass(pos.unrealized_pnl)">
                    {{ pos.unrealized_pnl >= 0 ? '+' : '' }}${{ fmt(pos.unrealized_pnl) }}
                  </span>
                </div>
                <div class="metric">
                  <span class="metric-label">Confidence</span>
                  <span class="conf-badge" :class="pos.confidence">{{ pos.confidence }}</span>
                </div>
              </div>
              <div class="pos-reasoning">{{ pos.reasoning }}</div>
              <div class="pos-actions">
                <button class="btn-close-pos" @click="closeAtLive(pos.market_id)">
                  Close at live price
                </button>
                <button class="btn-close-pos secondary" @click="openCloseModal(pos)">
                  Close at custom price
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- Trade history -->
        <div class="pt-card">
          <div class="card-header">
            <span class="card-title">Trade History</span>
            <span class="card-hint">{{ trades.length }} records</span>
          </div>
          <div v-if="trades.length === 0" class="empty-state">No trades yet</div>
          <div v-else class="trades-table-wrap">
            <table class="trades-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Type</th>
                  <th>Question</th>
                  <th>Side</th>
                  <th>Price</th>
                  <th>Amount</th>
                  <th>P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in trades" :key="t.id" :class="t.type.toLowerCase()">
                  <td class="time-cell">{{ fmtTime(t.timestamp) }}</td>
                  <td><span class="type-badge" :class="t.type.toLowerCase()">{{ t.type }}</span></td>
                  <td class="q-cell" :title="t.question">{{ truncate(t.question, 45) }}</td>
                  <td><span class="side-badge" :class="t.side?.toLowerCase()">{{ t.side }}</span></td>
                  <td>{{ t.exit_price != null ? pct(t.exit_price) : pct(t.price) }}</td>
                  <td>${{ fmt(t.amount_usdc ?? t.proceeds) }}</td>
                  <td v-if="t.pnl != null" :class="pnlClass(t.pnl)">
                    {{ t.pnl >= 0 ? '+' : '' }}${{ fmt(t.pnl) }}
                    <span class="pnl-pct">({{ t.pnl_pct >= 0 ? '+' : '' }}{{ t.pnl_pct }}%)</span>
                  </td>
                  <td v-else class="muted">—</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>
    </div>

    <!-- ── Close position modal ── -->
    <div v-if="closeModal.visible" class="modal-overlay" @click.self="closeModal.visible = false">
      <div class="modal">
        <div class="modal-title">Close Position</div>
        <p class="modal-q">{{ truncate(closeModal.pos?.question, 80) }}</p>
        <label>Exit price (0–1)</label>
        <input v-model.number="closeModal.price" type="number" step="0.01" min="0" max="1" />
        <div class="modal-actions">
          <button class="btn-ghost" @click="closeModal.visible = false">Cancel</button>
          <button class="btn-primary" @click="submitClose">Confirm Close</button>
        </div>
      </div>
    </div>

    <!-- ── Reset confirm modal ── -->
    <div v-if="resetModal" class="modal-overlay" @click.self="resetModal = false">
      <div class="modal">
        <div class="modal-title">Reset Portfolio?</div>
        <p>This will erase all trades, positions and equity history and restore the $10,000 virtual balance. This cannot be undone.</p>
        <div class="modal-actions">
          <button class="btn-ghost" @click="resetModal = false">Cancel</button>
          <button class="btn-danger" @click="doReset">Yes, reset everything</button>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <transition name="toast">
      <div v-if="toast.visible" class="toast" :class="toast.type">{{ toast.msg }}</div>
    </transition>

  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { polymarketApi } from '../api/polymarket'

// ── State ──────────────────────────────────────────────────
const stats = ref({
  balance: 10000, positions_value: 0, total_value: 10000,
  total_pnl: 0, realized_pnl: 0, unrealized_pnl: 0,
  total_return_pct: 0, total_trades: 0,
  closed_trades: 0, open_positions: 0, win_rate: 0, wins: 0, losses: 0,
})
const openPositions = ref([])
const trades = ref([])
const equityHistory = ref([])

const loading = ref({ refresh: false, pipeline: false, bot: false })

// ── Bot state ──────────────────────────────────────────────
const botRunning = ref(false)
const lastRun = ref(null)
const botSettings = ref({
  max_markets_per_cycle: 15,
  position_size_usdc: 200,
  min_edge: 0.07,
  cycle_interval_minutes: 30,
  take_profit_pct: 20,
  stop_loss_pct: 15,
  max_open_positions: 8,
  auto_close: true,
})
const pipelineResult = ref(null)
const resetModal = ref(false)
const closeModal = ref({ visible: false, pos: null, price: 0.5 })
const toast = ref({ visible: false, msg: '', type: 'success' })
const chartSvg = ref(null)
const chartRange = ref('ALL')
const ranges = ['ALL', '7D', '24H']

const pipelineForm = ref({
  report_id: '',
  report_markdown: '',
  max_markets: 20,
  position_size: 200,
  min_edge: 0.06,
  dry_run: false,
})

// ── Computed ───────────────────────────────────────────────
const filteredEquity = computed(() => {
  const h = equityHistory.value
  if (!h.length) return []
  if (chartRange.value === 'ALL') return h
  const now = Date.now()
  const ms = chartRange.value === '24H' ? 86400000 : 7 * 86400000
  return h.filter(p => new Date(p.timestamp).getTime() >= now - ms)
})

// ── Lifecycle ──────────────────────────────────────────────
let _autoRefreshTimer = null

onMounted(async () => {
  await Promise.all([refreshAll(), loadBotStatus()])
  // Auto-refresh every 30 s so the dashboard always shows live data
  // without the user having to click Refresh manually
  _autoRefreshTimer = setInterval(async () => {
    await Promise.all([loadPortfolio(), loadTrades(), loadBotStatus()])
  }, 30_000)
})

onUnmounted(() => {
  if (_autoRefreshTimer) clearInterval(_autoRefreshTimer)
})

watch(filteredEquity, () => nextTick(drawChart))

// ── Data loading ────────────────────────────────────────────
async function refreshAll() {
  loading.value.refresh = true
  try {
    await Promise.all([loadPortfolio(), loadTrades()])
  } finally {
    loading.value.refresh = false
  }
}

async function loadPortfolio() {
  try {
    const res = await polymarketApi.getPortfolio()
    stats.value = res.data.stats
    equityHistory.value = res.data.equity_history || []
    openPositions.value = Object.values(res.data.portfolio?.positions || {})
    nextTick(drawChart)
  } catch (e) {
    showToast('Failed to load portfolio: ' + e.message, 'error')
  }
}

async function loadTrades() {
  try {
    const res = await polymarketApi.getTrades()
    trades.value = res.data
  } catch (e) {
    showToast('Failed to load trades: ' + e.message, 'error')
  }
}

async function refreshPositions() {
  loading.value.refresh = true
  try {
    await polymarketApi.refreshPositions()
    await loadPortfolio()
    showToast('Prices updated')
  } catch (e) {
    showToast('Refresh failed: ' + e.message, 'error')
  } finally {
    loading.value.refresh = false
  }
}

// ── Pipeline ────────────────────────────────────────────────
async function runPipeline() {
  if (!pipelineForm.value.report_id && !pipelineForm.value.report_markdown) {
    showToast('Provide a Report ID or paste the report markdown', 'error')
    return
  }
  loading.value.pipeline = true
  pipelineResult.value = null
  try {
    const payload = { ...pipelineForm.value }
    if (!payload.report_id) delete payload.report_id
    if (!payload.report_markdown) delete payload.report_markdown
    const res = await polymarketApi.runPipeline(payload)
    pipelineResult.value = res.data
    showToast(`Pipeline done: ${res.data.trades_placed} trade(s) placed`)
    await refreshAll()
  } catch (e) {
    showToast('Pipeline error: ' + e.message, 'error')
  } finally {
    loading.value.pipeline = false
  }
}

// ── Close position ──────────────────────────────────────────
async function closeAtLive(marketId) {
  try {
    await polymarketApi.closePosition(marketId, 'live', 'manual_live')
    showToast('Position closed at live price')
    await refreshAll()
  } catch (e) {
    showToast('Close failed: ' + e.message, 'error')
  }
}

function openCloseModal(pos) {
  closeModal.value = { visible: true, pos, price: pos.current_price }
}

async function submitClose() {
  try {
    await polymarketApi.closePosition(
      closeModal.value.pos.market_id,
      closeModal.value.price,
      'manual_custom'
    )
    closeModal.value.visible = false
    showToast('Position closed')
    await refreshAll()
  } catch (e) {
    showToast('Close failed: ' + e.message, 'error')
  }
}

// ── Reset ────────────────────────────────────────────────────
function confirmReset() { resetModal.value = true }
async function doReset() {
  try {
    await polymarketApi.resetPortfolio()
    resetModal.value = false
    showToast('Portfolio reset to $10,000')
    await refreshAll()
  } catch (e) {
    showToast('Reset failed: ' + e.message, 'error')
  }
}

// ── Chart (vanilla SVG, no deps) ─────────────────────────────
function drawChart() {
  const svg = chartSvg.value
  if (!svg) return
  const data = filteredEquity.value
  svg.innerHTML = ''

  const W = svg.clientWidth || 600
  const H = svg.clientHeight || 200
  const PAD = { top: 16, right: 16, bottom: 32, left: 56 }
  const iW = W - PAD.left - PAD.right
  const iH = H - PAD.top - PAD.bottom

  if (data.length < 2) return

  const values = data.map(d => d.value)
  const minV = Math.min(...values)
  const maxV = Math.max(...values)
  const rangeV = maxV - minV || 1

  const xScale = i => PAD.left + (i / (data.length - 1)) * iW
  const yScale = v => PAD.top + iH - ((v - minV) / rangeV) * iH

  const ns = 'http://www.w3.org/2000/svg'

  // Grid lines
  for (let i = 0; i <= 4; i++) {
    const y = PAD.top + (i / 4) * iH
    const val = maxV - (i / 4) * rangeV
    const line = document.createElementNS(ns, 'line')
    line.setAttribute('x1', PAD.left); line.setAttribute('x2', W - PAD.right)
    line.setAttribute('y1', y); line.setAttribute('y2', y)
    line.setAttribute('stroke', '#2a2a3a'); line.setAttribute('stroke-width', '1')
    svg.appendChild(line)
    const txt = document.createElementNS(ns, 'text')
    txt.setAttribute('x', PAD.left - 6); txt.setAttribute('y', y + 4)
    txt.setAttribute('text-anchor', 'end')
    txt.setAttribute('fill', '#666'); txt.setAttribute('font-size', '10')
    txt.textContent = '$' + val.toFixed(0)
    svg.appendChild(txt)
  }

  // Area fill
  const isProfit = values[values.length - 1] >= values[0]
  const pathD = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${xScale(i)},${yScale(d.value)}`).join(' ')
  const area = document.createElementNS(ns, 'path')
  area.setAttribute('d',
    pathD +
    ` L${xScale(data.length - 1)},${PAD.top + iH} L${PAD.left},${PAD.top + iH} Z`
  )
  area.setAttribute('fill', isProfit ? 'rgba(0,210,120,0.08)' : 'rgba(255,80,80,0.08)')
  svg.appendChild(area)

  // Line
  const linePath = document.createElementNS(ns, 'path')
  linePath.setAttribute('d', pathD)
  linePath.setAttribute('fill', 'none')
  linePath.setAttribute('stroke', isProfit ? '#00d278' : '#ff5050')
  linePath.setAttribute('stroke-width', '2')
  svg.appendChild(linePath)

  // Start / end dots
  ;[[0, values[0]], [data.length - 1, values[values.length - 1]]].forEach(([i, v]) => {
    const c = document.createElementNS(ns, 'circle')
    c.setAttribute('cx', xScale(i)); c.setAttribute('cy', yScale(v))
    c.setAttribute('r', '4')
    c.setAttribute('fill', isProfit ? '#00d278' : '#ff5050')
    svg.appendChild(c)
  })

  // X labels (first / mid / last)
  const labelIndices = [0, Math.floor((data.length - 1) / 2), data.length - 1]
  labelIndices.forEach(i => {
    const d = data[i]
    const txt = document.createElementNS(ns, 'text')
    txt.setAttribute('x', xScale(i))
    txt.setAttribute('y', H - 6)
    txt.setAttribute('text-anchor', i === 0 ? 'start' : i === data.length - 1 ? 'end' : 'middle')
    txt.setAttribute('fill', '#555'); txt.setAttribute('font-size', '10')
    txt.textContent = fmtTimeShort(d.timestamp)
    svg.appendChild(txt)
  })
}

// ── Bot ──────────────────────────────────────────────────────
async function loadBotStatus() {
  try {
    const res = await polymarketApi.getBotStatus()
    botRunning.value = res.data.running
    lastRun.value = res.data.last_run || null
    const s = res.data.settings || {}
    botSettings.value = {
      max_markets_per_cycle: s.max_markets_per_cycle ?? 15,
      position_size_usdc: s.position_size_usdc ?? 200,
      min_edge: s.min_edge ?? 0.07,
      cycle_interval_minutes: s.cycle_interval_minutes ?? 30,
      take_profit_pct: Math.round((s.take_profit ?? 0.20) * 100),
      stop_loss_pct: Math.round(Math.abs(s.stop_loss ?? 0.15) * 100),
      max_open_positions: s.max_open_positions ?? 8,
      auto_close: s.auto_close ?? true,
    }
  } catch (e) {
    console.warn('Bot status unavailable:', e.message)
  }
}

async function runOnce() {
  loading.value.bot = true
  try {
    showToast('Bot cycle started — this may take a minute…')
    const res = await polymarketApi.runBotOnce()
    lastRun.value = res.data
    showToast(
      `Cycle done: ${res.data.trades_opened} opened, ${res.data.trades_closed} closed`
    )
    await refreshAll()
  } catch (e) {
    showToast('Bot cycle error: ' + e.message, 'error')
  } finally {
    loading.value.bot = false
  }
}

async function startBot() {
  try {
    await polymarketApi.startBot()
    botRunning.value = true
    showToast('Bot started — running every ' + botSettings.value.cycle_interval_minutes + ' min')
  } catch (e) {
    showToast('Start failed: ' + e.message, 'error')
  }
}

async function stopBot() {
  try {
    await polymarketApi.stopBot()
    botRunning.value = false
    showToast('Bot stopped')
  } catch (e) {
    showToast('Stop failed: ' + e.message, 'error')
  }
}

async function saveSettings() {
  try {
    await polymarketApi.updateBotSettings({
      max_markets_per_cycle: botSettings.value.max_markets_per_cycle,
      position_size_usdc: botSettings.value.position_size_usdc,
      min_edge: botSettings.value.min_edge,
      cycle_interval_minutes: botSettings.value.cycle_interval_minutes,
      max_open_positions: botSettings.value.max_open_positions,
      auto_close: botSettings.value.auto_close,
    })
  } catch (e) {
    showToast('Settings save failed: ' + e.message, 'error')
  }
}

async function saveSettingsTP() {
  try {
    await polymarketApi.updateBotSettings({ take_profit: botSettings.value.take_profit_pct / 100 })
  } catch (e) { /* silent */ }
}

async function saveSettingsSL() {
  try {
    await polymarketApi.updateBotSettings({ stop_loss: -(botSettings.value.stop_loss_pct / 100) })
  } catch (e) { /* silent */ }
}

// ── Helpers ──────────────────────────────────────────────────
const fmt = v => (v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))
const pct = v => v == null ? '—' : (Number(v) * 100).toFixed(1) + '%'
const truncate = (s, n) => s?.length > n ? s.slice(0, n) + '…' : (s || '—')
const pnlClass = v => v > 0 ? 'positive' : v < 0 ? 'negative' : ''

function fmtTime(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}
function fmtTimeShort(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
}

function showToast(msg, type = 'success') {
  toast.value = { visible: true, msg, type }
  setTimeout(() => { toast.value.visible = false }, 3500)
}
</script>

<style scoped>
/* ── Root ── */
.pt-root {
  min-height: 100vh;
  background: #0e0e18;
  color: #e0e0e0;
  font-family: 'Inter', 'Segoe UI', sans-serif;
  font-size: 13px;
}

/* ── Header ── */
.pt-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 28px;
  background: #13131f;
  border-bottom: 1px solid #1e1e2e;
}
.pt-brand {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 2px;
  color: #fff;
  cursor: pointer;
}
.pt-brand:hover { color: #7c6af7; }
.pt-header-title {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
}
.pt-badge {
  background: #7c6af7;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 20px;
  letter-spacing: 1px;
}
.pt-subtitle { color: #555; font-size: 12px; }
.pt-header-actions { display: flex; gap: 10px; }

/* ── Buttons ── */
.btn-primary {
  background: #7c6af7;
  color: #fff;
  border: none;
  padding: 8px 20px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.btn-primary:hover { background: #6a58e5; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-ghost {
  background: transparent;
  border: 1px solid #2a2a3a;
  color: #aaa;
  padding: 7px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  transition: border-color 0.15s;
}
.btn-ghost:hover { border-color: #7c6af7; color: #e0e0e0; }
.btn-ghost:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-ghost-sm {
  background: transparent;
  border: 1px solid #2a2a3a;
  color: #aaa;
  padding: 4px 10px;
  border-radius: 5px;
  cursor: pointer;
  font-size: 11px;
}
.btn-ghost-sm:hover { border-color: #7c6af7; }
.btn-danger-ghost {
  background: transparent;
  border: 1px solid #3a1a1a;
  color: #c44;
  padding: 7px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.btn-danger-ghost:hover { border-color: #c44; }
.btn-danger {
  background: #c44;
  color: #fff;
  border: none;
  padding: 8px 18px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}
.btn-danger:hover { background: #e55; }
.btn-close-pos {
  background: transparent;
  border: 1px solid #c44;
  color: #c44;
  padding: 5px 12px;
  border-radius: 5px;
  cursor: pointer;
  font-size: 11px;
}
.btn-close-pos:hover { background: #c4424215; }
.btn-close-pos.secondary { border-color: #444; color: #888; }
.btn-close-pos.secondary:hover { border-color: #7c6af7; color: #7c6af7; }

/* ── KPI cards ── */
.pt-kpis {
  display: flex;
  gap: 14px;
  padding: 18px 28px 0;
  flex-wrap: wrap;
}
.kpi-card {
  flex: 1;
  min-width: 140px;
  background: #13131f;
  border: 1px solid #1e1e2e;
  border-radius: 10px;
  padding: 16px 18px;
}
.kpi-label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
.kpi-value { font-size: 22px; font-weight: 700; color: #e0e0e0; }
.kpi-sub { font-size: 11px; color: #555; margin-top: 4px; }
.positive { color: #00d278 !important; }
.negative { color: #ff5050 !important; }

/* ── Main grid ── */
.pt-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding: 16px 28px 28px;
}
@media (max-width: 1100px) {
  .pt-grid { grid-template-columns: 1fr; }
}
.pt-left, .pt-right { display: flex; flex-direction: column; gap: 16px; }

/* ── Cards ── */
.pt-card {
  background: #13131f;
  border: 1px solid #1e1e2e;
  border-radius: 10px;
  padding: 18px 20px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 8px;
}
.card-title { font-size: 13px; font-weight: 600; color: #ccc; }
.card-hint { font-size: 11px; color: #555; }

/* ── Chart ── */
.chart-card {}
.chart-range-pills { display: flex; gap: 6px; }
.pill {
  background: transparent;
  border: 1px solid #2a2a3a;
  color: #666;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  cursor: pointer;
}
.pill.active { border-color: #7c6af7; color: #7c6af7; background: #7c6af710; }
.chart-wrapper { position: relative; height: 200px; }
.equity-svg { width: 100%; height: 100%; display: block; }
.chart-empty {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: #444; font-size: 12px;
}

/* ── Pipeline form ── */
.pipeline-form { display: flex; flex-direction: column; gap: 10px; }
.form-row { display: flex; flex-direction: column; gap: 5px; }
.form-row label { font-size: 11px; color: #666; }
.form-row input, .form-row textarea {
  background: #0e0e18;
  border: 1px solid #2a2a3a;
  border-radius: 6px;
  color: #e0e0e0;
  padding: 7px 10px;
  font-size: 12px;
  outline: none;
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
}
.form-row input:focus, .form-row textarea:focus { border-color: #7c6af7; }
.form-row-inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; }
.form-field { display: flex; flex-direction: column; gap: 5px; flex: 1; min-width: 90px; }
.form-field label { font-size: 11px; color: #666; }
.form-field input {
  background: #0e0e18;
  border: 1px solid #2a2a3a;
  border-radius: 6px;
  color: #e0e0e0;
  padding: 7px 10px;
  font-size: 12px;
  outline: none;
  width: 100%;
  box-sizing: border-box;
}
.checkbox-field { flex-direction: row; align-items: center; }
.checkbox-field label { display: flex; align-items: center; gap: 6px; color: #aaa; font-size: 12px; }
.checkbox-field input { width: auto; }

/* ── Pipeline result ── */
.pipeline-result {
  margin-top: 14px;
  border-top: 1px solid #1e1e2e;
  padding-top: 14px;
}
.result-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; color: #00d278; font-size: 12px; }
.result-meta { color: #555; font-size: 11px; }
.result-table-wrap { overflow-x: auto; max-height: 280px; overflow-y: auto; }
.result-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.result-table th { color: #555; font-weight: 600; padding: 5px 8px; border-bottom: 1px solid #1e1e2e; text-align: left; }
.result-table td { padding: 5px 8px; border-bottom: 1px solid #16162a; }
.edge-yes { color: #7c6af7; font-weight: 600; }
.action-badge {
  padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600;
  background: #1e1e2e; color: #666;
}
.action-badge.trade { background: #1a2a1a; color: #00d278; }
.action-badge.dry_run { background: #1a1a2a; color: #7c6af7; }
.action-badge.error { background: #2a1a1a; color: #ff5050; }

/* ── Open positions ── */
.empty-state { color: #444; font-size: 12px; text-align: center; padding: 20px 0; }
.positions-list { display: flex; flex-direction: column; gap: 12px; }
.position-row {
  background: #0e0e18;
  border: 1px solid #1e1e2e;
  border-radius: 8px;
  padding: 12px 14px;
}
.pos-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.pos-question { font-size: 12px; color: #ccc; }
.pos-side {
  font-size: 11px; font-weight: 700; padding: 2px 10px; border-radius: 10px;
}
.pos-side.yes { background: #0a2a1a; color: #00d278; }
.pos-side.no { background: #2a0a0a; color: #ff5050; }
.pos-metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin-bottom: 8px;
}
.metric { display: flex; flex-direction: column; gap: 2px; }
.metric-label { font-size: 10px; color: #555; }
.pos-reasoning { font-size: 11px; color: #555; font-style: italic; margin-bottom: 10px; }
.pos-actions { display: flex; gap: 8px; }
.conf-badge {
  font-size: 10px; padding: 1px 7px; border-radius: 8px;
}
.conf-badge.high { background: #0a2a1a; color: #00d278; }
.conf-badge.medium { background: #1a1a0a; color: #f0c040; }
.conf-badge.low { background: #2a1a0a; color: #f08030; }

/* ── Trades table ── */
.trades-table-wrap { overflow-x: auto; max-height: 360px; overflow-y: auto; }
.trades-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.trades-table th { color: #555; font-weight: 600; padding: 5px 8px; border-bottom: 1px solid #1e1e2e; text-align: left; position: sticky; top: 0; background: #13131f; }
.trades-table td { padding: 5px 8px; border-bottom: 1px solid #16162a; }
.q-cell { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.time-cell { color: #555; white-space: nowrap; }
.type-badge {
  padding: 2px 7px; border-radius: 8px; font-size: 10px; font-weight: 600;
}
.type-badge.open { background: #0a1a2a; color: #5080f0; }
.type-badge.close { background: #1a0a2a; color: #c080f0; }
.side-badge {
  padding: 2px 7px; border-radius: 8px; font-size: 10px; font-weight: 600;
}
.side-badge.yes { background: #0a2a1a; color: #00d278; }
.side-badge.no { background: #2a0a0a; color: #ff5050; }
.pnl-pct { font-size: 10px; color: #666; }
.muted { color: #555; }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.modal {
  background: #13131f;
  border: 1px solid #2a2a3a;
  border-radius: 12px;
  padding: 28px 32px;
  min-width: 340px;
  max-width: 480px;
}
.modal-title { font-size: 15px; font-weight: 700; margin-bottom: 12px; }
.modal-q { font-size: 12px; color: #888; margin-bottom: 14px; }
.modal label { font-size: 11px; color: #666; display: block; margin-bottom: 5px; }
.modal input {
  width: 100%; background: #0e0e18; border: 1px solid #2a2a3a;
  border-radius: 6px; color: #e0e0e0; padding: 8px 10px; font-size: 12px;
  outline: none; box-sizing: border-box; margin-bottom: 16px;
}
.modal input:focus { border-color: #7c6af7; }
.modal p { font-size: 12px; color: #888; margin-bottom: 16px; line-height: 1.6; }
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; }

/* ── Bot row ── */
.pt-bot-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding: 0 28px;
  margin-bottom: 0;
}
@media (max-width: 900px) { .pt-bot-row { grid-template-columns: 1fr; } }

.bot-control-card, .bot-log-card {}

.bot-status-pill {
  font-size: 11px; font-weight: 700; padding: 3px 12px; border-radius: 20px;
}
.bot-status-pill.running { background: #0a2a1a; color: #00d278; }
.bot-status-pill.stopped { background: #1e1e2e; color: #555; }

.bot-controls { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.btn-start {
  background: transparent; border: 1px solid #00d278; color: #00d278;
  padding: 7px 16px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600;
}
.btn-start:hover { background: #00d27815; }
.btn-stop {
  background: transparent; border: 1px solid #ff5050; color: #ff5050;
  padding: 7px 16px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600;
}
.btn-stop:hover { background: #ff505015; }

.bot-settings-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;
}
@media (max-width: 1200px) { .bot-settings-grid { grid-template-columns: repeat(2, 1fr); } }

.bs-field { display: flex; flex-direction: column; gap: 5px; }
.bs-field label { font-size: 11px; color: #666; }
.bs-field input[type="number"] {
  background: #0e0e18; border: 1px solid #2a2a3a; border-radius: 6px;
  color: #e0e0e0; padding: 6px 10px; font-size: 12px; outline: none; width: 100%; box-sizing: border-box;
}
.bs-field input[type="number"]:focus { border-color: #7c6af7; }
.bs-field.checkbox-field { flex-direction: row; align-items: center; }
.bs-field.checkbox-field label {
  display: flex; align-items: center; gap: 6px; color: #aaa; font-size: 12px; flex-direction: row;
}

/* ── Bot log ── */
.run-log {
  max-height: 220px; overflow-y: auto;
  font-family: 'Courier New', monospace; font-size: 11px;
  display: flex; flex-direction: column; gap: 3px;
}
.log-line { display: flex; gap: 10px; }
.log-ts { color: #444; min-width: 50px; }
.log-msg { color: #aaa; }
.log-line.warning .log-msg { color: #f0c040; }
.log-line.error .log-msg { color: #ff5050; }
.log-line.info .log-msg { color: #aaa; }

/* ── Toast ── */
.toast {
  position: fixed; bottom: 28px; right: 28px;
  background: #1e1e2e;
  border-left: 3px solid #7c6af7;
  color: #e0e0e0;
  padding: 12px 20px;
  border-radius: 8px;
  font-size: 12px;
  z-index: 2000;
  box-shadow: 0 4px 24px rgba(0,0,0,0.5);
}
.toast.error { border-left-color: #ff5050; }
.toast-enter-active, .toast-leave-active { transition: all 0.3s ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(10px); }
</style>
