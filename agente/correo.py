"""Lectura de correo por IMAP (SOLO LECTURA) y captación de clientes nuevos. Solo biblioteca estándar.

Qué es y qué NO es:
- Lee INBOX con `EXAMINE` (solo lectura) y `BODY.PEEK`: no marca leído, no mueve, no borra, no envía. Trae cabeceras (≤16 KB) y como
  máximo 8 KB del cuerpo; nunca adjuntos ni HTML. Conexión TLS con verificación de certificado (puerto 993 por defecto).
- El correo entrante es texto NO CONFIABLE. Al modelo solo llegan un alias opaco, el asunto y un extracto limpiados (sin correos ni
  números largos, HTML escapado, URLs neutralizadas). **Nunca recibe el remitente**: `captar` lo lee el código de la cabecera real.
- Descarte automático por código: remitentes `noreply`/`mailer-daemon`, mensajes de listas (`List-Id`), automáticos (`Auto-Submitted`
  distinto de `no`, `Precedence` bulk/list/junk), dominios propios, más de un `From`, y los que no traen DKIM o SPF en `pass` ALINEADO
  con el dominio del remitente en la cabecera `Authentication-Results` MÁS RECIENTE (la primera): sin eso el remitente es falsificable.
- Credenciales en un archivo fuera del proyecto (lo crea una persona); la clave no se imprime ni registra, y los errores no llevan
  respuestas del servidor.
- Estado local (`correo_estado.json`): UIDVALIDITY, UIDs ya procesados, alias pendientes y topes (60 lecturas/h, 20 captaciones/h).
  No toca el servidor. Si el estado es ilegible, falla cerrado.
NO verificado contra un servidor IMAP real: el formato de `Authentication-Results` de cada proveedor, límites del servidor, `UIDVALIDITY`.
Si el proveedor no añade esa cabecera, NINGÚN mensaje se considera verificado y no se registra nada.
"""
import datetime
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import ssl
import time
import unicodedata

import crm
import nocontacto
import revision
from cotizador import limpiar_texto

RUTA_CONFIG = os.path.join(os.environ["LOCALAPPDATA"], "agente-local", "imap.json") if os.path.isabs(os.environ.get("LOCALAPPDATA", "")) else None
MAX_CABECERA, MAX_CUERPO = 16384, 8192
MAX_POR_LLAMADA, MAX_CANDIDATOS, VENTANA_DIAS = 10, 30, 7
MAX_LECT_HORA, MAX_CAPT_HORA = 60, 20
MAX_PROCESADOS, MAX_PENDIENTES = 5000, 50
MAX_SEGUNDOS = 45  # tope de tiempo por llamada a correo_nuevos (el timeout de 20 s es por operación)
MESES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
ORIGENES_CORREO = ("consulta", "cotizacion", "referido", "otro")
_RE_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,252}")
_RE_UID = re.compile(r"\d{1,10}")
_RE_REF = re.compile(r"m-[a-z]{10}")  # solo letras: una referencia con dígitos podría ser ocultada por limpiar_texto al escalar
_RE_AUTOMATICO = re.compile(r"(?i)(^|[._\-+])(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?|notifications?)([._\-+@]|$)")
_RE_DOMINIO = re.compile(r"[a-z0-9][a-z0-9.\-]{0,252}")
_PRECEDENCIA_MALA = {"bulk", "list", "junk"}
_RE_PROP = re.compile(r"[a-z]+\.[a-z0-9\-]+")
_RE_VALOR = re.compile(r"[a-z0-9.@_+\-<>/=:]+")  # "/" y "=" por las firmas base64 (header.b=…); ":" por las IP v6
_RE_LINEA_CAB = re.compile(r"[!-9;-~]+:")  # nombre de cabecera ASCII seguido de ":"


def _estructura_ok(texto):
    """True si el bloque de cabeceras es limpio: sin separadores de línea raros y cada línea es una cabecera o un pliegue.
    Una línea suelta sin ":" haría que el parser tratara el resto como cuerpo y dejara fuera cabeceras como List-Id."""
    if re.search(r"[\x0b\x0c\x1c-\x1e\x85  ]|\r(?!\n)", texto):
        return False  # incluye un \r suelto: el parser de `email` lo trata como fin de línea y nosotros no
    for linea in re.split(r"\r?\n", texto):
        if not linea:
            break  # fin del bloque de cabeceras
        if linea[0] in " \t":
            continue
        if not _RE_LINEA_CAB.match(linea):
            return False
    return True


class CorreoNoConfigurado(RuntimeError):
    """Falta o es inválido el archivo de credenciales."""


class CorreoFallo(RuntimeError):
    """El servidor IMAP no respondió bien. El mensaje nunca incluye respuestas del servidor ni la clave."""


class CorreoEstadoInvalido(RuntimeError):
    """El estado local es ilegible o inconsistente: se falla cerrado."""


class CorreoTope(RuntimeError):
    """Tope de lecturas o captaciones por hora alcanzado."""


# ------------------------------------------------------------------ configuración
def leer_config(ruta=None):
    ruta = ruta or RUTA_CONFIG
    if not ruta:
        raise CorreoNoConfigurado("no se pudo determinar la ruta del archivo de credenciales")
    try:
        with open(ruta, "rb") as f:
            crudo = f.read(20001)
        if len(crudo) > 20000:
            raise ValueError
        c = json.loads(crudo.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise CorreoNoConfigurado("no hay archivo de credenciales legible")
    if not isinstance(c, dict):
        raise CorreoNoConfigurado("formato inválido")
    host, usuario, clave = c.get("host"), c.get("usuario"), c.get("clave")
    puerto = c.get("puerto", 993)
    propios = c.get("dominios_propios", [])
    authserv = c.get("authserv_id")  # nombre con el que TU servidor firma Authentication-Results (p. ej. "mx.google.com"); sin él no se verifica nada
    ok = (isinstance(host, str) and _RE_HOST.fullmatch(host) and isinstance(usuario, str) and 1 <= len(usuario) <= 254
          and (authserv is None or (isinstance(authserv, str) and _RE_HOST.fullmatch(authserv)))
          and isinstance(clave, str) and 1 <= len(clave) <= 200 and type(puerto) is int and 1 <= puerto <= 65535
          and isinstance(propios, list) and len(propios) <= 50 and all(isinstance(d, str) and _RE_DOMINIO.fullmatch(d.lower()) for d in propios))
    if not ok or any(ord(ch) < 32 or ord(ch) == 127 for ch in usuario + clave):
        raise CorreoNoConfigurado("credenciales con formato inválido")
    return {"host": host, "puerto": puerto, "usuario": usuario, "clave": clave, "dominios_propios": [d.lower() for d in propios],
            "authserv_id": authserv.lower() if authserv else None}


# ------------------------------------------------------------------ texto no confiable
def _libre(texto, maximo):
    texto = unicodedata.normalize("NFKC", texto)  # "＠" de ancho completo, etc., pasan a su forma normal antes de ocultar correos y números
    t = html.escape(limpiar_texto(texto, maximo), quote=False)
    return re.sub(r"(?i)\b(https?|ftp)://", lambda m: m.group(1) + "[:]//", t)[:maximo]  # el tope se aplica DESPUÉS de escapar


def _dominio(addr):
    return addr.rsplit("@", 1)[-1].lower()


def _alineado(dom_from, valores):
    """Alineación ESTRICTA: el dominio firmante/del sobre debe ser exactamente el del remitente (sin dominios padre: sin lista de sufijos
    públicos un proveedor compartido daría por válido cualquier subdominio). Un remitente legítimo que firma con otro dominio no se registra."""
    for v in valores:
        v = v.strip().strip("<>").lower().lstrip("@").rstrip(".")
        v = v.rsplit("@", 1)[-1]
        if v and v == dom_from:
            return True
    return False


MAX_ANIDAMIENTO = 3  # un resultado de autenticación real anida como mucho 2 niveles de comentarios


def _sin_comentarios(s):
    """Quita los comentarios entre paréntesis (RFC 8601): un tercero puede colar texto como 'dkim=pass' dentro de uno. Lanza ValueError si
    anidan más de MAX_ANIDAMIENTO niveles: sin ese tope, miles de paréntesis anidados harían miles de pasadas por cabecera (bloqueo de CPU)."""
    nivel = 0
    for ch in s:  # una pasada lineal ANTES del bucle de eliminación
        if ch == "(":
            nivel += 1
            if nivel > MAX_ANIDAMIENTO:
                raise ValueError("comentarios demasiado anidados")
        elif ch == ")" and nivel > 0:
            nivel -= 1
    anterior = None
    while anterior != s:
        anterior, s = s, re.sub(r"\([^()]*\)", " ", s)
    return s.replace("(", " ").replace(")", " ")


def _parentesis_balanceados(s):
    nivel = 0
    for ch in s:
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel -= 1
            if nivel < 0:
                return False
    return nivel == 0


def verificado(auth_resultados, dominio_from, authserv_id=None):
    """PURO. DKIM o SPF en `pass` y alineados con el dominio del remitente, según UNA cabecera Authentication-Results cuyo authserv-id
    (primer token) sea el del servidor propio configurado. Sin `authserv_id` configurado no se verifica NADA."""
    if not auth_resultados or not authserv_id:
        return False
    if '"' in auth_resultados or "\\" in auth_resultados:
        return False  # cadenas entrecomilladas (RFC 8601): el remitente puede colar ahí texto como "dkim=pass header.d=…"; se rechaza TODA la cabecera
    if "=?" in auth_resultados:
        return False  # palabra codificada (RFC 2047): una cabecera de autenticación es ASCII plano; decodificada podría fabricar `dkim=pass …`
    if not auth_resultados.isascii() or re.search(r"[\x00-\x08\x0a-\x1f\x7f]", auth_resultados) or not _parentesis_balanceados(auth_resultados):
        return False  # espacios Unicode, controles o paréntesis sueltos permiten esconder o partir tokens: se rechaza TODA la cabecera
    try:
        s = _sin_comentarios(auth_resultados).lower()
    except ValueError:
        return False  # comentarios anidados de forma absurda: se rechaza TODA la cabecera
    primero = [t for t in re.split(r"[ \t]+", s.split(";", 1)[0]) if t]
    if not primero or primero[0] != authserv_id.lower():
        return False
    # Cada resultado (separado por ";") se evalúa por sí solo y debe EMPEZAR por `dkim=pass` o `spf=pass`; todo `ptype.propiedad=valor`
    # que lo siga debe tener un valor de caracteres seguros y no repetirse. Se separa SOLO por espacio y tabulador.
    for clausula in s.split(";")[1:]:
        toks = [t for t in re.split(r"[ \t]+", clausula) if t]
        if not toks or toks[0] not in ("dkim=pass", "spf=pass"):
            continue
        props, valido = {}, True
        for t in toks[1:]:
            k, _, v = t.partition("=")
            if not _RE_PROP.fullmatch(k) or not v or not _RE_VALOR.fullmatch(v) or k in props:
                valido = False  # token raro, sin valor, o propiedad repetida
                break
            props[k] = v
        if not valido:
            continue
        if toks[0] == "spf=pass":
            alineado = _alineado(dominio_from, [props["smtp.mailfrom"]]) if "smtp.mailfrom" in props else False
        elif "header.d" in props:
            # si existe `d=` es el que manda (la alineación DMARC usa solo `d=`); `i=` lo escribe el firmante y NO puede alinear por sí solo
            alineado = "@" not in props["header.d"] and _alineado(dominio_from, [props["header.d"]])
        else:
            alineado = _alineado(dominio_from, [props["header.i"]]) if "header.i" in props else False  # Gmail: header.i=@dominio
        if alineado:
            return True
    return False


def analizar(cabeceras, propios=(), authserv_id=None):
    """PURO. De los bytes de cabecera devuelve (dict {email, nombre, asunto, verificado}, None) o (None, razón de descarte)."""
    try:
        # se decodifica como UTF-8 (con reemplazo) ANTES de analizar: con message_from_bytes, un encabezado con bytes 8-bit sin
        # codificar (SMTPUTF8) saldría como sustitutos inválidos (\udcXX) que romperían el nombre y el asunto
        if len(cabeceras) >= MAX_CABECERA:
            return None, "ilegible"  # cabeceras truncadas: las que quedaron fuera (List-Id, Auto-Submitted, otro From…) no se ven
        texto = cabeceras.decode("utf-8", "replace")
        if not _estructura_ok(texto):
            return None, "ilegible"
        msg = email.message_from_string(texto, policy=email.policy.default)
        desde = msg.get_all("From") or []
        if len(desde) != 1:
            return None, "remitente"
        dirs = desde[0].addresses
        if len(dirs) != 1:
            return None, "remitente"
        direccion, nombre = dirs[0].addr_spec, dirs[0].display_name or ""
        try:
            direccion = crm.validar_email(direccion)
            nocontacto.normalizar(direccion)  # el CRM acepta `+a@x.com` o `ana@-x.com` y la lista no los entiende: un remitente que la lista no
            # puede comparar no se capta ni puede dar de baja a nadie (su baja fallaría y acapararía la ventana de candidatos)
        except ValueError:
            return None, "remitente"
        if _RE_AUTOMATICO.search(direccion.split("@")[0]):
            return None, "automatico"
        if msg.get("List-Id") or msg.get("List-Unsubscribe-Post"):
            return None, "automatico"
        # TODAS las apariciones: con una sola cabecera "Auto-Submitted: no" delante, otra "auto-generated" pasaba el filtro
        if any(str(a).strip().lower() != "no" for a in (msg.get_all("Auto-Submitted") or [])):
            return None, "automatico"
        if any(str(p).strip().lower() in _PRECEDENCIA_MALA for p in (msg.get_all("Precedence") or [])):
            return None, "automatico"
        dom = _dominio(direccion)
        if any(dom == p or dom.endswith("." + p) for p in propios):
            return None, "propio"
        # SIN decodificar: `msg.get_all()` con la política por defecto decodifica palabras RFC 2047 (`=?utf-8?q?=29=3B…?=`) y un MAIL FROM del
        # atacante, que el servidor copia en un comentario, fabricaría `dkim=pass header.d=<dominio de la víctima>`. Se leen los valores en
        # crudo y solo se desdoblan las líneas plegadas.
        ars = [re.sub(r"\r?\n[ \t]+", " ", v) for k, v in msg.raw_items() if k.lower() == "authentication-results"]
        propias = [a for a in ars if authserv_id and (_sin_comentarios(a).split(";", 1)[0].split() or [""])[0].lower() == authserv_id.lower()]
        # el servidor propio añade UNA cabecera; si hay otra con su mismo authserv-id es una imitación: no se verifica
        ver = len(propias) == 1 and ars[0] == propias[0] and verificado(propias[0], dom, authserv_id)
        return {"email": direccion, "nombre": str(nombre), "asunto": str(msg.get("Subject", "")), "verificado": ver}, None
    except Exception:  # cabeceras malformadas de un tercero: se descartan, no se propagan
        return None, "ilegible"


def extracto(cabeceras, cuerpo):
    """PURO. Primeros ~300 caracteres de la parte text/plain, ya limpios. Vacío si no se puede leer."""
    try:
        msg = email.message_from_bytes(cabeceras.rstrip() + b"\r\n\r\n" + cuerpo, policy=email.policy.default)
        parte = msg.get_body(preferencelist=("plain",))
        if parte is None:
            return ""
        lineas = [l for l in parte.get_content().splitlines() if not l.lstrip().startswith(">")]
        return _libre(" ".join(" ".join(lineas).split()).replace("�", ""), 300)
    except Exception:
        return ""


# ------------------------------------------------------------------ bajas ("no me contacten")
MAX_TEXTO_BAJA = 2000  # caracteres del asunto + cuerpo que se examinan
# Frases YA normalizadas (sin acentos, minúsculas, solo [a-z0-9] separados por un espacio). Heurística conservadora: un falso positivo solo
# bloquea el contacto con esa persona hasta que alguien edite la lista; un falso negativo no la bloquea (límite conocido: no cubre
# letras separadas "b a j a", otros idiomas, ni bajas pedidas por imagen o adjunto).
_FRASES_BAJA = (
    "darme de baja", "darnos de baja", "dar de baja mi", "quiero la baja", "solicito la baja", "solicito mi baja", "solicitamos la baja",
    "baja de su lista", "baja de la lista", "baja de sus listas", "no me contacten", "no me contacte", "no me escriban", "no me escriba",
    "no me llamen", "no me llame", "no quiero recibir", "no deseo recibir", "no queremos recibir", "no quiero que me contacten",
    "dejen de escribirme", "dejen de contactarme", "dejen de enviarme", "dejen de llamarme", "deje de escribirme", "deje de enviarme",
    "deje de contactarme", "borrenme de", "eliminenme de", "eliminen mi correo", "eliminar mi correo", "cancelar suscripcion",
    "cancelar mi suscripcion", "cancelen mi suscripcion", "no mas correos", "no volver a contactarme", "no vuelvan a contactarme",
    "no me vuelvan a escribir",
    "unsubscribe", "remove me", "stop emailing", "stop contacting", "stop sending", "do not contact", "don t contact", "do not email",
    "don t email", "dont email", "opt out", "optout", "take me off", "remove my email", "remove my address",
    "no longer wish to receive", "no longer want to receive",
)


def _norm_baja(texto):
    t = unicodedata.normalize("NFKC", texto).casefold()
    t = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) not in ("Mn", "Cf"))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def detectar_baja(asunto, cuerpo, cabeceras):
    """True si el asunto o el comienzo del cuerpo (sin líneas citadas) piden no ser contactados. PURO y determinista; nunca lanza."""
    try:
        msg = email.message_from_bytes(cabeceras.rstrip() + b"\r\n\r\n" + cuerpo, policy=email.policy.default)
        parte = msg.get_body(preferencelist=("plain",))
        if parte is not None:
            lineas = [l for l in parte.get_content().splitlines() if not l.lstrip().startswith(">")]
        else:  # correo solo HTML: se examina el texto visible (sin etiquetas ni atributos ni citas)
            parte = msg.get_body(preferencelist=("html",))
            crudo = parte.get_content()[:20000] if parte is not None else ""
            crudo = re.sub(r"(?is)<blockquote.{0,5000}?</blockquote>", " ", crudo)
            lineas = html.unescape(re.sub(r"<[^>]{0,300}>", " ", crudo)).splitlines()
        texto = _norm_baja(str(asunto)[:300] + " " + " ".join(lineas)[:MAX_TEXTO_BAJA])
        return any(f" {f} " in f" {texto} " for f in _FRASES_BAJA)
    except Exception:
        return False


def nombre_para_lead(nombre):
    try:
        n = nombre.strip()
        if limpiar_texto(n, 80) != n:  # contiene números largos, correos o caracteres de formato: no se sube a HubSpot
            return "Cliente por correo"
        return crm.validar_nombre(n)
    except ValueError:
        return "Cliente por correo"


# ------------------------------------------------------------------ estado local
def cargar_estado(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            e = json.load(f)
    except FileNotFoundError:
        return {"uidvalidity": None, "procesados": [], "pendientes": {}, "hora": "", "lecturas": 0, "captaciones": 0}
    except (OSError, ValueError):
        raise CorreoEstadoInvalido("estado local ilegible")
    ok = (isinstance(e, dict) and isinstance(e.get("procesados"), list) and isinstance(e.get("pendientes"), dict)
          and all(type(u) is int and u >= 0 for u in e["procesados"])
          and all(isinstance(k, str) and _RE_REF.fullmatch(k) and type(v) is int and v >= 0 for k, v in e["pendientes"].items())
          and type(e.get("lecturas")) is int and e["lecturas"] >= 0 and type(e.get("captaciones")) is int and e["captaciones"] >= 0
          and isinstance(e.get("hora"), str) and (e.get("uidvalidity") is None or type(e["uidvalidity"]) is int))
    if not ok:
        raise CorreoEstadoInvalido("estado local inválido")
    return e


def guardar_estado(ruta, e):
    e["procesados"] = e["procesados"][-MAX_PROCESADOS:]
    if len(e["pendientes"]) > MAX_PENDIENTES:
        e["pendientes"] = dict(list(e["pendientes"].items())[-MAX_PENDIENTES:])
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = f"{ruta}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(e, f)
    os.replace(tmp, ruta)


def _cobrar(e, tipo, ahora):
    """Cobra una operación del tope horario ANTES de hacerla (falla cerrado)."""
    hora = datetime.datetime.fromtimestamp(ahora, datetime.timezone.utc).strftime("%Y-%m-%dT%H")
    if e["hora"] != hora:
        e["hora"], e["lecturas"], e["captaciones"] = hora, 0, 0
    tope = MAX_LECT_HORA if tipo == "lecturas" else MAX_CAPT_HORA
    if e[tipo] + 1 > tope:
        raise CorreoTope("tope por hora alcanzado")
    e[tipo] += 1


# ------------------------------------------------------------------ IMAP
class Buzon:
    """Sesión IMAP de solo lectura. `fabrica(host, puerto)` se inyecta en pruebas con un buzón falso."""

    def __init__(self, cfg, fabrica=None):
        self.cfg, self._fabrica, self._c = cfg, fabrica, None

    def __enter__(self):
        try:
            self._c = self._fabrica(self.cfg["host"], self.cfg["puerto"]) if self._fabrica else imaplib.IMAP4_SSL(
                self.cfg["host"], self.cfg["puerto"], ssl_context=ssl.create_default_context(), timeout=20)
            self._c.login(self.cfg["usuario"], self.cfg["clave"])
            typ, _ = self._c.select("INBOX", readonly=True)
            if typ != "OK":
                raise CorreoFallo("no se pudo abrir la bandeja")
        except CorreoFallo:
            self._cerrar()
            raise
        except (imaplib.IMAP4.error, OSError, ssl.SSLError, ValueError):
            self._cerrar()
            raise CorreoFallo("no se pudo conectar al servidor de correo")
        return self

    def __exit__(self, *a):
        self._cerrar()

    def _cerrar(self):
        try:
            if self._c is not None:
                self._c.logout()
        except Exception:
            pass
        self._c = None

    def uidvalidity(self):
        try:
            typ, datos = self._c.response("UIDVALIDITY")
            v = int(datos[0]) if typ == "UIDVALIDITY" and datos and datos[0] else None
        except (imaplib.IMAP4.error, OSError, ValueError, TypeError, IndexError):
            raise CorreoFallo("respuesta inválida del servidor")
        if v is None or v < 0:
            raise CorreoFallo("respuesta inválida del servidor")
        return v

    def buscar(self, ahora, dias=VENTANA_DIAS):
        d = datetime.datetime.fromtimestamp(ahora - dias * 86400, datetime.timezone.utc)
        fecha = f"{d.day:02d}-{MESES[d.month - 1]}-{d.year}"
        try:
            typ, datos = self._c.uid("SEARCH", None, "SINCE", fecha)
            if typ != "OK" or not datos:
                raise CorreoFallo("respuesta inválida del servidor")
            uids = [int(x) for x in (datos[0] or b"").split() if _RE_UID.fullmatch(x.decode("ascii", "replace"))]
        except CorreoFallo:
            raise
        except (imaplib.IMAP4.error, OSError, ValueError, AttributeError):
            raise CorreoFallo("respuesta inválida del servidor")
        return sorted(set(uids), reverse=True)

    def _fetch(self, uid, que, maximo, fallo=b""):
        if type(uid) is not int or uid < 0:
            raise CorreoFallo("uid inválido")
        try:
            typ, datos = self._c.uid("FETCH", str(uid), f"(BODY.PEEK[{que}]<0.{maximo}>)")
            if typ != "OK" or not datos or not isinstance(datos[0], tuple) or not isinstance(datos[0][1], (bytes, bytearray)):
                return fallo  # respuesta fallida o mal formada (distinta de un cuerpo realmente vacío, que es b"")
            return bytes(datos[0][1])[:maximo]
        except (imaplib.IMAP4.error, OSError, ValueError, TypeError):
            raise CorreoFallo("respuesta inválida del servidor")

    def cabeceras(self, uid):
        return self._fetch(uid, "HEADER", MAX_CABECERA)

    def cuerpo(self, uid):
        """Bytes del cuerpo (b"" si de verdad está vacío) o None si el servidor no lo entregó bien: así un fallo no se confunde con "sin cuerpo"."""
        return self._fetch(uid, "TEXT", MAX_CUERPO, fallo=None)


# ------------------------------------------------------------------ operaciones
def _ref(uidvalidity, uid):
    d = hashlib.sha256(f"{uidvalidity}:{uid}".encode()).digest()
    return "m-" + "".join(chr(ord("a") + b % 26) for b in d[:10])


def nuevos(cfg, ruta_estado, ahora, fabrica=None, baja=None):
    """Hasta MAX_POR_LLAMADA mensajes nuevos y verificados, SIN remitente. Devuelve dict.
    `baja(email)` (lista de no contacto): si se pasa, un mensaje VERIFICADO que pide no ser contactado se registra con ella, NO se lista
    y se da por procesado; si `baja` falla, el mensaje tampoco se lista pero se reintentará. Un mensaje sin verificar nunca da de baja a nadie."""
    e = cargar_estado(ruta_estado)
    _cobrar(e, "lecturas", ahora)
    guardar_estado(ruta_estado, e)  # el cobro queda aunque falle la conexión
    salida, auto, sin_ver, no_leidos, motivos, bajas, bajas_fallidas, baja_rota = [], 0, 0, 0, {}, 0, 0, False
    inicio = time.monotonic()
    with Buzon(cfg, fabrica) as b:
        uv = b.uidvalidity()
        if e["uidvalidity"] != uv:
            e["uidvalidity"], e["procesados"], e["pendientes"] = uv, [], {}
        hechos = set(e["procesados"])
        listados = set(e["pendientes"].values())
        todos = [u for u in b.buscar(ahora) if u not in hechos]
        # primero los que el modelo aún NO ha visto: así un remitente que mande muchos correos verificados y no se resuelvan no acapara la lista
        candidatos = ([u for u in todos if u not in listados] + [u for u in todos if u in listados])[:MAX_CANDIDATOS]
        for uid in candidatos:
            if len(salida) + bajas >= MAX_POR_LLAMADA or time.monotonic() - inicio > MAX_SEGUNDOS:  # las bajas también gastan plazas
                break
            cab = b.cabeceras(uid)
            if not cab:  # respuesta vacía o fallida del servidor: NO se descarta para siempre, se reintenta en otra llamada
                no_leidos += 1
                continue
            datos, razon = analizar(cab, cfg["dominios_propios"], cfg.get("authserv_id"))
            if datos is None:
                auto += 1
                motivos[razon] = motivos.get(razon, 0) + 1
                e["procesados"].append(uid)
            elif not datos["verificado"]:
                sin_ver += 1
                e["procesados"].append(uid)
            else:
                cuerpo = b.cuerpo(uid)
                if cuerpo is None:  # el cuerpo no llegó: sin él no se puede saber si pide la baja; se reintenta en otra llamada
                    no_leidos += 1
                    continue
                if baja is not None and detectar_baja(datos["asunto"], cuerpo, cab):
                    if not baja_rota:
                        try:
                            baja(datos["email"])
                            bajas += 1
                            e["procesados"].append(uid)
                            continue
                        except Exception:
                            baja_rota = True  # la lista no se puede escribir: no se insiste con el resto de esta llamada
                    bajas_fallidas += 1  # no se lista (podría contactarse a quien pidió no serlo) y se reintentará en otra llamada
                    continue
                ref = _ref(uv, uid)
                e["pendientes"][ref] = uid
                # el asunto y el extracto los escribió un TERCERO: salen marcados como datos (mitigación, no garantía)
                salida.append({"ref": ref, "asunto": revision.marcar_externo(_libre(datos["asunto"], 100)),
                               "extracto": revision.marcar_externo(extracto(cab, cuerpo))})
    # releer y FUSIONAR: otro proceso (o `captar`) pudo cambiar el estado mientras hablábamos con el servidor
    actual = cargar_estado(ruta_estado)
    if actual["uidvalidity"] in (None, e["uidvalidity"]):  # None = aún sin inicializar en disco (primera ejecución)
        vistos = set(actual["procesados"])
        actual["procesados"] += [u for u in e["procesados"] if u not in vistos]
        ya = set(actual["procesados"])
        for k, u in e["pendientes"].items():
            if u not in ya:
                actual["pendientes"][k] = u
        actual["uidvalidity"] = e["uidvalidity"]
        for k in [k for k, u in actual["pendientes"].items() if u in ya]:
            del actual["pendientes"][k]
        actual["hora"], actual["lecturas"], actual["captaciones"] = (
            (e["hora"], max(e["lecturas"], actual["lecturas"]), max(e["captaciones"], actual["captaciones"]))
            if e["hora"] >= actual["hora"] else (actual["hora"], actual["lecturas"], actual["captaciones"]))
        e = actual
    guardar_estado(ruta_estado, e)
    res = {"nuevos": salida, "descartados_automaticos": auto, "descartados_por_motivo": motivos, "sin_verificar": sin_ver,
           "no_leidos_por_error": no_leidos}
    if baja is not None:
        res["bajas_registradas"], res["bajas_no_registradas"] = bajas, bajas_fallidas  # solo contadores: sin remitente ni texto
        if bajas_fallidas:
            res["alerta"] = "la lista automática de no contacto no se puede escribir: avise a una persona (hay bajas sin registrar)"
    return res


def captar(cfg, ruta_estado, ahora, ref, origen, registrar_lead, fabrica=None, bloqueado=None):
    """Registra como lead al remitente REAL de un mensaje pendiente. `registrar_lead(nombre, email, origen, resumen)` hace la llamada a HubSpot.
    `bloqueado(email) -> bool` (lista de no contacto): si el remitente está bloqueado NO se registra nada, el mensaje se da por procesado y se
    devuelve `bloqueado_no_contactar` (sin dirección)."""
    if not isinstance(ref, str) or not _RE_REF.fullmatch(ref):
        raise ValueError("ref inválido")
    if origen not in ORIGENES_CORREO:
        raise ValueError("origen debe ser uno de: " + ", ".join(ORIGENES_CORREO))
    e = cargar_estado(ruta_estado)
    uid = e["pendientes"].get(ref)
    if uid is None:
        raise ValueError("ref desconocido o ya procesado: vuelva a pedir correo_nuevos")
    _cobrar(e, "captaciones", ahora)
    guardar_estado(ruta_estado, e)
    with Buzon(cfg, fabrica) as b:
        if b.uidvalidity() != e["uidvalidity"]:
            raise ValueError("la bandeja cambió: vuelva a pedir correo_nuevos")
        datos, razon = analizar(b.cabeceras(uid), cfg["dominios_propios"], cfg.get("authserv_id"))
    if datos is None or not datos["verificado"]:
        raise ValueError("el mensaje ya no cumple las condiciones para captarlo")
    if bloqueado is not None and bloqueado(datos["email"]):
        e = cargar_estado(ruta_estado)
        e["procesados"].append(uid)
        e["pendientes"].pop(ref, None)
        guardar_estado(ruta_estado, e)
        return {"registrado": False, "bloqueado_no_contactar": True}
    resumen = "Escribió por correo: " + _libre(datos["asunto"], 100)
    r = registrar_lead(nombre_para_lead(datos["nombre"]), datos["email"], origen, resumen)
    e = cargar_estado(ruta_estado)
    e["procesados"].append(uid)
    e["pendientes"].pop(ref, None)
    guardar_estado(ruta_estado, e)
    return {"registrado": True, "nuevo": r.get("nuevo"), "contacto": r.get("contacto")}


def descartar(ruta_estado, ref):
    if not isinstance(ref, str) or not _RE_REF.fullmatch(ref):
        raise ValueError("ref inválido")
    e = cargar_estado(ruta_estado)
    uid = e["pendientes"].pop(ref, None)
    if uid is None:
        raise ValueError("ref desconocido o ya procesado")
    e["procesados"].append(uid)
    guardar_estado(ruta_estado, e)
    return {"descartado": True}
