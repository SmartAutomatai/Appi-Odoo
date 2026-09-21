# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python/FastAPI middleware between an external system and Odoo ERP. It started as a **generic JSON-RPC proxy** (`POST /odoo` — run any method on any model) and now also provides a **stateful accounting sync layer**: idempotent creation + posting of invoices and payments, reconciliation, tax-total validation, and background processing with retries and logical rollback.

Code comments and log messages are in Spanish; keep that convention. Follow PEP8.

## Commands

```bash
# Run dev server (auto-reload)
uvicorn api:app --reload            # http://localhost:8000, Swagger at /docs

# Tests
pytest tests/ -v
pytest tests/test_sincronizador.py -v                          # one file
pytest tests/test_api.py::TestAutenticacion -v                 # one class
pytest tests/test_api.py::TestAutenticacion::test_acepta_api_key_correcta -v   # one test

# Docker (API only)
docker build -t api-odoo .
docker run --env-file .env -p 8000:8000 api-odoo

# Full stack with queue (web + Celery worker + beat + Redis)
docker compose up --build
# Run the Celery worker directly (needs CELERY_BROKER_URL set):
celery -A core.celery_app.celery_app worker --loglevel=info
# Beat is the CLOCK: without it the worker runs but nothing is scheduled, so
# the automatic flow (maestros -> ingesta -> poller) never starts on its own.
# Exactly ONE instance — two clocks would enqueue every task twice.
celery -A core.celery_app.celery_app beat --loglevel=info
```

**Environment note:** the checked-in `venv/` has a Unix layout (`venv/bin/`) and is **not** runnable on native Windows. Tests here run against a global Python 3.14 with the deps installed. `.\venv\Scripts\Activate` will not work until the venv is recreated on Windows.

## Architecture

The service is layered. The two original files are the **transport layer**; everything stateful lives under `core/` and `routers/`.

### Transport (unchanged foundation)
- **[odoo_universal.py](odoo_universal.py)** — `OdooUniversalAPI`, a thin JSON-RPC client. Constructor logs in immediately (raises `OdooConnectionError`); `execute(model, method, *args, **kwargs)` calls `execute_kw`. Typed exceptions: `OdooConnectionError` (network/auth) vs `OdooExecutionError` (Odoo returned an error). Multi-tenant via a module-level `_tenants` dict — `register_tenant()` / `get_tenant()`.
- **[api.py](api.py)** — the FastAPI app. On import: builds the `"default"` tenant from `ODOO_*` env vars (a failed connection is logged, does **not** crash startup — `/health` reports `degradado`), calls `init_db()`, and mounts the business routers. Holds `/odoo` (generic proxy) and `/health`.

### Stateful accounting layer (`core/`)
The invoice/payment flow chains three pieces: **state store** (idempotency) → **mapper** (translate) → **execute** (create + post).

- **[core/state_store.py](core/state_store.py)** + **[core/models_db.py](core/models_db.py)** — the **control database** (SQLAlchemy, separate from Odoo). Two tables: `sync_map` (unique `(entidad, id_origen)` ↔ `id_odoo` + `estado` + `hash_payload`) and `sync_log` (append-only audit). States: `PENDIENTE → PROCESANDO → PROCESADO | ERROR`. `DATABASE_URL` defaults to local SQLite; use PostgreSQL in prod. This is what makes everything idempotent.
- **[core/traduccion_errores.py](core/traduccion_errores.py)** — `traducir()` pasa los errores de Odoo al castellano antes de guardarlos. Se aplica al **escribir** (`marcar_estado`, `log` solo en filas ERROR, y `poller_source.marcar_resultado`), no al mostrar: así el texto en castellano queda también en la base de control y lo ve igual quien consulte por SQL, por `/estado` o por el panel. **Un mensaje no reconocido se devuelve intacto** — es preferible una frase en inglés a una traducción aproximada que despiste, y así un cambio de texto entre versiones de Odoo nunca rompe nada. El original se conserva entre `[Odoo: …]` para poder buscarlo o reportarlo a soporte. Algunas entradas **amplían** el mensaje en vez de traducirlo literalmente: `Missing required account on accountable line` no menciona al contacto, que es justo donde está el fallo. **El orden de la tabla importa** — el patrón específico (`'USD List Price' …`) va antes que el genérico que lo contiene, y hay un test que lo fija.
- **[core/mapper.py](core/mapper.py)** + **[core/mappings.yaml](core/mappings.yaml)** — declarative mapping from a source record to Odoo values. Applies `defaults`, renames fields, and resolves FKs (`resolver`) by looking up the real Odoo id from a business value (e.g. NIF→`res.partner.vat`). FK resolution doubles as master-data validation. Add/change an entity by editing the **YAML**, not code. Raises `MapeoError`.
- **[core/sincronizador.py](core/sincronizador.py)** — the orchestrator. `sincronizar_entidad(entidad, registro, odoo)`: idempotency check → `PROCESANDO` → map → `create` (id saved immediately) → `action_post` → optional total validation → `PROCESADO`. Every step logs; any failure sets `ERROR` and raises `SincronizacionError`. **Key detail:** if `create` succeeds but `action_post` fails, the `id_odoo` is kept and the error says "created but not posted" — so rollback knows what to compensate.
- **[core/facturacion.py](core/facturacion.py)** / **[core/pagos.py](core/pagos.py)** — thin business wrappers (`crear_factura` / `crear_pago`) over the orchestrator for `account.move` / `account.payment`.
- **[core/conciliacion.py](core/conciliacion.py)** — `conciliar(factura_id_odoo, pago_id_odoo, odoo, ...)`. Reconciles by finding the receivable/payable `account.move.line`s of the invoice and the payment's move and calling `reconcile()`. Idempotent via entity `"conciliacion"` keyed `"<factura>:<pago>"`. Raises `ConciliacionError`. **Note:** it checks `estado == PROCESADO` directly (not `ya_procesado()`) because reconciliation has no single `id_odoo`.

**Cobro y conciliación verificados de punta a punta (2026-09-04, Odoo 17)** — factura `VEN/2026/00001` cobrada con `PBCO/2026/00001`: 46.632,00 Bs, `amount_residual` a 0 y `payment_state=in_payment`. Reejecutar no duplica: el pago vuelve `idempotente=True`. Tres cosas hicieron falta, y ninguna es evidente desde el error de Odoo:
- **`partner_type: customer` en el `pago` de `mappings.yaml`** — es obligatorio en Odoo 17 y de él depende contra qué cuenta va la contrapartida. Sin él, `create()` responde `Missing required account on accountable line`.
- **Cuentas por cobrar/pagar por defecto de la compañía** (`ir.property` sin `res_id`, cuentas 1201/2101). Las instala el plan contable oficial; el plan mínimo de pruebas no. Un contacto sin cuenta heredada da **el mismo mensaje** que el punto anterior, sin mencionar al contacto — de ahí que despiste.
- **Cuentas transitorias de cobros/pagos pendientes** (1102/1103) asignadas al `account.payment.method.line` de banco y caja. Odoo no asienta un cobro contra el banco directamente: lo deja en una cuenta puente hasta que el extracto lo confirma. Vacías, el error es `No puede crear un nuevo pago sin una cuenta de pagos/recibos pendientes`.

Los tres los deja listos `scripts/preparar_pruebas_ve.py`. `payment_state` queda en `in_payment` (no `paid`) a propósito: el cobro está en la cuenta puente hasta que se concilie el extracto bancario.
- **[core/impuestos.py](core/impuestos.py)** — `verificar_total()`. Compares Odoo's `amount_total` with the source total using `Decimal` (via `str`, to avoid float noise) and a 1-cent tolerance. Raises `DescuadreError`; the invoice stays `ERROR` (not marked done). Wired into the orchestrator only when the entity's YAML has `validar_total: <campo>`.
- **[core/inventario.py](core/inventario.py)** — stock adjustments. `ajustar_stock(registro, odoo)` resolves the product (`default_code`) and internal location, finds/creates the `stock.quant`, writes `inventory_quantity`, then calls **`action_apply_inventory()`** — the official path, so Odoo generates the traceable `stock.move` instead of silently overwriting a quantity. Modes: `fijar` (absolute count, default) / `incrementar` / `decrementar`; a decrement below zero is refused. Idempotent by the source `ajuste_id` — **essential**, since replaying an `incrementar` would silently corrupt inventory. `consultar_stock()` is the read-only counterpart. Raises `InventarioError`. Note this module does **not** use `mappings.yaml` (its input is a fixed, small schema, not a record translation).

### Queue, retries, rollback (`core/`)
- **[core/celery_app.py](core/celery_app.py)** — Celery config. **Eager mode is automatic when `CELERY_BROKER_URL` is empty** (tasks run inline, synchronously) — that's how tests and dev run without Redis.
- **[core/tasks.py](core/tasks.py)** — background tasks. `sincronizar_factura_task` / `sincronizar_pago_task` retry with exponential backoff **only** on `OdooConnectionError` (not on data errors). `procesar_venta_task` runs the full invoice→payment→reconcile flow and triggers **logical rollback** ([core/rollback.py](core/rollback.py) `cancelar_factura`: `button_draft` + `button_cancel`) if payment/reconciliation fails after the invoice was created. Tasks resolve the tenant by name via `get_tenant` (the connector isn't serializable).

### Poller — modo pull desde la DB del cliente (`core/poller.py` + `core/poller_source.py`)
Alternativa al modo push (cliente hace `POST /facturas`…). Aquí **el cliente no llama al middleware**: deja registros en **su propia base de datos** (p.ej. un RDS PostgreSQL en su cuenta AWS, por aislamiento de seguridad — es dueño de sus datos) y el middleware la **sondea**. Flujo: `Cliente escribe → poller lee (Celery Beat) → sincronizar_entidad → Odoo → escribe resultado de vuelta`.
- **[core/poller_source.py](core/poller_source.py)** — acceso a la DB **de origen** del cliente, con engine **separado** del de control (`SOURCE_DATABASE_URL`, perezoso: sin esa var el modo pull está apagado). Tabla genérica `cola_sincronizacion` (`entidad`, `id_origen`, `payload` JSON en formato de `mappings.yaml`, `estado` PENDIENTE|PROCESADO|ERROR) más **`payload_original`**, el documento tal cual lo devolvió el origen. **Por qué el original:** `payload` guarda solo lo que el mapper necesita, así que un campo que Smartier añada mañana no dejaría rastro; el original permite auditar y reconstruir sin depender de que la API siga devolviendo la nota. Se consulta en `GET /panel/api/cola/{id}` — el listado solo dice `tiene_original`, porque meter el documento entero en 100 filas haría la respuesta inmanejable. La fila guarda además el resultado en Odoo: **`id_odoo`** (llave técnica) y **`numero_odoo`** (el número fiscal, `VEN/2026/00001`), escritos de vuelta por `marcar_resultado()` — así el sistema de origen cruza su nota con la factura sin entrar a Odoo ni consultar la base de control. Un fallo *después* del create conserva el `id_odoo` (se recupera del `sync_map`), para no tener que buscar a mano qué quedó a medias. **El reenvío idempotente devuelve el número desde la bitácora, no con un `read()`**: la idempotencia promete no tocar Odoo, y hay un test que lo fija. `init_source_db()` **añade las columnas nuevas** a una cola ya existente (`create_all` crea tablas, nunca altera una en marcha), y hay un test que falla sin esa migración. `tomar_lote()` lee PENDIENTE con `FOR UPDATE SKIP LOCKED` (degradado a SELECT normal en SQLite); `marcar_resultado()` escribe el estado de vuelta — el **único** permiso de escritura que el middleware necesita en la DB del cliente.
- **[core/poller.py](core/poller.py)** — `procesar_lote(tenant, limite)`: toma un lote y por cada fila llama a **`sincronizar_entidad`** (la misma maquinaria del modo push, así el `sync_map` sigue siendo la fuente de verdad de idempotencia — reprocesar no duplica en Odoo). **Aislamiento de errores:** un fallo de DATOS (`SincronizacionError`) deja esa fila `ERROR` y sigue con el resto; un fallo de CONEXIÓN (`OdooConnectionError`) aborta el lote y se propaga → Celery reintenta la pasada completa. **Doble fuente de verdad resuelta:** la cola dice *qué* enviar, `sync_map` dice *qué ya* se envió.
- **Programación:** `celery_app.py` registra un `beat_schedule` (`poller-cola-cliente`) **solo si** hay `SOURCE_DATABASE_URL`; intervalo `POLLER_INTERVALO_SEG` (30 s por defecto), `POLLER_TENANT`, `POLLER_LIMITE`. Requiere arrancar Beat: `celery -A core.celery_app.celery_app beat --loglevel=info`.
- **Disparo manual:** [routers/poller.py](routers/poller.py) expone `POST /poller/ejecutar` (protegido, síncrono) para forzar una pasada sin esperar al tick de Beat. → 400 si no hay `SOURCE_DATABASE_URL`, 503 si Odoo está caído.
- **Sembrar la cola en pruebas:** [scripts/sembrar_cola_poller.py](scripts/sembrar_cola_poller.py) crea la tabla y encola filas de ejemplo (no toca Odoo).

### Ingesta desde Smartier (`core/smartier_client.py` + `core/ingesta_smartier.py`)
Mitad **izquierda** del modo pull: `Smartier API → [ingesta] → cola_sincronizacion → [poller] → Odoo`. Un solo servicio hace las dos mitades; no hacen falta dos middlewares.
- **[core/smartier_client.py](core/smartier_client.py)** — cliente HTTP de solo lectura. Contrato verificado contra la API real: sobre `{"Data": [...], "Count": N}` (**no** `Items`/`TotalCount`), `Page`/`PageSize` (máx. **200**), `Sort` JSON:API con campos ordenables **`Id, Fecha, Estado`** (otro valor → 400). Respeta el límite de **5 req/s por key** espaciando llamadas (`SMARTIER_REQ_POR_SEG`, 4 por defecto) y reintenta ante 429 (con `Retry-After`) y 5xx; un 401/403 **no** se reintenta. Registra el `X-Correlation-Id` para reportar incidencias a soporte.
- **[core/ingesta_smartier.py](core/ingesta_smartier.py)** — traduce `NotaEntregaExternalDto` al registro de `mappings.yaml` (entidad `factura`) y lo encola. Solo encola las notas cuyo `Estado` esté en `SMARTIER_ESTADOS_FACTURAR` (por defecto `Facturada`, el disparador natural). Como la API es de **solo lectura**, el control de avance vive en el middleware: una **marca de agua** en el `sync_map` (entidad `_ingesta_smartier`, guardada en `hash_payload` — **no** en `error`, que `marcar_estado` borra salvo en estado ERROR) más un antiduplicado por `id_origen` antes de insertar. Aun colándose un repetido, `sincronizar_entidad` consulta el `sync_map` y no duplica en Odoo.
- **Traducción:** `Nota.Id → factura_id ("NE-<id>")` · `Documento.Contenido → cliente_nif` · `PrecioUnitario.Moneda` (`Nacional→VES`, `Extranjera→USD`) → `moneda_iso` · `Cantidad`/`Monto`/`Descuento` → línea. El IVA, la cuenta contable y la tasa de cambio **no se envían**: los resuelve Odoo desde la ficha del producto/cliente.
- **Endpoints:** `POST /smartier/ingerir` (pasada manual) y `GET /smartier/diagnostico` (cuenta recursos y avisa de clientes sin RIF y de las alícuotas encontradas). Beat programa `ingesta-smartier` solo si hay `SMARTIER_BASE_URL` + `SMARTIER_API_KEY` (`SMARTIER_INTERVALO_SEG`, 300 s).

**Estado de los datos de Smartier (verificado 2026-08-21):** 3 clientes (los **3 sin RIF**: `Documento.Contenido` a `null`), 48 productos (todos IVA **16 %**, ninguno exento), y **0 notas de entrega / 0 órdenes**. Sin RIF, Odoo con localización venezolana rechaza la factura: la nota se encola igualmente y el poller la deja en ERROR con el motivo, para que sea visible en el panel en vez de descartarse en silencio.

### Clientes en automático (`core/maestros_smartier.py`)
La lógica de clientes vive en **`core/maestros_smartier.py`**, no en el script: así la ejecutan igual Celery Beat (`sincronizar_maestros_task`, cada `SMARTIER_MAESTROS_INTERVALO_SEG`, 900 s) y el CLI, sin dos copias de las reglas de deduplicación que puedan separarse. `POST /smartier/maestros` fuerza una pasada. **El orden importa:** una nota de entrega necesita que su cliente exista en Odoo para resolver el `partner_id`; por eso los maestros van antes que la ingesta, y aun así una nota que se adelante queda en ERROR (visible), no perdida.

**Solo se escribe lo que cambió** (`cambios_para`): antes se reescribían todos los campos de todos los clientes en cada pasada — con cientos de contactos son cientos de escrituras inútiles y un historial de Odoo donde ya no se distingue un cambio real. Verificado contra la instancia: la segunda pasada seguida hace **cero escrituras**. Odoo devuelve `False` para los char vacíos, así que se normaliza contra `""` para no detectar un cambio falso en cada pasada.

`CAMPOS_ACTUALIZABLES` deja fuera **`comment`** a propósito: es texto libre donde contabilidad escribe sus notas, y una pasada de sincronización no debe apropiárselo — solo se rellena al crear, como pista inicial. Los campos de retención tampoco se actualizan nunca (neutros al crear, intocables después). Un fallo de datos de un cliente se anota y la pasada sigue; un fallo de red aborta y Celery reintenta.

### Documento fiscal: solo tipos 29 y 30 (respuesta de Smartier, 2026-09-09)
`Documento.Tipo` **no se puede ignorar**. El catálogo (que Smartier no expone por API) declara el **6** como *"Documento de identidad" genérico*, con validación permisiva — acepta letras, dígitos y espacios. **Su contenido no es un RIF.** Antes se enviaba `Documento.Contenido` a Odoo como `vat` sin mirar el tipo, así que cualquier cosa escrita en un documento genérico acababa en el campo fiscal de la factura. `rif_de()` acepta ahora solo **29** (CI Venezolana) y **30** (RIF Venezolano); un genérico con contenido se trata como ausente y el contacto queda `PENDIENTE`, que es la situación real. `ingesta_smartier._extraer_nif` delega en esa misma función: con dos criterios distintos, la factura buscaría un partner por un `vat` que el sincronizador de clientes nunca escribió.

**El RIF exige cliente de tipo Empresa.** Contacto y Empresa son dos caminos de alta distintos en Smartier, y el tipo restringe qué documentos admite. Los clientes de prueba están como `Contacto`, así que al operador **no le aparece la opción de RIF**. Corregirlo no es editar un campo: es dar de alta al cliente otra vez por el otro camino — con **`Id` nuevo**, y por tanto `SMARTIER-<id>` nuevo, así que nuestra deduplicación crearía un contacto nuevo en Odoo en vez de reutilizar el existente.

### El filtro de fecha de la ingesta no existe
`FILTRO_FECHA` está **vacío a propósito**. Se enviaba `FechaDesde`, un nombre supuesto; comprobado contra la API real: **no existe**. Smartier confirmó que un filtro no reconocido **se descarta en silencio** (200 con el listado completo), así que no daba error pero tampoco acotaba: cada pasada leía el histórico entero. Verificado con su propia prueba — comparar el `Count` con filtro contra el `Count` sin filtro:

| consulta | Count | |
|---|---|---|
| sin filtro | 4 | |
| `FechaDesde=2099-01-01` | 4 | no filtra |
| `FiltroInventado=xyz` | 4 | control: no filtra |
| `NombreContains=ZZZ` | 0 | control: **sí** filtra |

La marca de agua se sigue guardando y el antiduplicado evita reprocesar, pero **el coste de la lectura crece con el histórico**. Se activa poniendo el nombre real en `SMARTIER_FILTRO_FECHA`, sin tocar código. `Sort` inválido sí devuelve 400 con la lista de campos válidos — eso es deliberado por su parte.

### Sincronización de datos maestros (`scripts/sincronizar_*_smartier.py`)
Clientes y productos se cargan en Odoo con dos scripts que, como el resto del flujo, **registran en el `sync_map`**: por eso aparecen en el panel (entidades `cliente` y `producto`) en lugar de ser escrituras invisibles que solo dejan rastro dentro de Odoo. Ambos simulan por defecto y exigen `--aplicar` para escribir.
- **Deduplicación:** por RIF cuando existe (`res.partner.vat`), y si no —el caso hoy— por el `Id` de Smartier guardado como `SMARTIER-<id>` en `res.partner.ref` / `product.product.default_code`. **Nunca por nombre**, que varía en formato y mayúsculas. Un contacto que ya exista (p. ej. como proveedor) se reutiliza añadiéndole el rol de cliente.
- **Estados:** un cliente **sin RIF se marca `PENDIENTE`, no `PROCESADO`** — está en Odoo pero todavía no se le puede facturar, y esa diferencia tiene que verse en el panel.
- **Productos como `service`** (decisión "Opción A"): Odoo no lleva inventario paralelo, Smartier es la única fuente de verdad de stock. El script crea el `account.tax` que falte (el 16 % de Smartier no viene en una instancia nueva) y lo asigna según `PorcentajeIVA`/`Exento`.

- **Campos fiscales:** neutro al crear, intocable al actualizar. `l10n_ve_full` declara `wh_iva_agent` e `islr_withholding_agent` con `default=True`, así que un contacto nuevo quedaría marcado como agente de retención sin que nadie lo decidiera; al crear se fijan a `False` de forma explícita. Al **actualizar no se envía ningún campo fiscal**: son competencia de contabilidad y una pasada del sincronizador no debe revertir lo configurado a mano. `vat` solo se manda cuando Smartier trae valor, para no borrar un RIF cargado en Odoo. La localización se detecta por la existencia del campo `wh_iva_agent` (no por el nombre del módulo); sin ella no se envía nada, porque mandar campos inexistentes rompería el `create`.

### Fiscal venezolano sin la localización (`scripts/preparar_fiscal_ve.py`)
El submódulo `l10n_ve_full` **no consigue instalarse** en la instancia (el build de Odoo.sh muere con `Test: Failed`/`KILLED` sin dejar logs legibles, en producción y en staging). Para no bloquear el proyecto, este script cubre con campos **nativos** de Odoo 17 lo mínimo para facturar: activa la moneda **VES** (Odoo la trae desactivada), crea las retenciones de IVA (75 %, 100 %) e ISLR (1-5 %) como `account.tax` **negativos** —un impuesto negativo resta del total, que es lo que hace una retención— y tres **posiciones fiscales** (Ordinario / Especial 75 % / Especial 100 %) como equivalente nativo de `wh_iva_agent`.

**El detalle que importa:** la retención de IVA es un porcentaje **del IVA**, pero un `account.tax` se aplica siempre sobre la **base imponible**. Hay que convertirlo: retener el 75 % del IVA del 16 % es un **12 % de la base**. Creándolo con `-75` una factura de 1.000 Bs daba 410 en vez de 1.040 (Odoo restaba 750, el 75 % de la base, en lugar de 120). Verificado con factura real en VES: `1.000 + 160 − 120 = 1.040`. Contrapartida: el porcentaje queda atado a la alícuota del 16 %, de ahí la constante `IVA_VIGENTE` y que el nombre del impuesto lo diga.

**Lo que esto NO cubre:** comprobante de retención numerado del SENIAT, libros de compra/venta y TXT. Sirve para **operar** y que las cifras cuadren, no para cumplir del todo con la declaración. Cuando `l10n_ve_full` entre, hay que revisar que sus impuestos no dupliquen los de aquí.

**Gotcha operativo:** el panel y los scripts leen `DATABASE_URL` **al arrancar**. Si se cambia esa variable (por ejemplo al apuntar a otra instancia de Odoo), hay que **reiniciar el servidor**, o el panel seguirá mostrando la base anterior mientras los scripts escriben en la nueva. Lo mismo con `SOURCE_DATABASE_URL`: una cola nueva se crea vacía y **sin la tabla**, hay que inicializarla con `poller_source.init_source_db()`.

### Panel de observabilidad (`core/observabilidad.py` + `routers/panel.py`)
Dashboard de **solo lectura**, servido por el propio FastAPI. `core/observabilidad.py` es el reverso de `state_store`: `resumen()` (agregados por estado/entidad), `listar_sincronizaciones()`/`listar_logs()` (con filtros), `detalle_registro()` y **`cola_poller()`**, que lee la bandeja de entrada en la DB de ORIGEN del cliente (engine de `poller_source`, no el de control) y devuelve `habilitado=False` si el modo pull está apagado, en vez de fallar.
`routers/panel.py` expone `GET /panel` (dashboard HTML autocontenido, sin dependencias externas) y los datos en `GET /panel/api/{resumen,sincronizaciones,logs,detalle/...,cola}`. Tres pestañas: **Sincronizaciones**, **Bitácora** y **Cola del poller**; más un botón *Poller ahora* que dispara `POST /poller/ejecutar` y un check de auto-refresco cada 5 s.
**Seguridad:** los endpoints de datos van protegidos por `verify_api_key`; la página `/panel` es solo el shell HTML y su JS pide la API Key y la envía como `X-Api-Key` en cada `fetch`. El panel refleja la DB de control y la cola del cliente — no consulta Odoo.

### Routers (`routers/`)
`/facturas`, `/pagos`, `/conciliar`, `/stock/*` — each protected by the shared **[core/seguridad.py](core/seguridad.py)** (`verify_api_key` reads `API_KEY` from env each call; `resolver_tenant` → 400 on unknown tenant). Extracted to a neutral module so routers don't import `api.py` (avoids a circular import). `/facturas`, `/pagos` and `/stock/ajustar` take an optional `"async": true` field → enqueue and return `{encolado, task_id}` (in eager mode this still runs inline); otherwise synchronous. `api.py` also has `GET /estado/{entidad}/{id_origen}` to read a record's sync state.

### Endpoints
`/odoo` (generic, auth) · `/facturas` · `/pagos` · `/conciliar` · `/stock/ajustar` · `/stock/consultar` · `/poller/ejecutar` (pasada manual, auth) · `/smartier/ingerir` · `/smartier/diagnostico` · `/estado/{entidad}/{id_origen}` · `/panel` (HTML, no auth) · `/panel/api/*` (JSON, auth) · `/health` (no auth).

Note the whitelists (`ALLOWED_MODELS`/`ALLOWED_METHODS`) only gate the generic `/odoo` endpoint — the business routers call Odoo directly through the connector.

## Security & error codes

**Authentication is fail-closed.** With no `API_KEY` set, the service answers **503** rather than letting requests through — the previous fail-open behaviour meant an unmounted `.env` silently exposed invoice creation to anyone. Local development without a key needs the explicit `PERMITIR_SIN_API_KEY=true`. Keys are compared with `secrets.compare_digest` (constant time); a plain `!=` leaks the secret byte by byte through timing. There is **one** implementation, in [core/seguridad.py](core/seguridad.py) — `api.py` had a second, weaker copy that drifted out of sync.

**Rate limiting covers every route — but only because `SlowAPIMiddleware` is mounted.** `RATE_LIMIT_GLOBAL` (120/minute by default) applies app-wide via the shared limiter in [core/limites.py](core/limites.py); `/smartier/ingerir` and `/poller/ejecutar` take a stricter 6/minute because they trigger work against external systems and burn Smartier's 5 req/s quota. Note slowapi requires a `request: Request` parameter on any decorated endpoint.

**The gotcha that cost us:** declaring `default_limits` on the `Limiter` is *not enough* — without `app.add_middleware(SlowAPIMiddleware)` it is dead decoration, and only the endpoints carrying an explicit `@limitar(...)` are limited. Everything else (`/facturas`, `/pagos`, `/panel/api/*`, `/health`) stayed wide open and the API key was brute-forceable through them. Reading the code it looked right; it only showed up when run: with `RATE_LIMIT_GLOBAL=5/minute`, nine consecutive calls to `/health` returned nine 200s. Fixed 21/09/2026 after an external audit flagged it. `tests/test_auditoria_seguridad.py` pins it, and the test was validated by commenting the middleware out and confirming it fails.

**`/health` does not publish the whitelists to anonymous callers.** It used to return `modelos_permitidos` / `metodos_permitidos` with no authentication — precisely the map of what `/odoo` accepts, handed to whoever reaches the port. They are now included **only** when the caller sends a valid `X-Api-Key` (read by hand, since `/health` cannot depend on `verify_api_key`: a monitor must get 200 without a key, not 401). With no `API_KEY` configured the service is already open, so the detail is shown to keep local debugging usable.

**The interactive docs are closed by default.** `/docs`, `/redoc` and `/openapi.json` publish the full schema — routes, models, every field. Opt in with `EXPONER_DOCS=true`.

**The `tenant` field is checked against a whitelist.** It travels in the client's own JSON, so `resolver_tenant()` now contrasts it with `TENANTS_PERMITIDOS` (empty = only `default`) before picking a connector; a registered-but-not-allowed tenant gets 400. Today there is a single tenant and a single API key, so the risk is theoretical — that is exactly why it is worth closing now, before a second client makes it real.

**Whitelists are re-read per request** (an HTTP middleware calls `recargar_whitelists()`), so tightening `ALLOWED_MODELS` during an incident takes effect without a restart — they used to be frozen at import. `/odoo` is **read-only** by default: `create`/`write`/`unlink` there would let a key holder delete invoices and journal entries, bypassing the middleware's idempotency and audit trail. Writes go through the business endpoints, which log every operation.

**500s return a generic message.** The exception text goes to the log, never to the client — it can carry paths, table names or connection-string fragments.



Security layers, all `.env`-driven and **no-ops when their var is empty**: `API_KEY` (empty → endpoint unprotected, dev mode), `ALLOWED_MODELS` / `ALLOWED_METHODS` (comma-separated whitelists, enforced inside `OdooRequest`'s `field_validator`s → 422), rate limit fixed at `60/minute` per IP (slowapi).

Error mapping: `401` bad/missing API key · `422` whitelist rejection, `OdooExecutionError`, `SincronizacionError`, `ConciliacionError`, `DescuadreError` · `429` rate limit · `503` `OdooConnectionError` · `400` unknown tenant · `500` other.

## Adding capabilities

- **New model/method for `/odoo`:** edit `ALLOWED_MODELS` / `ALLOWED_METHODS` in `.env`.
- **New mapped entity (or change a field mapping):** edit [core/mappings.yaml](core/mappings.yaml). `id_origen` names the source-record key used for idempotency; `resolver` entries look up FKs; optional `validar_total` enables the tax-total check.
- **Add a tenant:** `register_tenant(name, OdooUniversalAPI(...))` at startup in [api.py](api.py); clients pass `"tenant": "<name>"`.

## Tests

HTTP tests ([tests/test_api.py](tests/test_api.py), [tests/test_routers.py](tests/test_routers.py)) patch `OdooUniversalAPI._login` **before importing api.py** and set dummy `ODOO_*` env vars, so no real Odoo connection is made. `core/` unit tests isolate the state store by pointing `DATABASE_URL` at a temp SQLite file (via `monkeypatch.setenv` + `importlib.reload(state_store)`) and mock the `odoo` connector.

**Gotcha:** do **not** `importlib.reload(core.tasks)` in a fixture — it re-registers the Celery tasks and corrupts global state across tests (this caused rollback tests to pass in isolation but fail in the full suite). Patch dependencies by name with `monkeypatch` instead (auto-reverted). When mocking a function a router imported by name (`from core.facturacion import crear_factura`), patch it **on the router module** (`routers.facturas.crear_factura`), not on the source module.

## scripts/ — standalone Odoo utilities (not part of the API)

CLI tools that call the running middleware over HTTP (they read `API_KEY` from env or by parsing `.env`). `excel_a_odoo.py` ↔ `generar_excel_demo.py` do bidirectional Excel⇄Odoo sync on `res.partner` (rows with an ID → `write`, without → `create`). `borrar_duplicados.py` supports `--dry-run`. These require the server running.

## Docs

Extended integration guides (agent client, Power Query/Excel M template, ETL to PostgreSQL/MySQL) live in [docs/](docs/); [README.md](README.md) links each one.
