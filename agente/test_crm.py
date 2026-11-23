"""Pruebas del cliente HubSpot y de las herramientas crm_* del MCP. NUNCA llaman a HubSpot real: usan un servidor falso en 127.0.0.1."""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.realpath(os.path.join(AQUI, ".."))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(RAIZ, "mcp"))
import crm
import cotizador
import servidor

TOKEN = "pat-na1-0000000000000000-FALSO-PARA-PRUEBAS"


class Falso(http.server.BaseHTTPRequestHandler):
    """Imita lo mínimo de la API v3. El estado vive en la clase del servidor (self.server.estado)."""

    def log_message(self, *a):
        pass

    def _responder(self, codigo, cuerpo):
        datos = json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_POST(self):
        e = self.server.estado
        cuerpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        e["peticiones"].append((self.path, cuerpo, self.headers.get("Authorization")))
        if e.get("forzar"):
            return self._responder(e["forzar"], {"message": "SECRETO-DEL-CUERPO"})
        if self.path.endswith("/contacts/search"):
            email = cuerpo["filterGroups"][0]["filters"][0]["value"]
            c = e["contactos"].get(email)
            return self._responder(200, {"results": [c] if c else []})
        e["n"] += 1
        nuevo = {"id": str(1000 + e["n"]), "properties": cuerpo.get("properties", {})}
        if self.path.endswith("/contacts"):
            e["contactos"][cuerpo["properties"]["email"]] = nuevo
        return self._responder(201, nuevo)


class BaseFalso(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Falso)
        self.srv.estado = {"peticiones": [], "contactos": {}, "n": 0}
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.reloj = [1_800_000_000.0]
        self.crm = self.nuevo()

    def nuevo(self, token=TOKEN):
        return crm.Crm(
            token,
            cotizador.limpiar_texto,
            os.path.join(self.t.name, "cuota.json"),
            f"http://127.0.0.1:{self.srv.server_address[1]}",
            permitir_http_local=True,
            ahora=lambda: self.reloj[0],
        )

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.t.cleanup()

    @property
    def peticiones(self):
        return self.srv.estado["peticiones"]

    def escrituras(self):
        return [p for p in self.peticiones if not p[0].endswith("/search")]


class Validacion(unittest.TestCase):
    def test_email(self):
        self.assertEqual(crm.validar_email("  Ana@Empresa.COM "), "ana@empresa.com")
        for malo in (
            "",
            "sin-arroba",
            "a@b",
            "a b@c.com",
            "a@c.com\nBcc: x@y.com",
            5,
            None,
            "x" * 300 + "@a.com",
        ):
            with self.assertRaises(ValueError, msg=repr(malo)):
                crm.validar_email(malo)

    def test_nombre_y_telefono(self):
        self.assertEqual(crm.validar_nombre("María José O'Neil-Pérez"), "María José O'Neil-Pérez")
        for malo in ("<script>", "Ana; DROP", "123", "", "a" * 81, "Ana\nBob", "Ana‮Bob"):
            with self.assertRaises(ValueError, msg=repr(malo)):
                crm.validar_nombre(malo)
        self.assertEqual(crm.validar_telefono(""), "")
        self.assertEqual(crm.validar_telefono("+56 9 1234-5678"), "+56 9 1234-5678")
        with self.assertRaises(ValueError):
            crm.validar_telefono("llámame al 1234567")

    def test_url_base_solo_hubspot_https(self):
        f = cotizador.limpiar_texto
        crm.Crm(TOKEN, f, "x.json")  # la de producción
        for mala in (
            "https://evil.example",
            "http://api.hubapi.com",
            "https://api.hubapi.com.evil.example",
            "https://api.hubapi.com:8443",
            "https://api.hubapi.com/otra",
            "http://127.0.0.1:1",
        ):
            with self.assertRaises(ValueError, msg=mala):
                crm.Crm(TOKEN, f, "x.json", base_url=mala)
        for mala in (
            "http://127.0.0.2:1",
            "http://127.0.0.1.evil.example:1",
            "http://localhost:1",
            "https://127.0.0.1:1",
        ):
            with self.assertRaises(ValueError, msg=mala):  # incluso con permitir_http_local solo vale http://127.0.0.1
                crm.Crm(TOKEN, f, "x.json", base_url=mala, permitir_http_local=True)

    def test_token(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "t")
            with self.assertRaises(crm.CrmNoConfigurado):
                crm.leer_token(ruta)
            for malo in ("", "corto", "con espacio dentro " + "x" * 30, "x" * 400):
                with open(ruta, "w") as f:
                    f.write(malo)
                with self.assertRaises(crm.CrmNoConfigurado, msg=malo[:10]):
                    crm.leer_token(ruta)
            with open(ruta, "w") as f:
                f.write(TOKEN + "\n")
            self.assertEqual(crm.leer_token(ruta), TOKEN)


class Operaciones(BaseFalso):
    def test_lead_nuevo_crea_contacto_y_trato_con_asociacion(self):
        r = self.crm.registrar_lead(
            "Ana Pérez",
            "Taller Sur",
            "ana@taller.com",
            "+56 9 1234 5678",
            "consulta",
            "Pide cotización",
        )
        self.assertTrue(r["nuevo"])
        rutas = [p[0] for p in self.escrituras()]
        self.assertEqual(rutas, ["/crm/v3/objects/contacts", "/crm/v3/objects/deals", "/crm/v3/objects/notes"])
        trato, nota = self.escrituras()[1][1], self.escrituras()[2][1]
        self.assertEqual(trato["associations"][0]["types"][0]["associationTypeId"], 3)  # literales: no la constante del código
        self.assertEqual(nota["associations"][0]["types"][0]["associationTypeId"], 202)
        self.assertEqual(trato["associations"][0]["to"]["id"], r["contacto"])
        self.assertEqual(trato["properties"]["pipeline"], "default")
        self.assertTrue(all(p[2] == "Bearer " + TOKEN for p in self.peticiones))
        c = self.escrituras()[0][1]["properties"]
        self.assertEqual((c["firstname"], c["lastname"], c["company"]), ("Ana", "Pérez", "Taller Sur"))

    def test_lead_repetido_no_duplica_ni_pisa_datos_ni_crea_otro_trato(self):
        self.crm.registrar_lead("Ana Pérez", "Taller Sur", "ana@taller.com", "", "consulta", "")
        antes = len(self.escrituras())
        r = self.crm.registrar_lead("Otro Nombre", "Otra Empresa", "ANA@taller.com", "", "cotizacion", "Vuelve a escribir")
        self.assertFalse(r["nuevo"])
        self.assertIsNone(r["trato"])
        nuevas = self.escrituras()[antes:]
        self.assertEqual([p[0] for p in nuevas], ["/crm/v3/objects/notes"])  # solo una nota
        self.assertEqual(len(self.srv.estado["contactos"]), 1)

    def test_buscar_contacto(self):
        self.assertEqual(self.crm.buscar_contacto("nadie@x.com"), {"existe": False})
        self.crm.registrar_lead("Ana Pérez", "Taller Sur", "ana@taller.com", "", "formulario", "")
        r = self.crm.buscar_contacto("ana@taller.com")
        self.assertEqual(set(r), {"existe", "id"})  # nunca nombre ni empresa: datos de terceros no vuelven al modelo
        self.assertTrue(r["existe"])

    def test_nota_y_tarea_exigen_contacto_existente(self):
        with self.assertRaises(ValueError):
            self.crm.registrar_nota("nadie@x.com", "hola")
        with self.assertRaises(ValueError):
            self.crm.crear_tarea("nadie@x.com", "seguimiento", "hola")
        self.assertEqual(self.escrituras(), [])

    def test_tarea_queda_como_borrador_no_enviado(self):
        self.crm.registrar_lead("Ana Pérez", "", "ana@taller.com", "", "referido", "")
        self.crm.crear_tarea("ana@taller.com", "contacto_frio", "Hola Ana, ¿le interesa?")
        p = self.escrituras()[-1]
        self.assertTrue(p[0].endswith("/tasks"))
        self.assertIn("BORRADOR", p[1]["properties"]["hs_task_body"])
        self.assertEqual(p[1]["properties"]["hs_task_status"], "NOT_STARTED")
        with self.assertRaises(ValueError):
            self.crm.crear_tarea("ana@taller.com", "enviar_correo", "x")

    def test_textos_libres_ocultan_numeros_largos_y_correos(self):
        self.crm.registrar_lead(
            "Ana Pérez",
            "",
            "ana@taller.com",
            "",
            "consulta",
            "mi tarjeta 4111111111111111 y otro@correo.com",
        )
        cuerpo = json.dumps(self.escrituras()[-1][1])
        self.assertNotIn("4111111111111111", cuerpo)
        self.assertNotIn("otro@correo.com", cuerpo)

    def test_nombre_en_blanco_es_error_de_validacion(self):
        for malo in ("   ", "\t"):
            with self.assertRaises(ValueError):
                self.crm.registrar_lead(malo, "", "ana@taller.com", "", "consulta", "")
        self.assertEqual(self.peticiones, [])

    def test_html_y_urls_se_neutralizan_en_nota_tarea_y_resumen(self):
        malo = '<a href="http://x.example">Aprobar</a> https://evil.example/p'
        self.crm.registrar_lead("Ana Pérez", "", "ana@taller.com", "", "consulta", malo)
        self.crm.registrar_nota("ana@taller.com", malo)
        self.crm.crear_tarea("ana@taller.com", "seguimiento", malo)
        for p in self.escrituras():
            cuerpo = json.dumps(p[1])
            self.assertNotIn("<a", cuerpo)
            self.assertNotIn("http://", cuerpo)
            self.assertNotIn("https://", cuerpo)

    def test_nota_y_tarea_ocultan_numeros_largos_y_correos(self):
        self.crm.registrar_lead("Ana Pérez", "", "ana@taller.com", "", "consulta", "")
        self.crm.registrar_nota("ana@taller.com", "tarjeta 4111111111111111 correo otro@correo.com")
        self.crm.crear_tarea("ana@taller.com", "seguimiento", "llamar al 56912345678 o a otro@correo.com")
        cuerpo = json.dumps([p[1] for p in self.escrituras()])
        for dato in ("4111111111111111", "56912345678", "otro@correo.com"):
            self.assertNotIn(dato, cuerpo)

    def test_lead_a_medias_se_avisa_a_una_persona(self):
        # el contacto se crea pero el trato falla: el error debe decirlo (un reintento no crearía el trato)
        original = Falso.do_POST

        def malo(self_):
            if self_.path.endswith("/deals"):
                self_.rfile.read(int(self_.headers.get("Content-Length", 0)))
                return self_._responder(500, {})
            return original(self_)

        Falso.do_POST = malo
        try:
            with self.assertRaises(crm.CrmFallo) as cm:
                self.crm.registrar_lead("Ana Pérez", "", "ana@taller.com", "", "consulta", "")
        finally:
            Falso.do_POST = original
        self.assertIn("LEAD A MEDIAS", str(cm.exception))

    def test_no_sigue_redirecciones(self):
        original = Falso.do_POST

        def redirige(self_):
            self_.rfile.read(int(self_.headers.get("Content-Length", 0)))
            self_.send_response(302)
            self_.send_header("Location", "http://127.0.0.1:9/robado")
            self_.send_header("Content-Length", "0")
            self_.end_headers()

        Falso.do_POST = redirige
        try:
            with self.assertRaises(crm.CrmFallo):
                self.crm.buscar_contacto("a@b.com")
        finally:
            Falso.do_POST = original

    def test_respuesta_enorme_o_invalida(self):
        original = Falso.do_POST

        def enorme(self_):
            self_.rfile.read(int(self_.headers.get("Content-Length", 0)))
            self_._responder(200, {"relleno": "x" * (crm.MAX_RESPUESTA + 10)})

        Falso.do_POST = enorme
        try:
            with self.assertRaises(crm.CrmFallo):
                self.crm.buscar_contacto("a@b.com")
        finally:
            Falso.do_POST = original
        Falso.do_POST = lambda s: (
            s.rfile.read(int(s.headers.get("Content-Length", 0))),
            s._responder(200, {"results": [{"id": "abc"}]}),
        )[1]
        try:
            with self.assertRaises(crm.CrmFallo):  # id no numérico
                self.crm.buscar_contacto("a@b.com")
        finally:
            Falso.do_POST = original

    def test_origen_invalido(self):
        with self.assertRaises(ValueError):
            self.crm.registrar_lead("Ana", "", "ana@taller.com", "", "spam", "")
        self.assertEqual(self.peticiones, [])

    def test_errores_http_no_filtran_cuerpo_ni_token(self):
        for codigo in (401, 429, 500):
            self.srv.estado["forzar"] = codigo
            with self.assertRaises(crm.CrmFallo) as cm:
                self.crm.buscar_contacto("a@b.com")
            self.assertNotIn("SECRETO-DEL-CUERPO", str(cm.exception))
            self.assertNotIn(TOKEN, str(cm.exception))

    def test_servidor_caido_da_fallo_controlado(self):
        c = self.crm
        self.srv.shutdown()
        self.srv.server_close()
        with self.assertRaises(crm.CrmFallo):
            c.buscar_contacto("a@b.com")


class Cuota(BaseFalso):
    def test_tope_por_hora_y_se_reinicia(self):
        for i in range(crm.MAX_POR_HORA):
            self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)
        with self.assertRaises(crm.CrmCuotaAgotada):
            self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)
        self.reloj[0] += 3600  # otra hora
        self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)

    def test_tope_por_dia(self):
        total = 0
        with self.assertRaises(crm.CrmCuotaAgotada):
            while True:
                self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)
                total += 1
                if total % crm.MAX_POR_HORA == 0:
                    self.reloj[0] += 3600
                if total > 500:
                    self.fail("sin tope diario")
        self.assertEqual(total, crm.MAX_POR_DIA)

    def test_cuota_persiste_entre_instancias(self):
        for _ in range(crm.MAX_POR_HORA):
            self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)
        with self.assertRaises(crm.CrmCuotaAgotada):
            self.nuevo()._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)

    def test_estado_corrupto_falla_cerrado_sin_llamar(self):
        with open(os.path.join(self.t.name, "cuota.json"), "w") as f:
            f.write("{no es json")
        with self.assertRaises(crm.CrmCuotaAgotada):
            self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)
        self.assertEqual(self.peticiones, [])

    def test_lectura_no_consume_cuota_de_escritura_pero_tiene_su_propio_tope(self):
        for _ in range(crm.MAX_LECT_HORA):
            self.crm.buscar_contacto("a@b.com")
        with open(os.path.join(self.t.name, "cuota.json"), encoding="utf-8") as f:
            estado = json.load(f)
        self.assertNotIn("e_n_hora", estado)  # no tocó la cuota de escrituras
        with self.assertRaises(crm.CrmCuotaAgotada):
            self.crm.buscar_contacto("a@b.com")
        n = len(self.peticiones)
        with self.assertRaises(crm.CrmCuotaAgotada):
            self.crm.buscar_contacto("c@d.com")
        self.assertEqual(len(self.peticiones), n)  # el tope corta ANTES de llamar a HubSpot

    def test_contadores_negativos_o_de_tipo_raro_fallan_cerrado(self):
        hora = "2027-01-15T08"
        for estado in (
            {"e_hora": hora, "e_n_hora": -100},
            {"e_hora": hora, "e_n_hora": "0"},
            {"e_hora": hora, "e_n_hora": True},
        ):
            with open(os.path.join(self.t.name, "cuota.json"), "w") as f:
                json.dump({**estado, "e_dia": "2027-01-15", "e_n_dia": 0}, f)
            self.reloj[0] = 1_800_000_000.0  # 2027-01-15T08:00Z
            with self.assertRaises(crm.CrmCuotaAgotada, msg=str(estado)):
                self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)

    def test_lead_nuevo_reserva_todas_sus_escrituras_o_no_hace_ninguna(self):
        for _ in range(crm.MAX_POR_HORA - 2):  # quedan 2; un lead nuevo con resumen necesita 3
            self.crm._pedir("POST", "/crm/v3/objects/notes", {"properties": {}}, True)
        antes = len(self.escrituras())
        with self.assertRaises(crm.CrmCuotaAgotada):
            self.crm.registrar_lead("Ana", "", "ana@taller.com", "", "consulta", "con resumen")
        self.assertEqual(len(self.escrituras()), antes)  # ninguna escritura (solo la búsqueda): el lead no queda a medias
        self.crm.registrar_lead("Ana", "", "ana@taller.com", "", "consulta", "")  # sin resumen son 2: cabe


class ServidorMCP(BaseFalso):
    def setUp(self):
        super().setUp()
        self.raiz = os.path.join(self.t.name, "proy")
        os.makedirs(os.path.join(self.raiz, "workspace"))
        self.lista_nc = os.path.join(self.t.name, "no_contactar.txt")  # lista de no contacto vacía (es obligatoria: falla cerrado)
        with open(self.lista_nc, "w", encoding="utf-8") as f:
            f.write("# vacía\n")
        self.s = servidor.Servicio(
            self.raiz,
            os.path.join(self.t.name, "estado"),
            os.path.join(RAIZ, "agente", "catalogo.json"),
            crm_fabrica=lambda: self.crm,
            no_contactar_persona=self.lista_nc,
        )

    def llamar(self, nombre, args):
        return servidor.manejar(
            self.s,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": nombre, "arguments": args},
            },
        )

    def test_flujo_completo_y_log_sin_correo(self):
        r = self.llamar(
            "crm_registrar_lead",
            {"nombre": "Ana Pérez", "email": "ana@taller.com", "origen": "consulta"},
        )
        self.assertFalse(r["result"]["isError"], r)
        r = self.llamar("crm_registrar_nota", {"email": "ana@taller.com", "texto": "pidió precio"})
        self.assertFalse(r["result"]["isError"], r)
        r = self.llamar("crm_buscar_contacto", {"email": "ana@taller.com"})
        self.assertTrue(json.loads(r["result"]["content"][0]["text"])["existe"])
        log = ""
        for d, _, fs in os.walk(os.path.join(self.t.name, "estado")):
            for f in fs:
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as h:
                    log += h.read()
        self.assertIn("crm_registrar_lead", log)
        self.assertNotIn("ana@taller.com", log)
        self.assertNotIn(TOKEN, log)

    def test_argumentos_extra_o_faltantes_se_rechazan_sin_llamar_a_hubspot(self):
        casos = [
            ("crm_registrar_lead", {"nombre": "Ana", "email": "a@b.com"}),
            (
                "crm_registrar_lead",
                {"nombre": "Ana", "email": "a@b.com", "origen": "consulta", "etapa": "ganado"},
            ),
            ("crm_buscar_contacto", {}),
            ("crm_crear_tarea", {"email": "a@b.com", "tipo": "seguimiento"}),
            ("crm_buscar_contacto", {"email": {"x": 1}}),
        ]
        for nombre, args in casos:
            r = self.llamar(nombre, args)
            self.assertTrue(r["result"]["isError"], (nombre, args))
        self.assertEqual(self.peticiones, [])

    def test_sin_token_solo_fallan_las_herramientas_crm(self):
        s = servidor.Servicio(
            self.raiz,
            os.path.join(self.t.name, "estado2"),
            os.path.join(RAIZ, "agente", "catalogo.json"),
        )
        original = crm.RUTA_TOKEN
        crm.RUTA_TOKEN = os.path.join(self.t.name, "no-existe")
        try:
            r = servidor.manejar(
                s,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "crm_buscar_contacto", "arguments": {"email": "a@b.com"}},
                },
            )
            ok = servidor.manejar(
                s,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "listar_catalogo", "arguments": {}},
                },
            )
        finally:
            crm.RUTA_TOKEN = original
        self.assertTrue(r["result"]["isError"])
        self.assertIn("CrmNoConfigurado", r["result"]["content"][0]["text"])
        self.assertFalse(ok["result"]["isError"])

    def _log(self):
        log = ""
        for d, _, fs in os.walk(os.path.join(self.t.name, "estado")):
            for f in fs:
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as h:
                    log += h.read()
        return log

    def test_fallos_de_hubspot_dejan_registro_de_intento_y_error(self):
        self.srv.estado["forzar"] = 500
        r = self.llamar("crm_registrar_lead", {"nombre": "Ana", "email": "ana@taller.com", "origen": "consulta"})
        self.assertTrue(r["result"]["isError"])
        log = self._log()
        self.assertIn("crm_registrar_lead_intento", log)
        self.assertIn("crm_registrar_lead_error", log)
        self.assertIn("CrmFallo", log)
        self.assertNotIn("ana@taller.com", log)
        self.assertNotIn("SECRETO-DEL-CUERPO", log)

    def test_error_de_hubspot_no_filtra_nada(self):
        self.srv.estado["forzar"] = 500
        r = self.llamar("crm_buscar_contacto", {"email": "a@b.com"})
        self.assertTrue(r["result"]["isError"])
        texto = r["result"]["content"][0]["text"]
        self.assertNotIn("SECRETO-DEL-CUERPO", texto)
        self.assertNotIn(TOKEN, texto)


if __name__ == "__main__":
    unittest.main()
