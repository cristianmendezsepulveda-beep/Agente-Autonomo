"""Compuerta de acciones: política + presupuesto + auditoría. Todo se deniega salvo lo permitido.

AVISO: esto es una CAPA DE POLÍTICA voluntaria dentro del mismo sistema operativo, no aislamiento. Solo
controla lo que pase por ella; el terminal propio de Hermes no pasa por aquí. Aislamiento real = VM con
kernel propio (pendiente: virtualización desactivada en este equipo).
Límites conocidos: TOCTOU entre comprobar la ruta y abrirla (una junction puede cambiar en medio); no se
verifican nombres 8.3 (se rechaza '~'); la política y el estado deben vivir fuera de los directorios escribibles.
"""
import json
import os
import re
import subprocess
import sys
import unicodedata
import uuid

from _lock import BloqueoNoObtenido
from audit_log import AuditLog
from budget_guard import BudgetGuard, LimiteExcedido

AQUI = os.path.dirname(os.path.abspath(__file__))
RESERVADOS = {"con", "conin$", "conout$", "prn", "aux", "nul",
              *(f"com{i}" for i in range(0, 10)), *(f"lpt{i}" for i in range(0, 10))}
CARACTERES_MALOS = re.compile(r'[<>:"|?*\x00-\x1f~]')


class Denegado(Exception):
    pass


class AprobacionRequerida(Exception):
    pass


def _dentro(real, base):
    try:
        return os.path.commonpath([os.path.normcase(real), os.path.normcase(base)]) == os.path.normcase(base)
    except ValueError:  # otra unidad
        return False


class Compuerta:
    def __init__(self, raiz, dir_estado, politica=None):
        with open(politica or os.path.join(AQUI, "policy.json"), encoding="utf-8") as f:
            self.p = json.load(f)
        self.raiz = os.path.realpath(raiz)
        self.estado = os.path.realpath(dir_estado)
        self.escribibles = [os.path.realpath(os.path.join(self.raiz, d)) for d in self.p["directorios_escribibles"]]
        if any(_dentro(self.estado, d) or _dentro(d, self.estado) for d in self.escribibles):
            raise ValueError("el directorio de estado no puede estar en una zona escribible")
        os.makedirs(self.estado, exist_ok=True)
        lim = self.p["limites"]
        self.presupuesto = BudgetGuard(os.path.join(self.estado, "presupuesto.json"),
                                       lim["tokens_max"], lim["acciones_max"])
        self.log = AuditLog(os.path.join(self.estado, "auditoria.jsonl"))

    # --- rutas ---
    def _ruta_segura(self, ruta, escritura=False):
        if not isinstance(ruta, str) or not ruta or CARACTERES_MALOS.search(ruta):
            raise Denegado("ruta inválida")  # incluye ':' (ADS, unidades), '?' (\\?\), '~' (8.3), nulos
        partes = [p for p in re.split(r"[\\/]+", ruta) if p]
        # NFKC convierte COM¹/LPT² en COM1/LPT2, que Windows también trata como dispositivos
        if any(unicodedata.normalize("NFKC", p).split(".")[0].strip().lower() in RESERVADOS or (p not in (".", "..") and p != p.rstrip(". "))
               for p in partes):
            raise Denegado("nombre reservado o inválido")
        real = os.path.realpath(os.path.join(self.raiz, ruta))  # resuelve symlinks/junctions y ..
        if not _dentro(real, self.raiz):
            raise Denegado("ruta fuera del proyecto")
        rel = os.path.relpath(real, self.raiz).lower().replace("\\", "/").split("/")
        if any(pat in comp for comp in rel for pat in self.p["patrones_secretos_bloqueados"]):
            raise Denegado("ruta sensible")  # se revisan TODOS los componentes, no solo el nombre
        if escritura:
            if not any(_dentro(real, d) and real != d for d in self.escribibles):
                raise Denegado("escritura solo en directorios permitidos")
            # lista BLANCA: ".exe" (sin nombre) tiene extensión vacía y queda fuera igual que ".hta" o ".url"
            if os.path.splitext(real)[1].lower() not in self.p["extensiones_permitidas_escritura"]:
                raise Denegado("extensión no permitida para escritura")
        return real

    def _autorizar(self, accion, detalle, tokens=0):
        if accion in self.p["acciones_con_aprobacion_humana"]:
            self.log.registrar("requiere_aprobacion", {"accion": accion, **detalle})
            raise AprobacionRequerida(accion)
        if accion not in self.p["acciones_permitidas"]:
            self.log.registrar("denegado", {"accion": accion, "motivo": "no permitida"})
            raise Denegado("acción no permitida")
        try:
            self.presupuesto.cobrar_accion(tokens)
        except LimiteExcedido as e:
            self.log.registrar("denegado", {"accion": accion, "motivo": f"límite {e}"})
            raise Denegado(f"límite de {e} excedido")
        except ValueError:
            self.log.registrar("denegado", {"accion": accion, "motivo": "tokens inválidos"})
            raise Denegado("tokens inválidos")
        except (RuntimeError, BloqueoNoObtenido):  # estado corrupto o bloqueo no disponible: falla cerrado
            raise Denegado("presupuesto no disponible")

    def _denegar(self, accion, e):
        self.log.registrar("denegado", {"accion": accion, "motivo": str(e)})
        raise (e if isinstance(e, Denegado) else Denegado(f"{type(e).__name__}"))

    # --- acciones ---
    def leer_archivo(self, ruta, tokens=0):
        self._autorizar("leer_archivo", {"ruta": ruta}, tokens)
        try:
            real = self._ruta_segura(ruta)
            with open(real, encoding="utf-8") as f:
                txt = f.read(self.p["limites"]["salida_max_chars"])
        except (Denegado, OSError, ValueError, UnicodeError) as e:
            self._denegar("leer_archivo", e)
        self.log.registrar("ok", {"accion": "leer_archivo", "ruta": ruta})
        return txt

    def escribir_archivo(self, ruta, contenido, tokens=0):
        self._autorizar("escribir_archivo", {"ruta": ruta}, tokens)
        try:
            if not isinstance(contenido, str) or len(contenido) > self.p["limites"]["escritura_max_chars"]:
                raise Denegado("contenido inválido o demasiado grande")
            real = self._ruta_segura(ruta, escritura=True)
            os.makedirs(os.path.dirname(real), exist_ok=True)
            tmp = f"{real}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(contenido)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, real)
        except (Denegado, OSError, ValueError, UnicodeError) as e:
            self._denegar("escribir_archivo", e)
        self.log.registrar("ok", {"accion": "escribir_archivo", "ruta": ruta, "chars": len(contenido)})

    def ejecutar(self, argv, tokens=0):
        # Bloqueado por defecto (aprobación humana): python permite código arbitrario con tus permisos.
        self._autorizar("ejecutar", {"argv0": argv[0] if argv else ""}, tokens)
        if not argv or os.path.basename(argv[0]).lower() not in self.p["ejecutables_permitidos"]:
            self._denegar("ejecutar", Denegado("ejecutable no permitido"))
        exe = sys.executable if os.path.basename(argv[0]).lower().startswith("python") else argv[0]
        env = {k: os.environ[k] for k in ("SystemRoot", "PATH") if k in os.environ}  # sin secretos del entorno
        lim = self.p["limites"]
        try:
            r = subprocess.run([exe, "-I", *argv[1:]], cwd=self.raiz, env=env, capture_output=True, text=True,
                               timeout=lim["timeout_s"], shell=False)
        except subprocess.TimeoutExpired:
            self._denegar("ejecutar", Denegado("timeout"))
        self.log.registrar("ok", {"accion": "ejecutar", "rc": r.returncode})
        return r.returncode, r.stdout[:lim["salida_max_chars"]], r.stderr[:lim["salida_max_chars"]]

    def red(self, host):
        # Egress denegado: siempre requiere aprobación humana, aunque el host esté en lista blanca.
        self._autorizar("red", {"host": host})
