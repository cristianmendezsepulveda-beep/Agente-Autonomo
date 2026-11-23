import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.realpath(os.path.join(AQUI, ".."))
sys.path.insert(0, AQUI)
import servidor


def rpc(id_, metodo, params=None):
    m = {"jsonrpc": "2.0", "id": id_, "method": metodo}
    if params is not None:
        m["params"] = params
    return m


def texto(resp):
    return json.loads(resp["result"]["content"][0]["text"])


class Logica(unittest.TestCase):
    """Servicio con raíz y estado temporales: no toca el proyecto real."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.raiz = os.path.join(self.t.name, "proy")
        os.makedirs(os.path.join(self.raiz, "workspace"))
        self.s = servidor.Servicio(self.raiz, os.path.join(self.t.name, "estado"),
                                   os.path.join(RAIZ, "agente", "catalogo.json"))

    def tearDown(self):
        self.t.cleanup()

    def llamar(self, nombre, args):
        return servidor.manejar(self.s, rpc(1, "tools/call", {"name": nombre, "arguments": args}))

    def test_handshake_y_lista(self):
        r = servidor.manejar(self.s, rpc(1, "initialize", {}))
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIsNone(servidor.manejar(self.s, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        nombres = [t["name"] for t in servidor.manejar(self.s, rpc(2, "tools/list"))["result"]["tools"]]
        self.assertEqual(nombres, ["consultar_catalogo", "cotizar", "escalar_a_humano", "listar_catalogo",
                                   "crm_buscar_contacto", "crm_registrar_lead", "crm_registrar_nota", "crm_crear_tarea",
                                   "correo_nuevos", "correo_captar", "correo_descartar",
                                   "seguimiento_pendientes", "cobranza_evaluar", "cobranza_preparar"])

    def test_catalogo_malformado_falla_al_construir_con_valueerror(self):
        # un catálogo real con un precio no numérico debe rechazarse al arrancar (ValueError), no soltar un
        # KeyError/InvalidOperation opaco en la primera cotización
        cat = json.load(open(os.path.join(RAIZ, "agente", "catalogo.json"), encoding="utf-8"))
        cat["items"][0]["precio_unitario"] = "N/A"
        ruta = os.path.join(self.t.name, "cat_malo.json")
        open(ruta, "w", encoding="utf-8").write(json.dumps(cat))
        with self.assertRaises(ValueError):
            servidor.Servicio(os.path.join(self.t.name, "p2"), os.path.join(self.t.name, "e2"), ruta)

    def test_consulta_faq_y_no_repite_el_texto(self):
        r = self.llamar("consultar_catalogo", {"texto": "¿Cuál es su horario? MARCADOR_UNICO"})
        self.assertEqual(texto(r)["tipo"], "faq")
        self.assertNotIn("MARCADOR_UNICO", json.dumps(r))
        log = open(os.path.join(self.t.name, "estado", "auditoria.jsonl"), encoding="utf-8").read()
        self.assertNotIn("MARCADOR_UNICO", log)  # el log guarda el tipo y el largo, no el texto

    def test_cotizar_guarda_borrador_en_workspace(self):
        r = self.llamar("cotizar", {"lineas": [{"sku": "SRV-ESTANDAR", "cantidad": 2}], "descuento_pct": 10})
        out = texto(r)
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(out["total"], "162.00")
        arch = os.path.join(self.raiz, "workspace", "borradores", out["borrador"] + ".json")
        self.assertEqual(json.load(open(arch, encoding="utf-8"))["estado"], "borrador_pendiente_aprobacion")

    def test_los_borradores_salen_sellados_y_la_bandeja_los_aprueba_de_extremo_a_extremo(self):
        sys.path.insert(0, os.path.join(RAIZ, "agente"))
        import bandeja
        bid = texto(self.llamar("cotizar", {"lineas": [{"sku": "SRV-ESTANDAR", "cantidad": 1}]}))["borrador"]
        arch = os.path.join(self.raiz, "workspace", "borradores", bid + ".json")
        d = json.load(open(arch, encoding="utf-8"))
        self.assertEqual(bandeja.estado_sello(d), "intacto")
        self.assertEqual([i["id"] for i in bandeja.pendientes(self.raiz)], [bid])
        visto = bandeja.hash_contenido(bandeja.ver(bid, self.raiz)[0])  # lo que la persona vería
        reg = json.load(open(bandeja.decidir(bid, "aprobado", self.raiz, quien="prueba", hash_esperado=visto), encoding="utf-8"))
        self.assertEqual((reg["sello"], reg["categoria"]), ("intacto", "cotizacion"))
        self.assertFalse(os.path.exists(arch))

    def test_cotizar_sobre_el_maximo_marca_aprobacion(self):
        out = texto(self.llamar("cotizar", {"lineas": [{"sku": "VISITA", "cantidad": 1}], "descuento_pct": 50}))
        self.assertTrue(out["requiere_aprobacion_humana"])

    def test_escalar_crea_archivo(self):
        out = texto(self.llamar("escalar_a_humano", {"motivo": "cliente molesto\x1b[0m", "referencia": "chat-A1"}))
        arch = os.path.join(self.raiz, "workspace", "escalados", out["escalado"] + ".json")
        self.assertNotIn("\x1b", open(arch, encoding="utf-8").read())

    def test_validacion_devuelve_error_sin_traza(self):
        casos = [("cotizar", {"lineas": [{"sku": "X", "cantidad": 1}]}), ("cotizar", {}), ("cotizar", {"lineas": [], "otro": 1}),
                 ("consultar_catalogo", {"texto": 5}), ("consultar_catalogo", {"texto": "a", "extra": 1}),
                 ("escalar_a_humano", {"motivo": 7}), ("inexistente", {}), ("cotizar", [1])]
        for nombre, args in casos:
            r = self.llamar(nombre, args)
            self.assertTrue(r["result"]["isError"], (nombre, args))
            self.assertNotIn("Traceback", json.dumps(r))

    def test_limite_de_acciones_se_traduce_en_error_no_en_caida(self):
        self.s.c.presupuesto.acciones_max = 2
        self.llamar("consultar_catalogo", {"texto": "horario"})
        self.llamar("consultar_catalogo", {"texto": "horario"})
        r = self.llamar("consultar_catalogo", {"texto": "horario"})
        self.assertTrue(r["result"]["isError"])

    def test_protocolo_invalido(self):
        for m in ({}, {"jsonrpc": "1.0", "id": 1, "method": "x"}, {"jsonrpc": "2.0", "id": 1}, [1]):
            self.assertEqual(servidor.manejar(self.s, m)["error"]["code"], -32600)
        self.assertEqual(servidor.manejar(self.s, rpc(1, "no/existe"))["error"]["code"], -32601)
        self.assertEqual(servidor.manejar(self.s, rpc(1, "tools/call", {"name": 5}))["error"]["code"], -32602)


class Proceso(unittest.TestCase):
    """Arranca el servidor real por stdio sobre una COPIA del proyecto (no escribe en el proyecto real)."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.copia = os.path.join(self.t.name, "p")
        for d in ("mcp", "sandbox", "agente"):
            shutil.copytree(os.path.join(RAIZ, d), os.path.join(self.copia, d),
                            ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        os.makedirs(os.path.join(self.copia, "workspace"))

    def tearDown(self):
        self.t.cleanup()

    def correr(self, entradas):
        p = subprocess.run([sys.executable, "-I", os.path.join(self.copia, "mcp", "servidor.py")],
                           input="".join(json.dumps(e) + "\n" for e in entradas) if isinstance(entradas, list) else entradas,
                           capture_output=True, text=True, timeout=60)
        return p

    def test_sesion_completa_por_stdio(self):
        p = self.correr([rpc(1, "initialize", {}), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                         rpc(2, "tools/list"), rpc(3, "tools/call", {"name": "consultar_catalogo", "arguments": {"texto": "horario"}})])
        self.assertEqual(p.returncode, 0, p.stderr)
        salidas = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([s["id"] for s in salidas], [1, 2, 3])  # la notificación no genera respuesta
        self.assertEqual(texto(salidas[2])["tipo"], "faq")

    def test_json_invalido_no_tumba_el_servidor(self):
        p = self.correr("no es json\n" + json.dumps(rpc(1, "ping")) + "\n")
        self.assertEqual(p.returncode, 0)
        salidas = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual(salidas[0]["error"]["code"], -32700)
        self.assertEqual(salidas[1]["result"], {})

    def test_manifiesto_modificado_impide_arrancar(self):
        ruta = os.path.join(self.copia, "mcp", "servidor.py")
        s = open(ruta, encoding="utf-8").read()
        s = s.replace("Responde una consulta de un cliente", "Responde y ENVÍA una consulta de un cliente", 1)  # descripción alterada
        open(ruta, "w", encoding="utf-8").write(s)
        p = self.correr([rpc(1, "ping")])
        self.assertEqual(p.returncode, 3)
        self.assertEqual(p.stdout, "")  # no respondió nada

    def test_pin_ausente_impide_arrancar(self):
        os.remove(os.path.join(self.copia, "mcp", "manifiesto.sha256"))
        self.assertEqual(self.correr([rpc(1, "ping")]).returncode, 3)

    def test_catalogo_malo_con_pin_coherente_sale_con_codigo_4(self):
        # catálogo malformado PERO con el pin re-fijado (así no es un fallo de pin): el servidor valida al arrancar
        # y sale con código 4, no con un error opaco en la primera consulta
        cat_ruta = os.path.join(self.copia, "agente", "catalogo.json")
        cat = json.load(open(cat_ruta, encoding="utf-8"))
        cat["items"][0]["precio_unitario"] = "N/A"
        open(cat_ruta, "w", encoding="utf-8").write(json.dumps(cat))
        srv = os.path.join(self.copia, "mcp", "servidor.py")
        f = subprocess.run([sys.executable, "-I", srv, "--fijar"], input="FIJAR\n", capture_output=True, text=True, timeout=60)
        self.assertEqual(f.returncode, 0, f.stderr)  # pin re-fijado sobre el catálogo malo
        p = self.correr([rpc(1, "ping")])
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn("configuración inválida", p.stderr)
        self.assertEqual(p.stdout, "")


class Endurecimiento(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.copia = os.path.join(self.t.name, "p")
        for d in ("mcp", "sandbox", "agente"):
            shutil.copytree(os.path.join(RAIZ, d), os.path.join(self.copia, d),
                            ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        os.makedirs(os.path.join(self.copia, "workspace"))
        self.servidor = os.path.join(self.copia, "mcp", "servidor.py")

    def tearDown(self):
        self.t.cleanup()

    def correr_bytes(self, datos):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}  # sin ayudas de codificación
        return subprocess.run([sys.executable, "-I", self.servidor], input=datos, capture_output=True, timeout=120, env=env)

    def salidas(self, p):
        return [json.loads(l) for l in p.stdout.decode("ascii").splitlines()]

    def test_utf8_crudo_en_stdin_no_mata_ni_degrada_la_deteccion(self):
        msg = rpc(1, "tools/call", {"name": "consultar_catalogo", "arguments": {"texto": "Quiero la devolución, horario ÁÍ"}})
        p = self.correr_bytes((json.dumps(msg, ensure_ascii=False) + "\n" + json.dumps(rpc(2, "ping")) + "\n").encode("utf-8"))
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.salidas(p)
        self.assertEqual(texto(s[0])["tipo"], "escalar")  # "devolución" se reconoció pese a los bytes crudos
        self.assertEqual(s[1]["result"], {})

    def test_bytes_invalidos_no_matan_el_servidor(self):
        p = self.correr_bytes(b"\xff\xfe\x81 basura\n" + json.dumps(rpc(1, "ping")).encode() + b"\n")
        self.assertEqual(p.returncode, 0)
        s = self.salidas(p)
        self.assertEqual(s[0]["error"]["code"], -32700)
        self.assertEqual(s[1]["result"], {})

    def test_json_muy_anidado_no_tumba_el_servidor(self):
        p = self.correr_bytes(b"[" * 100000 + b"\n" + json.dumps(rpc(1, "ping")).encode() + b"\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.salidas(p)
        self.assertEqual(s[0]["error"]["code"], -32700)
        self.assertEqual(s[1]["result"], {})

    def test_linea_enorme_se_descarta_sin_cargarla_entera(self):
        p = self.correr_bytes(b"a" * 1_500_000 + b"\n" + json.dumps(rpc(1, "ping")).encode() + b"\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.salidas(p)
        self.assertEqual(s[0]["error"]["code"], -32600)
        self.assertEqual(s[1]["result"], {})

    def test_el_pin_cubre_codigo_y_datos_no_solo_las_descripciones(self):
        for rel in ("agente/catalogo.json", "agente/consultas.py", "agente/cotizador.py", "sandbox/policy.py",
                    "sandbox/policy.json", "mcp/policy.json"):
            ruta = os.path.join(self.copia, rel)
            with open(ruta, "rb") as f:
                original = f.read()
            with open(ruta, "ab") as f:
                f.write(b"\n")  # un cambio mínimo
            p = self.correr_bytes(json.dumps(rpc(1, "ping")).encode() + b"\n")
            self.assertEqual((rel, p.returncode, p.stdout), (rel, 3, b""))
            with open(ruta, "wb") as f:
                f.write(original)

    def test_fijar_con_un_archivo_ausente_conserva_el_pin_anterior(self):
        pin = os.path.join(self.copia, "mcp", "manifiesto.sha256")
        with open(pin, "rb") as f:
            antes = f.read()
        os.remove(os.path.join(self.copia, "agente", "catalogo.json"))
        p = subprocess.run([sys.executable, "-I", self.servidor, "--fijar"], input=b"FIJAR\n", capture_output=True)
        self.assertEqual(p.returncode, 1)
        with open(pin, "rb") as f:
            self.assertEqual(f.read(), antes)  # antes quedaba truncado
        self.assertFalse(os.path.exists(pin + ".tmp"))

    def test_fijar_exige_escribir_fijar(self):
        pin = os.path.join(self.copia, "mcp", "manifiesto.sha256")
        with open(pin, "rb") as f:
            antes = f.read()
        with open(os.path.join(self.copia, "agente", "catalogo.json"), "ab") as f:
            f.write(b"\n")
        p = subprocess.run([sys.executable, "-I", self.servidor, "--fijar"], input=b"no\n", capture_output=True)
        self.assertEqual(p.returncode, 1)
        with open(pin, "rb") as f:
            self.assertEqual(f.read(), antes)  # no cambió
        p = subprocess.run([sys.executable, "-I", self.servidor, "--fijar"], input=b"FIJAR\n", capture_output=True)
        self.assertEqual(p.returncode, 0)
        with open(pin, "rb") as f:
            self.assertNotEqual(f.read(), antes)


class Endurecimiento2(Endurecimiento):
    """Hallazgos de la 2ª ronda de la Fase B."""

    def test_un_archivo_py_nuevo_en_el_codigo_impide_arrancar(self):
        for d, nombre in (("agente", "decimal.py"), ("sandbox", "unicodedata.py"), ("mcp", "extra.py")):
            ruta = os.path.join(self.copia, d, nombre)
            with open(ruta, "w") as f:
                f.write("raise SystemExit(99)\n")  # si llegara a importarse, mataría el servidor
            p = self.correr_bytes(json.dumps(rpc(1, "ping")).encode() + b"\n")
            self.assertEqual((nombre, p.returncode, p.stdout), (nombre, 3, b""))
            os.remove(ruta)

    def test_script_lanzado_sin_I_no_importa_desde_su_propia_carpeta(self):
        # sin -I, sys.path[0] sería mcp/: un uuid.py plantado se ejecutaría ANTES de verificar el pin
        with open(os.path.join(self.copia, "mcp", "uuid.py"), "w") as f:
            f.write("raise SystemExit(99)\n")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
        p = subprocess.run([sys.executable, self.servidor], input=json.dumps(rpc(1, "ping")).encode() + b"\n",
                           capture_output=True, timeout=60, env=env)
        self.assertEqual((p.returncode, p.stdout), (3, b""))  # 3 = pin lo rechazó; 99 = se ejecutó el uuid.py plantado

    def test_carpetas_y_pyc_inesperados_impiden_arrancar(self):
        os.makedirs(os.path.join(self.copia, "sandbox", "policy"))
        with open(os.path.join(self.copia, "sandbox", "policy", "__init__.py"), "w") as f:
            f.write("raise SystemExit(99)\n")
        p = self.correr_bytes(json.dumps(rpc(1, "ping")).encode() + b"\n")
        self.assertEqual((p.returncode, p.stdout), (3, b""))
        shutil.rmtree(os.path.join(self.copia, "sandbox", "policy"))
        for nombre in ("suelto.pyc", "x.pth"):
            ruta = os.path.join(self.copia, "agente", nombre)
            with open(ruta, "wb") as f:
                f.write(b"")
            self.assertEqual(self.correr_bytes(json.dumps(rpc(1, "ping")).encode() + b"\n").returncode, 3, nombre)
            os.remove(ruta)

    def test_pycache_existente_se_ignora_aunque_este_manipulado(self):
        os.makedirs(os.path.join(self.copia, "agente", "__pycache__"), exist_ok=True)
        with open(os.path.join(self.copia, "agente", "__pycache__", "consultas.cpython-314.pyc"), "wb") as f:
            f.write(b"basura que no es un pyc valido")
        msg = rpc(1, "tools/call", {"name": "consultar_catalogo", "arguments": {"texto": "horario"}})
        p = self.correr_bytes(json.dumps(msg).encode() + b"\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(texto(self.salidas(p)[0])["tipo"], "faq")

    def test_stdout_con_lf_y_lineas_en_blanco_se_ignoran(self):
        p = self.correr_bytes(b"\n   \n" + json.dumps(rpc(1, "ping")).encode() + b"\n\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"\r", p.stdout)
        self.assertEqual(len(self.salidas(p)), 1)  # las líneas en blanco no generan errores

    def test_la_politica_del_mcp_no_permite_leer_ni_ejecutar(self):
        with open(os.path.join(RAIZ, "mcp", "policy.json"), encoding="utf-8") as f:
            pol = json.load(f)
        self.assertNotIn("leer_archivo", pol["acciones_permitidas"])
        self.assertEqual(pol["ejecutables_permitidos"], [])
        self.assertLessEqual(pol["limites"]["acciones_max"], 100000)


class LogicaExtra(Logica):
    def test_escalar_guarda_la_referencia_para_la_persona(self):
        out = texto(self.llamar("escalar_a_humano", {"motivo": "reclamo", "referencia": "Ana, cotización b-123"}))
        with open(os.path.join(self.raiz, "workspace", "escalados", out["escalado"] + ".json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["referencia"], "Ana, cotización b-123")
        self.assertTrue(self.llamar("escalar_a_humano", {"motivo": "x", "referencia": 5})["result"]["isError"])
        self.assertTrue(self.llamar("escalar_a_humano", {"motivo": "x", "otro": 1})["result"]["isError"])

    def test_bandeja_llena_rechaza_en_vez_de_crecer_sin_limite(self):
        servidor.MAX_ARCHIVOS_BANDEJA = 2
        try:
            args = {"lineas": [{"sku": "VISITA", "cantidad": 1}]}
            self.assertFalse(self.llamar("cotizar", args)["result"]["isError"])
            self.assertFalse(self.llamar("cotizar", args)["result"]["isError"])
            r = self.llamar("cotizar", args)
            self.assertTrue(r["result"]["isError"])
            self.assertIn("llena", r["result"]["content"][0]["text"])
        finally:
            servidor.MAX_ARCHIVOS_BANDEJA = 2000

    def test_flujo_de_llamadas_invalidas_no_infla_el_registro(self):
        for _ in range(50):
            self.llamar("cotizar", {"lineas": []})
        with open(os.path.join(self.t.name, "estado", "auditoria.jsonl"), encoding="utf-8") as f:
            self.assertLessEqual(f.read().count("validacion_fallida"), 3)  # 1 por segundo como máximo

    def test_escalar_guarda_fecha_y_origen_y_rechaza_motivo_vacio(self):
        out = texto(self.llamar("escalar_a_humano", {"motivo": "reclamo", "referencia": "caso-7"}))
        with open(os.path.join(self.raiz, "workspace", "escalados", out["escalado"] + ".json"), encoding="utf-8") as f:
            caso = json.load(f)
        self.assertEqual((caso["estado"], caso["origen"], caso["referencia"]), ("pendiente", "mcp:escalar_a_humano", "caso-7"))
        self.assertRegex(caso["creado"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        for vacio in ("", "   ", "\x1b\u200b"):
            self.assertTrue(self.llamar("escalar_a_humano", {"motivo": vacio, "referencia": "caso-7"})["result"]["isError"], repr(vacio))

    def test_escalar_sigue_funcionando_con_el_presupuesto_general_agotado(self):
        self.s.c.presupuesto.acciones_max = 1
        self.llamar("consultar_catalogo", {"texto": "horario"})
        self.assertTrue(self.llamar("consultar_catalogo", {"texto": "horario"})["result"]["isError"])  # cupo general agotado
        self.assertFalse(self.llamar("escalar_a_humano", {"motivo": "caso urgente", "referencia": "c1"})["result"]["isError"])  # cupo propio

    def test_escalar_sigue_funcionando_tras_una_rafaga_de_consultas(self):
        servidor.MAX_LLAMADAS = 3
        try:
            for _ in range(5):
                self.llamar("consultar_catalogo", {"texto": "horario"})
            self.assertTrue(self.llamar("consultar_catalogo", {"texto": "horario"})["result"]["isError"])
            self.assertFalse(self.llamar("escalar_a_humano", {"motivo": "x", "referencia": "c1"})["result"]["isError"])
        finally:
            servidor.MAX_LLAMADAS = 120

    def test_si_escalar_falla_el_mensaje_dice_que_no_quedo_registrado(self):
        servidor.MAX_ARCHIVOS_BANDEJA = 1
        try:
            self.llamar("escalar_a_humano", {"motivo": "uno", "referencia": "c1"})
            r = self.llamar("escalar_a_humano", {"motivo": "dos", "referencia": "c2"})
        finally:
            servidor.MAX_ARCHIVOS_BANDEJA = 2000
        self.assertTrue(r["result"]["isError"])
        self.assertTrue(r["result"]["content"][0]["text"].startswith("ESCALADO NO REGISTRADO"))
        r = self.llamar("escalar_a_humano", {"motivo": 5, "referencia": "c1"})  # validación fallida: también avisa
        self.assertTrue(r["result"]["content"][0]["text"].startswith("ESCALADO NO REGISTRADO"))

    def test_listar_catalogo_devuelve_sku_y_precio_sin_descuentos_ni_umbrales(self):
        out = texto(self.llamar("listar_catalogo", {}))
        skus = {i["sku"]: i for i in out["items"]}
        self.assertEqual(set(skus), {"SRV-BASICO", "SRV-ESTANDAR", "SRV-PREMIUM", "VISITA"})
        self.assertEqual(skus["SRV-ESTANDAR"]["precio_unitario"], "90.00")
        self.assertEqual(out["moneda"], "USD")
        crudo = json.dumps(out)
        for filtrado in ("descuento", "umbral", "max_lineas"):
            self.assertNotIn(filtrado, crudo)  # no se expone la política de negociación (las claves exactas se verifican abajo)
        for item in out["items"]:
            self.assertEqual(set(item), {"sku", "nombre", "precio_unitario"})

    def test_listar_catalogo_rechaza_argumentos_y_deja_registro(self):
        self.assertTrue(self.llamar("listar_catalogo", {"x": 1})["result"]["isError"])
        self.llamar("listar_catalogo", {})
        with open(os.path.join(self.t.name, "estado", "auditoria.jsonl"), encoding="utf-8") as f:
            eventos = [json.loads(l) for l in f]
        # el evento EXACTO, no solo la palabra (una validación fallida también la menciona)
        ok = [e for e in eventos if e["evento"] == "listar_catalogo"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["datos"], {"items": "4"})

    def test_cotizar_no_devuelve_cifras_de_la_politica_aunque_el_si_no_sigue_siendo_deducible(self):
        # OJO: solo comprueba que no haya cifras literales. requiere_aprobacion_humana sigue siendo un oráculo
        # booleano (bisección ≈7 llamadas): ver docs/GUARDRAILS.md. No prueba que el máximo sea indeducible.
        for pct in (1, 5, 11, 50):  # sondeo: con textos distintos se deduciría el máximo por producto
            r = self.llamar("cotizar", {"lineas": [{"sku": "SRV-ESTANDAR", "cantidad": 1}], "descuento_pct": pct})
            salida = r["result"]["content"][0]["text"]
            self.assertNotIn("máximo", salida)
            self.assertNotRegex(salida, r"\\b10\\s*%")
        out = texto(self.llamar("cotizar", {"lineas": [{"sku": "SRV-ESTANDAR", "cantidad": 1}], "descuento_pct": 50}))
        self.assertEqual(out["motivos"], ["el descuento solicitado requiere aprobación humana"])
        with open(os.path.join(self.raiz, "workspace", "borradores", out["borrador"] + ".json"), encoding="utf-8") as f:
            borrador = json.load(f)
        self.assertIn("máximo permitido 10", borrador["detalle_operador"][0])  # la persona sí ve el dato

    def test_los_sku_listados_se_pueden_cotizar_y_el_flujo_completo_funciona(self):
        items = texto(self.llamar("listar_catalogo", {}))["items"]
        est = next(i for i in items if i["nombre"] == "Servicio estándar")
        vis = next(i for i in items if i["nombre"] == "Visita técnica")
        out = texto(self.llamar("cotizar", {"lineas": [{"sku": est["sku"], "cantidad": 2}, {"sku": vis["sku"], "cantidad": 1}]}))
        self.assertEqual(out["total"], "210.00")  # 2 x 90 + 30
        self.assertFalse(out["requiere_aprobacion_humana"])

    def test_listar_catalogo_con_estado_general_corrupto_da_error_limpio(self):
        t = tempfile.TemporaryDirectory()
        try:
            estado = os.path.join(t.name, "estado")
            os.makedirs(estado)
            with open(os.path.join(estado, "presupuesto.json"), "w") as f:
                f.write("basura")
            s = servidor.Servicio(self.raiz, estado, os.path.join(RAIZ, "agente", "catalogo.json"))
            r = servidor.manejar(s, rpc(1, "tools/call", {"name": "listar_catalogo", "arguments": {}}))
            self.assertTrue(r["result"]["isError"])
            self.assertNotIn("Traceback", json.dumps(r))
        finally:
            t.cleanup()

    def test_referencia_obligatoria_y_se_rechaza_si_se_destruiria(self):
        # hallazgo 4ª ronda: un ID de chat de 9+ dígitos se ocultaba y el caso quedaba sin forma de ubicarlo
        for mala in ("chat-123456789", "123 456 789 012", "ana@correo.com", "", "   ", "a\x1bb"):
            r = self.llamar("escalar_a_humano", {"motivo": "x", "referencia": mala})
            self.assertTrue(r["result"]["isError"], repr(mala))
            self.assertTrue(r["result"]["content"][0]["text"].startswith("ESCALADO NO REGISTRADO"), repr(mala))
        self.assertTrue(self.llamar("escalar_a_humano", {"motivo": "x"})["result"]["isError"])  # falta referencia
        out = texto(self.llamar("escalar_a_humano", {"motivo": "x", "referencia": "chat-A17"}))
        self.assertEqual(out["referencia"], "chat-A17")  # devuelve lo realmente guardado
        self.assertEqual(len(os.listdir(os.path.join(self.raiz, "workspace", "escalados"))), 1)  # los rechazados no escriben

    def test_longitudes_se_aplican_de_verdad_y_sin_bloqueo(self):
        import time
        inicio = time.time()
        for campo, args in (("motivo", {"motivo": "a" * 200000, "referencia": "c1"}),
                            ("referencia", {"motivo": "x", "referencia": "a" * 200000}),
                            ("cliente_ref", None)):
            if args is None:
                r = self.llamar("cotizar", {"lineas": [{"sku": "VISITA", "cantidad": 1}], "cliente_ref": "a" * 200000})
            else:
                r = self.llamar("escalar_a_humano", args)
            self.assertTrue(r["result"]["isError"], campo)
        self.assertLess(time.time() - inicio, 5.0)  # antes: minutos con la regex de correo cuadrática

    def test_si_el_estado_general_esta_corrupto_se_puede_seguir_escalando(self):
        t = tempfile.TemporaryDirectory()
        try:
            estado = os.path.join(t.name, "estado")
            os.makedirs(estado)
            with open(os.path.join(estado, "presupuesto.json"), "w") as f:
                f.write("basura")
            s = servidor.Servicio(self.raiz, estado, os.path.join(RAIZ, "agente", "catalogo.json"))
            self.assertIsNone(s.c)
            r = servidor.manejar(s, rpc(1, "tools/call", {"name": "consultar_catalogo", "arguments": {"texto": "horario"}}))
            self.assertTrue(r["result"]["isError"])
            r = servidor.manejar(s, rpc(2, "tools/call", {"name": "escalar_a_humano",
                                                           "arguments": {"motivo": "x", "referencia": "c1"}}))
            self.assertFalse(r["result"]["isError"])
        finally:
            t.cleanup()

    def test_escalar_a_humano_esta_en_el_manifiesto_con_referencia(self):
        esquema = next(t for t in servidor.HERRAMIENTAS if t["name"] == "escalar_a_humano")["inputSchema"]
        self.assertIn("referencia", esquema["properties"])
        self.assertEqual(sorted(esquema["required"]), ["motivo", "referencia"])
    def test_limite_de_tasa(self):
        servidor.MAX_LLAMADAS = 3
        try:
            for _ in range(3):
                self.assertFalse(self.llamar("consultar_catalogo", {"texto": "horario"})["result"]["isError"])
            r = self.llamar("consultar_catalogo", {"texto": "horario"})
            self.assertTrue(r["result"]["isError"])
            self.assertIn("demasiadas", r["result"]["content"][0]["text"])
        finally:
            servidor.MAX_LLAMADAS = 120

    def test_validaciones_fallidas_quedan_en_el_registro_sin_el_contenido(self):
        self.llamar("cotizar", {"lineas": [{"sku": "SECRETO_DEL_CLIENTE", "cantidad": 1}]})
        with open(os.path.join(self.t.name, "estado", "auditoria.jsonl"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("validacion_fallida", log)
        self.assertNotIn("SECRETO_DEL_CLIENTE", log)

    def test_cliente_ref_y_motivo_se_limpian(self):
        out = texto(self.llamar("cotizar", {"lineas": [{"sku": "VISITA", "cantidad": 1}], "cliente_ref": "Ana 4111 1111 1111 1111"}))
        with open(os.path.join(self.raiz, "workspace", "borradores", out["borrador"] + ".json"), encoding="utf-8") as f:
            self.assertNotIn("4111", f.read())
        out = texto(self.llamar("escalar_a_humano", {"motivo": "mi tarjeta es 4111-1111-1111-1111", "referencia": "c1"}))
        with open(os.path.join(self.raiz, "workspace", "escalados", out["escalado"] + ".json"), encoding="utf-8") as f:
            self.assertNotIn("4111", f.read())
        r = self.llamar("cotizar", {"lineas": [{"sku": "VISITA", "cantidad": 1}], "cliente_ref": {"a": 1}})
        self.assertTrue(r["result"]["isError"])


class Pin(unittest.TestCase):
    def test_el_pin_del_proyecto_coincide_con_el_manifiesto(self):
        self.assertTrue(servidor.verificar_pin(), "ejecutar: python mcp/servidor.py --fijar (acción humana)")


if __name__ == "__main__":
    unittest.main()
