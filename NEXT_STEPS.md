# MiroFish Bot

Portfolio: 10406.89
Posiciones: 5/5
Win rate: 19.5pct

## Proximo paso recomendado

Revisar y ajustar stop-loss / take-profit para entradas en precios < 0.10, ya que el stop loss de -15pct no funciona bien en ese rango.

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
2. Win rate muy bajo (19.5pct) dependiente de outliers positivos.
3. Stop loss de -15pct no funciona bien en precios < 0.10.
4. Sin simulacion de slippage ni liquidez en paper trading.
5. bot_settings.json puede quedar stale vs codigo fuente.

### Estado del deployment

- Commit deployado: `5caed1d` (`deploy(railway): force single replica to prevent JSON race conditions`)
- Deployment Railway: `13412549-b3f7-40b4-b80a-8b7c162bd344` — **SUCCESS**
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

Revisar y ajustar stop-loss / take-profit para entradas en precios < 0.10.
El stop loss fijo de -15pct es inefectivo cuando el precio de entrada ya es
extremo; hay que definir niveles adaptativos (sl_extreme_tight, sl_extreme_mid)
y validar con los ultimos trades cerrados.

### Prompt para la siguiente conversacion

Continua con el bot MiroFish Polymarket. El estado actual esta en NEXT_STEPS.md.
El siguiente paso es revisar el stop-loss para entradas en precios < 0.10.
Abre `backend/app/services/polymarket/autonomous_pipeline.py` y el historial de
trades recientes, analiza como se comportaron las posiciones que entraron por
debajo de 0.10, y propone ajustes a los parametros `sl_extreme_tight`,
`sl_extreme_mid`, `stop_loss_low_entry` y similares. Luego commitea y pushea
los cambios si mejoran el riesgo/retorno esperado.
