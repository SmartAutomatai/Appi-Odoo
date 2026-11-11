# Campos para asistencia biométrica en Odoo (`hr.attendance`)

_Fecha: 2026-10-06_

Para que Odoo registre la asistencia de un trabajador, el middleware necesita de la API biométrica tres datos por fichaje: **a quién** (identificador del trabajador), **cuándo entró** y **cuándo salió**. Con eso crea un registro `hr.attendance` en Odoo, que es lo que alimenta las horas trabajadas del empleado.

---

## Resumen ejecutivo

Un fichaje en Odoo es un registro del modelo `hr.attendance`: **un empleado, una hora de entrada y una hora de salida**. Esos son los tres datos que el middleware necesita recibir de la API biométrica para dejar la asistencia reflejada en Odoo.

La parte delicada no son los campos en sí, sino **cómo la API identifica al trabajador**. Odoo guarda al empleado con un id interno propio que la API no conoce; la API envía un dato de negocio (un código de empleado, una cédula o un correo) y el middleware lo traduce al empleado correcto en Odoo. Ese dato de identificación debe existir previamente en la ficha del trabajador en Odoo, o el fichaje no se puede asociar a nadie.

Qué debe aportar la API biométrica, por cada marcaje:

1. **Identificador del trabajador** — un valor único que ya conste en la ficha del empleado en Odoo (a definir: código, cédula o correo; ver sección correspondiente).
2. **Fecha y hora de entrada** (`check_in`) — momento en que el trabajador fichó la entrada.
3. **Fecha y hora de salida** (`check_out`) — momento de la salida. Puede llegar después, en un segundo mensaje, cerrando el fichaje que quedó abierto.
4. **Un identificador único del marcaje** en el sistema de origen — para que, si el mismo fichaje se reenvía, no se duplique en Odoo.
5. **El identificador del dispositivo biométrico / sede** donde se marcó — hay varias sedes, así que cada fichaje debe decir de qué equipo vino, para poder reportar la asistencia por sede.

Con esos datos el middleware crea el `hr.attendance`, y Odoo calcula automáticamente las horas trabajadas del empleado. El resto de este informe detalla los campos de Odoo, las opciones de identificación y el formato exacto de la API de entrada.

---

## El flujo de extremo a extremo

El proveedor expone (o consume) una API; el middleware recibe cada marcaje, traduce el identificador del trabajador al empleado real de Odoo y crea el `hr.attendance`. El cliente no toca Odoo directamente: solo manda el marcaje.

```
┌──────────────────┐   código / cédula   ┌──────────────────┐   employee_id real   ┌──────────────────┐
│  API biométrica  │ ──────────────────▶ │    Middleware    │ ───────────────────▶ │       Odoo       │
│  POST del marcaje│                     │ resuelve empleado│                      │ crea hr.attendance│
└──────────────────┘                     └──────────────────┘                      └──────────────────┘
```

_El middleware traduce el identificador de negocio al id interno del empleado en Odoo y evita duplicar el fichaje (idempotencia por id de marcaje)._

El paso central — resolver el empleado — es la razón por la que el identificador que envíe la API debe existir ya en la ficha del trabajador en Odoo. Si no existe, el middleware rechaza el marcaje con un error claro en vez de crear un fichaje huérfano.

---

## Modelo destino en Odoo: `hr.attendance`

La asistencia en Odoo vive en el modelo `hr.attendance`. Cada registro es **un tramo de presencia**: una entrada y, cuando se produce, una salida. Odoo deriva solo las horas trabajadas (`worked_hours`); no hay que enviárselas.

| Campo Odoo | Tipo | ¿Obligatorio? | Qué es | Lo aporta la API |
| --- | --- | --- | --- | --- |
| `employee_id` | FK a `hr.employee` | Sí | El trabajador al que pertenece el fichaje | Sí, pero como dato de negocio (código/cédula/correo); el middleware lo traduce al id |
| `check_in` | Datetime (UTC) | Sí | Fecha y hora de entrada | Sí |
| `check_out` | Datetime (UTC) | No | Fecha y hora de salida; vacío = fichaje abierto (dentro) | Sí, puede llegar después |
| `worked_hours` | Float | Calculado | Horas del tramo (`check_out − check_in`) | No — lo calcula Odoo |
| sede / dispositivo | — (sin campo estándar) | No | De qué biométrico/sede vino el fichaje | Sí — ver nota de abajo |

Notas que condicionan la integración:

- **`hr.attendance` no trae un campo estándar para el dispositivo/sede.** Odoo solo guarda `in_mode` / `out_mode` (el método: manual, systray, kiosco…), no de qué equipo físico vino el marcaje. Para registrar la sede hay dos vías, a decidir:
    - **Campo personalizado** en `hr.attendance` (p.ej. `x_biometrico_id` o `x_sede`), un texto o una relación a un catálogo de sedes. Es lo más directo si solo se quiere dejar constancia de la sede en cada fichaje.
    - **Reutilizar la ubicación de trabajo** (`hr.work_location`): el middleware traduce el `biometrico_id` a la sede correspondiente y la deja en el fichaje o en la ficha del empleado. Encaja si las sedes ya están modeladas en Odoo.
  Recomendación: un campo personalizado `x_biometrico_id` (texto) en `hr.attendance`, simple y suficiente para reportar por sede; se puede evolucionar a una relación con un catálogo si más adelante hace falta.
- **Las fechas van en UTC.** `hr.attendance` almacena los datetime en UTC y Odoo los muestra en la zona horaria del usuario. Si la API envía hora local sin indicar zona, los fichajes se guardarán corridos. Lo más seguro: la API envía UTC explícito (sufijo `Z` en ISO 8601) y el middleware lo pasa tal cual.
- **`check_in` sin `check_out` es válido:** representa a alguien que fichó entrada y aún no ha salido. La salida se completa después con un segundo mensaje (ver sección de entrada/salida).
- Odoo valida que `check_out` sea posterior a `check_in` y que un mismo empleado no tenga dos tramos solapados; un marcaje que viole esto será rechazado por Odoo y el middleware lo reportará como error.

---

## Cómo se identifica al trabajador (a definir)

Este es el punto que hay que cerrar con el proveedor. La API no puede mandar el id interno de Odoo (no lo conoce y cambia entre instancias), así que envía un **dato de negocio único** que ya conste en la ficha del empleado, y el middleware lo resuelve contra `hr.employee` — igual que hoy resuelve el cliente de una factura por su NIF.

Sea cual sea la opción, el requisito es el mismo: **ese valor debe estar cargado en la ficha de cada empleado en Odoo antes del primer fichaje**, y debe ser único.

| Opción | Campo en `hr.employee` | A favor | En contra |
| --- | --- | --- | --- |
| Código / PIN de empleado | `barcode` o `pin` | Es lo que un reloj biométrico suele emitir de forma nativa; corto y estable | Hay que darlo de alta en Odoo por empleado |
| Cédula / identificación | `identification_id` | Dato que la empresa ya tiene y no cambia | El dispositivo debe conocerlo; no siempre lo maneja el reloj |
| Correo corporativo | `work_email` | Suele estar ya cargado en Odoo | Más largo y propenso a erratas; puede faltar en operarios |

**Recomendación:** usar `barcode` (código de empleado). Es el identificador que los lectores biométricos y de tarjeta emiten de forma natural, es corto y Odoo ya lo trae como campo estándar y único en `hr.employee`. Si el proveedor confirma que su API solo expone la cédula, se usa `identification_id` sin cambios en la arquitectura — solo cambia el campo de búsqueda en la configuración del middleware.

**Pregunta abierta para el proveedor:** ¿qué valor identifica unívocamente al trabajador en los datos que envía su API, y ese mismo valor está (o puede cargarse) en la ficha del empleado en Odoo?

---

## Contrato de la API de entrada (anexo técnico)

El proveedor envía cada marcaje al middleware como un JSON. Siguiendo el patrón del resto de entidades del middleware, el cuerpo lleva los datos de negocio y el middleware se encarga de la traducción a Odoo. Campos del registro de origen:

| Campo (origen) | Tipo | Obligatorio | Descripción | Destino en Odoo |
| --- | --- | --- | --- | --- |
| `marcaje_id` | string | Sí | Identificador único del marcaje en el sistema de origen (idempotencia) | — (clave del `sync_map`) |
| `biometrico_id` | string | Sí | Identificador del dispositivo biométrico / sede donde se marcó | Campo personalizado (p.ej. `x_biometrico_id`) o resuelto a sede |
| `empleado_codigo` | string | Sí | Identificador del trabajador (p.ej. `barcode`); ver sección de identificación | `employee_id` (resuelto) |
| `check_in` | string ISO 8601 UTC | Sí | Fecha y hora de entrada, p.ej. `2026-10-05T13:02:00Z` | `check_in` |
| `check_out` | string ISO 8601 UTC | No | Fecha y hora de salida; puede omitirse y enviarse después | `check_out` |

Ejemplo de fichaje completo (entrada y salida en un solo mensaje):

```json
{
  "marcaje_id": "BIO-2026-10-05-000421",
  "biometrico_id": "SEDE-CARACAS-01",
  "empleado_codigo": "EMP0042",
  "check_in": "2026-10-05T13:02:00Z",
  "check_out": "2026-10-05T21:10:00Z"
}
```

Ejemplo de solo entrada (la salida llegará en otro mensaje):

```json
{
  "marcaje_id": "BIO-2026-10-05-000421",
  "biometrico_id": "SEDE-CARACAS-01",
  "empleado_codigo": "EMP0042",
  "check_in": "2026-10-05T13:02:00Z"
}
```

Reglas de formato que conviene fijar con el proveedor:

- **Fechas en ISO 8601 con zona explícita** (preferiblemente UTC con `Z`). Evita el desfase horario al guardar en Odoo.
- **`marcaje_id` estable:** el mismo marcaje reenviado debe traer el mismo `marcaje_id`. Es lo que impide duplicados.
- **`empleado_codigo` exactamente como consta en Odoo** (mayúsculas/ceros a la izquierda incluidos): la búsqueda es por igualdad.
- **`biometrico_id` con un valor estable por dispositivo/sede.** Conviene acordar un catálogo cerrado de identificadores de sede (p.ej. `SEDE-CARACAS-01`) para poder agrupar la asistencia por sede sin ambigüedad.

---

## Idempotencia y manejo de entrada/salida

**No duplicar fichajes.** El middleware ya resuelve esto para facturas, pagos y ajustes de stock con el mismo mecanismo: reserva atómica por `(entidad, id_origen)` en su base de control antes de tocar Odoo. Para asistencia, `id_origen` será el `marcaje_id`. Si el mismo marcaje llega dos veces (reintento de la API, reenvío tras un corte), el segundo se detecta como ya procesado y no crea un segundo `hr.attendance`. Esto exige que el proveedor mande un `marcaje_id` estable por marcaje.

**Entrada y salida.** Hay dos formas de operar, y conviene decidir cuál usa la API:

1. **Fichaje completo en un mensaje** — la API espera a tener entrada y salida y envía ambas juntas (`check_in` + `check_out`). Es el caso más simple: un marcaje = un `hr.attendance` cerrado.
2. **Entrada y salida en dos mensajes** — la API manda primero la entrada (solo `check_in`) y más tarde la salida. En este caso el segundo mensaje debe **completar** el mismo registro, no crear uno nuevo. Dos sub-opciones:
    - Mandar el segundo mensaje con el **mismo `marcaje_id`** y el `check_out`: el middleware localiza el fichaje abierto y le escribe la salida.
    - Mandar un marcaje de tipo "salida" que el middleware empareja con el último `hr.attendance` abierto de ese empleado.

La opción 1 es la más robusta y la recomendada si la API puede entregar el tramo cerrado. La opción 2 (dos mensajes con el mismo `marcaje_id`) es la natural si el dispositivo marca entrada y salida por separado. Esto es una **decisión de diseño a confirmar con el proveedor**, porque cambia la lógica de emparejamiento del middleware.

**Pregunta abierta para el proveedor:** ¿la API entrega el tramo ya cerrado (entrada + salida), o envía entrada y salida como eventos separados? Si son separados, ¿comparten identificador?

---

## Qué hace falta para habilitarlo

Del lado de Odoo (datos maestros):

1. **La app de Asistencias (Attendances) activa** en Odoo, que es la que provee el modelo `hr.attendance`.
2. **Cada trabajador dado de alta como `hr.employee`** con el identificador elegido (p.ej. `barcode`) cargado y único. Sin esto, el middleware no puede resolver el empleado.

Del lado del middleware (desarrollo):

1. **Dar de alta la entidad `asistencia`** apuntando a `hr.attendance`, resolviendo `employee_id` por el campo elegido — con la salvedad de que `hr.attendance` no encaja 1:1 en el mapeo declarativo genérico si hay que emparejar entrada/salida; en ese caso se trata como módulo de esquema fijo (como inventario o asientos), no vía `mappings.yaml`.
2. **Endpoint `POST /asistencias`** (protegido por API key, como el resto) que recibe el marcaje y crea/completa el fichaje, con la idempotencia por `marcaje_id`.
3. **Paso de la fecha a UTC** tal como la envíe la API, y validación mínima (que `check_in` exista y sea una fecha válida).

Decisiones a cerrar antes de construir (resumidas de las secciones anteriores):

- Campo de identificación del trabajador (recomendado: `barcode`).
- Dónde se guarda la sede/dispositivo: campo personalizado `x_biometrico_id` (recomendado) o relación a `hr.work_location`; y el catálogo de identificadores de sede.
- Si la API entrega el tramo cerrado o entrada/salida por separado.
- Formato y zona horaria de las fechas (recomendado: ISO 8601 en UTC).

Una vez cerradas esas tres decisiones, habilitar la asistencia reutiliza la maquinaria que el middleware ya tiene probada (idempotencia, reintentos, panel de observabilidad y auditoría), sin tocar el núcleo.
