import json
import os
import tempfile
import threading
import unittest

from audit_log import AuditLog, verificar
from budget_guard import BudgetGuard, LimiteExcedido
from policy import Compuerta, Denegado, AprobacionRequerida

AQUI = os.path.dirname(os.path.abspath(__file__))


def leer(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def escribir(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


class T(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.raiz = os.path.join(self.t.name, "proy")
        os.makedirs(os.path.join(self.raiz, "workspace"))
        self.est = os.path.join(self.t.name, "estado")
        self.c = Compuerta(self.raiz, self.est)

    def tearDown(self):
        self.t.cleanup()

    # --- presupuesto ---
    def test_presupuesto_se_corta_y_persiste(self):
        ruta = os.path.join(self.t.name, "b.json")
        b = BudgetGuard(ruta, 100, 2)
        b.cobrar_accion(60)
        b.cobrar_accion(30)
        with self.assertRaises(LimiteExcedido):
            b.cobrar_accion(1)
        with self.assertRaises(LimiteExcedido):
            BudgetGuard(ruta, 100, 5).cobrar_accion(20)  # estado persistido: 90+20>100

    def test_tokens_invalidos_nan_inf_negativo_float_bool(self):
        b = BudgetGuard(os.path.join(self.t.name, "b.json"), 100, 10)
        for malo in (float("nan"), float("inf"), -1, 1.5, True, "5", None):
            with self.assertRaises(ValueError, msg=repr(malo)):
                b.cobrar_accion(malo)
        self.assertEqual(b.tokens, 0)

    def test_estado_corrupto_falla_cerrado(self):
        ruta = os.path.join(self.t.name, "b.json")
        for malo in (
            '{"tokens": NaN, "acciones": 0}',
            '{"tokens": -5, "acciones": 0}',
            "",
            '{"tokens": 1.5, "acciones": 0}',
        ):
            escribir(ruta, malo)
            with self.assertRaises(RuntimeError, msg=malo):
                BudgetGuard(ruta, 100, 10)

    def test_concurrencia_no_supera_el_tope(self):
        ruta = os.path.join(self.t.name, "b.json")
        ok, fallos = [], []

        def trabajo():
            g = BudgetGuard(ruta, 10**6, 20)  # instancia propia por hilo
            for _ in range(10):
                try:
                    g.cobrar_accion(1)
                    ok.append(1)
                except LimiteExcedido:
                    fallos.append(1)

        hs = [threading.Thread(target=trabajo) for _ in range(4)]
        [h.start() for h in hs]
        [h.join() for h in hs]
        self.assertEqual(len(ok), 20)  # exactamente el tope, ni una más
        self.assertEqual(len(fallos), 20)

    # --- auditoría ---
    def test_cadena_detecta_manipulacion_y_claves_extra(self):
        ruta = os.path.join(self.t.name, "a.jsonl")
        a = AuditLog(ruta)
        for i in range(3):
            a.registrar("e", {"i": i})
        self.assertTrue(verificar(ruta)[0])
        lineas = leer(ruta).splitlines()
        r = json.loads(lineas[1])
        r["datos"]["i"] = "99"
        lineas[1] = json.dumps(r)
        escribir(ruta, "\n".join(lineas) + "\n")
        self.assertFalse(verificar(ruta)[0])
        with self.assertRaises(RuntimeError):
            AuditLog(ruta)

    def test_clave_extra_y_linea_truncada(self):
        ruta = os.path.join(self.t.name, "a.jsonl")
        a = AuditLog(ruta)
        a.registrar("e")
        contenido = leer(ruta)
        r = json.loads(contenido)
        r["extra"] = "x"
        escribir(ruta, json.dumps(r) + "\n")
        self.assertFalse(verificar(ruta)[0])
        escribir(ruta, contenido[:-10])  # línea a medias, sin salto final
        self.assertFalse(verificar(ruta)[0])
        with self.assertRaises(RuntimeError):
            AuditLog(ruta)

    def test_dos_instancias_no_bifurcan_y_sustituto_suelto(self):
        ruta = os.path.join(self.t.name, "a.jsonl")
        a1, a2 = AuditLog(ruta), AuditLog(ruta)
        a1.registrar("x")
        a2.registrar("y")
        a1.registrar("z", {"raro": "\ud800 ok"})
        self.assertEqual(verificar(ruta), (True, "ok"))

    # --- rutas ---
    def test_lectura_confinada_y_secretos(self):
        self.c.escribir_archivo("workspace/ok.txt", "hola")
        self.assertEqual(self.c.leer_archivo("workspace/ok.txt"), "hola")
        malas = [
            "../fuera.txt",
            os.path.join(self.t.name, "otro.txt"),
            ".env",
            "sub/id_rsa",
            "x.PEM",
            "id_ed25519",
            ".npmrc",
            ".netrc",
            "a.p12",
            "sub/.aws/config",
            ".ssh/config",
            ".git/config",
            "secrets.json",
            "token.txt",
            "a.txt:.env",
            "x:stream",
            "CON",
            "nul.txt",
            "dir/COM1",
            r"\\?\C:\x",
            "A~1.TXT",
            "evil. ",
            "evil.",
            "",
            None,
            5,
            "a\x00b",
        ]
        for mala in malas:
            with self.assertRaises(Denegado, msg=repr(mala)):
                self.c.leer_archivo(mala)

    def test_symlink_o_junction_hacia_fuera_se_bloquea(self):
        fuera = os.path.join(self.t.name, "fuera")
        os.makedirs(fuera)
        escribir(os.path.join(fuera, "dato.txt"), "no")
        enlace = os.path.join(self.raiz, "workspace", "enlace")
        try:
            os.symlink(fuera, enlace, target_is_directory=True)
        except OSError:
            import subprocess

            r = subprocess.run(["cmd", "/c", "mklink", "/J", enlace, fuera], capture_output=True)
            if r.returncode != 0:
                self.skipTest("no se pudo crear symlink/junction")
        with self.assertRaises(Denegado):
            self.c.leer_archivo("workspace/enlace/dato.txt")
        with self.assertRaises(Denegado):
            self.c.escribir_archivo("workspace/enlace/nuevo.txt", "x")

    # --- escritura ---
    def test_escritura_solo_en_workspace_y_no_toca_la_compuerta(self):
        for mala in (
            "policy.json",
            "sandbox/policy.py",
            "../x.txt",
            "workspace",
            "workspace/a.py",
            "workspace/run.BAT",
            "workspace/x.ps1",
            "otro/a.txt",
        ):
            with self.assertRaises(Denegado, msg=mala):
                self.c.escribir_archivo(mala, "x")
        with self.assertRaises(Denegado):
            self.c.escribir_archivo("workspace/a.txt", 123)
        with self.assertRaises(Denegado):
            self.c.escribir_archivo("workspace/g.txt", "x" * 300000)
        self.c.escribir_archivo("workspace/sub/ok.txt", "bien")
        self.assertEqual(self.c.leer_archivo("workspace/sub/ok.txt"), "bien")

    def test_estado_no_puede_estar_en_zona_escribible(self):
        with self.assertRaises(ValueError):
            Compuerta(self.raiz, os.path.join(self.raiz, "workspace", "estado"))

    def test_denegaciones_quedan_en_el_registro(self):
        with self.assertRaises(Denegado):
            self.c.leer_archivo("a\x00b")
        with self.assertRaises(Denegado):
            self.c.leer_archivo("workspace/no_existe.txt")
        texto = leer(os.path.join(self.est, "auditoria.jsonl"))
        self.assertEqual(texto.count('"evento": "denegado"'), 2)

    # --- aprobaciones y ejecución ---
    def test_accion_con_aprobacion_nunca_se_ejecuta(self):
        for a in ("enviar_mensaje", "publicar", "cobrar", "borrar", "ejecutar"):
            with self.assertRaises(AprobacionRequerida):
                self.c._autorizar(a, {})
        with self.assertRaises(AprobacionRequerida):
            self.c.red("example.com")
        with self.assertRaises(Denegado):
            self.c._autorizar("inventada", {})
        with self.assertRaises(AprobacionRequerida):
            self.c.ejecutar(["python", "-c", "print(1)"])

    def test_ejecutar_con_politica_de_prueba_allowlist_y_entorno_limpio(self):
        pol = json.loads(leer(os.path.join(AQUI, "policy.json")))
        pol["acciones_permitidas"].append("ejecutar")  # SOLO en tests
        pol["acciones_con_aprobacion_humana"].remove("ejecutar")
        ruta = os.path.join(self.t.name, "pol.json")
        escribir(ruta, json.dumps(pol))
        c = Compuerta(self.raiz, os.path.join(self.t.name, "e3"), politica=ruta)
        with self.assertRaises(Denegado):
            c.ejecutar(["cmd", "/c", "dir"])
        os.environ["SECRETO_PRUEBA"] = "xyz"
        try:
            rc, out, _ = c.ejecutar(["python", "-c", "import os;print(os.environ.get('SECRETO_PRUEBA','ausente'))"])
        finally:
            del os.environ["SECRETO_PRUEBA"]
        self.assertEqual((rc, out.strip()), (0, "ausente"))

    def test_limite_de_acciones_y_registro_integro(self):
        c = Compuerta(self.raiz, os.path.join(self.t.name, "e2"))
        c.presupuesto.acciones_max = 2
        c.escribir_archivo("workspace/a.txt", "1")
        c.escribir_archivo("workspace/b.txt", "2")
        with self.assertRaises(Denegado):
            c.escribir_archivo("workspace/c.txt", "3")
        self.assertTrue(verificar(os.path.join(self.t.name, "e2", "auditoria.jsonl"))[0])


class T2(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.raiz = os.path.join(self.t.name, "proy")
        os.makedirs(os.path.join(self.raiz, "workspace"))
        self.c = Compuerta(self.raiz, os.path.join(self.t.name, "estado"))

    def tearDown(self):
        self.t.cleanup()

    def test_escritura_lista_blanca_de_extensiones(self):
        for mala in (
            "workspace/.exe",
            "workspace/a.hta",
            "workspace/x.url",
            "workspace/y.scr",
            "workspace/z.pyz",
            "workspace/w.jar",
            "workspace/sin_extension",
            "workspace/q.reg",
        ):
            with self.assertRaises(Denegado, msg=mala):
                self.c.escribir_archivo(mala, "x")
        for buena in ("workspace/a.txt", "workspace/b.json", "workspace/c.MD", "workspace/d.csv"):
            self.c.escribir_archivo(buena, "ok")

    def test_dispositivos_con_superindices_y_com0(self):
        for mala in ("COM¹", "LPT²", "COM0", "lpt³.txt", "dir/AUX", "CONIN$"):
            with self.assertRaises(Denegado, msg=repr(mala)):
                self.c.leer_archivo(mala)

    def test_bloqueo_huerfano_se_recupera(self):
        import time

        ruta = os.path.join(self.t.name, "b.json")
        lock = ruta + ".lock"
        escribir(lock, "")
        viejo = time.time() - 120
        os.utime(lock, (viejo, viejo))  # un proceso murió hace 2 minutos sin soltar el bloqueo
        BudgetGuard(ruta, 100, 5).cobrar_accion(1)  # no debe colgarse ni fallar
        self.assertFalse(os.path.exists(lock))

    def test_bloqueo_reciente_no_se_roba_y_falla_cerrado(self):
        from _lock import bloqueo, BloqueoNoObtenido

        ruta = os.path.join(self.t.name, "b.json")
        with bloqueo(ruta):
            with self.assertRaises(BloqueoNoObtenido):
                with bloqueo(ruta, timeout=0.2):
                    pass

    def test_concurrencia_entre_procesos_reales(self):
        import subprocess
        import sys

        ruta = os.path.join(self.t.name, "p.json")
        codigo = (
            "import sys;sys.path.insert(0,%r);from budget_guard import BudgetGuard,LimiteExcedido;"
            "g=BudgetGuard(%r,10**6,20);ok=0\n"
            "for _ in range(10):\n"
            "    try: g.cobrar_accion(1);ok+=1\n"
            "    except LimiteExcedido: pass\n"
            "print(ok)"
        ) % (AQUI, ruta)
        ps = [subprocess.Popen([sys.executable, "-c", codigo], stdout=subprocess.PIPE, text=True) for _ in range(4)]
        total = sum(int(p.communicate()[0].strip()) for p in ps)
        self.assertEqual(total, 20)  # 4 procesos x 10 intentos, tope 20: ni uno más
        self.assertEqual(BudgetGuard(ruta, 10**6, 20).acciones, 20)

    def test_presupuesto_no_disponible_deniega_y_no_revienta(self):
        escribir(os.path.join(self.t.name, "estado", "presupuesto.json"), "basura")
        with self.assertRaises(Denegado):
            self.c.leer_archivo("workspace/x.txt")


if __name__ == "__main__":
    unittest.main()
