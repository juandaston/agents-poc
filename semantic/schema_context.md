BASE DE DATOS FINANCIERA (PostgreSQL — schema silver, referencias gold)

REGLAS GLOBALES:
- Filtra SIEMPRE por customer_id en tablas silver y vistas gold.
- PREFERIR gold.vw_fact_bdp_enriched para montos contables, P&L y tableros (BDP + rubros unidos).
- En TODA consulta a gold.vw_fact_bdp_enriched filtrar uso = 'ER' (Estado de Resultados). Por ahora NO consultar uso = 'BG' (Balance General).
- PREFERIR gold.vw_dim_accounts SOLO para catálogo de cuentas/rubros SIN montos.
- PREFERIR gold.vw_kpis_financiero SOLO para ratios pre-calculados (ROE, ROA, liquidez, semáforos agregados).
- PREFERIR gold.vw_fact_bdp_enriched para ventas/facturación/ingresos operacionales (nombre_rubro_grupo = 'Ingresos Operacionales').
- vw_ventas_netas_mes solo para detalle facturación Siigo (bruto − NC), no para ventas del tablero contable.
- dim_customers.customer_id es varchar (identificador de negocio), NO confundir con uuid customer_id de hechos.
- fact_bdp.id_tiempo referencia gold.dim_time(id_time) — SOLO fact_bdp; fact_venta NO tiene id_tiempo.
- fact_venta: filtrar fechas SOLO con invoice_date o load_ts en silver.fact_venta. NO JOIN a gold.dim_time_sales ni gold.dim_time.
- fact_venta incluye fila fallback cuando la factura no trae ítems (product_name = Sin detalle de ítems).
- EXTRACT Siigo usa created_start/created_end (fecha de creación del documento en Siigo).
  SILVER y gold.vw_ventas_netas_mes usan invoice_date / credit_note_date (fecha de elaboración).
  Una factura elaborada en junio pero creada en Siigo en julio entra al extract de julio con invoice_date de junio.
- presupuesto_proyeccion.anio_mes formato 'YYYY-MM' (CHECK ^\\d{4}-(0[1-9]|1[0-2])$); mes es columna generada (1-12).
- Tablas test_* son entornos de prueba; preferir tablas productivas salvo que se indique lo contrario.
- El servidor consulta primero la vista gold enrutada; si no hay filas, reintenta con silver_fallback del catálogo.

──────────────────────────────────────────────────────────────────────────────
FILTRADO POR FECHAS Y PERIODOS (usar cuando la pregunta lo pida o implique tiempo)
──────────────────────────────────────────────────────────────────────────────
Detectar en la pregunta: fechas concretas, rangos ("entre enero y marzo"), mes, año,
trimestre, "último periodo", "periodo actual", "este mes", "año pasado", etc.

gold.dim_time — dimensión calendario (JOIN desde fact_bdp.id_tiempo)
- id_time       serial PK
- Date          date UNIQUE NOT NULL — fecha calendario
- Anio          int NOT NULL
- AnioMes       varchar(7) NOT NULL — 'YYYY-MM' (equivalente a presupuesto.anio_mes)
- AnioMesDia    varchar(10) NULL
- Dia           int NULL
- Mes           int NOT NULL (1-12)
- MesNumero     int NOT NULL
- Trimestre     int NULL
- SemanaAnno    int NULL

Índices útiles: Date, (Anio, Mes), AnioMes, (Anio, Trimestre)

Por tabla — columna / patrón recomendado:

| Tabla                  | Filtrar por                                                                 |
|------------------------|-----------------------------------------------------------------------------|
| fact_bdp               | source_date (fecha exacta del extracto, YYYY-MM-DD) O JOIN gold.dim_time  |
| vw_kpis_financiero     | customer_id (UUID), anio (int), anio_mes ('YYYY-MM'), mes_corto              |
| presupuesto_proyeccion | anio_mes ('YYYY-MM'), mes (1-12); siempre deleted_at IS NULL               |
| vw_ventas_netas_mes    | anio_mes ('YYYY-MM'); ventas_brutas, notas_credito, ventas_netas           |
| vw_ventas_por_producto_mes | customer_id, anio_mes; product_name, total_ventas, cantidad            |
| vw_presupuesto_vs_real_mes | customer_id, anio_mes; presupuesto, real_saldo, variacion, pct_cumplimiento |
| vw_semaforos_cliente   | customer_id; semáforos del último anio_mes (una fila por cliente)          |
| vw_ultimo_periodo_cliente | customer_id, fuente (kpis|ventas|bdp|presupuesto), ultimo_anio_mes       |
| fact_venta             | invoice_date (preferir) o load_ts::date; detalle por línea/producto          |
| fact_nota_credito      | credit_note_date (preferir) o invoice_date; cabecera de notas crédito        |
| dim_accounts           | load_ts solo si pregunta por actualización del catálogo                     |
| dim_customers          | load_ts / metadata_last_updated solo si aplica                              |

fact_bdp — ejemplos de filtro temporal:
- Día exacto:     WHERE f.source_date = DATE '2025-12-01'
- Rango:          WHERE f.source_date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
- Mes/año:        JOIN gold.dim_time t ON t.id_time = f.id_tiempo
                  WHERE t.Anio = 2025 AND t.Mes = 12
- Por AnioMes:    JOIN gold.dim_time t ON t.id_time = f.id_tiempo
                  WHERE t.AnioMes = '2025-12'
- Último periodo: WHERE f.source_date = (
                    SELECT MAX(f2.source_date) FROM {schema}.fact_bdp f2
                    WHERE f2.customer_id = f.customer_id
                  )
  (alternativa: MAX(t.AnioMes) vía join)

presupuesto_proyeccion — ejemplos:
- Mes concreto:   WHERE anio_mes = '2025-03' AND deleted_at IS NULL
- Año completo:   WHERE anio_mes LIKE '2025-%' AND deleted_at IS NULL
- Por mes num:    WHERE mes = 3 AND anio_mes LIKE '2025-%' AND deleted_at IS NULL
- Último mes:     WHERE anio_mes = (
                    SELECT MAX(p.anio_mes) FROM {schema}.presupuesto_proyeccion p
                    WHERE p.customer_id = presupuesto_proyeccion.customer_id
                      AND p.deleted_at IS NULL
                  )

fact_venta — ejemplos (SOLO columnas de silver.fact_venta, sin JOIN gold):
- Rango factura:  WHERE invoice_date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
- Mes/año:        WHERE EXTRACT(YEAR FROM invoice_date) = 2026 AND EXTRACT(MONTH FROM invoice_date) = 6
- Mes calendario: WHERE invoice_date >= DATE '2026-06-01' AND invoice_date < DATE '2026-07-01'
- Rango carga:    WHERE load_ts::date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
- PROHIBIDO:      JOIN gold.dim_time, gold.dim_time_sales o usar id_tiempo (no existen en fact_venta)

vw_ventas_netas_mes — ejemplos (PREFERIR para ventas netas/brutas mensuales):
- Mes:            WHERE anio_mes = '2026-06'
- Año:            WHERE anio_mes LIKE '2026-%'
- Rango meses:    WHERE anio_mes BETWEEN '2026-01' AND '2026-06'
- Columnas:       ventas_brutas (SUM fact_venta), notas_credito (SUM NC), ventas_netas = bruto − NC
- NO usar fact_venta para ventas netas si esta vista responde la pregunta

Reglas de filtrado temporal:
- Si la pregunta NO menciona tiempo, NO agregues filtros de fecha (salvo deleted_at en presupuesto).
- Si menciona tiempo, SIEMPRE filtra; no devuelvas toda la historia del cliente.
- Para balance vs presupuesto en el mismo periodo, alinea fact_bdp (source_date o t.AnioMes)
  con presupuesto.anio_mes.
- Usa literales DATE 'YYYY-MM-DD' o anio_mes 'YYYY-MM'; evita funciones no deterministas innecesarias.

──────────────────────────────────────────────────────────────────────────────
0. gold.vw_fact_bdp_enriched — BDP + rubros + tiempo (CONSULTAR PRIMERO para montos)
──────────────────────────────────────────────────────────────────────────────
Vista gold. fact_bdp enriquecido con gold.vw_dim_accounts + gold.dim_time.
Misma lógica que Power BI (dim + fact pre-unido). Grain: customer_id + id_auxiliar + id_tiempo.

Columnas clave:
- customer_id           uuid — filtrar SIEMPRE
- nombre_cliente        varchar — atributo
- codigo_cuenta_contable / id_auxiliar, nombre_auxiliar, nombre_cuenta, nombre_grupo, nombre_subcuenta
- id_rubro, nombre_rubro_grupo, nombre_rubro_clase, uso
- sub_nodo_s3, nodo_s1, nodo_s2, cod_nodo — jerarquía tablero por cliente
- mvto, saldo_inicial, saldo_final, movimiento_debito, movimiento_credito
- anio_mes ('YYYY-MM'), anio, mes_corto, trimestre, fecha, source_date

Filtro: customer_id = uuid del tenant.
Agregación: SUM(mvto) — NUNCA ABS(mvto) ni SUM(ABS(mvto)).
Con GROUP BY: SELECT solo columnas del GROUP BY + agregados; no mezclar saldo_inicial/saldo_final/movimiento_* sin SUM.

JERARQUÍA DE FILTROS (elige el nivel según la pregunta):
1. Rubro KPI (nombre_rubro_grupo): SOLO totales de línea amplia
   (ej. "total gastos administrativos" → nombre_rubro_grupo = 'Gasto Admon')
2. Sub-cuenta (nombre_cuenta, sub_nodo_s3, nombre_auxiliar): conceptos ESPECÍFICOS
   (ej. "seguros" → nombre_cuenta = 'Seguros' o nombre_auxiliar ILIKE '%Seguro%';
   NO sumar todo Gasto Admon)
3. Valores exactos del catálogo; no pluralizar rubros ('Gasto Financiero', no 'Gastos Financieros')

FILTRO TEMPORAL:
- Si la pregunta o el historial mencionan mes/año → filtrar anio_mes en WHERE (no GROUP BY toda la serie).
- Comparación mismo mes distinto año: anio_mes IN ('2026-06','2025-06').
- Tendencia/histórico completo: solo entonces GROUP BY anio_mes sin filtro estrecho.

──────────────────────────────────────────────────────────────────────────────
0b. gold.vw_dim_accounts — catálogo plan de cuentas (SIN montos)
──────────────────────────────────────────────────────────────────────────────
Vista gold. Plan de cuentas enriquecido con rubros/nodos; misma lógica CASE que tableros Power BI.
NO tiene mvto ni saldo — solo estructura. Grain: customer_id + id_auxiliar.

Columnas:
- customer_id           uuid — filtrar SIEMPRE por este campo
- nombre_cliente        varchar — atributo (no usar para filtrar)
- id_auxiliar           varchar — código auxiliar / cuenta contable
- nombre_auxiliar       varchar
- id_subcuenta, nombre_subcuenta, id_cuenta, nombre_cuenta
- id_grupo, nombre_grupo, id_clase, nombre_clase
- id_rubro              int — agrupación contable
- nombre_rubro_grupo    varchar — rubro KPI (Activo Corriente, Ingresos Operacionales, …)
- nombre_rubro_clase    varchar — Activo, Pasivo, Patrimonio, Ingresos, Gasto, …
- uso                   varchar — BG (balance) o ER (estado de resultados)
- nodo_s1, nodo_s2, sub_nodo_s3, cod_nodo — nodos tablero por cliente

Filtro: customer_id = uuid del tenant.

Uso: catálogo de cuentas, rubros, nodos; preguntas SIN montos.
Para montos usar gold.vw_fact_bdp_enriched.
Fallback silver: silver.dim_accounts.

──────────────────────────────────────────────────────────────────────────────
1. silver.dim_accounts — plan de cuentas / auxiliares contables por cliente
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
UNIQUE: (customer_id, id_auxiliar)

Columnas:
- id                  serial4 NOT NULL — PK
- integration_id      uuid NOT NULL
- customer_id         uuid NOT NULL — filtrar consultas por este campo
- id_auxiliar         varchar(150) NOT NULL
- nombre_auxiliar     varchar(255) NULL
- id_subcuenta        varchar(150) NULL
- nombre_subcuenta    varchar(255) NULL
- id_cuenta           varchar(150) NULL
- nombre_cuenta       varchar(255) NULL
- id_grupo            varchar(150) NULL
- nombre_grupo        varchar(255) NULL
- id_clase            varchar(150) NULL
- nombre_clase        varchar(255) NULL
- load_ts             timestamp DEFAULT CURRENT_TIMESTAMP NULL
- source_table        varchar(100) NULL

Índices:
- idx_dim_accounts_customer_id ON (customer_id)

Uso: joins por codigo/nombre de cuenta; jerarquía auxiliar → subcuenta → cuenta → grupo → clase.

──────────────────────────────────────────────────────────────────────────────
2. silver.dim_customers — maestro de clientes (dimension)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)

Columnas:
- id                      serial4 NOT NULL — PK
- customer_id             varchar(50) NULL — id negocio (texto)
- type                    varchar(50) NULL
- person_type             varchar(50) NULL
- identification          varchar(100) NULL
- name                    varchar(255) NULL
- address                 varchar(255) NULL
- state_name              varchar(100) NULL
- city_name               varchar(100) NULL
- phone                   varchar(50) NULL
- contact_email           varchar(255) NULL
- metadata_created        varchar(50) NULL
- metadata_last_updated   varchar(50) NULL
- load_ts                 timestamp DEFAULT CURRENT_TIMESTAMP NULL

Uso: datos demográficos/contacto; NO tiene uuid customer_id de hechos.

──────────────────────────────────────────────────────────────────────────────
3. silver.fact_venta — líneas de facturación / ventas
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)

Columnas:
- id              serial4 NOT NULL — PK
- invoice_id      varchar(50) NULL
- item_id         varchar(50) NULL
- document_id     varchar(50) NULL
- invoice_date    date NULL — fecha de la factura (preferir para filtros temporales)
- code            varchar(100) NULL
- product_name    text NULL
- description     text NULL
- quantity        numeric(15, 4) NULL
- price           numeric(15, 2) NULL
- total           numeric(15, 2) NULL
- taxes_raw       text NULL
- load_ts         timestamp DEFAULT CURRENT_TIMESTAMP NULL
- integration_id  uuid NULL
- customer_id     uuid NULL — filtrar consultas por este campo

Índices:
- idx_fact_venta_customer_id ON (customer_id)
- idx_fact_venta_integration_id ON (integration_id)

Uso: ventas por factura, producto, totales; agregaciones SUM(total), SUM(quantity).
Facturas sin ítems en Siigo generan una fila con product_name = (Sin detalle de ítems).

──────────────────────────────────────────────────────────────────────────────
3b. silver.fact_nota_credito — notas crédito (cabecera)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)

Columnas:
- customer_id         uuid — filtrar consultas
- credit_note_id      varchar(50)
- credit_note_date    date — preferir para agregaciones mensuales
- invoice_date        date — fecha factura origen (referencia)
- total               numeric(15,2)
- reason, credit_note_name, document_id, customer_id_sale

Uso: devoluciones; agregada en gold.vw_ventas_netas_mes como notas_credito.

──────────────────────────────────────────────────────────────────────────────
3c. gold.vw_ventas_netas_mes — ventas brutas/netas por mes
──────────────────────────────────────────────────────────────────────────────
Grain: customer_id + anio_mes ('YYYY-MM')

Columnas:
- ventas_brutas   numeric — SUM fact_venta.total por invoice_date
- notas_credito   numeric — SUM fact_nota_credito.total por credit_note_date
- ventas_netas    numeric — ventas_brutas − notas_credito

Filtro: customer_id = uuid del tenant.

──────────────────────────────────────────────────────────────────────────────
4. silver.presupuesto_proyeccion — presupuesto / proyección mensual por cuenta
──────────────────────────────────────────────────────────────────────────────
PK: id (uuid, default gen_random_uuid())
UNIQUE: (integration_id, cuenta, anio_mes)

Columnas:
- id              uuid NOT NULL — PK
- integration_id  uuid NOT NULL
- customer_id     uuid NULL — filtrar consultas por este campo
- cuenta          int8 NOT NULL — código numérico de cuenta
- cuenta_contable text NOT NULL — nombre/descripción cuenta
- anio_mes        varchar(7) NOT NULL — 'YYYY-MM'
- mes             int2 GENERATED (1-12) desde anio_mes
- saldo           numeric(18, 6) DEFAULT 0 NOT NULL
- created_at      timestamptz DEFAULT now() NULL
- updated_at      timestamptz DEFAULT now() NULL
- deleted_at      timestamptz NULL — excluir filas con deleted_at IS NOT NULL

Constraints:
- chk_presupuesto_anio_mes: anio_mes ~ '^\\d{4}-(0[1-9]|1[0-2])$'
- chk_presupuesto_mes: mes entre 1 y 12

Índices:
- idx_presupuesto_customer_id ON (customer_id) WHERE deleted_at IS NULL
- idx_presupuesto_integration_anio_mes ON (integration_id, anio_mes)
- idx_presupuesto_integration_cuenta ON (integration_id, cuenta)

Uso: comparar presupuesto vs real; filtrar por anio_mes o mes; SUM(saldo) por cuenta_contable.

──────────────────────────────────────────────────────────────────────────────
5. silver.fact_bdp — balance de prueba (movimientos y saldos contables)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
FK: id_tiempo → gold.dim_time(id_time)

Columnas:
- id                      serial4 NOT NULL — PK
- integration_id          uuid NOT NULL
- customer_id             uuid NULL — filtrar consultas por este campo
- codigo_cuenta_contable  varchar(150) NULL — join con dim_accounts / presupuesto
- saldo_inicial           numeric(15, 2) NULL
- movimiento_debito       numeric(15, 2) NULL
- movimiento_credito      numeric(15, 2) NULL
- saldo_final             numeric(15, 2) NULL
- mvto                    numeric(15, 2) NULL — movimiento neto
- id_tiempo               int4 NOT NULL — FK gold.dim_time
- source_date             date NULL — fecha origen del extracto
- nombre_archivo          varchar(255) NULL
- created_at              timestamp DEFAULT CURRENT_TIMESTAMP NULL

Índices:
- idx_fact_bdp_codigo_cuenta ON (codigo_cuenta_contable)
- idx_fact_bdp_customer_id ON (customer_id)
- idx_fact_bdp_id_tiempo ON (id_tiempo)
- idx_fact_bdp_integration_nombre_archivo ON (integration_id, nombre_archivo)
- idx_fact_bdp_nombre_archivo ON (nombre_archivo)
- idx_fact_bdp_source_date ON (source_date)

Uso: balance de prueba; saldo_final total; variaciones por cuenta y periodo (id_tiempo / source_date).

──────────────────────────────────────────────────────────────────────────────
6. gold.vw_kpis_financiero — KPIs financieros pre-calculados (ratios / semáforos agregados)
──────────────────────────────────────────────────────────────────────────────
Vista en schema gold. Agrega gold.vw_fact_bdp_enriched (rubros de vw_dim_accounts).
Una fila por cliente, año y mes (anio_mes). NO requiere JOINs adicionales.

Dimensiones:
- customer_id       uuid — app.customers.id (filtro de cliente)
- anio              int — año calendario
- anio_mes          varchar(7) — 'YYYY-MM'
- mes_corto         varchar — nombre corto del mes
- nombre_cliente    varchar — nombre en app.customers (atributo; no usar para filtrar)

Balance / estructura patrimonial:
- activo_corriente, activo_no_corriente, activo_total
- pasivo_corriente, pasivo_no_corriente, pasivo_total
- patrimonio_total

Estado de resultados:
- ingresos_operacionales, ingresos_no_operacionales
- costo_ventas_total, materia_prima, mano_obra_directa, costos_indirectos
- gastos_administrativos, gastos_ventas, gastos_financieros, impuesto_renta
- utilidad_bruta, utilidad_operacional, utilidad_antes_impuestos, utilidad_neta

Ratios y métricas:
- razon_corriente, capital_trabajo_neto, apalancamiento
- pct_endeudamiento_total, pct_endeudamiento_corto_plazo, pct_autonomia_financiera
- pct_margen_bruto, pct_margen_operacional, pct_margen_neto
- pct_roe, pct_roa
- pct_gastos_admin_sobre_ingresos, pct_gastos_ventas_sobre_ingresos, pct_costo_ventas_sobre_ingresos

Semáforos (VERDE | AMARILLO | ROJO):
- semaforo_liquidez, semaforo_endeudamiento, semaforo_margen_bruto, semaforo_utilidad_neta

Filtro de cliente:
  SIEMPRE filtra por customer_id = '<uuid>' (el servidor sustituye el placeholder).

Filtro temporal — ejemplos (periodo + customer_id):
- Mes:        WHERE anio_mes = '2025-03'
- Año:        WHERE anio = 2025
- Rango mes:  WHERE anio_mes BETWEEN '2025-01' AND '2025-06'
- Último mes: ORDER BY anio DESC, anio_mes DESC LIMIT 1
  (o subconsulta MAX(anio_mes) sin filtrar por nombre de cliente)

Uso típico: margen bruto, utilidad neta, ROE, ROA, liquidez, endeudamiento, semáforos,
comparación de periodos, evolución de KPIs. NO usar fact_bdp si esta vista responde la pregunta.

──────────────────────────────────────────────────────────────────────────────
6b. gold.vw_ventas_por_producto_mes — ventas por producto (PREFERIR para mix/top)
──────────────────────────────────────────────────────────────────────────────
Grain: customer_id, anio_mes, product_name, product_code
Métricas: total_ventas, cantidad, num_facturas
Filtrar: customer_id = uuid AND anio_mes = 'YYYY-MM'
NO usar fact_venta si esta vista responde la pregunta.

──────────────────────────────────────────────────────────────────────────────
6c. gold.vw_presupuesto_vs_real_mes — presupuesto vs real por cuenta
──────────────────────────────────────────────────────────────────────────────
Grain: customer_id, anio_mes, cuenta_contable
Columnas: presupuesto, real_saldo, variacion, pct_cumplimiento
Rubros (vw_dim_accounts): nombre_rubro_grupo, nombre_rubro_clase, uso, nodo_s1, nodo_s2, sub_nodo_s3, cod_nodo
Real vía vw_fact_bdp_enriched; presupuesto enriquecido con vw_dim_accounts en filas solo presupuesto.
Filtrar: customer_id + anio_mes.

──────────────────────────────────────────────────────────────────────────────
6d. gold.vw_semaforos_cliente — semáforos del último mes KPI
──────────────────────────────────────────────────────────────────────────────
Una fila por customer_id (periodo más reciente en vw_kpis_financiero).
Columnas: semaforo_liquidez, semaforo_endeudamiento, semaforo_margen_bruto, semaforo_utilidad_neta, ratios.

──────────────────────────────────────────────────────────────────────────────
6e. gold.vw_ultimo_periodo_cliente — último periodo con datos
──────────────────────────────────────────────────────────────────────────────
Columnas: customer_id, fuente (kpis|ventas|bdp|presupuesto), ultimo_anio_mes, ultima_fecha
Filtrar por customer_id. Opcional fuente = 'kpis' etc.

──────────────────────────────────────────────────────────────────────────────
7. silver.test_dim_accounts — plan de cuentas (PRUEBAS, sin customer_id)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
UNIQUE: (integration_id, id_auxiliar)

Misma estructura jerárquica que dim_accounts (id_auxiliar … nombre_clase, load_ts, source_table)
pero keyed por integration_id; NO tiene customer_id.

──────────────────────────────────────────────────────────────────────────────
8. silver.test_fact_bdp — balance de prueba (PRUEBAS)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
FK: id_tiempo → gold.test_dim_time(id_time)

Columnas: integration_id, codigo_cuenta_contable, saldo_inicial, movimiento_debito,
movimiento_credito, saldo_final, mvto, id_tiempo, source_date (NOT NULL), nombre_archivo, created_at.
Sin customer_id.

──────────────────────────────────────────────────────────────────────────────
RELACIONES ÚTILES PARA JOINS
──────────────────────────────────────────────────────────────────────────────
- vw_fact_bdp_enriched = fact_bdp + vw_dim_accounts + dim_time (vista base contable con montos)
- fact_bdp.codigo_cuenta_contable ↔ vw_dim_accounts.id_auxiliar (mismo customer_id)
- vw_kpis_financiero agrega vw_fact_bdp_enriched por nombre_rubro_grupo / nombre_rubro_clase
- presupuesto_proyeccion.cuenta / cuenta_contable ↔ vw_dim_accounts.id_auxiliar (mismo customer_id + periodo)
- fact_venta.customer_id = vw_dim_accounts.customer_id = fact_bdp.customer_id = presupuesto_proyeccion.customer_id
