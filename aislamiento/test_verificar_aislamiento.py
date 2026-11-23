"""Pruebas de los evaluadores puros (sin Docker): con salidas de ejemplo buenas y malas, y un caso por cada hallazgo de auditoría."""

import copy
import io
import json
import ntpath
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

import verificar_aislamiento as v

PERFIL = r"C:\Users\PC\AppData\Local\hermes\profiles\agente"
IMG = v.IMAGEN_ESPERADA

MONTAJES_BUENOS = """overlay / overlay rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /dev tmpfs rw,nosuid,size=65536k,mode=755 0 0
/dev/sdc /etc/resolv.conf ext4 rw,relatime 0 0
shm /dev/shm tmpfs rw,nosuid,nodev,noexec,relatime,size=65536k 0 0
"""


class Evaluadores(unittest.TestCase):
    def test_red(self):
        self.assertTrue(v.red_aislada("lo\n")[0])
        self.assertFalse(v.red_aislada("eth0\nlo\n")[0])
        self.assertFalse(v.red_aislada("")[0])  # sin dato no es "aislado"

    def test_montajes_del_host(self):
        self.assertTrue(v.sin_montajes_del_host(MONTAJES_BUENOS)[0])
        for malo in (
            "C:\\ /host_mnt/c 9p rw 0 0",
            "drvfs /mnt/c drvfs rw 0 0",
            "x /workspace virtiofs rw 0 0",
            "grpcfuse /data fuse.grpcfuse rw 0 0",
            "/run/desktop/mnt/host/c /x ext4 rw 0 0",
        ):
            ok, det = v.sin_montajes_del_host(MONTAJES_BUENOS + malo + "\n")
            self.assertFalse(ok, malo)
            self.assertIn("montajes del host", det)

    def test_socket(self):
        self.assertTrue(v.sin_socket_docker("")[0])
        self.assertFalse(v.sin_socket_docker("/var/run/docker.sock\n")[0])

    def test_privilegios(self):
        self.assertTrue(v.sin_privilegios("CapEff:\t0000000000000000\nNoNewPrivs:\t1\n")[0])  # todo descartado
        self.assertTrue(v.sin_privilegios("CapEff:\t000000000000000b\nNoNewPrivs:\t1\n")[0])  # las 3 de la réplica de la fase 1
        self.assertFalse(v.sin_privilegios("CapEff:\t000000000000001b\nNoNewPrivs:\t1\n")[0])  # una capacidad de más
        self.assertFalse(v.sin_privilegios("CapEff:\t00000000a80425fb\nNoNewPrivs:\t0\n")[0])  # contenedor por defecto
        self.assertFalse(v.sin_privilegios("CapEff:\t0000000000000000\nNoNewPrivs:\t0\n")[0])  # falta no-new-privileges
        self.assertFalse(v.sin_privilegios("")[0])  # sin dato => no se da por bueno

    def test_limites(self):
        self.assertTrue(v.limite("1610612736\n", 1536 * 1024 * 1024, "m")[0])
        self.assertFalse(v.limite("max\n", 1536 * 1024 * 1024, "m")[0])
        self.assertTrue(v.limite("2048\n", 2048, "p")[0])
        self.assertFalse(v.limite("", 2048, "p")[0])

    def test_credenciales_por_nombre(self):
        self.assertTrue(v.sin_credenciales("PATH=/usr/bin\nHOME=/root\nHOSTNAME=abc\n")[0])
        for malo in (
            "ANTHROPIC_API_KEY=x",
            "GITHUB_TOKEN=x",
            "DB_PASSWORD=x",
            "OPENAI_BASE=x",
            "MY_SECRET=x",
            "AWS_ACCESS_KEY_ID=x",
            "DEPLOY_PAT=x",
            "DATABASE_URL=x",
            "SSH_AUTH_SOCK=x",
        ):
            self.assertFalse(v.sin_credenciales("PATH=/usr/bin\n" + malo + "\n")[0], malo)

    def test_credenciales_por_valor_aunque_el_nombre_sea_inocente(self):
        for valor in (
            "sk-ant-api03-abcdefghijklmnop",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "ghp_abcdefghijklmnopqrstuvwx",
            "xoxb-1234567890-abcdef",
            "AKIAABCDEFGHIJKLMNOP",
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            ok, det = v.sin_credenciales("PATH=/usr/bin\nFOO=" + valor + "\n")
            self.assertFalse(ok, valor)
            self.assertNotIn(valor, det)  # nunca se imprime el valor
        self.assertTrue(v.sin_credenciales("GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305\nPYTHON_SHA256=abc\nLANG=C.UTF-8\n")[0])

    def test_las_banderas_son_las_de_la_configuracion_objetivo(self):
        b = v.BANDERAS_SEGURAS
        self.assertEqual(b[b.index("--network") + 1], "none")
        self.assertIn("ALL", b)
        self.assertIn("no-new-privileges", b)
        self.assertEqual(b[b.index("--memory") + 1], "1536m")
        self.assertEqual(b[b.index("--pids-limit") + 1], "2048")
        self.assertEqual(
            [b[i + 1] for i, x in enumerate(b) if x == "--cap-add"],
            ["DAC_OVERRIDE", "CHOWN", "FOWNER"],
        )
        for ausente in ("-v", "--env", "-e", "--mount", "--privileged", "--network=host"):
            self.assertNotIn(ausente, b)  # ningún montaje del host ni variables en los contenedores de prueba

    def test_sin_docker_devuelve_prerrequisito_no_cumplido(self):
        original = v.docker
        v.docker = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("docker"))
        try:
            self.assertIn("no se pudo ejecutar docker", v.prerrequisitos("x", False))
        finally:
            v.docker = original

    def test_veredicto_fase1_es_puro_y_un_control_que_no_distingue_invalida(self):
        bien, ctrl_ok, ctrl_mal = [("x", True, "")], [("c", True, "")], [("c", False, "")]
        self.assertEqual(v.veredicto_fase1(bien, ctrl_ok), "CORRECTO")
        self.assertTrue(v.veredicto_fase1(bien, ctrl_mal).startswith("INVÁLIDO"))
        self.assertEqual(v.veredicto_fase1([("x", False, "")], ctrl_ok), "FALLÓ")
        self.assertEqual(v.veredicto_fase1([("x", False, "")], ctrl_mal), "FALLÓ")  # un fallo real prima sobre lo inválido


def runner_simulado(debilitar=None, rc_principal=0, salida_roja=None):
    """Simula `docker run`: devuelve lo que se vería con las banderas seguras y algo DISTINTO en cada control negativo.
    `debilitar=<clave>` hace que ese control devuelva lo mismo que el contenedor seguro (un control que no distingue).
    """
    seguro = {
        "red": "lo\n",
        "montajes": MONTAJES_BUENOS,
        "socket": "",
        "memoria": "1610612736\n",
        "pids": "2048\n",
        "privilegios": "CapEff:\t000000000000000b\nNoNewPrivs:\t1\n",
        "entorno": "PATH=/usr/bin\nHOME=/root\n",
    }
    comandos_a_clave = {v.COMANDOS[k]: k for k in seguro}

    def correr(imagen, comando, banderas):
        es_control = banderas != v.BANDERAS_SEGURAS or comando.startswith("mkdir")
        clave = comandos_a_clave.get(comando) or ("socket" if comando.startswith("mkdir") else None)
        if not es_control:
            if salida_roja and clave == salida_roja[0]:
                return rc_principal, salida_roja[1]
            return rc_principal, seguro[clave]
        if clave == debilitar:
            return (
                0,
                seguro[clave],
            )  # el "control" devuelve lo mismo que el contenedor seguro: no distingue
        distinto = {
            "red": "eth0\nlo\n",
            "montajes": MONTAJES_BUENOS + "C:\\ /sondeo 9p ro 0 0\n",
            "socket": "/var/run/docker.sock\n",
            "memoria": "max\n",
            "pids": "max\n",
            "privilegios": "CapEff:\t00000000a80425fb\nNoNewPrivs:\t0\n",
            "entorno": "PATH=/usr/bin\nX_TOKEN=abc\n",
        }
        return 0, distinto[clave]

    return correr


class LogicaFase1(unittest.TestCase):
    CONTROLES = ("red", "montajes", "socket", "privilegios", "memoria", "pids", "entorno")

    def veredicto(self, **kw):
        comprobaciones, controles = v.ejecutar("img", correr_fn=runner_simulado(**kw))
        return v.veredicto_fase1(comprobaciones, controles), comprobaciones, controles

    def test_todo_correcto_con_siete_controles_que_distinguen(self):
        veredicto, comprobaciones, controles = self.veredicto()
        self.assertEqual(veredicto, "CORRECTO", [c for c in comprobaciones + controles if not c[1]])
        self.assertEqual(len(controles), 7)  # uno por comprobación (red, montajes, socket, privilegios, memoria, pids, credenciales)

    def test_si_cualquiera_de_los_siete_controles_no_distingue_el_resultado_es_invalido(self):
        for clave in self.CONTROLES:
            veredicto, _, controles = self.veredicto(debilitar=clave)
            self.assertTrue(veredicto.startswith("INVÁLIDO"), (clave, veredicto))
            self.assertEqual(len([c for c in controles if not c[1]]), 1, clave)  # justo ese control

    def test_una_comprobacion_que_falla_da_fallo(self):
        veredicto, _, _ = self.veredicto(salida_roja=("red", "eth0\nlo\n"))
        self.assertEqual(veredicto, "FALLÓ")

    def test_un_docker_run_fallido_no_se_da_por_bueno_hallazgo_bajo(self):
        # un `docker run` que falla (rc!=0) y devuelve salida vacía NO debe contar como "sin montajes / sin credenciales"
        comprobaciones, controles = v.ejecutar("img", correr_fn=runner_simulado(rc_principal=125))
        self.assertNotEqual(v.veredicto_fase1(comprobaciones, controles), "CORRECTO")
        self.assertFalse(comprobaciones[0][1])  # la comprobación de "los contenedores se ejecutaron" falla
        # salida vacía con rc=0 tampoco vale (faltan /proc y PATH)
        comprobaciones, controles = v.ejecutar("img", correr_fn=lambda i, c, b: (0, ""))
        self.assertFalse(comprobaciones[0][1])

    def test_controles_que_fallan_al_ejecutarse_invalidan_ronda3_hallazgo_medio(self):
        # los contenedores principales van bien, pero TODOS los de control fallan (rc 125, salida vacía): antes pasaban como "válidos"
        seguro = runner_simulado()

        def correr(imagen, comando, banderas):
            if banderas != v.BANDERAS_SEGURAS or comando.startswith("mkdir"):
                return 125, ""
            return seguro(imagen, comando, banderas)

        comprobaciones, controles = v.ejecutar("img", correr_fn=correr)
        self.assertTrue(all(not c[1] for c in controles), controles)
        self.assertTrue(v.veredicto_fase1(comprobaciones, controles).startswith("INVÁLIDO"))


def contenedor_real(perfil=PERFIL):
    """Réplica de los campos de seguridad del contenedor REAL que creó Hermes (docker inspect, 2026-11-22)."""
    binds = [ntpath.join(perfil, rel) + ":/root/.hermes/" + rel.replace("\\", "/") + ":ro" for rel in v.MONTAJES_PERMITIDOS]
    mounts = [
        {
            "Type": "bind",
            "Source": ntpath.join(perfil, rel),
            "Destination": "/root/.hermes/" + rel.replace("\\", "/"),
            "Mode": "ro",
            "RW": False,
            "Propagation": "rprivate",
        }
        for rel in v.MONTAJES_PERMITIDOS
    ]
    return {
        "Name": "/hermes-c797eb82",
        "Config": {
            "Image": IMG,
            "User": "",
            "Labels": {
                "hermes-agent": "1",
                "hermes-egress": "off",
                "hermes-profile": ntpath.basename(perfil),
            },
            "Env": [
                "PATH=/usr/local/bin",
                "LANG=C.UTF-8",
                "GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305",
                "PYTHON_VERSION=3.12.16",
                "PYTHON_SHA256=abc",
            ],
        },
        "HostConfig": {
            "Privileged": False,
            "NetworkMode": "none",
            "CapAdd": ["CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_SETGID", "CAP_SETUID"],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 1610612736,
            "MemorySwap": 3221225472,
            "NanoCpus": 1000000000,
            "PidsLimit": 2048,
            "Devices": None,
            "PidMode": "",
            "IpcMode": "private",
            "UsernsMode": "",
            "UTSMode": "",
            "CgroupnsMode": "private",
            "Runtime": "runc",
            "CgroupParent": "",
            "PublishAllPorts": False,
            "PortBindings": {},
            "VolumesFrom": None,
            "Sysctls": None,
            "DeviceRequests": None,
            "DeviceCgroupRules": None,
            "ExtraHosts": None,
            "GroupAdd": None,
            "Links": None,
            "Binds": binds,
        },
        "Mounts": mounts,
        "NetworkSettings": {"Networks": {"none": {}}},
    }


class Fase2(unittest.TestCase):
    def fallos(self, c, perfil=PERFIL):
        return [n for n, ok, _ in v.evaluar_contenedor(c, perfil) if not ok]

    def con(self, **cambios_hostconfig):
        c = contenedor_real()
        c["HostConfig"].update(cambios_hostconfig)
        return c

    def test_el_contenedor_real_de_hermes_pasa(self):
        self.assertEqual(self.fallos(contenedor_real()), [])

    def test_cada_degradacion_se_detecta(self):
        casos = {
            "imagen exacta esperada (digest)": lambda c: c["Config"].update(Image="python@sha256:" + "0" * 64),
            "contenedor creado por Hermes para el perfil esperado": lambda c: c["Config"].update(Labels={}),
            "no privilegiado": lambda c: c["HostConfig"].update(Privileged=True),
            "sin red": lambda c: c["HostConfig"].update(NetworkMode="bridge"),
            "CapDrop ALL": lambda c: c["HostConfig"].update(CapDrop=[]),
            "capacidades añadidas dentro de las conocidas de Hermes": lambda c: c["HostConfig"]["CapAdd"].append("CAP_SYS_ADMIN"),
            "memoria acotada": lambda c: c["HostConfig"].update(Memory=0),
            "procesos acotados": lambda c: c["HostConfig"].update(PidsLimit=-1),
            "CPU acotada": lambda c: c["HostConfig"].update(NanoCpus=2_000_000_000),
            "sin dispositivos del host": lambda c: c["HostConfig"].update(Devices=[{"PathOnHost": "/dev/kvm"}]),
        }
        for nombre, rompe in casos.items():
            c = contenedor_real()
            rompe(c)
            self.assertTrue(any(nombre in f for f in self.fallos(c)), nombre)

    def test_security_opt_debilitado_no_pasa_hallazgo_3(self):
        for opciones in (
            ["no-new-privileges:false"],
            ["no-new-privileges", "seccomp=unconfined"],
            ["apparmor=unconfined"],
            ["label=disable"],
            ["no-new-privileges", "label=disable"],
            [],
            ["seccomp=unconfined"],
        ):
            self.assertTrue(
                any("SecurityOpt" in f for f in self.fallos(self.con(SecurityOpt=opciones))),
                opciones,
            )
        for ok in (["no-new-privileges"], ["no-new-privileges:true"], ["no-new-privileges=true"]):
            self.assertEqual(self.fallos(self.con(SecurityOpt=ok)), [], ok)

    def test_campos_de_hostconfig_no_evaluados_antes_hallazgo_4(self):
        casos = {
            "swap acotado": dict(MemorySwap=-1),  # swap ilimitado: el tope de memoria sería ficticio
            "swap acotado ": dict(MemorySwap=0),
            "namespaces y runtime": dict(UsernsMode="host"),
            "namespaces y runtime ": dict(UTSMode="host"),
            "namespaces y runtime  ": dict(Runtime="nvidia"),
            "namespaces y runtime   ": dict(CgroupnsMode="host"),
            "namespaces y runtime    ": dict(PidMode="host"),
            "campos de HostConfig que deben estar vacíos": dict(VolumesFrom=["otro-contenedor"]),
            "campos de HostConfig que deben estar vacíos ": dict(Sysctls={"net.ipv4.ip_forward": "1"}),
            "campos de HostConfig que deben estar vacíos  ": dict(DeviceRequests=[{"Driver": "nvidia"}]),
            "campos de HostConfig que deben estar vacíos   ": dict(ExtraHosts=["x:1.2.3.4"]),
            "campos de HostConfig que deben estar vacíos    ": dict(PortBindings={"80/tcp": [{"HostPort": "80"}]}),
        }
        for etiqueta, cambio in casos.items():
            self.assertTrue(
                any(etiqueta.strip() in f for f in self.fallos(self.con(**cambio))),
                (etiqueta, cambio),
            )

    def test_tipos_estrictos_un_booleano_no_cuenta_como_numero(self):
        self.assertTrue(any("memoria" in f for f in self.fallos(self.con(Memory=True))))

    def test_imagen_con_digest_distinto_hallazgo_5(self):
        c = contenedor_real()
        c["Config"]["Image"] = "python@sha256:" + "a" * 64  # otra imagen, también "con digest"
        self.assertTrue(any("imagen exacta" in f for f in self.fallos(c)))
        c["Config"]["Image"] = "python:3.12-slim"  # solo etiqueta
        self.assertTrue(any("imagen exacta" in f for f in self.fallos(c)))

    def test_montaje_con_mount_que_no_aparece_en_binds_hallazgo_1(self):
        c = contenedor_real()
        c["HostConfig"]["Binds"] = []  # --mount type=bind no rellena Binds
        c["Mounts"] = [{"Type": "bind", "Source": "C:\\", "Destination": "/h", "Mode": "ro", "RW": False}]
        f = self.fallos(c)
        self.assertTrue(any("EXACTAMENTE una de las 14" in x for x in f), f)

    def test_raiz_del_perfil_y_carpetas_no_conocidas_hallazgo_2(self):
        for fuente in (
            PERFIL,
            PERFIL + "\\",
            PERFIL + "\\sessions",
            PERFIL + "\\logs",
            PERFIL + "\\home",
            PERFIL + "\\skills\\otra",
            r"C:\Users\PC\AppData\Local\hermes\profiles\agente-otro\skills",
            r"C:\Users\PC\AppData\Local\hermes\skills",
        ):
            c = contenedor_real()
            c["HostConfig"]["Binds"].append(fuente + ":/x:ro")
            self.assertTrue(any("EXACTAMENTE una de las 14" in x for x in self.fallos(c)), fuente)

    def test_normalizacion_de_rutas_de_windows(self):
        c = contenedor_real()
        # mismas carpetas con otras mayúsculas y barras: siguen siendo las permitidas
        c["Mounts"][0]["Source"] = c["Mounts"][0]["Source"].replace("\\", "/").lower()
        c["HostConfig"]["Binds"][0] = c["Mounts"][0]["Source"] + ":/root/.hermes/cache/documents:ro"
        self.assertEqual(self.fallos(c), [])
        c = contenedor_real()
        c["Mounts"][0]["Source"] = PERFIL + "\\cache\\..\\.."  # .. que escapa del perfil
        self.assertTrue(any("EXACTAMENTE una de las 14" in x for x in self.fallos(c)))

    def test_montajes_escribibles_tipos_raros_socket_y_secretos(self):
        c = contenedor_real()
        c["HostConfig"]["Binds"][0] = c["HostConfig"]["Binds"][0].replace(":ro", ":rw")
        self.assertTrue(any("solo lectura" in n for n in self.fallos(c)))
        c = contenedor_real()
        c["Mounts"][0]["RW"] = True  # aunque Binds diga ro
        self.assertTrue(any("solo lectura" in n for n in self.fallos(c)))
        c = contenedor_real()
        c["Mounts"].append(
            {
                "Type": "volume",
                "Name": "x",
                "Source": "/var/lib/docker/volumes/x",
                "Destination": "/v",
                "RW": False,
            }
        )
        self.assertTrue(any("solo tipo bind" in n for n in self.fallos(c)))
        c = contenedor_real()
        c["Mounts"].append(
            {
                "Type": "npipe",
                "Source": r"\\.\pipe\docker_engine",
                "Destination": r"\\.\pipe\docker_engine",
                "RW": False,
            }
        )
        self.assertTrue(any("solo tipo bind" in n or "socket" in n for n in self.fallos(c)))
        for sospechoso in ("\\.env", "\\auth.json", "\\config.yaml", "\\.ssh", "\\x.pem"):
            c = contenedor_real()
            c["Mounts"].append(
                {
                    "Type": "bind",
                    "Source": PERFIL + sospechoso,
                    "Destination": "/x",
                    "Mode": "ro",
                    "RW": False,
                }
            )
            self.assertTrue(any("configuración o credenciales" in n for n in self.fallos(c)), sospechoso)
        c = contenedor_real()
        c["HostConfig"]["Binds"].append(PERFIL + r"\cache:/var/run/docker.sock:ro")
        self.assertTrue(any("socket de Docker" in n for n in self.fallos(c)))

    def test_credenciales_en_el_entorno_de_creacion(self):
        c = contenedor_real()
        c["Config"]["Env"].append("ANTHROPIC_API_KEY=sk-x")
        self.assertTrue(any("credenciales" in n for n in self.fallos(c)))
        c = contenedor_real()
        c["Config"]["Env"].append("FOO=ghp_abcdefghijklmnopqrstuvwx")
        self.assertTrue(any("credenciales" in n for n in self.fallos(c)))

    def test_datos_ausentes_no_se_dan_por_buenos(self):
        self.assertTrue(self.fallos({}))  # un inspect vacío no pasa

    def test_campos_que_la_auditoria_vio_sin_test(self):
        casos = [
            ("namespaces y runtime", dict(IpcMode="host")),
            ("namespaces y runtime", dict(CgroupParent="/otro")),
            ("namespaces y runtime", dict(PublishAllPorts=True)),
            ("campos de HostConfig que deben estar vacíos", dict(GroupAdd=["docker"])),
            ("campos de HostConfig que deben estar vacíos", dict(Links=["otro:alias"])),
            ("campos de HostConfig que deben estar vacíos", dict(DeviceCgroupRules=["c *:* rwm"])),
            ("swap acotado", dict(MemorySwap=1610612735)),  # swap menor que la memoria
            ("swap acotado", dict(MemorySwap=3221225473)),  # más del doble
            ("swap acotado", dict(MemorySwap=True)),
            ("CPU acotada", dict(NanoCpus=True)),
            ("CPU acotada", dict(NanoCpus=0)),
            ("CPU acotada", dict(NanoCpus=None)),
        ]
        for nombre, cambio in casos:
            self.assertTrue(any(nombre in f for f in self.fallos(self.con(**cambio))), (nombre, cambio))

    def test_propagacion_de_montajes_distinta_de_privada(self):
        c = contenedor_real()
        c["HostConfig"]["Binds"][0] = c["HostConfig"]["Binds"][0] + ",rshared"
        self.assertTrue(any("propagación" in f for f in self.fallos(c)))
        c = contenedor_real()
        c["Mounts"][0]["Propagation"] = "rshared"
        self.assertTrue(any("propagación" in f for f in self.fallos(c)))
        c = contenedor_real()
        c["HostConfig"]["Binds"][0] = c["HostConfig"]["Binds"][0] + ",z"  # modo de etiqueta SELinux: no es propagación
        self.assertEqual(self.fallos(c), [])

    def test_mas_formatos_de_credenciales(self):
        for variable in (
            "PRIVATE_KEY=x",
            "MY_ACCESS_KEY=x",
            "FOO=github_pat_abcdefghijklmnopqrstuvwxyz",
            "FOO=sk_live_abcdefghijkl",
            "FOO=eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NTY3ODkw",
        ):
            c = contenedor_real()
            c["Config"]["Env"].append(variable)
            self.assertTrue(any("credenciales" in f for f in self.fallos(c)), variable.split("=")[0])
        c = contenedor_real()
        self.assertEqual(self.fallos(c), [])  # GPG_KEY (público) no debe dar falso positivo


class EnlacesYFase2Completa(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self.t.name)  # sin enlaces ni nombres 8.3: buscar_enlaces compara con realpath
        self.perfil = os.path.join(self.base, "profiles", "agente")
        for rel in v.MONTAJES_PERMITIDOS:
            os.makedirs(os.path.join(self.perfil, rel))
        with open(os.path.join(self.perfil, "skills", "ejemplo.md"), "w") as f:
            f.write("x")

    def tearDown(self):
        self.t.cleanup()

    def crear_enlace(self, enlace, destino):
        try:
            os.symlink(destino, enlace, target_is_directory=True)
            return True
        except OSError:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", enlace, destino], capture_output=True)
            return r.returncode == 0

    def ejecutar_main(self, inspect):
        ruta = os.path.join(self.t.name, "inspect.json")
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump([inspect], f)
        salida = os.path.join(self.t.name, "informe.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = v.main(["--json", ruta, "--perfil-dir", self.perfil, "--salida", salida])
        return rc, buf.getvalue(), salida

    def test_buscar_enlaces_detecta_junction_o_symlink_y_carpetas_ausentes(self):
        limpia = os.path.join(self.perfil, "skills")
        self.assertEqual(v.buscar_enlaces([limpia])[0], [])
        fuera = os.path.join(self.base, "fuera")
        os.makedirs(fuera)
        if not self.crear_enlace(os.path.join(limpia, "enlace"), fuera):
            self.skipTest("no se pudo crear symlink/junction")
        enlaces, revisados, _, _ = v.buscar_enlaces([limpia])
        self.assertEqual((len(enlaces), revisados), (1, 1))
        _, revisados, ausentes, _ = v.buscar_enlaces([os.path.join(self.base, "no-existe")])
        self.assertEqual((revisados, len(ausentes)), (0, 1))

    def test_la_propia_carpeta_montada_es_una_junction_hallazgo_alto(self):
        # HALLAZGO ALTO de la 2.ª ronda: profiles\agente\skills -> carpeta ajena; la Source sigue pareciendo permitida
        ajeno = os.path.join(self.base, "ajeno")
        os.makedirs(ajeno)
        with open(os.path.join(ajeno, "secreto.txt"), "w") as f:
            f.write("x")
        skills = os.path.join(self.perfil, "skills")
        shutil_rmtree(skills)
        if not self.crear_enlace(skills, ajeno):
            self.skipTest("no se pudo crear symlink/junction")
        enlaces, revisados, _, _ = v.buscar_enlaces([skills])
        self.assertEqual(revisados, 1)
        self.assertTrue(enlaces and "enlace" in enlaces[0], enlaces)
        rc, texto, _ = self.ejecutar_main(contenedor_real(self.perfil))
        self.assertEqual(rc, 1)
        self.assertIn("FALLA sin enlaces", texto)

    def test_un_directorio_padre_es_un_enlace(self):
        real = os.path.join(self.base, "real")
        os.makedirs(os.path.join(real, "profiles", "agente", "skills"))
        puerta = os.path.join(self.base, "puerta")
        if not self.crear_enlace(puerta, real):
            self.skipTest("no se pudo crear symlink/junction")
        a_traves = os.path.join(puerta, "profiles", "agente", "skills")  # la carpeta existe, pero llegando por un enlace
        enlaces, revisados, _, _ = v.buscar_enlaces([a_traves])
        self.assertEqual(revisados, 1)
        self.assertTrue(enlaces, "un padre que es junction debe detectarse")

    def test_errores_de_lectura_y_tope_cuentan_como_no_verificado(self):
        original = v._es_enlace
        v._es_enlace = lambda p: None  # simula un archivo ilegible
        try:
            _, _, _, ilegibles = v.buscar_enlaces([os.path.join(self.perfil, "skills")])
        finally:
            v._es_enlace = original
        self.assertTrue(ilegibles)
        _, _, _, ilegibles = v.buscar_enlaces([os.path.join(self.perfil, "skills")], tope=0)  # 0 entradas permitidas
        self.assertTrue(any("tope" in x for x in ilegibles))

    def test_solo_se_recorren_las_fuentes_que_pasaron_la_lista_blanca(self):
        c = contenedor_real(self.perfil)
        c["Mounts"].append({"Type": "bind", "Source": "C:\\", "Destination": "/h", "Mode": "ro", "RW": False})
        c["HostConfig"]["Binds"].append("C:\\:/h:ro")
        recibido = []
        original = v.buscar_enlaces
        v.buscar_enlaces = lambda fuentes, *a, **k: (recibido.extend(fuentes) or ([], len(fuentes), [], []))
        try:
            rc, texto, _ = self.ejecutar_main(c)
        finally:
            v.buscar_enlaces = original
        self.assertEqual(rc, 1)  # la lista blanca ya la rechaza
        self.assertNotIn("C:\\", recibido)  # y NO se recorre C:\ (ni una ruta UNC de un inspect ajeno)
        self.assertEqual(len(recibido), 14)

    def test_fase2_de_punta_a_punta_correcta_y_con_informe_reproducible(self):
        rc, texto, salida = self.ejecutar_main(contenedor_real(self.perfil))
        self.assertEqual(rc, 0, texto)
        with open(salida, encoding="utf-8") as f:
            informe = json.load(f)
        self.assertEqual(informe["veredicto"], "CORRECTO")
        self.assertEqual(len(informe["inspect_sha256"]), 64)  # hash del inspect de entrada
        self.assertIn("HostConfig", informe["inspect"])  # el inspect crudo queda guardado para poder reauditar

    def test_fase2_de_punta_a_punta_falla_si_una_carpeta_montada_contiene_un_enlace(self):
        fuera = os.path.join(self.t.name, "fuera")
        os.makedirs(fuera)
        if not self.crear_enlace(os.path.join(self.perfil, "skills", "enlace"), fuera):
            self.skipTest("no se pudo crear symlink/junction")
        rc, texto, _ = self.ejecutar_main(contenedor_real(self.perfil))
        self.assertEqual(rc, 1)
        self.assertIn("FALLA sin enlaces simbólicos", texto)

    def test_fase2_falla_si_falta_aunque_sea_UNA_de_las_14_carpetas_hallazgo_medio(self):
        shutil_rmtree(os.path.join(self.perfil, "cache", "web"))  # 13 de 14 existen
        rc, texto, _ = self.ejecutar_main(contenedor_real(self.perfil))
        self.assertEqual(rc, 1)
        falla = [l for l in texto.splitlines() if l.startswith("FALLA")]
        self.assertEqual(len(falla), 1, falla)  # falla EXACTAMENTE la comprobación de enlaces/ausentes, ninguna otra
        self.assertIn("sin enlaces", falla[0])
        self.assertIn("no encontradas", falla[0])

    def test_fase2_con_todas_las_carpetas_ausentes_tambien_falla(self):
        otro = os.path.join(self.base, "otro-equipo", "profiles", "agente")
        c = contenedor_real(otro)
        buf = io.StringIO()
        ruta = os.path.join(self.base, "i.json")
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump([c], f)
        with redirect_stdout(buf):
            rc = v.main(["--json", ruta, "--perfil-dir", otro])  # coincide con el inspect pero no existe en este equipo
        self.assertEqual(rc, 1)
        self.assertIn("no encontradas", buf.getvalue())


def shutil_rmtree(ruta):
    import shutil

    shutil.rmtree(ruta)


if __name__ == "__main__":
    unittest.main()
