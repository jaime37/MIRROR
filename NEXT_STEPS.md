# MiroFish Bot

Portfolio: 10406.89
Posiciones: 5/5
Win rate: 22.2pct

## Proximo paso recomendado

Implementar simulación de slippage y liquidez en paper trading, o revisar el filtro de selección de mercados para reducir trades expirados/stale con pérdida de comisiones (~4%).

## Contexto completo

El bot MiroFish Polymarket es un trader autonomo contrarian sobre extremos de mercado.
Opera en paper trading en Railway. La estrategia compra el underdog cuando un lado
del mercado supera el 80pct o cae bajo el 20pct.

### Decisiones recientes tomadas

1. Umbral contrarian bajado de 0.85 a 0.80 para desbloquear entradas.
2. min_entry_price bajado de 0.15 a 0.03 para capturar extremos disponibles.
3. max_markets_per_cycle aumentado de 10 a 30 para mas oportunidades.
4. Heat limit elevado a 20pct.

### Problemas criticos identificados

1. ~~Persistencia JSON plano en Railway multi-replica -> race conditions graves.~~ **Mitigado:** forzada 1 replica en Railway.
2. Win rate muy bajo (22.2pct) dependiente de outliers positivos.
3. ~~Stop loss de -15pct no funciona bien en precios < 0.10.~~ **Mitigado:** TP/SL/hard-stop adaptativos por rango de entrada (RISK v4).
4. Sin simulacion de slippage ni liquidez en paper trading.
5. bot_settings.json puede quedar stale vs codigo fuente.

### Estado del deployment

- Commit deployado: ver último commit en `main`
- Deployment Railway: ver deployment más reciente en servicio `MIRROR`
- Replicas configuradas: **1**
- Instancias corriendo: **1 (RUNNING)**
- Healthcheck: **200 OK** (`mirror-production-bff6.up.railway.app/health`)
- Bot activo: `running=true`, portfolio `$10406.89`, 5 posiciones abiertas, ultimo ciclo `2026-06-12T10:22:45Z`

### Archivos clave

- backend/app/services/polymarket/autonomous_pipeline.py (logica principal)
- backend/app/services/polymarket/portfolio_db.py (persistencia JSON)
- backend/app/api/polymarket.py (endpoints Flask)
- railway.json (config Railway)

## Primer paso recomendado

Implementar simulación de slippage y liquidez en paper trading, o afinar el
filtro de selección de mercados para reducir la proporción de trades que
terminan `expired`/`stale` con pérdida de comisiones (~4%).

### Prompt para la siguiente conversacion

Continua con el bot MiroFish Polymarket. El estado actual esta en NEXT_STEPS.md.
El siguiente paso es implementar simulacion de slippage/liquidez en paper trading
o revisar el filtro de mercados para reducir trades expirados/stale. Abre
`backend/app/services/polymarket/paper_trader.py` y el historial de trades
recientes, analiza donde se podría modelar el impacto de liquidez/comisiones,
y propone ajustes. Luego commitea y pushea los cambios si mejoran el
riesgo/retorno esperado.
