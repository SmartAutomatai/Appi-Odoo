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

## 2. Un bug nuevo, encontrado hoy al verificar

Al comprobar el estado real apareció algo que **no se había visto antes porque
hasta ahora no había datos**: el tenant tenía 0 notas de entrega.

**Hoy ya hay 3 notas y 2 órdenes.** Y con ellas, el fallo:

| | |
|---|---|
| Lo que esperaba nuestro filtro | `Facturada` |
| Lo que devuelve la API | `Facturado` |

Una letra. La comparación es exacta, así que **las dos notas facturables se
descartaban en silencio**: sin error, sin aviso, sin nada en el panel. El
sistema habría parecido correcto sin llegar a facturar nunca.

**Corregido** (`SMARTIER_ESTADOS_FACTURAR=Facturado`) y verificado:

```
'Facturado'  -> SE ENCOLA
'Entregado'  -> SE DESCARTA     (correcto: aún no toca facturar)
```

No hizo falta tocar código: el valor ya era configurable por entorno. Queda
guardado también en el `.env` de la rama para que no se pierda.

> **La lección:** este fallo era invisible con el tenant vacío. Conviene
> revisar los demás valores de estado en cuanto haya datos de los que
> aprender — hasta ahora estaban puestos por suposición.

---

## 3. Estado real de los datos (hoy)

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

## 4. ¿Está todo bien? — La respuesta honesta

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

## 5. Qué hace falta para poder facturar

### 5.1 El RIF — bloqueo principal (Turicopy)

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

### 5.2 Pendiente de Smartier

1. **El nombre real del filtro de fecha** para `/external/notas-entrega` y
   `/external/ordenes`. Verificamos que `FechaDesde` no se aplica y probamos 13
   alternativas sin acierto. Sin él, cada pasada lee el histórico completo: hoy
   con 3 notas no se nota, con volumen real sí. Si no existe ninguno, que lo
   digan — cambia cómo dimensionamos la ingesta.
2. **Que verifiquen el control de documento obligatorio** en nuestra instancia
   (se ofrecieron en su consulta 1).
3. **Confirmar la lista de estados** de nota de entrega y de orden. Hoy hemos
   visto `Facturado`, `Entregado` y `Terminado`; el bug de §2 nació de suponer
   uno. Con el catálogo completo dejamos de adivinar.

### 5.3 Pendiente nuestro, antes de producción

- Plan contable venezolano oficial (el actual es un mínimo de 7 cuentas de
  prueba).
- Decidir `wh_iva_agent` / `islr_withholding_agent` de la compañía: están en
  `True` por defecto del módulo, nadie lo decidió.
- Reintento automático de los errores transitorios de la cola: hoy una fila en
  `ERROR` **no se reintenta sola**. Es el hueco más grande que queda.

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

Lo urgente es **decidir la vía del RIF** (§5.1), porque condiciona el histórico
de clientes en Odoo y conviene resolverlo antes de que se recree ninguno.
