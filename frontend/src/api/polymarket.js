import service from './index'

export const polymarketApi = {
  // Market data
  getMarkets: (params = {}) =>
    service.get('/api/polymarket/markets', { params }),

  // Portfolio
  getPortfolio: () =>
    service.get('/api/polymarket/portfolio'),

  resetPortfolio: () =>
    service.post('/api/polymarket/portfolio/reset'),

  // Trades
  getTrades: () =>
    service.get('/api/polymarket/trades'),

  closePosition: (marketId, exitPrice, reason = 'manual') =>
    service.post('/api/polymarket/trades/close', {
      market_id: marketId,
      exit_price: exitPrice,
      reason,
    }),

  // Pipeline (manual with report)
  runPipeline: (payload) =>
    service.post('/api/polymarket/pipeline/run', payload),

  refreshPositions: () =>
    service.post('/api/polymarket/pipeline/refresh'),

  // Autonomous Bot
  getBotStatus: () =>
    service.get('/api/polymarket/bot/status'),

  runBotOnce: () =>
    service.post('/api/polymarket/bot/run-once'),

  startBot: () =>
    service.post('/api/polymarket/bot/start'),

  stopBot: () =>
    service.post('/api/polymarket/bot/stop'),

  getBotRuns: (limit = 20) =>
    service.get('/api/polymarket/bot/runs', { params: { limit } }),

  getBotSettings: () =>
    service.get('/api/polymarket/bot/settings'),

  updateBotSettings: (settings) =>
    service.post('/api/polymarket/bot/settings', settings),
}
