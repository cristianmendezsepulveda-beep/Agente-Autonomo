"""Lista de no contacto. Solo biblioteca estándar.

Qué es: una lista de correos y dominios a los que el agente NO debe contactar ni registrar como clientes potenciales. Solo puede
FRENAR: nada aquí amplía lo que el resto del código permite, y ante cualquier duda (archivo ausente o ilegible, línea inválida,
destinatario con formato dudoso) responde "bloqueado".

Dos fuentes con dueños distintos:
- Archivo de la PERSONA (`%LOCALAPPDATA%\\agente-local\\no_contactar.txt`, fuera del proyecto y del pin): un correo (`ana@empresa.com`)
  o un dominio (`@empresa.com`, que incluye sus subdominios) por línea; `#` para comentarios. DEBE existir (puede estar vacío): si falta
  se deniega, no se asume "lista vacía". Una línea inválida invalida TODO el archivo (no se ignora) y se dice cuál es.
- Archivo AUTOMÁTICO (`estado/agente/no_contactar_auto.json`): lo escribe solo el código (bajas detectadas en correos verificados).
  Solo añade. Si está corrupto se deniega y NO se sobrescribe.
Comparación: NFKC, minúsculas y sin `+etiqueta` en la parte local (`ana+x@gmail.com` == `ana@gmail.com`: bloquea de más, nunca de menos).

LÍMITES: no sabe si alguien pidió la baja por un canal que el agente no ve; no sincroniza con HubSpot; quien pueda escribir el archivo de
la persona o el estado puede vaciar la lista (el agente no puede: solo escribe borradores y escalados por el MCP).
"""
import json
import os
import re
import sys
import unicodedata
import uuid

MAX_BYTES = 1_000_000
MAX_LINEAS = 20_000
MAX_ENTRADAS = 20_000
MAX_BYTES_AUTO = 12_000_000  # 20.000 entradas de hasta ~350 bytes caben de sobra: la lista automática llena NO debe volverse ilegible
_RE_DOM = r"(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
_RE_CORREO = re.compile(r"([a-z0-9._%+\-]{1,64})@(" + _RE_DOM + ")", re.ASCII)
_RE_DOMINIO = re.compile(r"@(" + _RE_DOM + ")", re.ASCII)


def ruta_persona_por_defecto():
    """`%LOCALAPPDATA%\\agente-local\\no_contactar.txt`, o None si LOCALAPPDATA no es una ruta absoluta (entonces se deniega)."""
    local = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(local, "agente-local", "no_contactar.txt") if os.path.isabs(local) else None


class NoContactoInvalido(Exception):
    """Lista ilegible, corrupta o inválida: las herramientas que contactan deben denegarse."""


class NoContactoNoConfigurado(NoContactoInvalido):
    """No existe el archivo de la persona."""


def normalizar(email):
    """Forma canónica de un correo, o ValueError si no tiene un formato aceptable."""
    if not isinstance(email, str) or len(email) > 300:
        raise ValueError("correo inválido")
    v = unicodedata.normalize("NFKC", email).strip().lower()
    m = _RE_CORREO.fullmatch(v) if len(v) <= 254 else None
    if not m:
        raise ValueError("correo inválido")
    local, dominio = m.group(1).split("+")[0], _dominio_canonico(m.group(2))
    if dominio == "gmail.com":
        local = local.replace(".", "")  # Gmail ignora los puntos: a.na@ y ana@ son el mismo buzón
    if not local:
        raise ValueError("correo inválido")
    return f"{local}@{dominio}"


def _dominio_canonico(dominio):
    return "gmail.com" if dominio == "googlemail.com" else dominio  # googlemail.com es Gmail


def _entrada(linea):
    """('correo'|'dominio', valor) de una línea ya sin espacios, o ValueError."""
    v = unicodedata.normalize("NFKC", linea).strip().lower()
    if v.startswith("@"):
        m = _RE_DOMINIO.fullmatch(v)
        if not m:
            raise ValueError("dominio inválido")
        return "dominio", _dominio_canonico(m.group(1))
    return "correo", normalizar(v)


def _limpiar(texto, tope):
    t = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(texto))
    return " ".join(t.split())[:tope]


class Lista:
    def __init__(self, correos, dominios):
        self._correos, self._dominios = frozenset(correos), frozenset(dominios)

    def bloquea(self, email):
        """True si hay que NO contactar a `email`. Un destinatario con formato dudoso se considera bloqueado."""
        try:
            n = normalizar(email)
        except ValueError:
            return True
        if n in self._correos:
            return True
        partes = n.split("@", 1)[1].split(".")
        return any(".".join(partes[i:]) in self._dominios for i in range(len(partes) - 1))


def _leer_texto(ruta, maximo=MAX_BYTES):
    if os.path.islink(ruta) or not os.path.isfile(ruta):
        raise NoContactoInvalido("no es un archivo normal")
    try:
        with open(ruta, "rb") as f:
            datos = f.read(maximo + 1)
    except OSError as e:
        raise NoContactoInvalido(f"no se pudo leer ({type(e).__name__})")
    if len(datos) > maximo:
        raise NoContactoInvalido("archivo demasiado grande")
    try:
        return datos.decode("utf-8-sig")
    except UnicodeError:
        if datos.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise NoContactoInvalido("el archivo parece UTF-16 (p. ej. creado con `echo` en PowerShell 5): guárdelo como UTF-8")
        raise NoContactoInvalido("no es texto UTF-8")


def _leer_persona(ruta):
    if not os.path.lexists(ruta):
        raise NoContactoNoConfigurado("falta el archivo de no contacto: créelo (puede estar vacío, con una línea de comentario)")
    lineas = _leer_texto(ruta).splitlines()
    if len(lineas) > MAX_LINEAS:
        raise NoContactoInvalido(f"más de {MAX_LINEAS} líneas")
    correos, dominios = set(), set()
    for n, linea in enumerate(lineas, 1):
        s = linea.strip()
        if not s or s.startswith("#"):
            continue
        try:
            tipo, valor = _entrada(s)
        except ValueError:
            raise NoContactoInvalido(f"línea {n} inválida (use un correo o @dominio)")
        (correos if tipo == "correo" else dominios).add(valor)
    return correos, dominios


def _leer_auto(ruta):
    """dict {correo: {...}} del archivo automático. Ausente = vacío. Corrupto = NoContactoInvalido."""
    if not os.path.lexists(ruta):
        return {}
    try:
        d = json.loads(_leer_texto(ruta, MAX_BYTES_AUTO))
    except (ValueError, RecursionError):
        raise NoContactoInvalido("el archivo automático no es JSON válido")
    if not isinstance(d, dict) or not set(d) <= {"version", "entradas"} or d.get("version", 1) != 1 \
            or not isinstance(d.get("entradas"), dict) or len(d["entradas"]) > MAX_ENTRADAS:
        raise NoContactoInvalido("el archivo automático tiene una estructura inesperada")
    for k, v in d["entradas"].items():
        try:
            ok = isinstance(v, dict) and normalizar(k) == k
        except ValueError:
            ok = False
        if not ok:
            raise NoContactoInvalido("el archivo automático tiene una entrada inválida")
    return d["entradas"]


def entradas_auto(ruta_auto):
    """{correo: {"motivo", "fecha"}} de la lista automática, para que una persona la revise (falsos positivos de bajas). NoContactoInvalido si
    está corrupta."""
    return _leer_auto(ruta_auto)


def _ruta_bloqueo(ruta_auto):
    return ruta_auto + ".bloqueo"


def bloqueo_activo(ruta_auto):
    """True si hay bajas pedidas por correo que NO se pudieron escribir en la lista automática."""
    return os.path.lexists(_ruta_bloqueo(ruta_auto))


def marcar_bloqueo(ruta_auto, n, ahora):
    """Deja constancia de que `n` bajas no se pudieron registrar: mientras exista este archivo `cargar` falla cerrado y NADA que contacte o
    registre personas funciona. Una persona debe revisar la lista (¿llena? ¿corrupta? ¿sin permisos?) y borrar el archivo. Acumula la cuenta
    y conserva la fecha de la primera vez. Puede lanzar OSError (el llamador conserva además el bloqueo en memoria)."""
    sandbox = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")
    if sandbox not in sys.path:
        sys.path.insert(0, sandbox)
    from _lock import bloqueo
    ruta = _ruta_bloqueo(ruta_auto)
    os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
    with bloqueo(ruta):
        previo = {"bajas_sin_registrar": 0, "desde": ahora}
        try:
            with open(ruta, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and type(d.get("bajas_sin_registrar")) is int and d["bajas_sin_registrar"] >= 0:
                previo = {"bajas_sin_registrar": d["bajas_sin_registrar"], "desde": d.get("desde", ahora)}
        except (OSError, ValueError):
            pass  # ausente o corrupto: lo único que importa es que el archivo exista
        previo["bajas_sin_registrar"] += int(n)
        tmp = f"{ruta}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(previo, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ruta)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def cargar(ruta_persona, ruta_auto):
    """Lista efectiva (persona + automática). Lanza NoContactoNoConfigurado o NoContactoInvalido (fallo cerrado)."""
    if bloqueo_activo(ruta_auto):
        raise NoContactoInvalido("hay bajas sin registrar en la lista automática: una persona debe revisarla y borrar "
                                 f"{os.path.basename(_ruta_bloqueo(ruta_auto))}")
    correos, dominios = _leer_persona(ruta_persona)
    correos |= set(_leer_auto(ruta_auto))
    return Lista(correos, dominios)


def agregar_auto(ruta_auto, email, motivo, ahora):
    """Añade `email` a la lista automática (solo añade; idempotente). Devuelve True si era nuevo. Con el archivo corrupto o lleno
    lanza NoContactoInvalido y no toca nada. Seguro entre procesos e hilos."""
    n = normalizar(email)
    sandbox = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")
    if sandbox not in sys.path:
        sys.path.insert(0, sandbox)
    from _lock import bloqueo
    os.makedirs(os.path.dirname(os.path.abspath(ruta_auto)), exist_ok=True)
    with bloqueo(ruta_auto):
        entradas = _leer_auto(ruta_auto)
        if n in entradas:
            return False
        if len(entradas) >= MAX_ENTRADAS:
            raise NoContactoInvalido("la lista automática está llena: una persona debe revisarla")
        entradas[n] = {"motivo": _limpiar(motivo, 60), "fecha": ahora}
        tmp = f"{ruta_auto}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "entradas": entradas}, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ruta_auto)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return True
