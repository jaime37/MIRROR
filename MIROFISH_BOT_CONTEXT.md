# MiroFish — Polymarket Paper Trading Bot
## Contexto completo del proyecto (actualizado 1 Junio 2026)

---

## 1. QUÉ ES EL PROYECTO

Bot autónomo de paper trading (dinero simulado) que opera en **Polymarket** — mercados de predicción donde se apuesta si eventos van a ocurrir o no (política, geopolítica, deportes, cripto, etc.).

- **No usa dinero real** — paper trading con $10,000 virtuales
- **Corre 24/7 en Railway** (cloud) — nunca se para
- **Frontend local** en `localhost:3000` que conecta a Railway vía `VITE_API_BASE_URL`
- **El usuario solo arranca `npm run dev`** — nunca el backend local
- **Auto-refresh en frontend cada 60s** — datos siempre actualizados sin pulsar Refresh

---

## 2. STACK TÉCNICO

### Backend (Railway)
- **Flask** — servidor web Python
- **Polymarket Gamma API** — lista de mercados activos, precios desde `outcomePrices`, token IDs desde `clobTokenIds`
- **Polymarket CLOB API** — precios en tiempo real por token (midpoint)
- **Groq API** — LLM `llama-3.3-70b-versatile` para estimar probabilidades
- **DuckDuckGo Search** (`ddgs`) — búsqueda de noticias recientes por mercado
- **dateutil** — parsing de fechas de expiración
- **Threading** — bot corre en hilo daemon con `_bot_thread` / `_stop_event`

### Frontend
- **Vue 3 + Vite** — dashboard en `localhost:3000`
- Conecta a Railway via `VITE_API_BASE_URL`
- Auto-refresh cada 60 segundos (onMounted setInterval)

### Infraestructura
- **Railway** — servidor cloud, deploys automáticos desde GitHub push
- **Railway Volume** — disco persistente que survives redeploys
- **GitHub** — repo `jaime37/MIRROR` (privado), branch `main`

---

## 3. ARCHIVOS CLAVE

```
backend/
  app/
    services/
      polymarket/
        autonomous_pipeline.py   ← NÚCLEO del bot (más modificado)
        market_fetcher.py
        news_researcher.py
        paper_trader.py          ← Threading lock anti-duplicados
        portfolio_db.py          ← Stats con deduplicación de CLOSEs
    api/
      polymarket.py              ← Endpoints Flask
  uploads/
    paper_trading/
      portfolio.json             ← Balance y posiciones abiertas (Railway Volume)
      trades.json                ← Historial completo de trades
      bot_runs.json              ← Log de cada ciclo del bot
      bot_settings.json          ← Settings guardados (Railway Volume)
      .bot_last_start            ← Debounce anti-doble-bot

frontend/
  src/
    views/
      PaperTradingView.vue       ← Dashboard principal (auto-refresh 60s)
    api/
      polymarket.js              ← Client API
```

> ⚠️ Los archivos locales son STALE — Railway es la fuente de verdad.
> Para revisar datos en tiempo real usar: `https://mirror-production-bff6.up.railway.app/api/polymarket/portfolio?_=TIMESTAMP`
> Siempre añadir `?_=TIMESTAMP` para evitar caché de WebFetch.

---

## 4. CÓMO FUNCIONA EL BOT (ciclo cada 30 min)

```
1. Fetch top 10 mercados de Polymarket (filtros: volumen, liquidez, keywords)
2. Para cada mercado → DuckDuckGo busca noticias recientes
3. LLM (Groq/Llama) estima probabilidad YES + confianza
4. Calcula edge: |prob_LLM - precio_mercado|
5. Si edge >= 10% Y confianza >= medium → abre posición (sizing dinámico)
6. Revisa posiciones abiertas → cierra si TP/SL/expiración/pre-expiry-lock/stale
7. Refresca precios de posiciones abiertas
```

**Lógica de cierre (por orden de prioridad):**
1. **Expiración:** end_date + 4h grace period
2. **Zombie:** >14 días abierta sin end_date
3. **Pre-expiry lock:** ≤5 días para vencer Y P&L > 0 → cierra para asegurar ganancia
4. **Stale:** >5 días abierta Y precio movido <3% → libera capital
5. **Take Profit:** +20%
6. **Stop Loss:** variable por precio de entrada (ver Tiered SL)

---

## 5. DEFAULT_SETTINGS ACTUALES

```python
DEFAULT_SETTINGS = {
    "max_markets_per_cycle": 10,
    "position_size_usdc": 200.0,
    "min_edge": 0.10,
    "min_confidence": ["high", "medium"],
    "take_profit": 0.20,
    "stop_loss": -0.15,                    # base SL (mid-range entries)
    "stop_loss_low_entry": -0.20,          # Tier 2: entry < 30%
    "stop_loss_high_entry": -0.10,         # Tier 2: entry > 70%
    "cycle_interval_minutes": 30,
    "max_open_positions": 5,
    "min_volume": 5000,
    "min_liquidity": 1000,
    "min_entry_price": 0.15,
    "stoploss_cooldown_days": 7,
    "takeprofit_cooldown_days": 3,
    "auto_close": True,
    "delay_between_markets": 8,
    "max_days_to_expiry": 60,
    "long_term_position_ratio": 0.30,
    "pre_expiry_lock_days": 5,             # Tier 2: subido de 3→5
    "min_days_to_expiry_entry": 7,         # Tier 2: hard block (antes solo logging)
    "stale_position_days": 5,
    "stale_position_movement": 0.03,
    # Dynamic sizing multipliers
    "size_multiplier_high_conf_large_edge": 1.25,   # high conf + edge ≥20% → $250
    "size_multiplier_high_conf_base": 1.00,          # high conf + edge 10-20% → $200
    "size_multiplier_medium_conf_large_edge": 0.85,  # medium conf + edge ≥20% → $170
    "size_multiplier_medium_conf_base": 0.70,        # medium conf + edge 10-20% → $140
    "excluded_market_keywords": [
        "nba", "nfl", "nhl", "mlb", "mls",
        "premier league", "la liga", "bundesliga", "serie a", "ligue 1", "champions league",
        "world cup", "euro ", "copa", "league cup",
        "super bowl", "stanley cup", "world series",
        "match", "vs.", " vs ", "game 1"..."game 7",
        "o/u ", "over/under", "spread",
        "lpl ", "lck ", "esports", "esport",
    ],
}
```

**Floor hardcodeado en `load_settings()`:**
```python
FLOOR = {
    "min_entry_price": 0.15,
    "min_edge": 0.10,
    "min_days_to_expiry_entry": 7,
    "pre_expiry_lock_days": 5,
}
```

---

## 6. TIER 2 — DYNAMIC POSITION SIZING

El tamaño de cada posición varía según confianza + fuerza del edge:

| Confianza | Edge | Multiplicador | Tamaño ($200 base) |
|-----------|------|---------------|---------------------|
| high | ≥20% | 1.25x | $250 |
| high | 10-20% | 1.00x | $200 |
| medium | ≥20% | 0.85x | $170 |
| medium | 10-20% | 0.70x | $140 |

---

## 7. TIER 2 — TIERED STOP-LOSS

| Precio de entrada | Stop-Loss | Razón |
|-------------------|-----------|-------|
| < 30% | -20% | Mercados volátiles, necesitan margen |
| 30% – 70% | -15% | Rango estándar |
| > 70% | -10% | Mercados casi-ciertos, SL ajustado |

---

## 8. BUGS RESUELTOS (cronología completa)

| Fecha | Bug | Fix |
|-------|-----|-----|
| Abr | `timedelta` NameError silencioso en zombie detection | Añadir `timedelta` al import |
| Abr | Bot doble/triple en reinicios de Railway | Debounce cross-process via `.bot_last_start` (90s) |
| Abr | UTF-8 decode error en `bot_runs.json` | `errors="replace"` en apertura |
| May 6 | Posiciones zombie sin cierre | Guardar `end_date` en posición, detectar expiración |
| May 10 | Grace period 2 días causaba zombies deportivos | `timedelta(days=2)` → `timedelta(hours=4)` |
| May 11 | ETH expiraba sin auto-cierre con ganancia | Implementar `pre_expiry_lock_days` |
| May 12 | Filtros Tier 1 no surtían efecto (settings.json viejo) | Floor hardcodeado en `load_settings()` |
| May 19 | Race condition → 47 registros CLOSE duplicados | `threading.Lock()` en `paper_trader.py` + swap write order |
| May 24 | Stats incorrectos (win rate 14.8% inflado) | `get_stats()` deduplica CLOSEs por market_id |
| May 24 | Frontend datos stale (sin auto-refresh) | `setInterval` 60s en `PaperTradingView.vue` |

---

## 9. MEJORAS IMPLEMENTADAS (cronología)

| Commit | Mejora |
|--------|--------|
| `8d613de` | Filtros calidad: skip penny markets, low confidence, SL cooldown |
| `e108715` | Fix penny market filter: `min(yes, no) < threshold` |
| `d680432` | TP cooldown para evitar re-entrada inmediata |
| `6638165` | Auto-close posiciones expiradas, max 5 posiciones |
| `9fc0bb0` | Guardar `end_date` en posición, auto-close por fecha |
| `875062f` | Filtro max 60 días de expiración |
| `afdfe9f` | 70/30 long-term cap |
| `558df72` | Grace period expiración: 2 días → 4 horas |
| `94c2fe4` | Pre-expiry profit lock + short-expiry logger |
| `a5c077d` | Council Tier 1: sports filter, min_entry 15%, min_edge 10% |
| `e757385` | Floor hardcodeado en load_settings() |
| `408a528` | Fix race condition: threading.Lock + safe write order en paper_trader.py |
| `0e4a969` | Tier 2: dynamic sizing, tiered SL, hard expiry block (7d), pre-lock 5d |
| `84130a1` | Fix stats deduplicación + auto-refresh frontend 60s |

---

## 10. RENDIMIENTO ACTUAL (1 Junio 2026)

- **Días operando:** ~41
- **Portfolio:** $9,998.58 (-0.01% sobre $10,000 inicial)
- **Trades cerrados únicos:** 32
- **Win rate:** 15.6% (5W / 27L)
- **P&L realizado:** +$34.02
- **P&L no realizado (abiertos):** ~-$27.53
- **Total real:** -$1.42 (break-even)

### Historial de wins
| Trade | P&L | Razón |
|-------|-----|-------|
| US x Iran ceasefire NO | +$173.25 (+86.6%) | take_profit |
| West Ham relegated YES | +$217.57 (+108.8%) | take_profit |
| US x Iran diplomatic NO | +$24.64 (+12.3%) | expired |
| Pistons Spread YES | +$3.84 (+1.9%) | expired |
| US x Iran Jun 7 NO | +$60.07 (+42.9%) | take_profit |
| **Total wins** | **+$479.37** | |

### Historial de losses notables
| Trade | P&L | Razón |
|-------|-----|-------|
| Mortal Kombat II YES | -$197.44 (-98.7%) 🚨 | stop_loss (7.5% entry, pre-floor) |
| Ukraine NATO YES | -$33.88 (-16.9%) | stop_loss |
| Fed rate cut YES | -$37.01 (-21.8%) | stop_loss (price gap) |
| S&P 500 SPX NO | -$16.56 (-8.3%) | expired |
| ~22 posiciones planas | -$7.92 c/u | stale/expired (solo fees) |

**Lección clave:** Las -$7.92 son posiciones que no se movieron (solo fees). Los stops grandes son por gapping de precio entre ciclos. Sin Mortal Kombat: +$247 neto.

---

## 11. POSICIONES ABIERTAS ACTUALES (1 Junio 2026)

| Mercado | Lado | Entrada | Actual | P&L | Vence |
|---------|------|---------|--------|-----|-------|
| US-Iran no meeting Jun 30 | YES | 21.4% | 20.4% | -$9.21 | Jun 30 |
| Hormuz traffic normal Jun 30 | YES | 40.5% | 38.5% | -$11.63 | Jun 30 |
| Crude Oil $105 Jun 30 | YES | 30.0% | 30.0% | -$5.00 | Jun 30 |
| Trump Project Freedom Jun 30 | YES | 21.5% | 23.5% | **+$9.96** | Jun 30 |
| ETH no dip $1,500 (NO) | NO | 50.5% | 48.0% | -$11.65 | Jan 2027 |

---

## 12. ANÁLISIS DEL CONSEJO LLM (Mayo 2026)

### Tier 1 — Implementado ✅
1. Filtrar mercados deportivos
2. min_entry_price 5% → 15%
3. min_edge 7% → 10%

### Tier 2 — Implementado ✅
4. Position sizing dinámico (Kelly-inspired)
5. Stop-loss tiered por precio de entrada
6. Auto-close trades muertos (stale 5 días)
7. Hard block min_days_to_expiry (7 días)
8. Pre-expiry lock 3 → 5 días

### Tier 3 — Pendiente
9. Focus explícito en mercados geopolíticos/políticos (whitelist keywords)
10. Market maturity filter (mercados >24h de existencia)
11. Evaluar solo "high" confidence (si lo justifican los datos a los 50 trades)

---

## 13. PLAN: OBJETIVO 50 TRADES

- **Trades cerrados actuales:** 32
- **Faltan:** ~18 más
- **ETA estimada:** ~mediados de junio 2026
- **Acción a los 50:** analizar rendimiento post-filtros, decidir si:
  - Subir min_confidence a solo "high"
  - Aumentar min_edge a 15%
  - Considerar trading real con capital pequeño

**Política actual:** no cambiar más parámetros hasta los 50 trades (experimento limpio).

---

## 14. ARQUITECTURA TÉCNICA CLAVE

### paper_trader.py — Thread Safety
```python
_position_lock = threading.Lock()

def close_position(self, market_id, exit_price, reason="manual"):
    with _position_lock:
        return self._close_position_locked(market_id, exit_price, reason)

def _close_position_locked(self, market_id, exit_price, reason):
    # Re-lee portfolio DENTRO del lock (evita TOCTOU race condition)
    portfolio = self.db.get_portfolio()
    if market_id not in portfolio["positions"]:
        raise ValueError(...)
    # Borra posición ANTES de escribir el trade (crash-safe ordering)
    del portfolio["positions"][market_id]
    self.db.save_portfolio(portfolio)
    trade = self.db.add_trade({...})  # CLOSE trade escrito después
    return trade
```

### portfolio_db.py — Stats Deduplication
```python
def get_stats(self):
    # Deduplica CLOSEs por market_id, mantiene el más antiguo (real)
    seen_market_ids = {}
    for t in trades:
        if t.get("type") == "CLOSE":
            mid = t.get("market_id", "")
            ts = t.get("timestamp", "")
            if mid not in seen_market_ids or ts < seen_market_ids[mid]["timestamp"]:
                seen_market_ids[mid] = t
    closed = list(seen_market_ids.values())
```

### autonomous_pipeline.py — Dynamic Sizing
```python
base_size = self.settings.get("position_size_usdc", 200)
edge_strength = abs(edge)
if confidence == "high":
    multiplier = 1.25 if edge_strength >= 0.20 else 1.00
elif confidence == "medium":
    multiplier = 0.85 if edge_strength >= 0.20 else 0.70
dynamic_size = round(base_size * multiplier)
```

---

## 15. CÓMO REVISAR EL BOT (para Claude)

Siempre usar timestamps frescos para evitar caché de WebFetch:

```
1. Obtener timestamp: Get-Date -Format 'yyyyMMddHHmmss'
2. GET https://mirror-production-bff6.up.railway.app/api/polymarket/portfolio?_=TIMESTAMP
3. GET https://mirror-production-bff6.up.railway.app/api/polymarket/trades?limit=6&_=TIMESTAMP
```

Alertas a reportar:
- TP o SL disparado
- Posición nueva abierta
- Posición acercándose a TP (+15%) o SL (-12%)
- Posición venciendo en ≤6 días
- Portfolio < $9,500
- Bug o error en el bot

---

## 16. REPO Y DEPLOY

- **GitHub:** `jaime37/MIRROR` (privado), branch `main`
- **Railway:** auto-deploy en cada push a main
- **Railway URL:** `mirror-production-bff6.up.railway.app`
- **Último commit:** `84130a1` — fix: accurate stats + live auto-refresh dashboard

### Para deployar:
```bash
git add <archivos>
git commit -m "mensaje"
git push origin main
# Railway auto-deploys en ~2-3 minutos
```

---

## 17. NOTAS IMPORTANTES

1. **Railway es la fuente de verdad** — archivos locales pueden estar stale
2. **Nunca resetear el portfolio** — el usuario lo tiene explícito
3. **WebFetch cachea 15 min** — siempre usar `?_=TIMESTAMP` en las URLs
4. **El usuario solo arranca `npm run dev`** para el frontend
5. **bot_settings.json en Railway** puede tener valores viejos — el FLOOR en `load_settings()` lo protege
6. **Los trades.json tienen 6 CLOSEs duplicados históricos** — `get_stats()` los filtra, pero siguen en el archivo
7. **Cron de revisión automática** — cada 4 horas mientras la sesión de Claude esté activa (session-only, no persiste)
