"""Comprueba, con contenedores reales, que el aislamiento que Hermes usará es el que decimos.

Dos fases:
  Fase 1 (--imagen):       lanza contenedores de PRUEBA con una réplica de las banderas de Hermes y comprueba lo que se ve
                           desde dentro. Demuestra que ESTA máquina sabe aislar. Cada comprobación tiene un CONTROL NEGATIVO
                           (un contenedor SIN la protección debe dar un resultado distinto); si el control no difiere, el
                           resultado es INVÁLIDO y no se da por bueno.
  Fase 2 (--inspeccionar / --json): evalúa un contenedor REAL creado por Hermes a partir de `docker inspect`. Es la que manda:
                           la réplica de la fase 1 puede derivar si Hermes cambia sus banderas. Es una LISTA BLANCA: todo lo
                           que no se reconoce (montajes, opciones de seguridad, campos) FALLA y obliga a revisarlo a mano.

Uso:  python aislamiento/verificar_aislamiento.py --imagen <imagen> [--permitir-descarga] [--salida informe.json]
      python aislamiento/verificar_aislamiento.py --inspeccionar <contenedor> [--salida informe.json]
      python aislamiento/verificar_aislamiento.py --json <docker-inspect.json> [--salida informe.json]

- NO descarga nada salvo que se pase --permitir-descarga (una descarga es una decisión de la persona).
- Los contenedores de prueba de la fase 1 no montan carpetas del host (salvo el control negativo, con una carpeta temporal
  vacía y de solo lectura) ni reciben variables de entorno del host (salvo la variable falsa del control).
Salida: 0 todo correcto; 1 alguna comprobación falló o es inválida; 2 prerrequisitos no cumplidos.
LÍMITES: un contenedor no es una garantía absoluta (escape de contenedor o de la VM de WSL2). La fase 2 solo ve el contenedor en
el momento de inspeccionarlo y su entorno de CREACIÓN (no variables inyectadas después con `docker exec -e`). No comprueba que
el contenedor sea el único en marcha, ni lo que alguien lance a mano con `docker run`.
"""
import argparse
import datetime
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

MEMORIA_MB = 1536
MEMORIA_BYTES = MEMORIA_MB * 1024 * 1024
PIDS = 2048  # el valor por defecto de Hermes: sus comentarios dicen que 256 se agotaba en uso normal
# Fase 1: réplica de lo que Hermes aplica a TODO contenedor (tools/environments/docker.py: _BASE_SECURITY_ARGS,
# no-new-privileges, resource_args) más la red desactivada. Hermes añade además SETUID/SETGID en la práctica (ver fase 2).
CAPS_PERMITIDAS = (1 << 0) | (1 << 1) | (1 << 3)  # CHOWN, DAC_OVERRIDE, FOWNER
BANDERAS_SEGURAS = ["--network", "none", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN",
                    "--cap-add", "FOWNER", "--security-opt", "no-new-privileges", "--pids-limit", str(PIDS),
                    "--memory", f"{MEMORIA_MB}m", "--cpus", "1", "--tmpfs", "/tmp:rw,nosuid,size=512m",
                    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m"]
PATRON_CREDENCIALES = re.compile(r"(api[_-]?key|token|secret|password|passwd|credential|anthropic|openai|openrouter|bearer|"
                                 r"^aws_|_pat$|database_url|ssh_auth_sock|^auth|private[_-]?key|access[_-]?key)", re.I)
# Heurística: no cubre todos los formatos de clave. No se usa `_key$` a propósito (marcaría GPG_KEY, que es público).
PATRON_VALOR_CREDENCIAL = re.compile(r"(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|sk_live_[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|"
                                     r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
                                     r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,})")
FS_DEL_HOST = {"9p", "drvfs", "virtiofs", "fakeowner", "fuse.grpcfuse", "grpcfuse"}
RUTAS_DEL_HOST = ("/host_mnt", "/run/desktop/mnt/host", "/mnt/host", "/mnt/c", "/mnt/wsl")


# ---------- evaluadores PUROS de la fase 1 (reciben texto, devuelven (ok, detalle)); se prueban sin Docker ----------
def interfaces(txt):
    return {l.strip() for l in txt.splitlines() if l.strip()}


def red_aislada(txt):
    ifs = interfaces(txt)
    return ifs == {"lo"}, f"interfaces: {sorted(ifs) or 'ninguna'}"


def sin_montajes_del_host(txt):
    malos = []
    for linea in txt.splitlines():
        partes = linea.split()
        if len(partes) < 3:
            continue
        origen, destino, fstype = partes[0], partes[1], partes[2]
        if fstype in FS_DEL_HOST or any(destino.startswith(r) or origen.startswith(r) for r in RUTAS_DEL_HOST):
            malos.append(f"{destino} ({fstype})")
    return not malos, ("montajes del host: " + ", ".join(malos)) if malos else "solo montajes del contenedor"


def sin_socket_docker(txt):
    return txt.strip() == "", ("encontrado: " + txt.strip()) if txt.strip() else "no hay socket de Docker"


def sin_privilegios(txt):
    """Solo se admiten las capacidades que Hermes añade a propósito (CHOWN, DAC_OVERRIDE, FOWNER) y no-new-privileges."""
    cap = re.search(r"CapEff:\s*([0-9a-fA-F]+)", txt)
    nnp = re.search(r"NoNewPrivs:\s*(\d)", txt)
    if not cap or not nnp:
        return False, "no se pudo leer CapEff/NoNewPrivs"
    valor = int(cap.group(1), 16)
    solo_permitidas = (valor & ~CAPS_PERMITIDAS) == 0
    return solo_permitidas and nnp.group(1) == "1", f"CapEff={cap.group(1)} (permitidas 0x{CAPS_PERMITIDAS:x}) NoNewPrivs={nnp.group(1)}"


def limite(txt, esperado, nombre):
    v = txt.strip()
    return v == str(esperado), f"{nombre}={v or 'sin dato'} (esperado {esperado})"


def sin_credenciales(txt):
    """Mira NOMBRES y VALORES de las variables (sin imprimir nunca los valores)."""
    nombres, por_valor = [], []
    for linea in txt.splitlines():
        if "=" not in linea:
            continue
        nombre, valor = linea.split("=", 1)
        nombres.append(nombre)
        if PATRON_CREDENCIALES.search(nombre):
            por_valor.append(nombre)
        elif PATRON_VALOR_CREDENCIAL.search(valor):
            por_valor.append(nombre + " (valor con forma de clave)")
    return not por_valor, ("variables sospechosas: " + ", ".join(por_valor)) if por_valor else f"{len(nombres)} variables, ninguna de credenciales"


# ---------- FASE 2: evaluar un contenedor REAL creado por Hermes (a partir de `docker inspect`) ----------
IMAGEN_ESPERADA = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
CAPS_HERMES = {"CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_SETGID", "CAP_SETUID"}  # observadas en el contenedor real
SECURITY_OPT_VALIDAS = {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
# LISTA BLANCA exacta de lo que Hermes monta (14 carpetas del perfil, solo lectura). Una carpeta nueva, o la raíz del perfil,
# FALLA a propósito: la raíz contiene .env, config.yaml y auth.json. Si Hermes añade una, se revisa a mano y se añade aquí.
MONTAJES_PERMITIDOS = ("cache\\documents", "cache\\images", "cache\\videos", "cache\\pruned", "images", "composer-pastes",
                       "cache\\audio", "cache\\screenshots", "cache\\web", "cache\\delegation", "cache\\spillover",
                       "cache\\generated", "attachments", "skills")
NOMBRES_SENSIBLES = {".env", "auth.json", "auth.lock", "config.yaml", "credentials", "id_rsa", ".ssh", ".aws"}
_BIND = re.compile(r"^(?P<src>.+?):(?P<dst>/[^:]*)(?::(?P<modo>[A-Za-z,]+))?$")
CAMPOS_QUE_DEBEN_ESTAR_VACIOS = ("VolumesFrom", "Sysctls", "DeviceRequests", "DeviceCgroupRules", "ExtraHosts", "GroupAdd",
                                 "Links", "PortBindings")
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
PROPAGACION_MALA = {"shared", "rshared", "slave", "rslave"}


def _ruta_win(p):
    return ntpath.normcase(ntpath.normpath(p))


def rutas_permitidas(perfil_dir):
    return {_ruta_win(ntpath.join(perfil_dir, rel)) for rel in MONTAJES_PERMITIDOS}


def _entero(x):
    return type(x) is int  # excluye bool


def _fuentes(c):
    """Todas las fuentes de montaje, de Binds Y de Mounts (--mount no aparece en Binds): (origen, src, dst, solo_lectura, tipo)."""
    fuentes = []
    for b in (c.get("HostConfig") or {}).get("Binds") or []:
        m = _BIND.match(b)
        if m:
            fuentes.append(("Binds", m.group("src"), m.group("dst"), "ro" in (m.group("modo") or "rw").split(","), "bind"))
        else:
            fuentes.append(("Binds", b, "?", False, "bind"))
    for mnt in c.get("Mounts") or []:
        fuentes.append(("Mounts", mnt.get("Source") or "", mnt.get("Destination") or "?", mnt.get("RW") is False, mnt.get("Type")))
    return fuentes


def evaluar_contenedor(c, perfil_dir, imagen_esperada=IMAGEN_ESPERADA, memoria_max=MEMORIA_BYTES, pids_max=PIDS, cpus=1):
    """Evalúa un `docker inspect` (dict de UN contenedor). Devuelve lista de (nombre, ok, detalle). PURO: no usa Docker."""
    hc, cfg = c.get("HostConfig") or {}, c.get("Config") or {}
    r = []
    imagen = cfg.get("Image") or ""
    r.append(("imagen exacta esperada (digest)", imagen == imagen_esperada, imagen or "sin dato"))
    etiquetas = cfg.get("Labels") or {}
    perfil = ntpath.basename(perfil_dir.rstrip("\\/"))
    r.append(("contenedor creado por Hermes para el perfil esperado",
              etiquetas.get("hermes-agent") == "1" and etiquetas.get("hermes-profile") == perfil,
              f"hermes-agent={etiquetas.get('hermes-agent')!r} hermes-profile={etiquetas.get('hermes-profile')!r} (esperado {perfil!r})"))
    r.append(("no privilegiado", hc.get("Privileged") is False, f"Privileged={hc.get('Privileged')}"))
    redes = set(((c.get("NetworkSettings") or {}).get("Networks") or {}))
    r.append(("sin red", hc.get("NetworkMode") == "none" and redes <= {"none"}, f"NetworkMode={hc.get('NetworkMode')} redes={sorted(redes)}"))
    cap_add, cap_drop = set(hc.get("CapAdd") or []), set(hc.get("CapDrop") or [])
    r.append(("CapDrop ALL", "ALL" in cap_drop, f"CapDrop={sorted(cap_drop)}"))
    extra = sorted(cap_add - CAPS_HERMES)
    r.append(("capacidades añadidas dentro de las conocidas de Hermes", not extra,
              ("de más: " + ", ".join(extra)) if extra else "añadidas: " + ", ".join(sorted(cap_add))))
    opciones = hc.get("SecurityOpt") or []
    r.append(("SecurityOpt: solo no-new-privileges activo (sin seccomp/apparmor/label debilitados)",
              bool(opciones) and all(o in SECURITY_OPT_VALIDAS for o in opciones), f"SecurityOpt={opciones}"))
    mem, swap, pids, nano = hc.get("Memory"), hc.get("MemorySwap"), hc.get("PidsLimit"), hc.get("NanoCpus")
    r.append(("memoria acotada", _entero(mem) and 0 < mem <= memoria_max, f"Memory={mem}"))
    r.append(("swap acotado (no ilimitado)", _entero(mem) and _entero(swap) and mem <= swap <= 2 * mem, f"MemorySwap={swap}"))
    r.append(("procesos acotados", _entero(pids) and 0 < pids <= pids_max, f"PidsLimit={pids}"))
    r.append(("CPU acotada", _entero(nano) and 0 < nano <= int(cpus * 1e9), f"NanoCpus={nano}"))
    r.append(("sin dispositivos del host", not hc.get("Devices"), f"Devices={len(hc.get('Devices') or [])}"))
    r.append(("namespaces y runtime esperados",
              hc.get("PidMode") in ("", None) and hc.get("IpcMode") in ("private", "", None) and hc.get("UsernsMode") in ("", None)
              and hc.get("UTSMode") in ("", None) and hc.get("CgroupnsMode") in ("private", "", None)
              and hc.get("Runtime") in ("runc", "", None) and not hc.get("CgroupParent") and hc.get("PublishAllPorts") in (False, None),
              f"Pid={hc.get('PidMode')!r} Ipc={hc.get('IpcMode')!r} Userns={hc.get('UsernsMode')!r} UTS={hc.get('UTSMode')!r} "
              f"Cgroupns={hc.get('CgroupnsMode')!r} Runtime={hc.get('Runtime')!r}"))
    raros = [k for k in CAMPOS_QUE_DEBEN_ESTAR_VACIOS if hc.get(k)]
    r.append(("campos de HostConfig que deben estar vacíos (VolumesFrom, Sysctls, ExtraHosts…)", not raros,
              ("no vacíos: " + ", ".join(raros)) if raros else "todos vacíos"))
    # montajes del host (de Binds Y de Mounts)
    fuentes = _fuentes(c)
    permitidas = rutas_permitidas(perfil_dir)
    no_bind = [f"{dst} ({tipo})" for _, _, dst, _, tipo in fuentes if tipo != "bind"]
    escribibles = [dst for _, _, dst, ro, _ in fuentes if not ro]
    fuera = [src for _, src, _, _, tipo in fuentes if tipo == "bind" and _ruta_win(src) not in permitidas]
    sensibles = [src for _, src, _, _, _ in fuentes
                 if any(comp in NOMBRES_SENSIBLES or comp.endswith((".env", ".pem", ".key")) for comp in _ruta_win(src).split("\\"))]
    sock = [dst for _, src, dst, _, _ in fuentes if "docker.sock" in src + dst or "docker_engine" in src + dst]
    r.append((f"montajes: solo tipo bind ({len(fuentes)} fuentes evaluadas, Binds y Mounts)", not no_bind,
              ("otros tipos: " + ", ".join(no_bind)) if no_bind else "todos bind"))
    r.append(("montajes: todos de solo lectura", not escribibles, ("escribibles: " + ", ".join(escribibles[:5])) if escribibles else "todos :ro"))
    r.append(("montajes: cada fuente es EXACTAMENTE una de las 14 carpetas permitidas del perfil", not fuera,
              ("no permitidas: " + "; ".join(fuera[:3])) if fuera else f"{len(permitidas)} permitidas, todas conocidas"))
    r.append(("montajes: ninguno de configuración o credenciales", not sensibles, ("sospechosos: " + "; ".join(sensibles[:3])) if sensibles else "ninguno"))
    propag = []
    for b in hc.get("Binds") or []:
        m = _BIND.match(b)
        if m and (set((m.group("modo") or "").split(",")) & PROPAGACION_MALA):
            propag.append(m.group("dst"))
    propag += [mnt.get("Destination", "?") for mnt in c.get("Mounts") or [] if (mnt.get("Propagation") or "rprivate") not in ("rprivate", "private")]
    r.append(("montajes: propagación privada (sin shared/slave)", not propag, ("propagación distinta: " + ", ".join(propag[:3])) if propag else "rprivate"))
    r.append(("sin socket de Docker montado", not sock, "ninguno" if not sock else ", ".join(sock)))
    r.append(("sin credenciales en el entorno de creación",) + sin_credenciales("\n".join(cfg.get("Env") or [])))
    return r


def _es_enlace(p):
    """True/False, o None si no se pudo leer (cuenta como NO verificado)."""
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return os.path.islink(p) or bool(getattr(st, "st_file_attributes", 0) & REPARSE)


def buscar_enlaces(fuentes, tope=100000):
    """En el HOST, para cada carpeta que se monta: (a) que ella y TODOS sus directorios padre no sean enlaces simbólicos ni
    junctions (si lo fueran, Docker montaría otra carpeta con el mismo nombre: realpath resuelve la cadena entera), y (b) que
    no haya enlaces dentro. Devuelve (enlaces, directorios_revisados, no_encontrados, ilegibles). Lo que no se puede leer, o si se
    alcanza el tope, cuenta como NO verificado."""
    enlaces, revisados, ausentes, ilegibles, visto = [], 0, [], [], 0
    for src in fuentes:
        if not os.path.isdir(src):
            ausentes.append(src)
            continue
        revisados += 1
        real = os.path.realpath(src)
        if _ruta_win(real) != _ruta_win(os.path.abspath(src)):
            enlaces.append(f"{src} -> {real} (la carpeta o un directorio padre es un enlace)")
            continue  # no se recorre lo que hay dentro de un destino ajeno
        for ruta, dirs, ficheros in os.walk(src, onerror=lambda e, s=src: ilegibles.append(str(getattr(e, "filename", s)))):
            for nombre in dirs + ficheros:
                p = os.path.join(ruta, nombre)
                visto += 1
                if visto > tope:
                    ilegibles.append(f"(tope de revisión alcanzado: {tope} entradas)")
                    return enlaces, revisados, ausentes, ilegibles
                es = _es_enlace(p)
                if es is None:
                    ilegibles.append(p)
                elif es:
                    enlaces.append(p)
    return enlaces, revisados, ausentes, ilegibles


# ---------- ejecución ----------
def docker(args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def correr(imagen, comando, banderas):
    p = docker(["run", "--rm", *banderas, imagen, "sh", "-c", comando])
    return p.returncode, p.stdout


def prerrequisitos(imagen, permitir_descarga):
    try:
        v = docker(["version", "--format", "{{.Server.Version}}"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"no se pudo ejecutar docker: {type(e).__name__}"
    if v.returncode != 0:
        return "el motor de Docker no está en marcha (abre Docker Desktop y espera a 'Engine running')"
    if docker(["image", "inspect", imagen], timeout=30).returncode != 0:
        if not permitir_descarga:
            return f"la imagen '{imagen}' no está descargada; ejecuta de nuevo con --permitir-descarga si autorizas la descarga"
        p = docker(["pull", imagen], timeout=900)
        if p.returncode != 0:
            return f"no se pudo descargar la imagen: {p.stderr.strip()[:200]}"
    return None


COMANDOS = {
    "red": "ls /sys/class/net",
    "montajes": "cat /proc/mounts",
    "socket": "ls /var/run/docker.sock /run/docker.sock 2>/dev/null; true",
    "privilegios": "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status",
    "memoria": "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes",
    "pids": "cat /sys/fs/cgroup/pids.max 2>/dev/null || cat /sys/fs/cgroup/pids/pids.max",
    "entorno": "env",
}


def veredicto_fase1(comprobaciones, controles):
    """PURO. CORRECTO solo si todo pasa y TODOS los controles distinguen; un control que no distingue invalida el resultado."""
    fallos = [c for c in comprobaciones if not c[1]]
    invalidos = [c for c in controles if not c[1]]
    if not fallos and not invalidos:
        return "CORRECTO"
    return "INVÁLIDO (un control no distingue)" if invalidos and not fallos else "FALLÓ"


def ejecutar(imagen, correr_fn=None):
    """`correr_fn(imagen, comando, banderas) -> (rc, salida)` se puede inyectar para probar la lógica sin Docker."""
    correr = correr_fn or globals()["correr"]
    salidas, fallidas = {}, []
    for k, cmd in COMANDOS.items():
        rc, out = correr(imagen, cmd, BANDERAS_SEGURAS)
        if rc != 0:
            fallidas.append(f"{k}(rc={rc})")
        salidas[k] = out
    datos_ok = (not fallidas and "/proc" in salidas["montajes"] and "PATH=" in salidas["entorno"]
                and salidas["red"].strip() != "")
    comprobaciones = [
        ("los contenedores de prueba se ejecutaron y devolvieron datos", datos_ok,
         ("fallaron: " + ", ".join(fallidas)) if fallidas else
         ("datos mínimos presentes (/proc, PATH, interfaces)" if datos_ok else "faltan datos mínimos en la salida")),
        ("sin red (solo loopback)",) + red_aislada(salidas["red"]),
        ("sin carpetas del host montadas",) + sin_montajes_del_host(salidas["montajes"]),
        ("sin socket de Docker",) + sin_socket_docker(salidas["socket"]),
        ("solo capacidades mínimas y no-new-privileges",) + sin_privilegios(salidas["privilegios"]),
        ("tope de memoria",) + limite(salidas["memoria"], MEMORIA_BYTES, "memory.max"),
        ("tope de procesos",) + limite(salidas["pids"], PIDS, "pids.max"),
        ("sin credenciales en el entorno",) + sin_credenciales(salidas["entorno"]),
    ]
    # CONTROLES NEGATIVOS, uno por comprobación: sin la protección el resultado DEBE cambiar; si no, la comprobación no vale.
    tmp = tempfile.mkdtemp(prefix="sondeo-aislamiento-")  # carpeta temporal VACÍA, montada solo lectura, solo en el control
    try:
        mont_ctrl = correr(imagen, COMANDOS["montajes"], ["-v", f"{tmp}:/sondeo:ro"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    red_ctrl = correr(imagen, COMANDOS["red"], ["--network", "bridge"])
    sock_ctrl = correr(imagen, "mkdir -p /var/run && touch /var/run/docker.sock && " + COMANDOS["socket"], BANDERAS_SEGURAS)
    priv_ctrl = correr(imagen, COMANDOS["privilegios"], [])
    mem_ctrl = correr(imagen, COMANDOS["memoria"], [])
    pids_ctrl = correr(imagen, COMANDOS["pids"], [])
    env_ctrl = correr(imagen, COMANDOS["entorno"], ["-e", "X_TOKEN=valor-de-prueba"])

    def distingue(nombre, resultado, evaluador):
        """Un control solo vale si su contenedor SE EJECUTÓ (rc 0) y devolvió datos; si no, es inválido (no 'distingue')."""
        rc, out = resultado
        if rc != 0 or not out.strip():
            return (nombre, False, f"el contenedor de control falló o no devolvió datos (rc={rc})")
        ok, detalle = evaluador(out)
        return (nombre, not ok, detalle)

    controles = [
        distingue("control de red (con red debe ver otra interfaz)", red_ctrl, red_aislada),
        distingue("control de montajes (con una carpeta del host montada debe detectarse)", mont_ctrl, sin_montajes_del_host),
        distingue("control de socket (con un docker.sock presente debe detectarse)", sock_ctrl, sin_socket_docker),
        distingue("control de privilegios (sin banderas debe tener capacidades de más)", priv_ctrl, sin_privilegios),
        distingue("control de memoria (sin --memory no debe dar el tope)", mem_ctrl,
                  lambda o: limite(o, MEMORIA_BYTES, "memory.max")),
        distingue("control de procesos (sin tope no debe dar 2048)", pids_ctrl, lambda o: limite(o, PIDS, "pids.max")),
        distingue("control de credenciales (con X_TOKEN en el entorno debe detectarse)", env_ctrl, sin_credenciales),
    ]
    return comprobaciones, controles


def main_fase2(a):
    """Fase 2: evalúa un contenedor real (vivo, por --inspeccionar, o un `docker inspect` guardado, por --json)."""
    if a.json:
        with open(a.json, "rb") as f:
            crudo = f.read()
        datos = json.loads(crudo.decode("utf-8-sig"))
    else:
        p = docker(["inspect", a.inspeccionar], timeout=30)
        if p.returncode != 0:
            print("PRERREQUISITO NO CUMPLIDO: no se pudo inspeccionar el contenedor:", p.stderr.strip()[:200], file=sys.stderr)
            return 2
        crudo = p.stdout.encode("utf-8")
        datos = json.loads(p.stdout)
    if isinstance(datos, list):
        if len(datos) != 1:
            print(f"PRERREQUISITO NO CUMPLIDO: se esperaba exactamente 1 contenedor y hay {len(datos)}", file=sys.stderr)
            return 2
        contenedor = datos[0]
    else:
        contenedor = datos
    if not isinstance(contenedor, dict):
        print("PRERREQUISITO NO CUMPLIDO: el inspect no es un objeto", file=sys.stderr)
        return 2
    resultados = evaluar_contenedor(contenedor, a.perfil_dir, a.imagen_esperada)
    # comprobación en el HOST: enlaces/junctions en la carpeta montada, en sus directorios padre y dentro. Solo se recorren las
    # fuentes que ya pasaron la lista blanca (un inspect ajeno no puede hacernos recorrer C:\ ni una ruta UNC).
    permitidas = rutas_permitidas(a.perfil_dir)
    montadas = sorted({src for _, src, _, _, tipo in _fuentes(contenedor) if tipo == "bind" and src and _ruta_win(src) in permitidas})
    enlaces, revisados, ausentes, ilegibles = buscar_enlaces(montadas)
    # fallo cerrado: enlaces, carpetas ausentes (TODAS deben existir), lo ilegible o los topes alcanzados = NO verificado
    ok_enlaces = not enlaces and not ausentes and not ilegibles and (revisados > 0 or not montadas)
    partes = []
    if enlaces:
        partes.append("enlaces: " + "; ".join(enlaces[:3]))
    if ausentes:
        partes.append(f"no encontradas en este equipo: {len(ausentes)} (no verificado)")
    if ilegibles:
        partes.append(f"no se pudieron leer: {len(ilegibles)} (no verificado)")
    resultados.append((f"sin enlaces simbólicos ni junctions en las carpetas montadas ni en sus directorios padre ({revisados} revisadas)",
                       ok_enlaces, "; ".join(partes) if partes else "ninguno"))
    for nombre, ok, detalle in resultados:
        print(("OK    " if ok else "FALLA ") + nombre + " -> " + detalle)
    veredicto = "CORRECTO" if all(ok for _, ok, _ in resultados) else "FALLÓ"
    print("Veredicto fase 2:", veredicto)
    if a.salida:
        guardado = json.loads(json.dumps(contenedor))  # copia: el informe nunca guarda VALORES del entorno
        cfg = guardado.get("Config")
        if isinstance(cfg, dict) and isinstance(cfg.get("Env"), list):
            cfg["Env"] = [str(e).split("=", 1)[0] + "=<redactado>" for e in cfg["Env"]]
        with open(a.salida, "w", encoding="utf-8") as f:
            json.dump({"fecha": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                       "contenedor": contenedor.get("Name") or contenedor.get("Id", "")[:12], "veredicto": veredicto,
                       "inspect_sha256": hashlib.sha256(crudo).hexdigest(), "inspect": guardado,
                       "nota": "inspect_sha256 es del inspect CRUDO de entrada; el informe guarda una copia con valores de Env redactados",
                       "resultados": [list(x) for x in resultados]}, f, ensure_ascii=False, indent=1)
    return 0 if veredicto == "CORRECTO" else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--imagen", help="fase 1: imagen para los contenedores de prueba")
    ap.add_argument("--permitir-descarga", action="store_true")
    ap.add_argument("--inspeccionar", help="fase 2: nombre o id de un contenedor real de Hermes ya en marcha")
    ap.add_argument("--json", help="fase 2: archivo con la salida de `docker inspect` de un contenedor de Hermes")
    ap.add_argument("--perfil-dir", default=r"C:\Users\PC\AppData\Local\hermes\profiles\agente",
                    help="fase 2: directorio del perfil; solo sus 14 subcarpetas conocidas pueden montarse (solo lectura)")
    ap.add_argument("--imagen-esperada", default=IMAGEN_ESPERADA, help="fase 2: imagen exacta (con digest) que debe usar el contenedor")
    ap.add_argument("--salida", default=None)
    a = ap.parse_args(argv)
    if a.inspeccionar or a.json:
        return main_fase2(a)
    if not a.imagen:
        ap.error("indica --imagen (fase 1) o --inspeccionar/--json (fase 2)")
    problema = prerrequisitos(a.imagen, a.permitir_descarga)
    if problema:
        print("PRERREQUISITO NO CUMPLIDO:", problema, file=sys.stderr)
        return 2
    comprobaciones, controles = ejecutar(a.imagen)
    for nombre, ok, detalle in comprobaciones + controles:
        print(("OK    " if ok else "FALLA ") + nombre + " -> " + detalle)
    veredicto = veredicto_fase1(comprobaciones, controles)
    print("Veredicto:", veredicto)
    informe = {"fecha": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "imagen": a.imagen,
               "veredicto": veredicto, "comprobaciones": [list(c) for c in comprobaciones], "controles": [list(c) for c in controles]}
    if a.salida:
        with open(a.salida, "w", encoding="utf-8") as f:
            json.dump(informe, f, ensure_ascii=False, indent=1)
    return 0 if veredicto == "CORRECTO" else 1


if __name__ == "__main__":
    sys.exit(main())
