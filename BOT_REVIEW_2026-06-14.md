# Revisión Técnica — MiroFish Polymarket Bot
**Fecha:** 2026-06-14  
**Referencia:** Producción `mirror-production-bff6.up.railway.app` (Railway, service `MIRROR`, 1 réplica)  
**Estado operativo:** ✅ saludable (healthcheck 200), bot activo, último ciclo 2026-06-12 sin errores.

---

## 1. Estado financiero resumido (producción)

| Métrica | Valor |
|---------|-------|
| Valor total | **$10,406.89** |
| Balance inicial | $10,000.00 |
| Retorno total | **+4.07 %** |
| P&L realizado | +$400.00 |
| P&L no realizado | +$14.80 |
| Cash | $9,092.08 |
| Posiciones abiertas | **5 / 5** (tope alcanzado) |
| Win rate (trades cerrados) | **20.9 %** (9W / 34L) |
| Total trades API | 93 |

**Interpretación:** El bot es rentable a pesar de un win rate muy bajo. Eso indica una estrategia **asimétrica de alto payout**: muchas pérdidas pequeñas compensadas por unas pocas ganancias grandes. El riesgo estadístico es que la rentabilidad depende de "colas positivas" poco frecuentes.

---

## 2. Arquitectura general

```
Flask (run.py / app/__init__.py)
  ├── Auto-start del bot en hilo daemon (+ watchdog cada 5 min)
  ├── Blueprint /api/polymarket
  └── Lógica del bot en app/services/polymarket/
        ├── autonomous_pipeline.py  ← núcleo del ciclo autónomo
        ├── market_fetcher.py       ← Gamma API + CLOB precios
        ├── news_researcher.py      ← DuckDuckGo/Tavily + LLM
        ├── paper_trader.py         ← motor paper (fees 2 %)
        ├── portfolio_db.py         ← persistencia JSON
        └── bot_reporter.py         ← reportes automáticos
```

**Persistencia:** Todo se guarda en JSON plano (`backend/uploads/paper_trading/`). Aunque se forzó 1 réplica en Railway para evitar condiciones de carrera, sigue siendo frágil ante cortes de luz o crashes entre escrituras.

---

## 3. Estrategia actual: contrarian sobre extremos

### Entrada
- Solo se entra si un lado supera el **80 %** (YES > 80 % → compra NO; NO > 80 % → compra YES).
- Se descartan mercados de "rango medio" (ambos lados entre 15 % y 80 %).
- Antes de entrar, el LLM busca noticias y decide si hay **disruption** que invalide el consenso.
- Edge mínimo: **10 %** (harcodeado por `FLOOR`).
- Precio mínimo de entrada: **3 ¢** (`min_entry_price=0.03`).

### Sizing
- Base: **$200** por posición.
- Multiplicadores: high + large edge → $250; high/base → $200; medium/base → $140.
- Tope: 5 % del portfolio por posición (~$520 actualmente).
- Floor: **$100** mínimo.
- Si drawdown > 10 %, se reduce sizing a la mitad.

### Cierre
Mecanismos mecánicos, sin decisión discrecional:

| Rango entrada | Take-profit | Stop-loss | Hard-stop (mov. lado dominante) |
|---------------|-------------|-----------|---------------------------------|
| < 5 % o > 95 % | +100 % | -15 % | +10 pp |
| < 10 % o > 90 % | +50 % | -10 % | +7 pp |
| < 20 % o > 80 % | +30 % | -15 % | +7 pp |
| Estándar | +20 % | -15 % | +10 pp |

Otros cierres:
- Expiración (`end_date` + 4 h de gracia).
- Zombie (> 14 días abierta).
- Pre-expiry lock (≤ 5 días y P&L > 0).
- Stale (≥ 5 días sin movimiento o movimiento en contra).

---

## 4. Hallazgos y problemas

### 🔴 Críticos

#### 4.1 Expectativa matemática débil en el tramo "base"
Con fee del **2 % por patada** (4 % round-trip), un trade estándar:
- TP +20 % → neto +16 %.
- SL -15 % → neto -19 %.
- Ratio riesgo/beneficio neto ≈ **1 : 0.84**.

Para que sea rentable con ese ratio, la tasa de acierto debería ser > **53 %**, pero el histórico real es ~21 %. La rentabilidad actual proviene exclusivamente de los outliers con TP +50 % / +100 %. Si desaparecen esos outliers, el bot quemará capital lentamente.

#### 4.2 Umbral de rango medio es inconsistente
En `autonomous_pipeline.py:625`:
```python
if (0.15 < market.yes_price < 0.80) and (0.15 < market.no_price < 0.80):
```
- Es redundante: si YES está entre 15 % y 80 %, NO automáticamente está entre 20 % y 85 %.
- Es asimétrico: permite entradas cuando YES ≈ 15 % pero no cuando YES ≈ 85 % (límite superior 80 %).
- El 15 % inferior frente al 80 % superior no tiene justificación clara.

#### 4.3 Filtro de disruption es binario y no calibrado
El LLM responde solo `"DISRUPTION: YES"`, `"DISRUPTION: NO"` o `"NO DISRUPTION"`. No hay:
- Grado de disruption.
- Histórico de falsos positivos/negativos.
- Ajuste por modelo de LLM usado.

Esto puede rechazar buenas oportunidades o dejar pasar sesgos del mercado.

#### 4.4 Sin slippage ni impacto de liquidez
`paper_trader.py` asume ejecución al midpoint con fee fija del 2 %. En mercados de baja liquidez (muchas entradas < 10 ¢), una orden de $200 puede:
- Mover el precio.
- No llenarse completamente al midpoint.
- Tener spread amplio.

Esto **sobreestima el P&L real** y es exactamente lo que `NEXT_STEPS.md` marcaba como pendiente.

### 🟡 Importantes

#### 4.5 Win rate bajo puede ser estructural
20.9 % significa que ~4 de cada 5 trades pierden. La estrategia requiere que los winners paguen > 4× los losers después de comisiones. Eso es sostenible solo si:
- El mercado sobreestima sistemáticamente la probabilidad de los favoritos (posible en eventos políticos/geopolíticos).
- El LLM filtra correctamente las "seguras" que realmente lo son.

No hay evidencia empírica de que el LLM tenga ese skill de forma consistente.

#### 4.6 Cap de 5 posiciones limita el capital desplegado
Con 5 posiciones de ~$200 y $9,092 en cash, el bot está **sub-invertido**. El "heat" real (~10 %) está lejos del límite del 20 %. Esto reduce el retorno sobre capital pero también limita la volatilidad.

#### 4.7 Falta de diversificación por categoría / correlación
Las posiciones abiertas actuales incluyen eventos geopolíticos de Oriente Medio. Sin análisis de correlación, el portfolio puede estar expuesto a un único factor de riesgo. No hay límite de posiciones por categoría (política, crypto, deportes, etc.).

#### 4.8 Drawdown pause a -20 % es tardío
Esperar a perder $2,000 antes de detener entradas es reactivo. Un drawdown del 20 % requiere un retorno del 25 % solo para recuperarse.

#### 4.9 Cooldown post-TP de solo 3 días vs 7 días post-SL
El bot penaliza más las pérdidas que las ganancias, lo cual es razonable, pero 3 días puede ser corto para mercados que ya revertieron.

#### 4.10 Persistencia JSON sin transaccionalidad
Si el proceso se mata entre `save_portfolio` y `add_trade`, puede quedar:
- Posición abierta sin trade OPEN.
- Balance descontado sin posición.
- CLOSE duplicado (aunque `portfolio_db.py` ya deduplica en stats).

SQLite con WAL sería mucho más robusto.

### 🟢 Menores / Observaciones

#### 4.11 `estimated_prob=0.5` hardcodeado en entradas autónomas
En `autonomous_pipeline.py:774` se pasa `estimated_prob=0.5` fijo, lo que hace que el campo pierda significado para el bot autónomo.

#### 4.12 Prompt de `news_researcher.py` dice "April 2025"
Los prompts están desactualizados (`Today is April 2025`). Aunque el LLM puede inferir la fecha actual de otros contextos, esto introduce ruido en el research.

#### 4.13 Logs locales están desactualizados
Los archivos locales (`portfolio.json`, `trades.json`, `bot_runs.json`) tienen fecha 2026-06-01, mientras que el contexto de producción es 2026-06-12. **Railway es la fuente de verdad.**

#### 4.14 No hay tests automatizados del bot
`pyproject.toml` incluye pytest, pero no hay suite de tests para el motor de paper trading ni para la estrategia. Esto dificulta refactors seguros.

---

## 5. Recomendaciones priorizadas

### Inmediatas (esta semana)

1. **Corregir el filtro de rango medio**
   ```python
   # Opción A: simétrico estricto
   if 0.20 < market.yes_price < 0.80:
       continue
   # Opción B: simétrico con colas 15/85
   if 0.15 < market.yes_price < 0.85:
       continue
   ```

2. **Ajustar TP/SL para tener en cuenta comisiones**
   - Definir "minimum edge after fees" ≥ 6-8 %.
   - O calcular TP/SL sobre P&L neto, no bruto.

3. **Actualizar fecha en prompts del LLM**
   - Reemplazar "April 2025" por fecha dinámica o instrucción genérica de usar conocimiento actual.

### Corto plazo (1-2 semanas)

4. **Modelar slippage/liquidez en paper_trader.py**
   - Si `amount_usdc / market.liquidity > 0.10`, aplicar slippage proporcional.
   - Registrar spread observado y ajustar fill price.

5. **Añadir scoring compuesto en lugar de decisión binaria**
   - Combinar: extremo del precio, edge, liquidez, tiempo a expiración, volatilidad reciente, calidad del research.
   - Solo abrir si score > umbral.

6. **Limitar correlación / categoría**
   - Máximo 2 posiciones por categoría (geopolítica, política USA, crypto, deportes).
   - Evitar concentración en un solo evento macro.

7. **Mejorar gestión de drawdown**
   - Pausa progresiva: -5 % reduce sizing 25 %, -10 % reduce 50 %, -15 % pausa total.
   - Actual: solo -10 % y -20 %.

### Medio plazo (1 mes)

8. **Backtest histórico formal**
   - Descargar precios históricos de tokens de Polymarket.
   - Simular la estrategia contrarian con parámetros actuales.
   - Medir expectativa, max drawdown, frecuencia de trades, sensibilidad al umbral (80 % vs 85 % vs 90 %).

9. **Implementar trailing stop o cierre parcial**
   - Para entradas extremas (< 5 ¢), proteger ganancias cuando se alcance +30-50 %.
   - Cerrar 50 % de la posición y dejar correr el resto.

10. **Migrar persistencia a SQLite/Redis**
    - SQLite con WAL es transaccional y compatible con un solo proceso.
    - Mantiene la simplicidad sin necesidad de servidor de BD.

11. **Crear tests unitarios del motor de trading**
    - Tests para open/close, fees, TP/SL, stats, deduplicación de CLOSEs.
    - Facilita refactors futuros.

---

## 6. Conclusión

El bot está técnicamente funcionando y es ligeramente rentable (+4.07 %), pero la rentabilidad depende de una cola de alto payout muy infrecuente. El **win rate de 20.9 % es preocupante** si los outliers positivos no se mantienen.

Las mejoras de mayor impacto son:
1. Corregir el filtro de rango medio (asimetría).
2. Ajustar TP/SL por comisiones reales (o modelar slippage).
3. Añadir scoring compuesto y diversificación.
4. Hacer backtest formal para validar la estrategia antes de seguir arriesgando capital real.

No se detectan bugs críticos de seguridad ni de estabilidad inmediata, pero la estrategia actual tiene una **expectativa matemática frágil** en el tramo base de trades.
