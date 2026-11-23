"""Cliente mínimo de HubSpot (CRM v3) para el MCP local. Solo biblioteca estándar.

Qué es y qué NO es:
- Cuatro operaciones fijas: buscar contacto, registrar lead (contacto + trato), nota y tarea. NO hay borrar, exportar,
  listar ni búsquedas libres. El control real contra esas acciones es el token: debe ser una "private app" SIN alcances de
  borrado ni de exportación (el código no puede impedir lo que el token permita).
- Solo habla con api.hubapi.com por HTTPS (fijado aquí, no en la política), sin seguir redirecciones, con tiempo máximo y
  respuesta acotada. La URL base solo se cambia en pruebas (servidor falso en 127.0.0.1).
- El token se lee de un archivo FUERA del proyecto (lo crea la persona); nunca se imprime, registra ni devuelve. Los mensajes
  de error no incluyen el cuerpo de las respuestas ni el token.
- Topes propios persistidos: 30 escrituras/hora y 100/día (cuenta cada petición de escritura).
- Los textos libres (resumen, nota, borrador) pasan por `limpiar_texto` (sin controles, números largos ni correos): los datos
  personales viajan SOLO en los campos estructurados (nombre, empresa, email, teléfono).
NO verificado contra la API real: los identificadores de asociación (3 trato→contacto, 202 nota→contacto, 204 tarea→contacto)
y los nombres de etapa del pipeline por defecto; la prueba real en una cuenta de desarrollador debe confirmarlos.
"""

import datetime
import html
import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "api.hubapi.com"
URL_BASE = "https://" + HOST
_LOCAL = os.environ.get("LOCALAPPDATA")
RUTA_TOKEN = os.path.join(_LOCAL, "agente-local", "hubspot.token") if _LOCAL and os.path.isabs(_LOCAL) else None  # nunca una ruta relativa
TIMEOUT_S = 10
MAX_RESPUESTA = 200_000
MAX_POR_HORA, MAX_POR_DIA = 30, 100  # escrituras
MAX_LECT_HORA, MAX_LECT_DIA = 60, 300  # búsquedas (frenan la enumeración del CRM)
PIPELINE, ETAPA_NUEVO = "default", "appointmentscheduled"
ORIGENES = ("consulta", "cotizacion", "formulario", "referido", "otro")
TIPOS_TAREA = ("seguimiento", "contacto_frio", "revisar_cotizacion")
ASOC_TRATO_CONTACTO, ASOC_NOTA_CONTACTO, ASOC_TAREA_CONTACTO = 3, 202, 204
_RE_EMAIL = re.compile(r"^[a-z0-9._%+\-]{1,64}@[a-z0-9\-]+(\.[a-z0-9\-]+)+$")
_RE_NOMBRE = re.compile(r"^[^\W\d_][\w .'\-]{0,79}$", re.UNICODE)
_RE_TEL = re.compile(r"^[0-9+()\- ]{6,25}$")


class CrmNoConfigurado(RuntimeError):
    """No hay token utilizable (archivo ausente o inválido)."""


class CrmCuotaAgotada(RuntimeError):
    """Se alcanzó el tope de escrituras por hora o por día."""


class CrmFallo(RuntimeError):
    """HubSpot no respondió bien (red, estado HTTP, respuesta inválida). El mensaje nunca lleva cuerpo ni token."""


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # una redirección sería un error: no se sigue


def leer_token(ruta=None):
    ruta = ruta or RUTA_TOKEN
    if not ruta:
        raise CrmNoConfigurado("no se pudo determinar la ruta del archivo de token")
    try:
        with open(ruta, "r", encoding="ascii") as f:
            t = f.read(500).strip()
    except (OSError, UnicodeDecodeError):
        raise CrmNoConfigurado("no hay archivo de token legible")
    if not (20 <= len(t) <= 300) or not all(33 <= ord(c) < 127 for c in t):
        raise CrmNoConfigurado("el archivo de token no tiene un formato válido")
    return t


def _texto(valor, nombre, maximo, obligatorio=True):
    if valor is None or valor == "":
        if obligatorio:
            raise ValueError(f"{nombre} es obligatorio")
        return ""
    if not isinstance(valor, str) or len(valor) > maximo:
        raise ValueError(f"{nombre} debe ser texto de hasta {maximo} caracteres")
    return valor


def validar_email(valor):
    v = _texto(valor, "email", 254).strip().lower()
    if not _RE_EMAIL.match(v):
        raise ValueError("email con formato inválido")
    return v


def validar_nombre(valor, nombre="nombre", obligatorio=True):
    v = _texto(valor, nombre, 80, obligatorio).strip()
    if obligatorio and not v:
        raise ValueError(f"{nombre} es obligatorio")
    if v and not _RE_NOMBRE.fullmatch(v):
        raise ValueError(f"{nombre} contiene caracteres no permitidos")
    return v


def validar_telefono(valor):
    v = _texto(valor, "telefono", 25, False).strip()
    if v and not _RE_TEL.match(v):
        raise ValueError("telefono con formato inválido")
    return v


class Crm:
    def __init__(
        self,
        token,
        limpiar,
        ruta_cuota,
        base_url=URL_BASE,
        permitir_http_local=False,
        ahora=time.time,
    ):
        if not callable(limpiar):
            raise ValueError("falta la función de limpieza de texto")
        self._token = token
        self._limpiar = limpiar
        self._ruta_cuota = ruta_cuota
        self._ahora = ahora
        p = urllib.parse.urlsplit(base_url)
        if p.scheme == "https" and p.hostname == HOST and p.port in (None, 443) and not p.path.strip("/") and not p.query:
            self._base = URL_BASE
        elif permitir_http_local and p.scheme == "http" and p.hostname == "127.0.0.1" and not p.path.strip("/"):
            self._base = f"http://127.0.0.1:{p.port}"  # solo pruebas: servidor falso local
        else:
            raise ValueError("URL base no permitida")
        # ProxyHandler({}) explícito: sin proxies del entorno (HTTPS_PROXY) en medio del tráfico con el token
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _SinRedireccion,
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    # ---------- topes (persistidos) ----------
    def _cuota(self, consumir, lecturas=False):
        """Cobra `consumir` operaciones de escritura (o de lectura) ANTES de hacerlas. Falla cerrado si el estado es ilegible o inválido."""
        pref, max_h, max_d = ("l", MAX_LECT_HORA, MAX_LECT_DIA) if lecturas else ("e", MAX_POR_HORA, MAX_POR_DIA)
        ahora = datetime.datetime.fromtimestamp(self._ahora(), datetime.timezone.utc)
        dia, hora = ahora.strftime("%Y-%m-%d"), ahora.strftime("%Y-%m-%dT%H")
        try:
            with open(self._ruta_cuota, encoding="utf-8") as f:
                e = json.load(f)
            if not isinstance(e, dict):
                raise ValueError
        except FileNotFoundError:
            e = {}
        except (OSError, ValueError):
            raise CrmCuotaAgotada("estado de cuota ilegible: falla cerrado")
        n_dia = e.get(pref + "_n_dia", 0) if e.get(pref + "_dia") == dia else 0
        n_hora = e.get(pref + "_n_hora", 0) if e.get(pref + "_hora") == hora else 0
        if (not all(type(n) is int and n >= 0 for n in (n_dia, n_hora))) or n_dia + consumir > max_d or n_hora + consumir > max_h:
            raise CrmCuotaAgotada("tope de operaciones en HubSpot alcanzado (hora o día) o estado de cuota inválido")
        e.update(
            {
                pref + "_dia": dia,
                pref + "_n_dia": n_dia + consumir,
                pref + "_hora": hora,
                pref + "_n_hora": n_hora + consumir,
            }
        )
        os.makedirs(os.path.dirname(self._ruta_cuota), exist_ok=True)
        tmp = f"{self._ruta_cuota}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(e, f)
        os.replace(tmp, self._ruta_cuota)

    # ---------- HTTP ----------
    def _pedir(self, metodo, ruta, cuerpo=None, escritura=False, cobrar=True):
        """`escritura` cobra 1 de la cuota de escrituras salvo `cobrar=False` (cuando el llamador ya reservó el lote completo)."""
        if escritura and cobrar:
            self._cuota(1)
        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        req = urllib.request.Request(
            self._base + ruta,
            data=datos,
            method=metodo,
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "agente-local",
            },
        )
        try:
            with self._opener.open(req, timeout=TIMEOUT_S) as r:
                crudo = r.read(MAX_RESPUESTA + 1)
        except urllib.error.HTTPError as e:
            codigo = e.code
            e.close()
            raise CrmFallo(f"HubSpot respondió HTTP {codigo}")
        except (
            urllib.error.URLError,
            OSError,
            ssl.SSLError,
            ValueError,
            http.client.HTTPException,
        ):
            raise CrmFallo("no se pudo contactar a HubSpot")
        if len(crudo) > MAX_RESPUESTA:
            raise CrmFallo("respuesta demasiado grande")
        try:
            r = json.loads(crudo.decode("utf-8")) if crudo.strip() else {}
        except ValueError:
            raise CrmFallo("respuesta inválida")
        if not isinstance(r, dict):
            raise CrmFallo("respuesta inválida")
        return r

    def _libre(self, texto, maximo):
        """Texto libre que leerá una persona en HubSpot: sin controles, números largos ni correos; HTML escapado y URLs neutralizadas."""
        t = html.escape(self._limpiar(texto, maximo), quote=False)
        return re.sub(r"(?i)\b(https?|ftp)://", lambda m: m.group(1) + "[:]//", t)

    def _buscar(self, email):
        self._cuota(1, lecturas=True)  # tope de búsquedas: frena la enumeración del CRM
        r = self._pedir(
            "POST",
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
                "properties": ["firstname", "lastname", "company", "email"],
                "limit": 1,
            },
        )
        res = r.get("results")
        if not isinstance(res, list):
            raise CrmFallo("respuesta inválida")
        if not res:
            return None
        c = res[0]
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"].isdigit():
            raise CrmFallo("respuesta inválida")
        return {"id": c["id"]}  # solo el id: nombre y empresa de HubSpot no se devuelven al modelo (datos de terceros, texto no confiable)

    @staticmethod
    def _asoc(id_, tipo):
        return [
            {
                "to": {"id": id_},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": tipo}],
            }
        ]

    @staticmethod
    def _id(r):
        i = r.get("id")
        if not isinstance(i, str) or not i.isdigit():
            raise CrmFallo("respuesta inválida")
        return i

    def _ahora_iso(self):
        return datetime.datetime.fromtimestamp(self._ahora(), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # ---------- operaciones ----------
    def buscar_contacto(self, email):
        c = self._buscar(validar_email(email))
        return {"existe": False} if c is None else {"existe": True, **c}

    def registrar_lead(self, nombre, empresa, email, telefono, origen, resumen):
        email = validar_email(email)
        nombre = validar_nombre(nombre)
        empresa = validar_nombre(empresa, "empresa", obligatorio=False)
        telefono = validar_telefono(telefono)
        if origen not in ORIGENES:
            raise ValueError("origen debe ser uno de: " + ", ".join(ORIGENES))
        resumen = self._libre(_texto(resumen, "resumen", 500, False), 500)
        partes = nombre.split(None, 1)
        props = {
            "email": email,
            "firstname": partes[0],
            "lastname": partes[1] if len(partes) > 1 else "",
        }
        if empresa:
            props["company"] = empresa
        if telefono:
            props["phone"] = telefono
        existente = self._buscar(email)
        if existente:
            cid, nuevo, trato = existente["id"], False, None
            # contacto existente: NO se pisan sus datos ni se crea otro trato; solo se deja constancia en una nota (si hay resumen)
            if resumen:
                self._cuota(1)
                self._nota(cid, f"[Agente] origen={origen}. {resumen}", cobrar=False)
            return {"contacto": cid, "nuevo": False, "trato": None}
        # lead nuevo: se RESERVAN las escrituras del lote completo (contacto, trato y, si hay resumen, nota) antes de la primera,
        # para no dejar un contacto sin trato por quedarse sin cuota a mitad
        self._cuota(3 if resumen else 2)
        cid = self._id(self._pedir("POST", "/crm/v3/objects/contacts", {"properties": props}, True, cobrar=False))
        try:
            trato = self._id(
                self._pedir(
                    "POST",
                    "/crm/v3/objects/deals",
                    {
                        "properties": {
                            "dealname": f"Lead {origen}: {partes[0]}",
                            "pipeline": PIPELINE,
                            "dealstage": ETAPA_NUEVO,
                        },
                        "associations": self._asoc(cid, ASOC_TRATO_CONTACTO),
                    },
                    True,
                    cobrar=False,
                )
            )
            if resumen:
                self._nota(cid, f"[Agente] origen={origen}. {resumen}", cobrar=False)
        except CrmFallo:
            # el contacto ya existe en HubSpot y un reintento NO crearía el trato: debe saberlo una persona
            raise CrmFallo(f"LEAD A MEDIAS: el contacto {cid} se creó en HubSpot pero falló un paso posterior; avise a una persona")
        return {"contacto": cid, "nuevo": True, "trato": trato}

    def _nota(self, cid, texto, cobrar=True):
        return self._id(
            self._pedir(
                "POST",
                "/crm/v3/objects/notes",
                {
                    "properties": {"hs_timestamp": self._ahora_iso(), "hs_note_body": texto},
                    "associations": self._asoc(cid, ASOC_NOTA_CONTACTO),
                },
                True,
                cobrar=cobrar,
            )
        )

    def registrar_nota(self, email, texto):
        email = validar_email(email)
        texto = self._libre(_texto(texto, "texto", 1000), 1000)
        if not texto:
            raise ValueError("el texto no puede quedar vacío")
        c = self._buscar(email)
        if c is None:
            raise ValueError("no existe un contacto con ese email: registre primero el lead")
        return {"nota": self._nota(c["id"], "[Agente] " + texto), "contacto": c["id"]}

    def crear_tarea(self, email, tipo, borrador):
        email = validar_email(email)
        if tipo not in TIPOS_TAREA:
            raise ValueError("tipo debe ser uno de: " + ", ".join(TIPOS_TAREA))
        borrador = self._libre(_texto(borrador, "borrador", 1000), 1000)
        if not borrador:
            raise ValueError("el borrador no puede quedar vacío")
        c = self._buscar(email)
        if c is None:
            raise ValueError("no existe un contacto con ese email: registre primero el lead")
        tid = self._id(
            self._pedir(
                "POST",
                "/crm/v3/objects/tasks",
                {
                    "properties": {
                        "hs_timestamp": self._ahora_iso(),
                        "hs_task_subject": f"[Agente] {tipo}: revisar y decidir",
                        "hs_task_body": "BORRADOR (no enviado, requiere decisión humana): " + borrador,
                        "hs_task_status": "NOT_STARTED",
                        "hs_task_type": "TODO",
                    },
                    "associations": self._asoc(c["id"], ASOC_TAREA_CONTACTO),
                },
                True,
            )
        )
        return {"tarea": tid, "contacto": c["id"]}
