# Estado del sistema tras las correcciones — 17/09/2026

Informe de cierre de las correcciones pedidas en la respuesta de Smartier
(09/09/2026). Todo lo que sigue se **verificó ejecutándolo** contra la API real
y la suite de pruebas el 17/09/2026; no se da nada por bueno sobre el papel.

**Resumen en una línea:** las correcciones están hechas y verificadas, y la
integración funciona de punta a punta — pero **todavía no se puede facturar**,
por un motivo que no depende de nosotros: los clientes no tienen RIF.

---

## 1. Las correcciones pedidas: hechas y verificadas

| # | Qué se pidió | Estado | Cómo se comprobó |
|---|---|---|---|
| 1 | No tratar como RIF el contenido de un documento genérico (tipo 6) | **Hecho** | `rif_de()` acepta solo 29 y 30; leído en el código |
| 2 | Retirar el filtro de fecha inventado (`FechaDesde`) | **Hecho** | `FILTRO_FECHA` vacío y configurable por entorno |
| 3 | Un solo criterio de RIF en toda la integración | **Hecho** | `ingesta_smartier._extraer_nif` delega en `rif_de()` |
| 4 | Corregir el informe que les devolvemos | **Hecho** | `mapeo-smartier-odoo-v2.md` en v2.2 |

**Suite de pruebas: 234 en verde** (ejecutada hoy).

### Por qué importaba la nº 1

Antes leíamos el documento del cliente **sin mirar de qué tipo era**. El tipo 6
es *"Documento de identidad" genérico* y su validación es permisiva: acepta
letras, dígitos y espacios. Cualquier cosa escrita ahí habría acabado en el
campo fiscal de la factura.

Ahora un documento genérico se trata como *ausente*, y el cliente queda
`PENDIENTE` — que es la situación real, y además **visible**, en vez de una
factura emitida con un dato fiscal sin validar.

---


## 2. Estado real de los datos (hoy)

| Recurso | 21/08 | **17/09** |
|---|---|---|
| Clientes | 3 | **4** |
| Productos | 48 | **58** |
| Notas de entrega | 0 | **3** |
| Órdenes | 0 | **2** |

Las notas y sus estados:

| Nota | Estado | Orden | Cliente | ¿Se factura? |
|---|---|---|---|---|
| 3 | `Facturado` | 114 | Omer Rivas (9) | Sí |
| 2 | `Facturado` | 114 | Omer Rivas (9) | Sí |
| 1 | `Entregado` | 113 | JUNIOR SUMOZA (6) | No, aún no toca |

**Los 4 clientes siguen sin RIF** — los cuatro con `Documento.Tipo = 6` y
`Contenido = null`.

---

## 3. ¿Está todo bien? — La respuesta honesta

**Lo que funciona:** la cadena completa. Leemos la API, traducimos la nota al
formato de Odoo y la encolamos correctamente. Comprobado con las notas reales
de hoy: líneas, cantidades (250), precio (14,234276), moneda (USD) y producto
(`SMARTIER-344`) se traducen bien.

**Lo que no:** esas mismas notas llegan con `cliente_nif = null`.

```
NE-3  cliente_nif = None
NE-2  cliente_nif = None
```

Odoo con localización venezolana **rechazará la factura sin RIF**. La nota se
encola igualmente y queda en `ERROR` con el motivo visible en el panel — no se
pierde ni se descarta en silencio — pero no se emite.

**Es decir: el middleware está listo; los datos no.** No es un problema técnico
nuestro y no se arregla con código.

---

## 4. Qué hace falta para poder facturar

### 4.1 El RIF — bloqueo principal (Turicopy)

El RIF **solo se puede asignar a clientes de tipo Empresa** en Smartier.
Nuestros 4 clientes están dados de alta como `Contacto`, así que al operador
**ni siquiera le aparece la opción** — no es un descuido de captura.

Corregirlo no es editar un campo: es **dar de alta al cliente otra vez** por el
otro camino. Eso genera un `Id` nuevo en Smartier y, por tanto, un
`SMARTIER-<id>` nuevo. Nuestra deduplicación crearía un **contacto nuevo** en
Odoo en vez de reutilizar el existente, y el histórico quedaría partido.

Hay que decidirlo **antes** de que empiecen a recrear clientes:

- **Opción A** — recrear en Smartier como Empresa, archivar la ficha vieja en
  Odoo y asumir el corte de histórico.
- **Opción B** — cargar los RIF a mano en Odoo y dejar Smartier como está. El
  sincronizador no los pisa (`vat` solo se envía cuando Smartier trae valor).

### 4.2 Pendiente de Smartier

1. **El nombre real del filtro de fecha** para `/external/notas-entrega` y
   `/external/ordenes`. Verificamos que `FechaDesde` no se aplica y probamos 13
   alternativas sin acierto. Sin él, cada pasada lee el histórico completo: hoy
   con 3 notas no se nota, con volumen real sí. Si no existe ninguno, que lo
   digan — cambia cómo dimensionamos la ingesta.
2. **Que verifiquen el control de documento obligatorio** en nuestra instancia
   (se ofrecieron en su consulta 1).

---

## 5. Lo que Smartier NO nos da

Inventario levantado **consultando la API real** el 17/09/2026 (no la
documentación): todos los campos que devuelve cada recurso, uniendo varias filas
para no dar por ausente un campo que venga `null` en una y con valor en otra.

### 5.1 Lo que sí llega, recurso por recurso

| Recurso | Campos |
|---|---|
| **clientes** | `Id`, `Nombre`, `RazonSocial`, `Email`, `Estado`, `Tipo`, `CreadoUtc`, `Documento{Tipo, Contenido}` |
| **notas-entrega** | `Id`, `Estado`, `Tipo`, `Cantidad`, `CantidadTerminada`, `Descuento`, `Direccion`, `Comentarios`, 4 fechas, `PrecioUnitario{Monto, Moneda}`, `Planta`, `Transportista`, `Contacto`, `Orden{Id, Referencia, Cliente, Producto}` |
| **ordenes** | `Id`, `Estado`, `Referencia`, `Pausado`, `Prioritario`, `PlazoEntrega`, 3 fechas, `Cliente`, `Producto`, `Vendedor` |
| **productos** | `Id`, `Nombre`, `Estado`, `Tipo`, `PorcentajeIVA`, `Exento` |

### 5.2 Retenciones: no existen en la API

**Ningún recurso expone un solo campo de retención.** Ni IVA ni ISLR, ni en el
cliente, ni en la orden, ni en la nota:

| Dato fiscal | ¿Lo da Smartier? | De dónde sale |
|---|---|---|
| ¿Es agente de retención de IVA? | **No** | Odoo (`wh_iva_agent`) |
| % de retención de IVA (75 / 100) | **No** | Odoo, posición fiscal |
| ¿Es agente de retención de ISLR? | **No** | Odoo (`islr_withholding_agent`) |
| % de retención de ISLR (1-5) | **No** | Odoo, posición fiscal |
| Nº de comprobante de retención | **No** | Odoo / SENIAT |
| Tipo de contribuyente (ordinario / especial) | **No** | Odoo, posición fiscal |

Ellos ya lo confirmaron (consulta 3): **no hay campos personalizados por tenant**
y las categorías del CRM no se exponen en `/external`. No es un olvido, es el
alcance del producto.

**Consecuencia:** las retenciones son **competencia exclusiva de Odoo**, y la
sincronización no debe tocarlas nunca. Ya está así por diseño:
`CAMPOS_ACTUALIZABLES` deja fuera los campos fiscales, y al **crear** un
contacto se fijan `wh_iva_agent` e `islr_withholding_agent` a `False` de forma
explícita — porque `l10n_ve_full` los declara con `default=True` y, sin eso, un
cliente nuevo quedaría marcado como agente de retención **sin que nadie lo
decidiera**. Al actualizar no se envía ningún campo fiscal, para no revertir lo
que contabilidad configuró a mano.

### 5.3 Impuestos: solo en el catálogo, no en la nota

El producto **sí** trae `PorcentajeIVA` y `Exento`. Hoy el catálogo entero son
**58 productos, todos al 16 %, ninguno exento**.

Pero la **nota de entrega no repite el IVA de su producto**. Verificado hoy con
la nota 3: el producto 344 está al 16 % en el catálogo, y en la nota traducida
`producto_iva` llega `null`.

**No es un problema:** el IVA no se envía a Odoo, que lo resuelve desde la ficha
del producto. Es más robusto que copiarlo — si mañana cambia la alícuota, se
cambia en un sitio. Conviene saberlo para no buscar en la nota un dato que está
en el catálogo.

### 5.4 Lo demás que falta y Odoo necesita

| Dato | ¿Smartier? | De dónde sale |
|---|---|---|
| **RIF del cliente** | Campo existe, **los 4 vienen vacíos** | **Bloqueo actual** (§4.1) |
| Dirección fiscal del cliente | **No** (la nota trae la de *entrega*) | Odoo, ficha del contacto |
| Teléfono del cliente | **No** | Odoo |
| Condiciones de pago / vencimiento | **No** | Odoo, ficha del cliente |
| Tasa de cambio USD→VES | **No** | Odoo, tabla de tasas (BCV) |
| Cuenta contable de ingreso | **No** | Odoo, ficha del producto |
| Nº de factura fiscal / control | **No** | Odoo, secuencia del diario |
| Nº de orden de compra del cliente | Solo `Referencia` (texto libre) | — |
| Vendedor como comercial de Odoo | Solo nombre en la orden | Sin enlazar |

**El patrón:** Smartier es la fuente de verdad de **qué se vendió, a quién y por
cuánto**; Odoo lo es de **cómo tributa eso**. La división es correcta y no
conviene borrarla — pedirle a Smartier datos fiscales sería duplicar la
configuración contable en un sitio donde nadie la mantiene.

**Las dos excepciones** son los datos que Smartier debería dar y hoy no da:

1. **El RIF** — el campo existe (`Documento`), pero exige cliente tipo Empresa
   (§4.1). Sin él no hay factura.
2. **La dirección fiscal** — hoy solo llega la de entrega (`"Valencia"`), que no
   sirve para el documento fiscal. Hay que cargarla en Odoo.

---

## 6. Conclusión

| | |
|---|---|
| Correcciones pedidas | **Hechas y verificadas** (4/4) |
| Suite de pruebas | **234 en verde** |
| Lectura y traducción de notas | **Funciona** con datos reales |
| Emisión de facturas | **Bloqueada** — faltan los RIF |

El sistema **no está listo para facturar**, y el motivo no es técnico: es que
los clientes no tienen identificación fiscal. En cuanto haya RIF, las notas que
hoy quedan en `ERROR` se procesan reencolándolas, sin tocar código.

Lo urgente es **decidir la vía del RIF** (§4.1), porque condiciona el histórico
de clientes en Odoo y conviene resolverlo antes de que se recree ninguno.
