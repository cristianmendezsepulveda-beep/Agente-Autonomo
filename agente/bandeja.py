"""Bandeja de aprobación para la PERSONA que opera el agente (no es una herramienta del MCP). Solo biblioteca estándar.

Uso (desde la raíz del proyecto, en una terminal interactiva):
    python agente/bandeja.py listar                       # TODO lo pendiente: escalados, cotizaciones y cobranza
    python agente/bandeja.py ver <id>                     # contenido completo, hash y estado del sello
    python agente/bandeja.py aprobar <id> [--forzar]      # pide escribir el id para confirmar; NO envía nada
    python agente/bandeja.py rechazar <id> --motivo "..."
    python agente/bandeja.py cerrar <e-id>                # escalados: se cierran, no se aprueban
    python agente/bandeja.py incidente <id> --motivo "..."  # marca un aprobado que acabó mal (queja, error)
    python agente/bandeja.py resumen [--dias 30]          # métricas por categoría vs. criterios de ascenso

Qué es y qué NO es:
- El servidor MCP solo CREA borradores y escalados en workspace/. Esta herramienta los revisa: aprobar o rechazar deja un
  REGISTRO (workspace/archivo/aprobados|rechazados/) y mueve el archivo fuera de la cola. NO envía nada ni contacta a nadie:
  "aprobado" significa que una persona lo validó y lo enviará por su cuenta. Para cobranza, anotar el envío en cartera.csv.
- Lo que se aprueba es EXACTAMENTE lo que se mostró: `aprobar` pasa el hash del contenido mostrado y `decidir` aborta si el archivo
  cambió; además toma el archivo con un renombrado atómico y comprueba que lo tomado coincide con lo leído.
- El servidor sella cada borrador (campo "sello" = sha256 del contenido). Si una persona lo edita antes de aprobar, el sello
  ya no coincide y queda "editado": así se mide cuántos borradores se aprueban sin tocar (criterio de ascenso de nivel).
- Cada registro lleva su propio sello (`sello_registro`) y cada decisión queda en una cadena de hashes
  (estado/bandeja/decisiones.jsonl) con id, hash, fecha de decisión y categoría. `resumen` SOLO cuenta registros cuyo contenido,
  sello, categoría, nombre de archivo y carpeta son coherentes Y cuyo evento (con la MISMA fecha) está en la cadena. Además
  concilia en sentido contrario: un evento de decisión sin archivo (registro borrado) o un archivo sin evento (fabricado o
  copiado), un registro ilegible o alterado, una cadena rota o un truncado impiden TODO "CUMPLE". Un incidente cuenta si está en
  el archivo O en la cadena (borrar el campo no lo borra).
  LÍMITE HONESTO: la cadena no usa HMAC con clave fuera de alcance. Con escritura SOLO en workspace/ no se puede esconder nada
  sin que `resumen` lo señale; quien pueda escribir además en estado/ puede reescribir la cadena entera y recalcular todo. Esto
  detecta ediciones honestas, accidentes y falsificaciones torpes, NO a un atacante con acceso de escritura al host. La fecha de
  creación del borrador es editable (la caducidad es una ayuda, no un control) y la ventana de `resumen` solo cuenta
  incidentes de decisiones dentro de ella.
- `aprobar` exige una terminal interactiva (stdin con teclado) y escribir el id; no existe --si. `decidir()` es importable
  (necesita el hash mostrado), pero ninguna herramienta del MCP lo llama. `decidido_por` es el usuario del sistema, NO una
  identidad autenticada.
- Un pendiente de más de VENCE_DIAS días no se aprueba sin --forzar (queda anotado): las cifras pueden haber cambiado.
- Subir de nivel de autonomía NO es automático y en el nivel 1 TAMPOCO hay descenso automático: `resumen` solo informa y una
  persona baja de nivel. Fuera del nivel 1: editar y aprobar en un paso, nivel vigente por categoría y descenso automático.
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid

import nocontacto
import revision

RAIZ = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ID_VALIDO = re.compile(r"^e-[0-9a-f]{12}$")
_RE_COT = re.compile(r"b-[0-9a-f]{12}")
_RE_COB = re.compile(r"c-[a-z0-9][a-z0-9-]{0,29}-(?:amable|firme)")
_RE_ESC = re.compile(r"e-[0-9a-f]{12}")
COLAS = (
    ("cotizacion", "borradores", _RE_COT),
    ("cobranza", "borradores_cobranza", _RE_COB),
    ("escalado", "escalados", _RE_ESC),
)
CATEGORIAS = {"cotizacion", "cobranza_amable", "cobranza_firme"}
MAX_BYTES = 200_000
MAX_ARCHIVOS = 2000  # por cola de pendientes
MAX_REGISTROS = 20_000  # por carpeta de registros
VENCE_DIAS = 7
TOLERANCIA_FUTURO_S = 300
TIMEOUT_BLOQUEO = 5.0
CRITERIO = {
    "casos": 50,
    "tasa_sin_edicion_pct": 95,
    "incidentes": 0,
}  # propuesta de docs/superpowers/specs/...jefatura...


def tasa_cumple(sin_edicion, medibles):
    """>= 95 % en ENTEROS (sin redondear antes de comparar: 189/199 = 94,97 % NO cumple)."""
    return medibles > 0 and sin_edicion * 100 >= CRITERIO["tasa_sin_edicion_pct"] * medibles


# ---------- sello de contenido ----------
def _canon(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _sha(o):
    return hashlib.sha256(_canon(o).encode("ascii")).hexdigest()


def sellar(d):
    """Devuelve una copia de `d` con "sello" = sha256 del resto del contenido (lo usa el servidor al crear borradores)."""
    cuerpo = {k: v for k, v in d.items() if k != "sello"}
    return {**cuerpo, "sello": _sha(cuerpo)}


def estado_sello(d):
    """'intacto', 'editado' o 'sin_sello' (borradores anteriores a la bandeja)."""
    s = d.get("sello")
    if s is None:
        return "sin_sello"
    cuerpo = {k: v for k, v in d.items() if k != "sello"}
    return "intacto" if isinstance(s, str) and s == _sha(cuerpo) else "editado"


def _sello_registro(reg):
    return _sha({k: v for k, v in reg.items() if k not in ("incidente", "sello_registro")})


# ---------- utilidades ----------
def _confinar(ruta, raiz):
    """Rechaza `ruta` si su ancestro existente más profundo resuelve (enlace simbólico o junction) fuera del proyecto: así tampoco
    se pueden CREAR carpetas fuera con makedirs. Devuelve `ruta`."""
    actual = os.path.abspath(ruta)
    while not os.path.lexists(actual):
        padre = os.path.dirname(actual)
        if padre == actual:
            break
        actual = padre
    real, base = os.path.normcase(os.path.realpath(actual)), os.path.normcase(os.path.realpath(raiz))
    try:
        dentro = os.path.commonpath([real, base]) == base
    except ValueError:
        dentro = False
    if not dentro:
        raise ValueError("la carpeta resuelve fuera del proyecto (enlace o junction): revisar a mano")
    return ruta


def _carpeta(raiz, *partes):
    """workspace/<partes>, confinada dentro del proyecto."""
    return _confinar(os.path.join(raiz, "workspace", *partes), raiz)


def _limpiar(texto, tope):
    """Texto de una línea sin caracteres de control ni de formato (se imprime en la terminal de la persona)."""
    t = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(texto))
    return " ".join(t.split())[:tope]


def _pantalla(texto):
    return "".join((c if c == "\n" or not (unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp")) else "?") for c in texto)


def _quien(nombre=None):
    n = _limpiar(
        (nombre if nombre is not None else os.environ.get("USERNAME") or os.environ.get("USER") or ""),
        40,
    )
    n = re.sub(r"[^\w.\- ]", "", n)
    return n or "desconocido"


def _leer_bytes(ruta):
    if os.path.islink(ruta) or not os.path.isfile(ruta):
        raise FileNotFoundError("no existe (o no es un archivo normal)")
    with open(ruta, "rb") as f:
        datos = f.read(MAX_BYTES + 1)
    if len(datos) > MAX_BYTES:
        raise ValueError("archivo demasiado grande")
    return datos


def _objeto(datos):
    try:
        o = json.loads(datos.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("no es JSON válido")
    if not isinstance(o, dict):
        raise ValueError("no es un objeto")
    return o


def _creado_ts(o, ruta):
    c = o.get("creado")
    if isinstance(c, str):
        try:
            d = datetime.datetime.fromisoformat(c)
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            return d.timestamp()
        except ValueError:
            pass
    return os.path.getmtime(ruta)


def _resolver(item_id, raiz):
    """(tipo, ruta) de un id válido; ValueError si el id no tiene forma de ninguna cola."""
    if isinstance(item_id, str):
        for tipo, carpeta, rx in COLAS:
            if rx.fullmatch(item_id):
                return tipo, os.path.join(_carpeta(raiz, carpeta), item_id + ".json")
    raise ValueError("id inválido (formato b-…, c-…-amable|firme o e-…)")


def _categoria(tipo, item_id):
    """Sale del ID (validado por regex), nunca del contenido editable."""
    return "cobranza_" + item_id.rsplit("-", 1)[1] if tipo == "cobranza" else tipo


def _lista_no_contacto(raiz, ruta_persona=None):
    """Lista de no contacto vigente, o None si no está disponible (ausente, ilegible o inválida: entonces la cobranza queda bloqueada)."""
    try:
        ruta = ruta_persona or nocontacto.ruta_persona_por_defecto()
        if ruta is None:
            return None
        return nocontacto.cargar(ruta, os.path.join(raiz, "estado", "agente", "no_contactar_auto.json"))
    except nocontacto.NoContactoInvalido:
        return None


def _ruta_auto(raiz):
    return os.path.join(raiz, "estado", "agente", "no_contactar_auto.json")


def _revisar(tipo, o, lista):
    return revision.revisar(tipo, o, lista)


def _dir_estado(raiz):
    d = _confinar(os.path.join(raiz, "estado", "bandeja"), raiz)
    os.makedirs(d, exist_ok=True)
    return d


def _auditoria(raiz):
    sandbox = os.path.join(RAIZ, "sandbox")
    if sandbox not in sys.path:
        sys.path.insert(0, sandbox)
    from audit_log import AuditLog

    return AuditLog(os.path.join(_dir_estado(raiz), "decisiones.jsonl"))


@contextlib.contextmanager
def _bloqueo(raiz):
    sandbox = os.path.join(RAIZ, "sandbox")
    if sandbox not in sys.path:
        sys.path.insert(0, sandbox)
    from _lock import bloqueo, BloqueoNoObtenido

    ruta = os.path.join(_dir_estado(raiz), "bandeja")
    try:
        with bloqueo(ruta, timeout=TIMEOUT_BLOQUEO):
            yield
    except BloqueoNoObtenido:
        raise RuntimeError("otra operación de la bandeja está en curso: espere y reintente")


def _escribir_atomico(destino, objeto):
    tmp = f"{destino}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(objeto, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, destino)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _eventos(raiz):
    """(cadena_ok, {(evento, id, hash, decidido)}) leído del registro encadenado, con el mismo bloqueo que usa `registrar` para no
    ver una línea a medias. Sin archivo: (True, vacío). `decidido` es None en eventos que no lo llevan (incidente, cerrado).
    """
    ruta = _confinar(os.path.join(raiz, "estado", "bandeja", "decisiones.jsonl"), raiz)
    if not os.path.exists(ruta):
        return True, set()
    sandbox = os.path.join(RAIZ, "sandbox")
    if sandbox not in sys.path:
        sys.path.insert(0, sandbox)
    from audit_log import verificar
    from _lock import bloqueo, BloqueoNoObtenido

    try:
        with bloqueo(ruta, timeout=TIMEOUT_BLOQUEO):
            ok, _ = verificar(ruta)
            if not ok:
                return False, set()
            ev = set()
            with open(ruta, encoding="ascii") as f:
                for linea in f:
                    r = json.loads(linea)
                    d = r.get("datos", {})
                    ev.add((r.get("evento"), d.get("id"), d.get("hash"), d.get("decidido")))
    except BloqueoNoObtenido:
        raise RuntimeError("el registro de decisiones está ocupado: reintente")
    return True, ev


# ---------- escalados (comportamiento original) ----------
def listar(raiz=RAIZ):
    """Devuelve los escalados pendientes (lista de dicts) ordenados por fecha de creación."""
    carpeta = _carpeta(raiz, "escalados")
    casos = []
    if os.path.isdir(carpeta):
        for nombre in os.listdir(carpeta):
            if not nombre.endswith(".json"):
                continue
            try:
                caso = _objeto(_leer_bytes(os.path.join(carpeta, nombre)))
            except (OSError, ValueError):
                caso = {
                    "id": nombre[:-5],
                    "motivo": "(archivo ilegible: revisar a mano)",
                    "creado": "",
                }
            casos.append(caso)
    return sorted(casos, key=lambda c: (str(c.get("creado", "")), str(c.get("id", ""))))


def cerrar(caso_id, raiz=RAIZ):
    if not isinstance(caso_id, str) or not ID_VALIDO.fullmatch(caso_id):  # fullmatch: "$" aceptaría un salto final
        raise ValueError("id inválido (formato e-xxxxxxxxxxxx)")
    origen = os.path.join(_carpeta(raiz, "escalados"), caso_id + ".json")
    if not os.path.isfile(origen):
        raise FileNotFoundError("no existe un escalado pendiente con ese id")
    try:
        caso = _objeto(_leer_bytes(origen))
    except ValueError:
        raise ValueError("el archivo no es un caso válido: revisarlo a mano")
    if caso.get("id") != caso_id:
        raise ValueError("el contenido no corresponde al id: revisarlo a mano")
    caso["estado"] = "cerrado"
    caso["cerrado"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    destino_dir = _carpeta(raiz, "archivo", "escalados")
    os.makedirs(destino_dir, exist_ok=True)
    destino = os.path.join(destino_dir, caso_id + ".json")
    _escribir_atomico(destino, caso)  # atómico: nunca queda un archivo a medias
    os.remove(origen)
    try:  # rastro del cierre; el cierre ya está hecho, así que un fallo del registro solo avisa
        _auditoria(raiz).registrar("cerrado", {"id": caso_id, "hash": _sha(caso)})
    except Exception as e:
        print(
            f"aviso: no se pudo registrar el cierre en la cadena ({type(e).__name__})",
            file=sys.stderr,
        )
    return destino


# ---------- bandeja unificada ----------
def _resumen_item(tipo, o):
    if tipo == "cotizacion":
        lineas = o.get("lineas")
        n = len(lineas) if isinstance(lineas, list) else "?"
        extra = " · REQUIERE APROBACIÓN (descuento o monto)" if o.get("requiere_aprobacion_humana") is True else ""
        return _limpiar(
            f"total {o.get('total', '?')} {o.get('moneda', '')} · {n} línea(s) · ref {o.get('cliente_ref', '')}{extra}",
            120,
        )
    if tipo == "cobranza":
        return _limpiar(
            f"factura {o.get('factura_id', '?')} · {o.get('etapa', '?')} · para {o.get('destinatario', '?')}",
            120,
        )
    return _limpiar(f"{o.get('motivo', '')} (ref {o.get('referencia', '')})", 120)


def _item_ilegible(base, motivo):
    return {
        **base,
        "ilegible": True,
        "resumen": motivo,
        "edad_dias": 0.0,
        "sello": "n/a",
        "vencido": False,
        "creado_ts": 0.0,
        "bloqueos": 1,
        "avisos": 0,
    }


def pendientes(raiz=RAIZ, ahora=None, ruta_lista_persona=None):
    """Todo lo que espera a una persona, lo más antiguo primero, con el número de bloqueos y avisos de la revisión previa. Lo que no se
    puede interpretar sale marcado `ilegible`, y si una cola supera MAX_ARCHIVOS se añade un aviso: nunca se omite en silencio.
    """
    ahora = time.time() if ahora is None else ahora
    lista = _lista_no_contacto(raiz, ruta_lista_persona)
    items = []
    for tipo, carpeta, rx in COLAS:
        base0 = {"id": f"({carpeta})", "tipo": tipo, "categoria": tipo, "ilegible": False}
        try:
            entradas = list(os.scandir(_carpeta(raiz, carpeta)))
        except FileNotFoundError:
            continue
        except ValueError as e:
            items.append(_item_ilegible(base0, f"({e})"))
            continue
        if len(entradas) > MAX_ARCHIVOS:
            items.append(
                _item_ilegible(
                    base0,
                    f"(AVISO: más de {MAX_ARCHIVOS} archivos; se muestran solo los primeros: vaciar la cola)",
                )
            )
        for e in entradas[:MAX_ARCHIVOS]:
            if e.name.endswith(".tmp"):
                continue  # escritura en curso
            nombre = e.name[:-5] if e.name.endswith(".json") else ""
            base = {
                "id": _limpiar(nombre or e.name, 70),
                "tipo": tipo,
                "categoria": tipo,
                "ilegible": False,
            }
            if not rx.fullmatch(nombre):
                items.append(_item_ilegible(base, "(nombre inesperado: revisar a mano)"))
                continue
            try:
                o = _objeto(_leer_bytes(e.path))
                ts = _creado_ts(o, e.path)
                edad = max(0.0, (ahora - ts) / 86400)
                rev = _revisar(tipo, o, lista)
                item = {
                    **base,
                    "categoria": _categoria(tipo, nombre),
                    "resumen": _resumen_item(tipo, o),
                    "edad_dias": round(edad, 1),
                    "sello": estado_sello(o) if tipo != "escalado" else "n/a",
                    "vencido": tipo != "escalado" and edad > VENCE_DIAS,
                    "creado_ts": ts,
                    "bloqueos": len(rev["bloqueos"]),
                    "avisos": len(rev["avisos"]),
                }
            except (OSError, ValueError, RecursionError, OverflowError):
                items.append(_item_ilegible(base, "(archivo ilegible: revisar a mano)"))
                continue
            items.append(item)
    return sorted(items, key=lambda i: (i["creado_ts"], i["id"]))


def ver(item_id, raiz=RAIZ):
    """(contenido dict, estado del sello) de un pendiente."""
    tipo, ruta = _resolver(item_id, raiz)
    o = _objeto(_leer_bytes(ruta))
    return o, (estado_sello(o) if tipo != "escalado" else "n/a")


def hash_contenido(o):
    return _sha(o)


def revision_de(item_id, raiz=RAIZ, ruta_lista_persona=None):
    """Revisión previa determinista de un pendiente: {"bloqueos": [...], "avisos": [...]} (ver agente/revision.py)."""
    tipo, ruta = _resolver(item_id, raiz)
    return _revisar(tipo, _objeto(_leer_bytes(ruta)), _lista_no_contacto(raiz, ruta_lista_persona))


def decidir(
    item_id,
    decision,
    raiz=RAIZ,
    quien=None,
    motivo="",
    forzar=False,
    ahora=None,
    hash_esperado=None,
    confirmacion="programatica",
    ruta_lista_persona=None,
):
    """Aprueba o rechaza un borrador pendiente. Deja un registro en workspace/archivo/aprobados|rechazados/ y líneas en la
    cadena de decisiones; devuelve la ruta del registro. No envía nada.
    - Aprobar EXIGE `hash_esperado` (hash del contenido que la persona vio): si el archivo cambió, aborta.
    - Aprobar pasa por la revisión previa determinista (revision.py) y la lista de no contacto: con un BLOQUEO no se aprueba (ni con
      `forzar`); los avisos se aprueban pero quedan en el registro. Rechazar nunca se bloquea.
    - Todo ocurre bajo un bloqueo entre procesos; el archivo se toma con un renombrado atómico y solo se borra si lo tomado
      coincide byte a byte con lo leído; ante cualquier fallo se devuelve a su sitio."""
    if decision not in ("aprobado", "rechazado"):
        raise ValueError("decisión inválida")
    tipo, ruta = _resolver(item_id, raiz)
    if tipo == "escalado":
        raise ValueError("los escalados se cierran con 'cerrar', no se aprueban ni se rechazan")
    if decision == "aprobado" and not (isinstance(hash_esperado, str) and re.fullmatch(r"[0-9a-f]{64}", hash_esperado)):
        raise ValueError("aprobar exige el hash del contenido mostrado (hash_esperado)")
    motivo = _limpiar(motivo or "", 200)
    if decision == "rechazado" and not motivo:
        raise ValueError("rechazar requiere un motivo")
    ahora = time.time() if ahora is None else ahora
    quien = _quien(quien)
    with _bloqueo(raiz):
        datos = _leer_bytes(ruta)  # UNA lectura: lo que se decide es exactamente lo que se archiva
        o = _objeto(datos)
        h = _sha(o)
        if hash_esperado is not None and h != hash_esperado:
            raise ValueError("el contenido cambió desde que se mostró: vuelva a revisarlo")
        if o.get("id") != item_id:
            raise ValueError("el contenido no corresponde al id: revisarlo a mano")
        edad = (ahora - _creado_ts(o, ruta)) / 86400
        if decision == "aprobado" and edad > VENCE_DIAS and not forzar:
            raise ValueError(f"tiene {int(edad)} días (más de {VENCE_DIAS}): las cifras pueden haber cambiado; " "vuelva a evaluarlo o use --forzar")
        avisos = []
        if decision == "aprobado":
            rev = _revisar(tipo, o, _lista_no_contacto(raiz, ruta_lista_persona))
            if rev["bloqueos"]:
                raise ValueError("no se puede aprobar: " + "; ".join(_limpiar(b, 120) for b in rev["bloqueos"][:5]))
            avisos = [_limpiar(a, 120) for a in rev["avisos"][:10]]
        registro = {
            "id": item_id,
            "tipo": tipo,
            "categoria": _categoria(tipo, item_id),
            "estado": decision,
            "decidido": datetime.datetime.fromtimestamp(ahora, datetime.timezone.utc).isoformat(timespec="seconds"),
            "decidido_por": quien,
            "motivo": motivo,
            "sello": estado_sello(o),
            "forzado": bool(forzar),
            "confirmacion": confirmacion,
            "avisos": avisos,
            "hash_contenido": h,
            "contenido": o,
        }
        registro["sello_registro"] = _sello_registro(registro)
        registro["incidente"] = None
        dest_dir = _carpeta(raiz, "archivo", "aprobados" if decision == "aprobado" else "rechazados")
        os.makedirs(dest_dir, exist_ok=True)
        marca = datetime.datetime.fromtimestamp(ahora, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destino = os.path.join(dest_dir, f"{item_id}.{marca}.json")
        if os.path.exists(destino):
            raise FileExistsError("ya existe un registro con ese id y esa marca de tiempo")
        log = _auditoria(raiz)  # si la cadena no está disponible o está rota, NO se decide (falla cerrado)
        datos_log = {
            "id": item_id,
            "categoria": registro["categoria"],
            "sello": registro["sello"],
            "hash": h,
            "por": quien,
            "forzado": str(bool(forzar)),
            "confirmacion": confirmacion,
            "decidido": registro["decidido"],
        }
        log.registrar(decision + "_intento", datos_log)
        tomado = f"{ruta}.{uuid.uuid4().hex}.tomado"
        creado_destino = False
        try:
            os.replace(ruta, tomado)  # sale de la cola de forma atómica: nadie más puede editarlo ahora
            if _leer_bytes(tomado) != datos:
                raise RuntimeError("el archivo cambió mientras se decidía: no se archivó nada")
            _escribir_atomico(destino, registro)
            creado_destino = True
            os.remove(tomado)
        except Exception as e:
            destino_fuera = True
            if creado_destino:  # primero se retira el registro; si no se puede, el borrador NO vuelve a la cola (evita duplicados)
                try:
                    os.remove(destino)
                except OSError:
                    destino_fuera = False
            if destino_fuera and os.path.exists(tomado) and not os.path.exists(ruta):
                try:
                    os.replace(tomado, ruta)  # se devuelve a la cola
                except OSError:
                    pass
            try:
                log.registrar("decision_fallida", {"id": item_id, "tipo_error": type(e).__name__})
            except Exception:
                pass
            raise
        try:
            log.registrar(decision, datos_log)
        except Exception as e:  # ya está decidido y archivado; el _intento quedó en la cadena y `resumen` lo acepta
            print(
                f"aviso: no se pudo registrar el cierre en la cadena ({type(e).__name__})",
                file=sys.stderr,
            )
    return destino


def _registro_integro(reg, carpeta_estado=None):
    """Coherencia completa del registro: nada de lo que alimenta las métricas se puede cambiar sin que falle (salvo
    `incidente`, que se cruza con la cadena)."""
    try:
        c = reg.get("contenido")
        tipo, rid = reg.get("tipo"), reg.get("id")
        if not (isinstance(c, dict) and tipo in ("cotizacion", "cobranza") and isinstance(rid, str)):
            return False
        rx = _RE_COT if tipo == "cotizacion" else _RE_COB
        return (
            rx.fullmatch(rid) is not None
            and c.get("id") == rid
            and reg.get("hash_contenido") == _sha(c)
            and reg.get("sello_registro") == _sello_registro(reg)
            and reg.get("categoria") == _categoria(tipo, rid)
            and reg.get("categoria") in CATEGORIAS
            and reg.get("sello") == estado_sello(c)
            and reg.get("sello") in ("intacto", "editado", "sin_sello")
            and reg.get("estado") in ("aprobado", "rechazado")
            and isinstance(reg.get("avisos"), list)
            and len(reg["avisos"]) <= 10
            and all(isinstance(a, str) for a in reg["avisos"])
            and (carpeta_estado is None or carpeta_estado == reg["estado"])
            and isinstance(reg.get("decidido"), str)
        )
    except (TypeError, ValueError, AttributeError, KeyError, RecursionError, OverflowError):
        return False


def _nombre_esperado(reg):
    """Nombre de archivo que le corresponde a un registro: <id>.<AAAAMMDDTHHMMSSZ>.json según su fecha de decisión."""
    d = datetime.datetime.fromisoformat(reg["decidido"])
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return f"{reg['id']}.{d.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"


def incidente(item_id, motivo, raiz=RAIZ, quien=None, ahora=None):
    """Marca el último registro APROBADO de `item_id` como incidente (queja, error, algo que salió mal). El evento queda en la
    cadena ANTES de tocar el archivo, y `resumen` cuenta el incidente si está en cualquiera de los dos.
    """
    _resolver(item_id, raiz)
    motivo = _limpiar(motivo or "", 200)
    if not motivo:
        raise ValueError("un incidente requiere un motivo")
    ahora = time.time() if ahora is None else ahora
    quien = _quien(quien)
    with _bloqueo(raiz):
        carpeta = _carpeta(raiz, "archivo", "aprobados")
        try:
            candidatos = sorted(n for n in os.listdir(carpeta) if re.fullmatch(re.escape(item_id) + r"\.\d{8}T\d{6}Z\.json", n))
        except FileNotFoundError:
            candidatos = []
        if not candidatos:
            raise FileNotFoundError("no hay un registro aprobado con ese id")
        ruta = os.path.join(carpeta, candidatos[-1])
        reg = _objeto(_leer_bytes(ruta))
        if not _registro_integro(reg, "aprobado"):
            raise ValueError("el registro está alterado: revisarlo a mano")
        cadena_ok, ev = _eventos(raiz)
        if not cadena_ok:
            raise ValueError("la cadena de decisiones está rota: revisarla a mano antes de registrar nada")
        if reg.get("incidente") or ("incidente", item_id, reg["hash_contenido"], None) in ev:  # el campo se puede borrar; la cadena no
            raise ValueError("ese registro ya tiene un incidente")
        _auditoria(raiz).registrar("incidente", {"id": item_id, "por": quien, "hash": reg["hash_contenido"]})
        reg["incidente"] = {
            "fecha": datetime.datetime.fromtimestamp(ahora, datetime.timezone.utc).isoformat(timespec="seconds"),
            "motivo": motivo,
            "por": quien,
        }
        _escribir_atomico(ruta, reg)
    return ruta


def resumen(raiz=RAIZ, dias=30, ahora=None):
    """Métricas por categoría de las decisiones de los últimos `dias` días, contra CRITERIO. Informativo: no cambia nada.
    Solo cuentan registros íntegros, con su evento en la cadena y fecha no futura; el resto se informa aparte. Si la cadena
    está rota, o algo se truncó, NINGUNA categoría puede dar CUMPLE."""
    if not isinstance(dias, int) or not 1 <= dias <= 3650:
        raise ValueError("--dias debe estar entre 1 y 3650")
    ahora = time.time() if ahora is None else ahora
    desde = ahora - dias * 86400
    cadena_ok, ev = _eventos(raiz)
    finales = {e for e in ev if e[0] in ("aprobado", "rechazado")}  # eventos de decisión que DEBEN tener su archivo
    vistos = set()
    cats, ilegibles, alterados, sin_cadena, truncado = {}, 0, 0, 0, False
    for estado_dir in ("aprobados", "rechazados"):
        estado = "aprobado" if estado_dir == "aprobados" else "rechazado"
        carpeta = _carpeta(raiz, "archivo", estado_dir)
        try:
            nombres = [n for n in os.listdir(carpeta) if n.endswith(".json")]
        except FileNotFoundError:
            continue
        if len(nombres) > MAX_REGISTROS:
            truncado = True
            nombres = nombres[:MAX_REGISTROS]
        for n in nombres:
            try:
                reg = _objeto(_leer_bytes(os.path.join(carpeta, n)))
            except (OSError, ValueError):
                ilegibles += 1
                continue
            ts = 0.0
            try:  # íntegro, en su carpeta y con el nombre que le corresponde por su fecha (una copia con otro nombre no vale)
                ok = _registro_integro(reg, estado) and n == _nombre_esperado(reg)
                if ok:
                    ts = datetime.datetime.fromisoformat(reg["decidido"]).timestamp()
            except (ValueError, OverflowError, OSError, KeyError, TypeError, RecursionError):
                ok = False
            if not ok or ts > ahora + TOLERANCIA_FUTURO_S:  # fecha futura: entraría siempre en la ventana
                alterados += 1
                continue
            clave = (estado, reg["id"], reg["hash_contenido"], reg["decidido"])
            if clave in vistos:
                alterados += 1
                continue
            vistos.add(clave)
            if cadena_ok and clave not in ev and (estado + "_intento",) + clave[1:] not in ev:
                sin_cadena += 1  # archivo sin su evento en la cadena (fabricado o con la fecha cambiada)
                continue
            if ts < desde:
                continue
            c = cats.setdefault(
                reg["categoria"],
                {
                    "decididos": 0,
                    "aprobados": 0,
                    "sin_edicion": 0,
                    "editados": 0,
                    "sin_sello": 0,
                    "rechazados": 0,
                    "incidentes": 0,
                },
            )
            c["decididos"] += 1
            c["sin_sello"] += reg["sello"] == "sin_sello"
            if reg["estado"] == "rechazado":
                c["rechazados"] += 1
                continue
            c["aprobados"] += 1
            c["sin_edicion"] += reg["sello"] == "intacto"
            c["editados"] += reg["sello"] == "editado"
            c["incidentes"] += bool(reg.get("incidente")) or ("incidente", reg["id"], reg["hash_contenido"], None) in ev
    sin_archivo = 0 if (truncado or not cadena_ok) else len(finales - vistos)  # evento de decisión sin archivo = registro borrado
    integro = cadena_ok and not truncado and not (ilegibles or alterados or sin_cadena or sin_archivo)
    for c in cats.values():
        medibles = c["decididos"] - c["sin_sello"]  # sin sello no se sabe si se editó: no entra en la tasa
        c["tasa_sin_edicion"] = round(c["sin_edicion"] / medibles, 3) if medibles > 0 else None
        c["cumple_criterio_numerico"] = (
            integro and c["decididos"] >= CRITERIO["casos"] and tasa_cumple(c["sin_edicion"], medibles) and c["incidentes"] <= CRITERIO["incidentes"] and c["sin_sello"] == 0
        )
    return {
        "dias": dias,
        "categorias": cats,
        "registros_ilegibles": ilegibles,
        "registros_alterados": alterados,
        "registros_sin_evento_en_cadena": sin_cadena,
        "eventos_sin_archivo": sin_archivo,
        "cadena_ok": cadena_ok,
        "truncado": truncado,
        "integro": integro,
    }


# ---------- línea de comandos ----------
def _confirmar(item_id, entrada):
    print(
        f"Para aprobar escriba el id exacto ({item_id}) y pulse Intro (cualquier otra cosa cancela): ",
        end="",
        flush=True,
    )
    return entrada.readline().strip() == item_id


def _dias(valor):
    n = int(valor)
    if not 1 <= n <= 3650:
        raise argparse.ArgumentTypeError("entre 1 y 3650")
    return n


def _aviso_bloqueo_bajas(raiz):
    if nocontacto.bloqueo_activo(_ruta_auto(raiz)):
        print(
            "ATENCIÓN: hay bajas pedidas por correo que NO se pudieron registrar (lista llena, corrupta o sin permisos): nada que contacte o registre "
            "personas funciona hasta que usted revise la lista automática, la repare y borre estado/agente/no_contactar_auto.json.bloqueo "
            "(y reinicie el servidor MCP)."
        )


def _aviso_lista_llena(n):
    """Al llenarse la lista automática no se pueden registrar más bajas (y un remitente con dominio propio puede llenarla a propósito)."""
    if n >= 0.8 * nocontacto.MAX_ENTRADAS:
        print(
            f"ATENCIÓN: la lista automática de no contacto está casi llena ({n} de {nocontacto.MAX_ENTRADAS}): al llenarse no se registrarán "
            "más bajas. Revísela (puede estar siendo llenada a propósito) y vacíe lo que sea falso."
        )


def _imprimir_revision(rev):
    for b in rev["bloqueos"]:
        print(f"BLOQUEO: {_limpiar(b, 160)}")
    for a in rev["avisos"]:
        print(f"AVISO: {_limpiar(a, 160)}")


def main(argv, entrada=None, raiz=RAIZ, ruta_lista_persona=None):
    interactiva = entrada is None
    entrada = entrada or sys.stdin
    p = argparse.ArgumentParser(prog="bandeja.py", add_help=True)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("listar")
    sub.add_parser("nocontacto")
    for nombre in ("ver", "aprobar", "rechazar", "cerrar", "incidente"):
        s = sub.add_parser(nombre)
        s.add_argument("id")
        if nombre in ("rechazar", "incidente"):
            s.add_argument("--motivo", required=True)
        if nombre == "aprobar":
            s.add_argument("--forzar", action="store_true")
    s = sub.add_parser("resumen")
    s.add_argument("--dias", type=_dias, default=30)
    try:
        a = p.parse_args(argv[1:])
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        if a.cmd == "listar":
            items = pendientes(raiz, ruta_lista_persona=ruta_lista_persona)
            for i in items:
                marca = " ".join(
                    m
                    for m in (
                        ("VENCIDO" if i["vencido"] else ""),
                        ("ILEGIBLE" if i["ilegible"] else ""),
                        ("EDITADO" if i["sello"] == "editado" else ""),
                        ("SIN_SELLO" if i["sello"] == "sin_sello" else ""),
                        ("BLOQUEADO" if i["bloqueos"] and not i["ilegible"] else ""),
                        ("AVISOS" if i["avisos"] else ""),
                    )
                    if m
                )
                print(f"{i['id']:<34} {i['tipo']:<10} {i['edad_dias']:>5}d  {i['resumen']}  {marca}".rstrip())
            print(f"{len(items)} pendiente(s)")
            try:  # las bajas las registra el CÓDIGO sin que nadie vea el mensaje: aquí se hacen visibles para detectar falsos positivos
                n = len(nocontacto.entradas_auto(_ruta_auto(raiz)))
                if n:
                    print(f"{n} baja(s) automática(s) en la lista de no contacto (revíselas con: python agente/bandeja.py nocontacto)")
                _aviso_lista_llena(n)
                _aviso_bloqueo_bajas(raiz)
            except nocontacto.NoContactoInvalido as e:
                print(f"ATENCIÓN: la lista automática de no contacto no se puede leer ({_limpiar(e, 100)}): nada que contacte funcionará")
            return 0
        if a.cmd == "nocontacto":
            entradas = nocontacto.entradas_auto(_ruta_auto(raiz))
            for correo, d in sorted(entradas.items(), key=lambda kv: str(kv[1].get("fecha", ""))):
                fecha = d.get("fecha")
                cuando = datetime.datetime.fromtimestamp(fecha, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M") if isinstance(fecha, (int, float)) and 0 < fecha < 4e9 else "?"
                print(f"{_limpiar(correo, 80):<40} {cuando}  {_limpiar(d.get('motivo', ''), 60)}")
            _aviso_lista_llena(len(entradas))
            print(
                f"{len(entradas)} entrada(s). Para quitar un falso positivo, edite estado/agente/no_contactar_auto.json (o, mejor, no lo toque y "
                "avise: cada baja pedida por correo debe respetarse)."
            )
            return 0
        if a.cmd == "ver":
            o, sello = ver(a.id, raiz)
            print(_pantalla(json.dumps(o, ensure_ascii=False, indent=1)))
            print(f"sello: {sello}   hash: {hash_contenido(o)}")
            _imprimir_revision(revision_de(a.id, raiz, ruta_lista_persona))
            return 0
        if a.cmd == "aprobar":
            if interactiva and not sys.stdin.isatty():
                print(
                    "error: aprobar exige una terminal interactiva (no se acepta entrada redirigida)",
                    file=sys.stderr,
                )
                return 1
            o, sello = ver(a.id, raiz)
            h = hash_contenido(o)
            print(_pantalla(json.dumps(o, ensure_ascii=False, indent=1)))
            print(f"sello: {sello}  (editado = alguien lo cambió después de que el agente lo creó)   hash: {h}")
            rev = _revisar(_resolver(a.id, raiz)[0], o, _lista_no_contacto(raiz, ruta_lista_persona))
            _imprimir_revision(rev)
            if rev["bloqueos"]:
                print(
                    "no se puede aprobar mientras haya BLOQUEOS (--forzar no los salta): corrija el borrador o recházelo",
                    file=sys.stderr,
                )
                return 1
            if not _confirmar(a.id, entrada):
                print("cancelado: no se aprobó nada", file=sys.stderr)
                return 1
            print(
                "registrado en",
                decidir(
                    a.id,
                    "aprobado",
                    raiz,
                    forzar=a.forzar,
                    hash_esperado=h,
                    confirmacion="interactiva" if interactiva else "inyectada",
                    ruta_lista_persona=ruta_lista_persona,
                ),
            )
            print("Recuerde: esto NO envía nada. Envíelo usted y, si es cobranza, anote el envío en workspace/cartera/cartera.csv.")
            return 0
        if a.cmd == "rechazar":
            print("registrado en", decidir(a.id, "rechazado", raiz, motivo=a.motivo))
            return 0
        if a.cmd == "cerrar":
            print("archivado en", cerrar(a.id, raiz))
            return 0
        if a.cmd == "incidente":
            print("incidente registrado en", incidente(a.id, a.motivo, raiz))
            return 0
        if a.cmd == "resumen":
            r = resumen(raiz, dias=a.dias)
            print(
                f"Últimos {r['dias']} días (criterio propuesto: >={CRITERIO['casos']} casos, "
                f">={CRITERIO['tasa_sin_edicion_pct']}% sin edición, {CRITERIO['incidentes']} incidentes, sin borradores sin sello)"
            )
            for nombre, c in sorted(r["categorias"].items()):
                tasa = "n/d" if c["tasa_sin_edicion"] is None else f"{c['tasa_sin_edicion'] * 100:.0f}%"
                print(
                    f"  {nombre:<18} decididos={c['decididos']} aprobados={c['aprobados']} sin_edicion={c['sin_edicion']} "
                    f"editados={c['editados']} sin_sello={c['sin_sello']} rechazados={c['rechazados']} "
                    f"incidentes={c['incidentes']} tasa={tasa}  criterio numérico: {'CUMPLE' if c['cumple_criterio_numerico'] else 'no'}"
                )
            if not r["categorias"]:
                print("  (sin decisiones en el periodo)")
            if not r["cadena_ok"]:
                print("  ATENCIÓN: la cadena de decisiones está ROTA: nada puede dar CUMPLE hasta revisarla a mano")
            if r["truncado"]:
                print("  ATENCIÓN: hay demasiados registros y se truncó la lectura: nada puede dar CUMPLE")
            if not r["integro"]:
                print(
                    f"  ATENCIÓN: {r['registros_ilegibles']} registro(s) ilegible(s), {r['registros_alterados']} alterado(s), "
                    f"{r['registros_sin_evento_en_cadena']} sin evento en la cadena y {r['eventos_sin_archivo']} decisión(es) "
                    "de la cadena SIN archivo (¿borrado?): nada puede dar CUMPLE hasta revisarlo a mano"
                )
            print("Informativo: subir de nivel lo decide una persona; nada cambia solo.")
            return 0
    except (ValueError, OSError, RuntimeError, nocontacto.NoContactoInvalido) as e:
        print("error:", e, file=sys.stderr)
        return 1
    print(__doc__)
    return 2


def _salida_segura():
    """La consola de Windows suele ser cp1252: un carácter que no codifica (emojis del texto) no debe tumbar el comando."""
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    _salida_segura()
    sys.exit(main(sys.argv))
