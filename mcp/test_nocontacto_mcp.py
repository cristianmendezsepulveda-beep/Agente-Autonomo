"""La lista de no contacto aplicada a las herramientas del MCP. Servidores falsos en memoria: nunca usa la red."""
import datetime
import json
import os
import sys
import tempfile
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.realpath(os.path.join(AQUI, ".."))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(RAIZ, "agente"))
import servidor  # noqa: E402
import test_cobranza as tcb  # noqa: E402
import test_correo as tco  # noqa: E402


class FalsoCrm(tco.FalsoCrm):
    def __init__(self):
        super().__init__()
        self.otras = []

    def buscar_contacto(self, email):
        self.otras.append(("buscar", email))
        return {"existe": True, "id": "1001"}

    def registrar_nota(self, email, texto):
        self.otras.append(("nota", email))
        return {"nota": "9"}

    def crear_tarea(self, email, tipo, borrador):
        self.otras.append(("tarea", email))
        return {"tarea": "7"}


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.raiz = os.path.join(self.t.name, "proy")
        os.makedirs(os.path.join(self.raiz, "workspace", "cartera"))
        self.persona = os.path.join(self.t.name, "no_contactar.txt")
        self.lista("# vacía\n")
        self.crm = FalsoCrm()
        self.falso = tco.FalsoIMAP(dict([tco.mensaje(1)]))
        self.reloj = datetime.datetime(2027, 3, 1, 12, tzinfo=datetime.timezone.utc).timestamp()
        self.estado = os.path.join(self.t.name, "estado")
        self.s = servidor.Servicio(self.raiz, self.estado, os.path.join(RAIZ, "agente", "catalogo.json"), crm_fabrica=lambda: self.crm,
                                   reloj=lambda: self.reloj, correo_cfg=tco.CFG, correo_fabrica=tco.fab(self.falso),
                                   no_contactar_persona=self.persona)
        self.cartera(tcb.csv_de(tcb.fila("F-001", vence="2027-02-20")))

    def tearDown(self):
        self.t.cleanup()

    def lista(self, texto):
        with open(self.persona, "w", encoding="utf-8", newline="") as f:
            f.write(texto)

    def cartera(self, texto):
        with open(os.path.join(self.raiz, "workspace", "cartera", "cartera.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(texto)

    def llamar(self, nombre, args):
        r = servidor.manejar(self.s, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": nombre, "arguments": args}})
        txt = r["result"]["content"][0]["text"]
        return r["result"]["isError"], (json.loads(txt) if not r["result"]["isError"] else txt)

    def todo_el_estado(self):
        salida = ""
        for d, _, fs in os.walk(self.t.name):
            for f in fs:
                if f not in ("no_contactar.txt", "no_contactar_auto.json"):  # las listas contienen direcciones por definición
                    with open(os.path.join(d, f), encoding="utf-8", errors="replace") as h:
                        salida += h.read()
        return salida


LEAD = {"nombre": "Ana", "email": "ana@cliente.com", "origen": "consulta"}


class Crm(Base):
    def test_lead_de_alguien_en_la_lista_se_deniega_sin_llamar_al_crm(self):
        self.lista("ana@cliente.com\n")
        err, txt = self.llamar("crm_registrar_lead", LEAD)
        self.assertTrue(err)
        self.assertIn("no contacto", txt)
        self.assertEqual(self.crm.llamadas, [])

    def test_por_dominio_por_etiqueta_y_por_mayusculas(self):
        self.lista("@cliente.com\n")
        for email in ("ana@cliente.com", "x@sub.cliente.com", "ANA+Ventas@Cliente.com"):
            self.assertTrue(self.llamar("crm_registrar_lead", {**LEAD, "email": email})[0], email)
        self.assertFalse(self.llamar("crm_registrar_lead", {**LEAD, "email": "ana@otro.com"})[0])
        self.assertEqual(len(self.crm.llamadas), 1)

    def test_un_correo_que_el_crm_aceptaria_pero_la_lista_no_entiende_se_bloquea(self):
        self.assertTrue(self.llamar("crm_registrar_lead", {**LEAD, "email": "ana@-cliente.com"})[0])
        self.assertEqual(self.crm.llamadas, [])

    def test_crear_tarea_tambien_se_deniega(self):
        self.lista("ana@cliente.com\n")
        err, _ = self.llamar("crm_crear_tarea", {"email": "ana@cliente.com", "tipo": "seguimiento", "borrador": "Hola"})
        self.assertTrue(err)
        self.assertEqual(self.crm.otras, [])

    def test_buscar_y_nota_siguen_permitidas_porque_no_contactan(self):
        self.lista("ana@cliente.com\n")
        self.assertFalse(self.llamar("crm_buscar_contacto", {"email": "ana@cliente.com"})[0])
        self.assertFalse(self.llamar("crm_registrar_nota", {"email": "ana@cliente.com", "texto": "pidió no ser contactada"})[0])
        self.assertEqual([x[0] for x in self.crm.otras], ["buscar", "nota"])

    def test_sin_archivo_de_la_persona_se_deniega_todo_lo_que_contacta(self):
        os.remove(self.persona)
        err, txt = self.llamar("crm_registrar_lead", LEAD)
        self.assertTrue(err)
        self.assertIn("lista de no contacto", txt)
        self.assertTrue(self.llamar("crm_crear_tarea", {"email": "ana@cliente.com", "tipo": "seguimiento", "borrador": "Hola"})[0])
        self.assertTrue(self.llamar("cobranza_evaluar", {"factura_id": "F-001"})[0])
        self.assertEqual((self.crm.llamadas, self.crm.otras), ([], []))

    def test_archivo_invalido_se_deniega_y_dice_la_linea(self):
        self.lista("ana@cliente.com\nesto no es un correo\n")
        err, txt = self.llamar("crm_registrar_lead", LEAD)
        self.assertTrue(err)
        self.assertIn("línea 2", txt)

    def test_la_edicion_de_la_persona_vale_sin_reiniciar(self):
        self.assertFalse(self.llamar("crm_registrar_lead", LEAD)[0])
        self.lista("ana@cliente.com\n")
        self.assertTrue(self.llamar("crm_registrar_lead", LEAD)[0])
        self.lista("# ya no\n")
        self.assertFalse(self.llamar("crm_registrar_lead", LEAD)[0])

    def test_el_registro_deja_constancia_sin_la_direccion(self):
        self.lista("ana@cliente.com\n")
        self.llamar("crm_registrar_lead", LEAD)
        estado = self.todo_el_estado()
        self.assertIn("no_contactar_bloqueado", estado)
        self.assertNotIn("ana@cliente.com", estado)


class Correo(Base):
    def test_captar_a_alguien_en_la_lista_no_registra_y_consume_el_mensaje(self):
        self.lista("@cliente.com\n")
        ref = self.llamar("correo_nuevos", {})[1]["nuevos"][0]["ref"]
        err, r = self.llamar("correo_captar", {"ref": ref, "origen": "consulta"})
        self.assertFalse(err, r)
        self.assertEqual((r["registrado"], r["bloqueado_no_contactar"]), (False, True))
        self.assertNotIn("cliente.com", json.dumps(r))
        self.assertEqual(self.crm.llamadas, [])
        self.assertTrue(self.llamar("correo_captar", {"ref": ref, "origen": "consulta"})[0])  # ya procesado
        self.assertEqual(self.llamar("correo_nuevos", {})[1]["nuevos"], [])

    def test_captar_a_alguien_que_no_esta_sigue_funcionando(self):
        ref = self.llamar("correo_nuevos", {})[1]["nuevos"][0]["ref"]
        err, r = self.llamar("correo_captar", {"ref": ref, "origen": "consulta"})
        self.assertFalse(err, r)
        self.assertTrue(r["registrado"])
        self.assertEqual(len(self.crm.llamadas), 1)

    def test_sin_la_lista_correo_captar_se_deniega_y_no_gasta_la_cuota(self):
        ref = self.llamar("correo_nuevos", {})[1]["nuevos"][0]["ref"]
        os.remove(self.persona)
        self.assertTrue(self.llamar("correo_captar", {"ref": ref, "origen": "consulta"})[0])
        self.assertEqual(self.crm.llamadas, [])
        self.lista("# ya\n")
        self.assertFalse(self.llamar("correo_captar", {"ref": ref, "origen": "consulta"})[0])  # el ref sigue vigente


class Bajas(Base):
    def test_una_baja_por_correo_queda_en_la_lista_automatica_y_frena_despues(self):
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="Por favor, quiero darme de baja"), tco.mensaje(2, de="Luis <luis@cliente.com>")])
        err, r = self.llamar("correo_nuevos", {})
        self.assertFalse(err, r)
        self.assertEqual((r["bajas_registradas"], len(r["nuevos"])), (1, 1))
        self.assertNotIn("ana@cliente.com", json.dumps(r))
        with open(os.path.join(self.estado, "no_contactar_auto.json"), encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)["entradas"]), ["ana@cliente.com"])
        self.assertTrue(self.llamar("crm_registrar_lead", LEAD)[0])  # ahora frena aunque el archivo de la persona esté vacío
        self.assertFalse(self.llamar("crm_registrar_lead", {**LEAD, "email": "luis@cliente.com"})[0])
        self.assertIn("baja_registrada", self.todo_el_estado())
        self.assertNotIn("ana@cliente.com", self.todo_el_estado())

    def test_la_baja_se_registra_aunque_falte_el_archivo_de_la_persona(self):
        os.remove(self.persona)
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="unsubscribe")])
        err, r = self.llamar("correo_nuevos", {})
        self.assertFalse(err, r)
        self.assertEqual(r["bajas_registradas"], 1)

    def test_con_la_lista_automatica_corrupta_el_mensaje_no_se_lista(self):
        os.makedirs(self.estado, exist_ok=True)
        with open(os.path.join(self.estado, "no_contactar_auto.json"), "w") as f:
            f.write("{roto")
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="darme de baja")])
        err, r = self.llamar("correo_nuevos", {})
        self.assertFalse(err, r)
        self.assertEqual((r["nuevos"], r["bajas_registradas"], r["bajas_no_registradas"]), ([], 0, 1))
        self.assertIn("avise a una persona", r["alerta"])
        self.assertIn("baja_no_registrada", self.todo_el_estado())


class BajaSinRegistrar(Base):
    """Una baja que no se pudo escribir deja a esa persona fuera de la lista: hasta que una persona lo revise, NADA que contacte o registre
    puede funcionar (si no, un correo posterior suyo, sin frase de baja, se captaría)."""

    def romper_lista_automatica(self):
        os.makedirs(self.estado, exist_ok=True)
        with open(os.path.join(self.estado, "no_contactar_auto.json"), "w") as f:
            f.write("{roto")

    def test_tras_una_baja_fallida_todo_lo_que_contacta_se_frena(self):
        self.romper_lista_automatica()
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="darme de baja"), tco.mensaje(2, de="Luis <luis@cliente.com>")])
        err, r = self.llamar("correo_nuevos", {})
        self.assertFalse(err, r)
        self.assertEqual(r["bajas_no_registradas"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.estado, "no_contactar_auto.json.bloqueo")))
        for nombre, args in (("crm_registrar_lead", {**LEAD, "email": "otra@persona.com"}),
                             ("crm_crear_tarea", {"email": "otra@persona.com", "tipo": "seguimiento", "borrador": "Hola"}),
                             ("cobranza_evaluar", {"factura_id": "F-001"})):
            err, txt = self.llamar(nombre, args)
            self.assertTrue(err, nombre)
            self.assertIn("bajas sin registrar", txt)
        ref = r["nuevos"][0]["ref"]
        err, txt = self.llamar("correo_captar", {"ref": ref, "origen": "consulta"})
        self.assertTrue(err)
        self.assertEqual(self.crm.llamadas, [])

    def test_borrar_el_marcador_no_basta_mientras_el_proceso_siga_vivo_y_si_tras_repararlo_y_reiniciar(self):
        self.romper_lista_automatica()
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="darme de baja")])
        self.llamar("correo_nuevos", {})
        os.remove(os.path.join(self.estado, "no_contactar_auto.json.bloqueo"))  # lo borran sin reparar nada
        self.assertTrue(self.llamar("crm_registrar_lead", LEAD)[0])  # el proceso lo recuerda: sigue bloqueado
        os.remove(os.path.join(self.estado, "no_contactar_auto.json"))  # la persona repara la lista...
        self.s = servidor.Servicio(self.raiz, self.estado, os.path.join(RAIZ, "agente", "catalogo.json"), crm_fabrica=lambda: self.crm,
                                   reloj=lambda: self.reloj, correo_cfg=tco.CFG, correo_fabrica=tco.fab(self.falso),
                                   no_contactar_persona=self.persona)  # ...y reinicia el servidor
        self.assertFalse(self.llamar("crm_registrar_lead", LEAD)[0])

    def test_si_ni_siquiera_se_puede_escribir_el_marcador_el_proceso_igualmente_se_bloquea(self):
        self.romper_lista_automatica()
        os.makedirs(os.path.join(self.estado, "no_contactar_auto.json.bloqueo"))  # un directorio donde iría el archivo: escribirlo falla
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="darme de baja")])
        self.llamar("correo_nuevos", {})
        self.assertTrue(self.llamar("crm_registrar_lead", LEAD)[0])

    def test_sin_fallos_no_se_crea_ningun_marcador(self):
        self.falso.mensajes = dict([tco.mensaje(1, cuerpo="darme de baja")])
        self.llamar("correo_nuevos", {})
        self.assertFalse(os.path.exists(os.path.join(self.estado, "no_contactar_auto.json.bloqueo")))
        self.assertFalse(self.llamar("crm_registrar_lead", {**LEAD, "email": "otra@persona.com"})[0])


class Cobranza(Base):
    def test_evaluar_un_deudor_en_la_lista_escala_a_una_persona(self):
        self.lista("ana@taller.com\n")
        err, r = self.llamar("cobranza_evaluar", {"factura_id": "F-001"})
        self.assertFalse(err, r)
        self.assertEqual((r["etapa"], r["requiere_persona"], r["mensaje_posible"], r["motivo"]), ("escalar", True, False, "no_contactar"))
        self.assertNotIn("ana@taller.com", json.dumps(r))

    def test_preparar_un_deudor_en_la_lista_no_deja_borrador(self):
        self.lista("@taller.com\n")
        self.assertTrue(self.llamar("cobranza_preparar", {"factura_id": "F-001"})[0])
        carpeta = os.path.join(self.raiz, "workspace", "borradores_cobranza")
        self.assertEqual(os.listdir(carpeta) if os.path.isdir(carpeta) else [], [])

    def test_sin_lista_en_cobranza_nada_cambia(self):
        err, r = self.llamar("cobranza_evaluar", {"factura_id": "F-001"})
        self.assertFalse(err, r)
        self.assertEqual(r["etapa"], "firme")
        self.assertNotIn("motivo", r)
        self.assertFalse(self.llamar("cobranza_preparar", {"factura_id": "F-001"})[0])


if __name__ == "__main__":
    unittest.main()
