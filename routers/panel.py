"""
Panel de OBSERVABILIDAD del middleware.

Expone:
  GET /panel                     -> dashboard HTML (shell sin datos; sin auth)
  GET /panel/api/resumen         -> tarjetas agregadas (JSON, requiere API Key)
  GET /panel/api/sincronizaciones-> listado de sync_map con filtros (JSON, API Key)
  GET /panel/api/logs            -> bitacora sync_log con filtros (JSON, API Key)
  GET /panel/api/detalle/{entidad}/{id_origen} -> mapeo + logs (JSON, API Key)

Seguridad: los endpoints de DATOS estan protegidos con la misma API Key
(X-Api-Key) que el resto del middleware. La pagina /panel en si es solo el shell
HTML (no lleva datos), y su JavaScript pide la clave y la envia en cada fetch.
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from core import observabilidad
from core.seguridad import LIMITE_LECTURA, limiter, verify_api_key

# Router de datos (protegido). Prefijo /panel/api.
router = APIRouter(prefix="/panel", tags=["Panel"])

datos = APIRouter(
    prefix="/panel/api", tags=["Panel"], dependencies=[Depends(verify_api_key)]
)


@datos.get("/resumen")
@limiter.limit(LIMITE_LECTURA)
def api_resumen(request: Request):
    """Resumen agregado para las tarjetas del panel."""
    return observabilidad.resumen()


@datos.get("/sincronizaciones")
@limiter.limit(LIMITE_LECTURA)
def api_sincronizaciones(
    request: Request,
    estado: str | None = Query(None, description="PROCESADO|ERROR|PROCESANDO|PENDIENTE"),
    entidad: str | None = Query(None, description="factura|pago|conciliacion|..."),
    id_origen: str | None = Query(None, description="Coincidencia parcial."),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Listado de sync_map (estado de cada sincronizacion) con filtros."""
    return observabilidad.listar_sincronizaciones(
        estado=estado, entidad=entidad, id_origen=id_origen,
        limite=limite, offset=offset,
    )


@datos.get("/logs")
@limiter.limit(LIMITE_LECTURA)
def api_logs(
    request: Request,
    entidad: str | None = Query(None),
    id_origen: str | None = Query(None),
    resultado: str | None = Query(None, description="OK|ERROR"),
    limite: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Bitacora de auditoria (sync_log) con filtros."""
    return observabilidad.listar_logs(
        entidad=entidad, id_origen=id_origen, resultado=resultado,
        limite=limite, offset=offset,
    )


@datos.get("/detalle/{entidad}/{id_origen}")
@limiter.limit(LIMITE_LECTURA)
def api_detalle(entidad: str, id_origen: str, request: Request):
    """Detalle de un registro: su mapeo mas toda su bitacora."""
    return observabilidad.detalle_registro(entidad, id_origen)


@datos.get("/cola/{fila_id}")
@limiter.limit(LIMITE_LECTURA)
def api_cola_detalle(fila_id: int, request: Request):
    """
    Una fila de la cola con su payload y el ORIGINAL de Smartier.

    Va aparte del listado a proposito: el documento original de cada fila haria
    la respuesta del listado inmanejable, y solo se necesita al auditar un caso
    concreto.
    """
    return observabilidad.detalle_cola(fila_id)


@datos.get("/cola")
@limiter.limit(LIMITE_LECTURA)
def api_cola(
    request: Request,
    estado: str | None = Query(None, description="PENDIENTE | PROCESADO | ERROR"),
    limite: int = Query(100, ge=1, le=500),
):
    """
    Cola del poller (modo pull), leida de la DB de ORIGEN del cliente.
    Devuelve habilitado=False si no hay SOURCE_DATABASE_URL configurado.
    """
    return observabilidad.cola_poller(limite=limite, estado=estado)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel_html():
    """Sirve el dashboard HTML (shell). Los datos se cargan via fetch con API Key."""
    return HTMLResponse(content=_PANEL_HTML)


# ---------------------------------------------------------------------------
# Dashboard HTML autocontenido (sin dependencias externas).
# ---------------------------------------------------------------------------

_PANEL_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel de sincronizacion | API-Odoo</title>
<style>
  :root {
    --bg:#0f172a; --panel:#1e293b; --panel2:#273449; --txt:#e2e8f0; --muted:#94a3b8;
    --border:#334155; --ok:#22c55e; --err:#ef4444; --proc:#f59e0b; --pend:#64748b;
    --accent:#38bdf8;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg); color:var(--txt); font-size:14px; }
  header { padding:16px 24px; background:var(--panel); border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; font-weight:600; }
  header .status { margin-left:auto; display:flex; gap:8px; align-items:center; color:var(--muted); }
  .status-pill { display:flex; gap:6px; align-items:center; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--pend); }
  .dot.ok { background:var(--ok); } .dot.err { background:var(--err); }
  main { padding:24px; max-width:1280px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:24px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .card .n { font-size:28px; font-weight:700; } .card .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  .card.ok .n{color:var(--ok);} .card.err .n{color:var(--err);} .card.proc .n{color:var(--proc);} .card.pend .n{color:var(--pend);}
  .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; align-items:center; }
  input,select,button { background:var(--panel2); color:var(--txt); border:1px solid var(--border);
    border-radius:8px; padding:8px 12px; font-size:13px; }
  button { cursor:pointer; } button:hover { border-color:var(--accent); }
  /* Un boton deshabilitado se atenua y deja de responder al puntero: en la
     primera y la ultima pagina, "Anterior"/"Siguiente" no llevan a ningun
     sitio y conviene que se vea antes de pulsarlos. */
  button:disabled { opacity:.4; cursor:default; }
  button:disabled:hover { border-color:var(--border); }
  .pager { display:flex; gap:8px; align-items:center; margin-top:12px; }
  .pager select { margin-left:auto; }
  /* --- Vista 3D del flujo --- */
  .flujo3d { background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:12px 16px 8px; margin-bottom:16px; }
  .flujo3d-head { display:flex; gap:12px; align-items:center; margin-bottom:8px; }
  .flujo3d-head .titulo { font-weight:600; }
  .flujo3d-head .mini { margin-left:auto; padding:4px 10px; font-size:12px; }
  #lienzo3d canvas { display:block; width:100% !important; height:100% !important; }
  #lienzo3d { height:300px; border-radius:10px; overflow:hidden; cursor:grab;
    background:radial-gradient(ellipse at 50% 40%, #16233c 0%, #0d1626 70%); }
  #lienzo3d.oculto { display:none; }
  /* En pausa la escena se atenua y pierde el color: se ve de inmediato que lo
     que muestra ya no esta vivo, sin tener que leer el aviso de al lado. */
  #lienzo3d.pausado { filter:grayscale(.75) opacity(.45); cursor:default; }
  #lienzo3d.pausado canvas { transition:filter .4s ease; }
  #lienzo3d:active { cursor:grabbing; }
  .leyenda { display:flex; gap:18px; flex-wrap:wrap; padding:8px 2px 2px;
    font-size:12px; color:var(--muted); }
  .leyenda b { color:var(--txt); font-weight:600; }
  /* Fila que cambio desde el ultimo refresco: destaca un instante y se apaga.
     Con auto-refresco encendido es la forma de ver QUE se movio sin releer
     toda la tabla. */
  @keyframes destello { from { background:rgba(56,189,248,.22); } to { background:transparent; } }
  tr.nuevo { animation:destello 2.2s ease-out; }
  button.primary { background:var(--accent); color:#04283a; border-color:var(--accent); font-weight:600; }
  .tabs { display:flex; gap:4px; margin-bottom:12px; border-bottom:1px solid var(--border); }
  .tab { padding:8px 16px; cursor:pointer; color:var(--muted); border-bottom:2px solid transparent; }
  .tab.active { color:var(--txt); border-bottom-color:var(--accent); }
  table { width:100%; border-collapse:collapse; background:var(--panel); border-radius:10px; overflow:hidden; }
  th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:13px; vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  tr:hover td { background:var(--panel2); }
  .badge { padding:2px 9px; border-radius:99px; font-size:11px; font-weight:600; display:inline-block; }
  .badge.PROCESADO{background:rgba(34,197,94,.15);color:var(--ok);}
  .badge.ERROR{background:rgba(239,68,68,.15);color:var(--err);}
  .badge.PROCESANDO{background:rgba(245,158,11,.15);color:var(--proc);}
  .badge.PENDIENTE{background:rgba(100,116,139,.15);color:var(--pend);}
  .badge.OK{background:rgba(34,197,94,.15);color:var(--ok);}
  .mono { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; }
  .err-txt { color:#fca5a5; max-width:420px; }
  .muted { color:var(--muted); }
  .empty { text-align:center; padding:40px; color:var(--muted); }
  .gate { max-width:380px; margin:80px auto; background:var(--panel); border:1px solid var(--border);
    border-radius:12px; padding:28px; text-align:center; }
  .gate input { width:100%; margin:14px 0; }
  .warn-txt { color: var(--proc); }
  .hide { display:none; }
</style>
</head>
<body>
<div id="gate" class="gate">
  <h2>Panel API-Odoo</h2>
  <p class="muted">Introduce la API Key para ver el estado de las sincronizaciones.</p>
  <input id="key" type="password" placeholder="X-Api-Key" autocomplete="off">
  <button class="primary" onclick="entrar()" style="width:100%">Entrar</button>
  <p id="gerr" class="err-txt hide" style="margin-top:12px"></p>
</div>

<div id="app" class="hide">
<header>
  <h1>🔄 Panel de sincronizacion</h1>
  <div class="status">
    <div class="status-pill">
      <span id="poller-dot" class="dot"></span>
      <span id="poller-status" class="muted">Sin ejecución aún</span>
    </div>
    <span id="lastupd" class="muted"></span>
    <label class="muted" style="display:flex;gap:6px;align-items:center">
      <input type="checkbox" id="auto" style="width:auto" checked> auto (5s)
    </label>
    <button onclick="pollerAhora(this)">Poller ahora</button>
    <button onclick="cargarTodo()">Refrescar</button>
    <button onclick="salir()">Salir</button>
  </div>
</header>
<main>
  <div class="cards" id="cards"></div>

  <!-- Vista 3D del flujo. Los nodos representan las cuatro etapas y las
       particulas que viajan entre ellos son registros reales: el brillo y el
       caudal salen de los datos, no de una animacion decorativa. -->
  <section class="flujo3d">
    <div class="flujo3d-head">
      <span class="titulo">Flujo en vivo</span>
      <span id="flujo-estado" class="muted">iniciando...</span>
      <button onclick="toggle3d(this)" id="btn3d" class="mini">Ocultar</button>
    </div>
    <div id="lienzo3d"></div>
    <div class="leyenda" id="leyenda3d"></div>
  </section>

  <div class="tabs">
    <div class="tab active" data-tab="sync" onclick="verTab('sync')">Sincronizaciones</div>
    <div class="tab" data-tab="logs" onclick="verTab('logs')">Bitacora</div>
    <div class="tab" data-tab="cola" onclick="verTab('cola')">Cola del poller</div>
  </div>

  <div id="tab-sync">
    <div class="toolbar">
      <select id="f-estado" onchange="cargarSync()">
        <option value="">Todos los estados</option>
        <option>PROCESADO</option><option>ERROR</option>
        <option>PROCESANDO</option><option>PENDIENTE</option>
      </select>
      <input id="f-entidad" placeholder="entidad (factura, pago...)" oninput="debounce(cargarSync)">
      <input id="f-idorigen" placeholder="id_origen (parcial)" oninput="debounce(cargarSync)">
      <span id="sync-count" class="muted"></span>
    </div>
    <table>
      <thead><tr>
        <th>Estado</th><th>Entidad</th><th>ID origen</th><th>Modelo Odoo</th>
        <th>ID Odoo</th><th>Actualizado</th><th>Observaciones</th>
      </tr></thead>
      <tbody id="sync-body"></tbody>
    </table>
    <div class="pager">
      <button onclick="paginaSync(-1)" id="sync-prev">&#8592; Anterior</button>
      <span id="sync-rango" class="muted"></span>
      <button onclick="paginaSync(1)" id="sync-next">Siguiente &#8594;</button>
      <select id="sync-tam" onchange="tamSync()">
        <option value="25">25 por pagina</option>
        <option value="50" selected>50 por pagina</option>
        <option value="100">100 por pagina</option>
        <option value="250">250 por pagina</option>
        <option value="500">500 por pagina</option>
      </select>
    </div>
  </div>

  <div id="tab-logs" class="hide">
    <div class="toolbar">
      <select id="l-resultado" onchange="cargarLogs()">
        <option value="">Todos</option><option>OK</option><option>ERROR</option>
      </select>
      
      <input id="l-entidad" placeholder="entidad" oninput="debounce(cargarLogs)">
      <input id="l-idorigen" placeholder="id_origen (parcial)" oninput="debounce(cargarLogs)">
      <span id="logs-count" class="muted"></span>
    </div>
    <table>
      <thead><tr>
        <th>Resultado</th><th>Entidad</th><th>ID origen</th><th>Accion</th>
        <th>Detalle</th><th>Fecha</th>
      </tr></thead>
      <tbody id="logs-body"></tbody>
    </table>
    <div class="pager">
      <button onclick="paginaLogs(-1)" id="logs-prev">&#8592; Anterior</button>
      <span id="logs-rango" class="muted"></span>
      <button onclick="paginaLogs(1)" id="logs-next">Siguiente &#8594;</button>
      <select id="logs-tam" onchange="tamLogs()">
        <option value="25">25 por pagina</option>
        <option value="50" selected>50 por pagina</option>
        <option value="100">100 por pagina</option>
        <option value="250">250 por pagina</option>
        <option value="500">500 por pagina</option>
      </select>
    </div>
  </div>

  <div id="tab-cola" class="hide">
    <div class="filters">
      <select id="c-estado" onchange="cargarCola()">
        <option value="">Todos los estados</option>
        <option>PENDIENTE</option><option>PROCESADO</option><option>ERROR</option>
      </select>
      <span id="cola-count" class="muted"></span>
    </div>
    <p id="cola-off" class="muted hide">
      Modo pull desactivado: define <code>SOURCE_DATABASE_URL</code> en el .env
      para que el middleware sondee la base de datos del cliente.
    </p>
    <table id="cola-tabla">
      <thead><tr>
        <th>#</th><th>Entidad</th><th>id_origen</th><th>Estado</th>
        <th>Factura Odoo</th><th>Creado</th><th>Procesado</th><th>Error</th>
      </tr></thead>
      <tbody id="cola-body"></tbody>
    </table>
  </div>
</main>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/animejs@4.1.4/lib/anime.umd.min.js"></script>
<script>
let APIKEY = sessionStorage.getItem("apikey") || "";
let tab = "sync";
let dbTimer = null, autoTimer = null;

function debounce(fn){ clearTimeout(dbTimer); dbTimer=setTimeout(fn,300); }

async function api(path, opts = {}){
  const r = await fetch(path, {
    ...opts,
    headers: {
      "X-Api-Key": APIKEY,
      ...(opts.headers || {})
    }
  });
  if(r.status===401) throw new Error("API Key invalida");
  if(!r.ok) throw new Error("Error "+r.status);
  return r.json();
}

function setPollerStatus(ok, text){
  const dot = document.getElementById("poller-dot");
  const out = document.getElementById("poller-status");
  dot.classList.toggle("ok", ok === true);
  dot.classList.toggle("err", ok === false);
  out.textContent = text;
}

async function pollerAhora(btn){
  if(btn) btn.disabled = true;
  try {
    const d = await api("/poller/ejecutar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant: "default", limite: 50 })
    });
    setPollerStatus(true,
      `Poller: ${d.leidas} leidas · ${d.procesadas} procesadas · ${d.con_error} con error`);
    // Rafaga en la escena: hace visible el momento exacto en que el poller
    // movio registros de la cola a Odoo, que es el instante que interesa ver.
    if(d.procesadas > 0) rafaga3d(d.procesadas);
    await cargarTodo();
  } catch (e) {
    setPollerStatus(false, e.message);
  } finally {
    if(btn) btn.disabled = false;
  }
}

// Valida una clave contra la API y, si sirve, abre el panel.
//
// La clave llega como PARAMETRO y no se lee aqui del formulario. Antes se
// leia del campo de texto, y eso rompia el refresco: al recargar la pagina el
// campo esta vacio, asi que el autologin machacaba con "" la clave que venia
// de sessionStorage y la sesion se caia al login. Como el catch borraba la
// clave guardada, la siguiente vez tampoco quedaba nada que recordar: de ahi
// que hiciera falta escribirla dos veces.
function acceder(clave, esAutologin){
  APIKEY = clave;
  return api("/panel/api/resumen").then(()=>{
    sessionStorage.setItem("apikey", APIKEY);
    document.getElementById("gate").classList.add("hide");
    document.getElementById("app").classList.remove("hide");
    cargarTodo();
    // El auto-refresco empieza aqui, con la clave ya validada.
    arrancarAuto();
  }).catch(e=>{
    // Una clave guardada que ya no vale se descarta; una recien tecleada se
    // conserva en pantalla para que el usuario pueda corregirla.
    if(esAutologin) sessionStorage.removeItem("apikey");
    APIKEY = "";
    mostrarPuerta(e.message);
  });
}

function entrar(){
  const campo = document.getElementById("key");
  const clave = campo.value.trim();
  if(!clave){ mostrarPuerta("Introduce la API Key."); return; }
  acceder(clave, false);
}

// Deja el panel en la pantalla de acceso, con un aviso si lo hay.
function mostrarPuerta(mensaje){
  document.getElementById("app").classList.add("hide");
  document.getElementById("gate").classList.remove("hide");
  const g = document.getElementById("gerr");
  if(mensaje){ g.textContent = mensaje; g.classList.remove("hide"); }
  else { g.classList.add("hide"); }
  document.getElementById("key")?.focus();
}

function salir(){ sessionStorage.removeItem("apikey"); location.reload(); }

// Escapa para insertar en HTML. Incluye comillas (" y ') a proposito: esta
// funcion se usa tambien DENTRO de atributos -p.ej. class="badge ${esc(x)}"-,
// y sin escapar las comillas un valor como '" onerror=...' podria romper el
// atributo e inyectar codigo (XSS). Hoy esos atributos reciben enums fijos,
// pero el contrato de esc() debe ser seguro en cualquier contexto para que
// reutilizarla con un dato libre no abra un agujero.
function esc(s){ return (s===null||s===undefined)?"":String(s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
function fecha(s){ return s? new Date(s).toLocaleString() : ""; }

// Ultimas respuestas, compartidas con la escena 3D para no duplicar peticiones.
let ULTIMO_RESUMEN = {}, ULTIMA_COLA = {};

async function cargarResumen(){
  const d = await api("/panel/api/resumen");
  ULTIMO_RESUMEN = d;
  const c = document.getElementById("cards");
  const e = d.por_estado;
  c.innerHTML = `
    <div class="card"><div class="n">${d.total}</div><div class="l">Total</div></div>
    <div class="card ok"><div class="n">${e.PROCESADO}</div><div class="l">Procesado</div></div>
    <div class="card err"><div class="n">${e.ERROR}</div><div class="l">Error</div></div>
    <div class="card proc"><div class="n">${e.PROCESANDO}</div><div class="l">Procesando</div></div>
    <div class="card pend"><div class="n">${e.PENDIENTE}</div><div class="l">Pendiente</div></div>
    <div class="card"><div class="n">${d.logs.total}</div><div class="l">Logs (${d.logs.errores} err)</div></div>`;
}

// Paginacion: el desplazamiento se guarda aparte de los filtros porque
// cambiar un filtro debe volver a la primera pagina -- si no, una busqueda que
// devuelve pocas filas se veria vacia al conservar un offset alto.
let syncOffset = 0, logsOffset = 0;

function tamPagina(id){ return parseInt(document.getElementById(id).value, 10) || 50; }

function pintarPager(pre, offset, mostradas, total){
  const desde = total ? offset + 1 : 0;
  document.getElementById(pre+"-rango").textContent =
    `${desde}-${offset + mostradas} de ${total}`;
  document.getElementById(pre+"-prev").disabled = offset <= 0;
  document.getElementById(pre+"-next").disabled = offset + mostradas >= total;
}

function paginaSync(dir){
  syncOffset = Math.max(0, syncOffset + dir * tamPagina("sync-tam"));
  cargarSync(true);
}
function tamSync(){ syncOffset = 0; cargarSync(true); }

function paginaLogs(dir){
  logsOffset = Math.max(0, logsOffset + dir * tamPagina("logs-tam"));
  cargarLogs(true);
}
function tamLogs(){ logsOffset = 0; cargarLogs(true); }

async function cargarSync(mantenerPagina){
  // Al filtrar se vuelve al principio; al paginar se conserva el offset.
  if(!mantenerPagina) syncOffset = 0;
  const lim = tamPagina("sync-tam");
  const p = new URLSearchParams();
  const est=document.getElementById("f-estado").value;
  const ent=document.getElementById("f-entidad").value;
  const ido=document.getElementById("f-idorigen").value;
  if(est)p.set("estado",est); if(ent)p.set("entidad",ent); if(ido)p.set("id_origen",ido);
  p.set("limite", lim); p.set("offset", syncOffset);
  const d = await api("/panel/api/sincronizaciones?"+p.toString());
  document.getElementById("sync-count").textContent = d.total+" registro(s)";
  pintarPager("sync", syncOffset, d.items.length, d.total);
  const b=document.getElementById("sync-body");
  if(!d.items.length){ b.innerHTML=`<tr><td colspan="7" class="empty">Sin resultados</td></tr>`; return; }
  b.innerHTML = d.items.map(m=>`<tr>
    <td><span class="badge ${esc(m.estado)}">${esc(m.estado)}</span></td>
    <td>${esc(m.entidad)}</td>
    <td class="mono">${esc(m.id_origen)}</td>
    <td class="muted">${esc(m.model_odoo)}</td>
    <td class="mono">${m.id_odoo??""}</td>
    <td class="muted">${fecha(m.actualizado)}</td>
    <td class="${m.estado==='ERROR'?'err-txt':'warn-txt'}">${esc(m.error)}</td>
  </tr>`).join("");
}

async function cargarLogs(mantenerPagina){
  if(!mantenerPagina) logsOffset = 0;
  const lim = tamPagina("logs-tam");
  const p = new URLSearchParams();
  const res=document.getElementById("l-resultado").value;
  const ent=document.getElementById("l-entidad").value;
  const ido=document.getElementById("l-idorigen").value;
  if(res)p.set("resultado",res); if(ent)p.set("entidad",ent); if(ido)p.set("id_origen",ido);
  p.set("limite", lim); p.set("offset", logsOffset);
  const d = await api("/panel/api/logs?"+p.toString());
  document.getElementById("logs-count").textContent = d.total+" entrada(s)";
  pintarPager("logs", logsOffset, d.items.length, d.total);
  const b=document.getElementById("logs-body");
  if(!d.items.length){ b.innerHTML=`<tr><td colspan="6" class="empty">Sin resultados</td></tr>`; return; }
  b.innerHTML = d.items.map(l=>`<tr>
    <td><span class="badge ${esc(l.resultado)}">${esc(l.resultado)}</span></td>
    <td>${esc(l.entidad)}</td>
    <td class="mono">${esc(l.id_origen)}</td>
    <td>${esc(l.accion)}</td>
    <td class="${l.resultado==='ERROR'?'err-txt':'muted'}">${esc(l.detalle)}</td>
    <td class="muted">${fecha(l.timestamp)}</td>
  </tr>`).join("");
}

function verTab(t){
  tab=t;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.tab===t));
  document.getElementById("tab-sync").classList.toggle("hide",t!=="sync");
  document.getElementById("tab-logs").classList.toggle("hide",t!=="logs");
  document.getElementById("tab-cola").classList.toggle("hide",t!=="cola");
}

async function cargarCola(){
  const p = new URLSearchParams();
  const est = document.getElementById("c-estado").value;
  if(est) p.set("estado", est);
  const d = await api("/panel/api/cola?"+p.toString());
  ULTIMA_COLA = d;

  // Modo pull apagado: se avisa en vez de mostrar una tabla vacia enganosa.
  document.getElementById("cola-off").classList.toggle("hide", d.habilitado);
  document.getElementById("cola-tabla").classList.toggle("hide", !d.habilitado);
  if(!d.habilitado){
    document.getElementById("cola-count").textContent = "";
    return;
  }

  const tot = d.totales || {};
  const partes = Object.keys(tot).sort().map(k=>`${k}: ${tot[k]}`);
  document.getElementById("cola-count").textContent =
    partes.length ? partes.join(" · ") : "cola vacia";

  const b = document.getElementById("cola-body");
  if(!d.filas.length){ b.innerHTML=`<tr><td colspan="8" class="empty">Sin filas</td></tr>`; return; }
  b.innerHTML = d.filas.map(f=>`<tr>
    <td class="mono">${esc(f.id)}</td>
    <td>${esc(f.entidad)}</td>
    <td class="mono">${esc(f.id_origen)}</td>
    <td><span class="badge ${esc(f.estado)}">${esc(f.estado)}</span></td>
    <td class="mono">${f.numero_odoo ? esc(f.numero_odoo)
        : (f.id_odoo ? '<span class="muted">id '+esc(f.id_odoo)+'</span>' : '')}</td>
    <td class="muted">${fecha(f.creado_en)}</td>
    <td class="muted">${fecha(f.procesado_en)}</td>
    <td class="${f.estado==='ERROR'?'err-txt':'muted'}">${esc(f.error_detalle)}</td>
  </tr>`).join("");
}


// ---------------------------------------------------------------------------
// Vista 3D del flujo (Three.js)
//
// Cuatro nodos, uno por etapa del pipeline, unidos por particulas que viajan
// entre ellos. Lo que se ve sale de los datos reales:
//
//   - el tamano y el brillo de cada nodo dependen de cuantos registros tiene,
//   - el caudal de particulas crece con los que estan en transito,
//   - un nodo con errores late en rojo.
//
// Si Three.js no carga (CDN bloqueado, red caida), la seccion se oculta sola:
// es un extra visual, no debe impedir usar el panel.
// ---------------------------------------------------------------------------

const ETAPAS = [
  { id:"smartier", nombre:"Smartier",  x:-10.5, color:0x8b5cf6 },
  { id:"cola",     nombre:"Cola",      x:-3.5, color:0x38bdf8 },
  { id:"middle",   nombre:"Middleware",x:  3.5, color:0x22c55e },
  { id:"odoo",     nombre:"Odoo",      x: 10.5, color:0xf59e0b },
];

let esc3d = null;   // {scene, camera, renderer, nodos, particulas, ...}

// Texto dentro de la escena. Se dibuja en un canvas 2D y se pega como sprite,
// que siempre mira a la camara: asi la etiqueta sigue legible por mucho que se
// gire el conjunto.
function etiqueta3d(texto, color){
  const c = document.createElement("canvas");
  c.width = 256; c.height = 64;
  const g = c.getContext("2d");
  g.font = "600 30px system-ui, -apple-system, Segoe UI, sans-serif";
  g.fillStyle = "#" + color.toString(16).padStart(6, "0");
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(texto, 128, 32);
  const tex = new THREE.CanvasTexture(c);
  tex.minFilter = THREE.LinearFilter;
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({
    map:tex, transparent:true, depthWrite:false,
  }));
  sp.scale.set(3.4, .85, 1);
  return sp;
}

function init3d(){
  const cont = document.getElementById("lienzo3d");
  if(!cont || typeof THREE === "undefined"){
    document.querySelector(".flujo3d")?.classList.add("hide");
    return;
  }

  const scene = new THREE.Scene();
  const ancho = cont.clientWidth || 800, alto = cont.clientHeight || 300;
  const camera = new THREE.PerspectiveCamera(34, ancho/alto, 0.1, 200);
  camera.position.set(0, 1.6, 17.5);
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ antialias:true, alpha:true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(ancho, alto);
  cont.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.42));
  const luz = new THREE.DirectionalLight(0xffffff, 1.0);
  luz.position.set(5, 9, 11);
  scene.add(luz);
  // Luz de relleno fria desde el lado opuesto: sin ella la mitad en sombra
  // queda negra y los nodos se ven planos.
  const relleno = new THREE.DirectionalLight(0x88bbff, 0.45);
  relleno.position.set(-8, -3, 6);
  scene.add(relleno);

  // --- Nodos ---
  // Un nucleo solido con dos anillos concentricos, no una esfera facetada.
  // Los anillos dan un lenguaje mas tecnico -- se leen como una estacion del
  // pipeline, no como una burbuja -- y al girar en planos distintos aportan
  // profundidad sin necesidad de que el nodo sea grande.
  const nodos = {};
  ETAPAS.forEach(e=>{
    const grupo = new THREE.Group();
    grupo.position.x = e.x;

    const esfera = new THREE.Mesh(
      new THREE.SphereGeometry(1.05, 48, 48),
      new THREE.MeshStandardMaterial({
        color:e.color, roughness:.25, metalness:.65,
        emissive:e.color, emissiveIntensity:.3,
      })
    );
    grupo.add(esfera);

    // Anillo exterior: marca el limite del nodo con una linea nitida.
    const anillo = new THREE.Mesh(
      new THREE.TorusGeometry(1.75, .035, 12, 96),
      new THREE.MeshBasicMaterial({ color:e.color, transparent:true, opacity:.75 })
    );
    anillo.rotation.x = Math.PI / 2.6;
    grupo.add(anillo);

    // Anillo interior en plano contrario: al girar los dos, la interseccion
    // sugiere volumen mucho mejor que un halo difuso.
    const anillo2 = new THREE.Mesh(
      new THREE.TorusGeometry(1.4, .02, 10, 80),
      new THREE.MeshBasicMaterial({ color:0xffffff, transparent:true, opacity:.28 })
    );
    anillo2.rotation.x = Math.PI / 2.6;
    anillo2.rotation.y = Math.PI / 3;
    grupo.add(anillo2);

    // Resplandor suave, muy tenue: solo separa el nodo del fondo.
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(2.1, 32, 32),
      new THREE.MeshBasicMaterial({
        color:e.color, transparent:true, opacity:.07,
        side:THREE.BackSide,
      })
    );
    grupo.add(halo);

    // Etiqueta con el nombre, dentro de la escena: leerla en la escena evita
    // tener que cruzar la mirada con la leyenda de abajo.
    const etiqueta = etiqueta3d(e.nombre, e.color);
    etiqueta.position.y = -1.95;
    grupo.add(etiqueta);

    scene.add(grupo);
    nodos[e.id] = { grupo, esfera, halo, anillo, anillo2, etiqueta,
                    base:e, valor:0, error:false };
  });

  // --- Tuberias entre etapas ---
  for(let i=0; i<ETAPAS.length-1; i++){
    const a = ETAPAS[i], b = ETAPAS[i+1];
    const tubo = new THREE.Mesh(
      new THREE.CylinderGeometry(.02, .02, b.x-a.x, 6),
      new THREE.MeshBasicMaterial({ color:0x3b4a63, transparent:true, opacity:.45 })
    );
    tubo.rotation.z = Math.PI/2;
    tubo.position.x = (a.x + b.x)/2;
    scene.add(tubo);
  }

  // --- Particulas: cada una es un registro viajando por el pipeline ---
  const particulas = [];
  const geoP = new THREE.SphereGeometry(.16, 10, 10);
  for(let i=0; i<60; i++){
    const m = new THREE.Mesh(geoP, new THREE.MeshBasicMaterial({ color:0x38bdf8 }));
    m.visible = false;
    scene.add(m);
    particulas.push({ malla:m, t:0, tramo:0, activa:false, vel:.004 });
  }

  esc3d = { scene, camera, renderer, nodos, particulas, cont,
            caudal:.02, giro:0, arrastrando:false, ultimoX:0 };

  // Arrastrar para girar la escena.
  cont.addEventListener("pointerdown", ev=>{
    esc3d.arrastrando = true; esc3d.ultimoX = ev.clientX;
  });
  window.addEventListener("pointerup", ()=>{ esc3d.arrastrando = false; });
  // Doble clic: vuelve a la vista frontal.
  cont.addEventListener("dblclick", ()=>{
    if(typeof anime !== "undefined"){
      anime.animate(esc3d, { giro:0, duration:600, ease:"outCubic" });
    }else{ esc3d.giro = 0; }
  });
  window.addEventListener("pointermove", ev=>{
    if(!esc3d.arrastrando) return;
    // Tope de +-35 grados: mas que eso pone los nodos en fila hacia el
    // fondo y se pierde la lectura de izquierda a derecha, que es lo que
    // cuenta el diagrama.
    const LIM = Math.PI / 5.2;
    esc3d.giro = Math.max(-LIM, Math.min(LIM,
      esc3d.giro + (ev.clientX - esc3d.ultimoX) * .004));
    esc3d.ultimoX = ev.clientX;
  });

  function ajustarTamano(){
    if(!esc3d) return;
    const w = cont.clientWidth, h = cont.clientHeight;
    if(!w || !h) return;                       // aun sin layout: no medir
    esc3d.camera.aspect = w / h;
    esc3d.camera.updateProjectionMatrix();
    esc3d.renderer.setSize(w, h);
    encuadrar3d();
  }
  window.addEventListener("resize", ajustarTamano);

  // El contenedor puede cambiar de ancho SIN que cambie la ventana: al
  // asentarse el layout, al mostrar la escena tras ocultarla, al aparecer una
  // barra de desplazamiento. Escuchando solo el resize de window, el renderer
  // se quedaba dibujando al tamano inicial -- 800px de reserva -- dentro de un
  // rectangulo mucho mas ancho, y la escena aparecia pegada a la izquierda.
  if(typeof ResizeObserver !== "undefined"){
    new ResizeObserver(ajustarTamano).observe(cont);
  }
  // Y una medida diferida, por si el observador no llega a tiempo al montar.
  requestAnimationFrame(ajustarTamano);

  encuadrar3d();
  animar3d();
  entrada3d();
  // El navegador recuerda el estado del checkbox al recargar: si 'auto'
  // quedo apagado, la escena debe nacer en pausa.
  const chk = document.getElementById('auto');
  if(chk && !chk.checked) pausar3d(true);
  document.getElementById("flujo-estado").textContent = "arrastra para girar";
}

// Entrada escalonada: los nodos caen desde arriba de izquierda a derecha,
// siguiendo el sentido del flujo. Da a entender el recorrido antes de que
// llegue el primer dato.
// Situa la camara a la distancia justa para que el pipeline entero quepa a lo
// ancho, sea cual sea el tamano de la ventana. Sin esto, en una pantalla
// estrecha los nodos de los extremos quedan fuera del encuadre.
function encuadrar3d(){
  if(!esc3d) return;
  const cam = esc3d.camera;

  // Ancho real a cubrir: el nodo mas extremo esta en x=10.5, y alrededor lleva
  // anillo (1.75) y halo (2.1). Ademas el grupo se ESCALA con el volumen de
  // datos -- hasta 1.9x -- asi que se toma el peor caso, o el nodo mas cargado
  // se sale del encuadre justo cuando mas interesa verlo.
  // Se encuadra por el ANILLO (1.75), no por el halo: este ultimo es un
  // degradado muy tenue y no pasa nada porque roce el borde. Encuadrar por el
  // halo obligaba a alejar la camara de mas y la escena quedaba pequeña en
  // medio del rectangulo.
  const ESCALA_MAX = 1.9;
  const semiAncho = 10.5 + 1.75 * ESCALA_MAX;  // ~13.8
  const MARGEN = 1.30;                          // aire alrededor de la escena

  const fovV = cam.fov * Math.PI / 180;
  const fovH = 2 * Math.atan(Math.tan(fovV / 2) * cam.aspect);

  // Distancia necesaria por ancho y por alto; manda la mayor. Con un lienzo
  // apaisado suele mandar el ancho, pero en una ventana estrecha el alto pasa
  // a ser el limite y sin esto los nodos rozarian arriba y abajo.
  const semiAlto = 3.6;   // nodo escalado, su anillo y la etiqueta de debajo
  const distH = (semiAncho * MARGEN) / Math.tan(fovH / 2);
  const distV = (semiAlto  * MARGEN) / Math.tan(fovV / 2);

  // Sin tope inferior: acercar la camara "por si acaso" era justo lo que
  // recortaba los nodos de los extremos.
  cam.position.z = Math.max(distH, distV);
  cam.updateProjectionMatrix();
}

function entrada3d(){
  if(typeof anime === "undefined" || !esc3d) return;
  ETAPAS.forEach((e, i)=>{
    const n = esc3d.nodos[e.id];
    n.grupo.scale.setScalar(0);
    n.grupo.position.y = 3.5;
    anime.animate(n.grupo.position, {
      y: 0, duration: 900, delay: i * 110, ease: "outCubic",
    });
    anime.animate(n.grupo.scale, {
      x: 1, y: 1, z: 1, duration: 850, delay: i * 110, ease: "outBack(1.4)",
    });
  });
}

function animar3d(){
  if(!esc3d) return;
  // En pausa se pinta un ultimo fotograma y se corta el bucle. Dejar de pedir
  // frames libera la GPU: una escena animada sin datos que la respalden solo
  // gasta bateria y aparenta actividad que no existe.
  if(esc3d.pausada){
    esc3d.renderer.render(esc3d.scene, esc3d.camera);
    return;
  }
  requestAnimationFrame(animar3d);
  const t = performance.now() * .001;

  // La escena entera oscila despacio, mas lo que el usuario arrastre.
  esc3d.scene.rotation.y = esc3d.giro + Math.sin(t*.18) * .06;

  Object.values(esc3d.nodos).forEach((n, i)=>{
    n.esfera.rotation.y += .0035;

    // Los dos anillos giran en sentidos opuestos y a distinto ritmo. Es lo que
    // hace legible el volumen: el cruce entre ambos da la referencia de
    // profundidad que una esfera sola no ofrece.
    if(n.anillo)  n.anillo.rotation.z  += .006;
    if(n.anillo2) n.anillo2.rotation.z -= .010;

    // Latido: rapido y marcado si hay errores, sereno si todo va bien.
    const ritmo = n.error ? 6 : 1.4;
    const amp   = n.error ? .14 : .04;
    n.halo.scale.setScalar(1 + Math.sin(t*ritmo + i) * amp);
    n.halo.material.opacity = n.error ? .18 + Math.sin(t*6+i)*.10 : .07;

    // Un nodo sin datos se apaga: el anillo casi desaparece y se distingue de
    // un vistazo cual etapa esta inactiva.
    if(n.anillo){
      n.anillo.material.opacity = n.valor > 0
        ? .70 + Math.sin(t*1.4 + i)*.14
        : .18;
    }
  });

  // Particulas viajando de un nodo al siguiente.
  esc3d.particulas.forEach(p=>{
    if(!p.activa){
      if(Math.random() < esc3d.caudal){
        p.activa = true; p.t = 0;
        p.tramo = Math.floor(Math.random() * (ETAPAS.length-1));
        p.malla.material.color.setHex(ETAPAS[p.tramo+1].color);
        p.malla.visible = true;
      }
      return;
    }
    p.t += p.vel;
    if(p.t >= 1){ p.activa = false; p.malla.visible = false; return; }
    const a = ETAPAS[p.tramo].x, b = ETAPAS[p.tramo+1].x;
    p.malla.position.x = a + (b-a)*p.t;
    // Arco: sube y baja entre nodo y nodo.
    p.malla.position.y = Math.sin(p.t*Math.PI) * 1.3;
    p.malla.position.z = Math.sin(p.t*Math.PI*2) * .35;
  });

  esc3d.renderer.render(esc3d.scene, esc3d.camera);
}

// Alimenta la escena con los datos del resumen y de la cola.
function actualizar3d(resumen, cola){
  if(!esc3d) return;
  const est = resumen.por_estado || {};
  const ent = resumen.por_entidad || {};
  const enCola = (cola && cola.filas) ? cola.filas.length : 0;
  const pendientes = est.PENDIENTE || 0;
  const errores = est.ERROR || 0;
  const procesados = est.PROCESADO || 0;

  const datos = {
    smartier: { valor: Object.values(ent).reduce((a,b)=>a+b,0), error:false },
    cola:     { valor: enCola, error:false },
    middle:   { valor: (est.PROCESANDO||0) + pendientes, error: errores>0 },
    odoo:     { valor: procesados, error:false },
  };

  Object.entries(datos).forEach(([id, d], i)=>{
    const n = esc3d.nodos[id];
    if(!n) return;
    const cambio = n.valor !== d.valor;
    const previo = n.valor;
    n.valor = d.valor;
    n.error = d.error;

    // El tamano crece con el logaritmo del volumen: 3 registros y 300 tienen
    // que caber en la misma escena sin que el nodo grande tape a los demas.
    const escala = 1 + Math.min(Math.log10(d.valor + 1) * .38, .9);
    const brillo = d.valor > 0 ? .55 : .12;

    // La etiqueta es hija del grupo, asi que hereda su escala. Se compensa con
    // la inversa para que el texto mida igual en todos los nodos y no se
    // desplace hacia abajo en los mas cargados.
    if(n.etiqueta){
      const inv = 1 / escala;
      n.etiqueta.scale.set(3.4 * inv, .85 * inv, 1);
      n.etiqueta.position.y = -1.95 * inv - .55;
    }

    if(typeof anime === "undefined"){
      // Sin anime.js el valor se aplica de golpe: el panel debe seguir
      // funcionando aunque el CDN falle.
      n.grupo.scale.setScalar(escala);
      n.esfera.material.emissiveIntensity = brillo;
      return;
    }

    // Escala y brillo se interpolan en vez de saltar. El escalonado por indice
    // hace que los nodos reaccionen en cascada de izquierda a derecha, que es
    // el sentido en que fluyen los datos.
    anime.animate(n.grupo.scale, {
      x: escala, y: escala, z: escala,
      duration: 800, delay: i * 60, ease: "outBack(1.6)",
    });
    anime.animate(n.esfera.material, {
      emissiveIntensity: brillo, duration: 700, delay: i * 70, ease: "outQuad",
    });

    // Un nodo que RECIBE registros da un empujon hacia arriba y vuelve: es la
    // senal de que algo acaba de llegar ahi, visible aunque no se mire fijo.
    if(cambio && d.valor > previo){
      anime.animate(n.grupo.position, {
        y: [0, .55, 0], duration: 900, delay: i * 60, ease: "outQuad",
      });
    }
  });

  // Mas trabajo en transito, mas particulas por segundo. Tambien se interpola:
  // un salto brusco del caudal se percibe como un parpadeo.
  const caudalNuevo = .012 + Math.min((enCola + pendientes) * .02, .1);
  if(typeof anime !== "undefined"){
    anime.animate(esc3d, { caudal: caudalNuevo, duration: 1200, ease: "inOutQuad" });
  }else{
    esc3d.caudal = caudalNuevo;
  }

  pintarLeyenda(errores);
}

// La leyenda cuenta hacia el valor nuevo en vez de sustituirlo. Ver el numero
// subir de 55 a 56 comunica que ENTRO uno; reemplazarlo no dice nada.
function pintarLeyenda(errores){
  const cont = document.getElementById("leyenda3d");
  const previos = esc3d.leyendaPrevia || {};

  cont.innerHTML = ETAPAS.map(e=>{
    const n = esc3d.nodos[e.id];
    const col = "#" + e.color.toString(16).padStart(6,"0");
    return `<span><span style="color:${col}">&#9679;</span> ${e.nombre}:
            <b id="cnt-${e.id}">${previos[e.id] ?? n.valor}</b></span>`;
  }).join("") + (errores ? ` <span class="err-txt">&#9679; ${errores} con error</span>` : "");

  if(typeof anime === "undefined") return;
  ETAPAS.forEach((e, i)=>{
    const n = esc3d.nodos[e.id];
    const desde = previos[e.id] ?? n.valor;
    if(desde === n.valor) return;
    const obj = { v: desde };
    anime.animate(obj, {
      v: n.valor, duration: 800, delay: i * 70, ease: "outQuad",
      onUpdate: ()=>{
        const el = document.getElementById("cnt-" + e.id);
        if(el) el.textContent = Math.round(obj.v);
      },
    });
  });
  esc3d.leyendaPrevia = Object.fromEntries(
    ETAPAS.map(e=>[e.id, esc3d.nodos[e.id].valor])
  );
}

// Lanza una rafaga de particulas por el pipeline. Se llama cuando el poller
// informa de registros procesados: el numero de particulas es proporcional,
// con un tope para que 500 facturas no saturen la escena.
function rafaga3d(cuantas){
  if(!esc3d || typeof anime === "undefined") return;
  const libres = esc3d.particulas.filter(p=>!p.activa);
  const n = Math.min(cuantas, libres.length, 24);
  libres.slice(0, n).forEach((p, i)=>{
    setTimeout(()=>{
      p.activa = true; p.t = 0; p.tramo = 1;   // de la Cola hacia Odoo
      p.vel = .012;                            // mas rapida que el flujo normal
      p.malla.material.color.setHex(0x22c55e);
      p.malla.visible = true;
    }, i * 55);
  });

  // Los nodos de Middleware y Odoo acusan el golpe.
  ["middle","odoo"].forEach((id, i)=>{
    const nodo = esc3d.nodos[id];
    if(!nodo) return;
    anime.animate(nodo.esfera.rotation, {
      y: nodo.esfera.rotation.y + Math.PI * 2,
      duration: 1200, delay: i * 200, ease: "outQuart",
    });
  });
}

// Sincroniza la escena con el auto-refresco. Sin datos frescos detras, una
// animacion en marcha afirma que el flujo esta vivo cuando en realidad esta
// congelado: es peor que no animar nada.
function pausar3d(pausar){
  if(!esc3d) return;
  const estaba = esc3d.pausada;
  esc3d.pausada = pausar;

  const cont = document.getElementById("lienzo3d");
  const aviso = document.getElementById("flujo-estado");
  if(cont) cont.classList.toggle("pausado", pausar);
  if(aviso){
    aviso.textContent = pausar ? "en pausa - activa 'auto' para ver el flujo"
                               : "arrastra para girar";
    aviso.classList.toggle("warn-txt", pausar);
  }
  // Al reanudar hay que relanzar el bucle: se corto solo al entrar en pausa.
  if(estaba && !pausar) animar3d();
}

function toggle3d(btn){
  const c = document.getElementById("lienzo3d");
  const oculto = c.classList.toggle("oculto");
  btn.textContent = oculto ? "Mostrar" : "Ocultar";
}

async function cargarTodo(){
  try{
    await cargarResumen();
    await cargarSync();
    await cargarLogs();
    await cargarCola();
    // La escena 3D se alimenta de los mismos datos que las tarjetas: no hace
    // peticiones propias.
    try{ actualizar3d(ULTIMO_RESUMEN, ULTIMA_COLA); }catch(e){ /* extra visual */ }
    document.getElementById("lastupd").textContent = "Actualizado "+new Date().toLocaleTimeString();
  }catch(e){
    // Si la clave dejo de valer (se cambio en el servidor, o caduco la
    // sesion), se vuelve a la pantalla de acceso SIN recargar la pagina: un
    // location.reload() aqui se repetiria cada 5 s con el auto-refresco
    // encendido, dejando el panel recargandose en bucle.
    if(e.message.includes("API Key")){
      clearInterval(autoTimer);
      sessionStorage.removeItem("apikey");
      APIKEY = "";
      mostrarPuerta("La sesion ha caducado. Vuelve a introducir la API Key.");
    }
  }
}

const chkAuto = document.getElementById("auto");

// Arranca el refresco periodico. Se llama al ACCEDER, no al cargar la pagina:
// mientras se ve el login todavia no hay API Key, y un temporizador lanzado
// antes solo produce 401 -- y el primero de ellos apagaba el auto-refresco
// (cargarTodo() lo detiene ante una clave invalida), de modo que al entrar ya
// no quedaba temporizador vivo y el panel se quedaba congelado hasta pulsar
// "Poller ahora" a mano.
function arrancarAuto(){
  clearInterval(autoTimer);          // nunca dos temporizadores a la vez
  if(chkAuto.checked){ autoTimer = setInterval(cargarTodo, 5000); }
}

chkAuto.addEventListener("change", ev=>{
  const vivo = ev.target.checked;
  if(vivo){ arrancarAuto(); cargarTodo(); }
  else{ clearInterval(autoTimer); }
  // La escena acompana al interruptor: con "auto" apagado los datos dejan
  // de refrescarse y la animacion mentiria.
  pausar3d(!vivo);
});
document.getElementById("key")?.addEventListener("keydown",e=>{ if(e.key==="Enter")entrar(); });

// La escena 3D se monta al cargar: el lienzo ya tiene tamano en el DOM.
window.addEventListener('load', ()=>{ try{ init3d(); }catch(e){
  // Si Three.js no esta disponible el panel debe seguir siendo usable.
  document.querySelector('.flujo3d')?.classList.add('hide');
} });

// Autologin: se entra con la clave GUARDADA, no con la del formulario, que
// tras un refresco esta vacio. Es lo que permite que F5 mantenga la sesion.
if(APIKEY){ acceder(APIKEY, true); }
</script>
</body>
</html>"""
