"""Pruebas del motor de seguimiento y cobranza y de sus herramientas en el MCP. Todo con fechas y reloj fijos; no se envía nada."""
import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.realpath(os.path.join(AQUI, ".."))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(RAIZ, "mcp"))
import cobranza as cb
import servidor

HOY = datetime.date(2027, 3, 1)
ENC = ",".join(cb.COLUMNAS)


def fila(fid="F-001", ref="cli-A1", email="ana@taller.com", monto="100.00", moneda="USD", vence="2027-02-20", estado="pendiente",
         n="0", ultimo=""):
    return ",".join([fid, ref, email, monto, moneda, vence, estado, n, ultimo])


def csv_de(*filas):
    return ENC + "\n" + "\n".join(filas) + "\n"


def factura(**kw):
    base = {"factura_id": "F-001", "cliente_ref": "cli-A1", "email": "ana@taller.com", "monto": Decimal("100.00"), "moneda": "USD",
            "vence": HOY - datetime.timedelta(days=10), "estado": "pendiente", "recordatorios": 0, "ultimo": None}
    base.update(kw)
    return base


class Motor(unittest.TestCase):
    def etapa(self, dias, **kw):
        return cb.evaluar(factura(vence=HOY - datetime.timedelta(days=dias), **kw), HOY)

    def test_bordes_de_dias(self):
        esperado = {-5: "al_dia", 0: "al_dia", 1: "amable", 7: "amable", 8: "firme", 30: "firme", 31: "escalar"}
        for dias, etapa in esperado.items():
            self.assertEqual(self.etapa(dias)[0], etapa, dias)

    def test_dias_atraso_se_devuelven(self):
        self.assertEqual(self.etapa(12), ("firme", 12))

    def test_pagada_anulada_y_disputa_nunca_generan_mensaje(self):
        for estado in ("pagada", "anulada"):
            self.assertEqual(self.etapa(15, estado=estado)[0], "sin_accion")
        self.assertEqual(self.etapa(15, estado="disputa")[0], "escalar")
        self.assertEqual(self.etapa(0, estado="disputa")[0], "escalar")  # aunque aún no venza

    def test_monto_alto_y_tres_recordatorios_escalan(self):
        self.assertEqual(self.etapa(3, monto=Decimal("2000.00"))[0], "amable")  # justo en el umbral: no escala
        self.assertEqual(self.etapa(3, monto=Decimal("2000.01"))[0], "escalar")
        self.assertEqual(self.etapa(3, recordatorios=2)[0], "amable")
        self.assertEqual(self.etapa(3, recordatorios=3)[0], "escalar")

    def test_espera_siete_dias_entre_recordatorios(self):
        self.assertEqual(self.etapa(10, ultimo=HOY - datetime.timedelta(days=6))[0], "espera")
        self.assertEqual(self.etapa(10, ultimo=HOY - datetime.timedelta(days=7))[0], "firme")

    def test_plantillas_con_cifras_del_codigo_y_sin_amenazas(self):
        for etapa in ("amable", "firme"):
            b = cb.preparar(factura(monto=Decimal("1234.50")), etapa, 10, "2027-03-01T00:00:00+00:00")
            self.assertIn("USD 1234.50", b["mensaje"])
            self.assertIn("2027-02-19", b["mensaje"])
            self.assertIn("no recibir más recordatorios", b["mensaje"])
            for prohibida in ("demanda", "legal", "embargo", "policía", "reportar", "buró", "consecuencias", "descuento", "plazo adicional"):
                self.assertNotIn(prohibida, b["mensaje"].lower())
            self.assertEqual(b["estado"], "borrador_pendiente_aprobacion")
            self.assertIn("NO enviado", b["nota"])
        for etapa in ("escalar", "al_dia", "espera", "sin_accion"):
            with self.assertRaises(ValueError):
                cb.preparar(factura(), etapa, 10, "x")


class Lector(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.ruta = os.path.join(self.t.name, "cartera.csv")

    def tearDown(self):
        self.t.cleanup()

    def escribir(self, datos):
        with open(self.ruta, "wb") as f:
            f.write(datos if isinstance(datos, bytes) else datos.encode("utf-8"))

    def test_cartera_valida(self):
        self.escribir(csv_de(fila(), fila("F-002", monto="50.50")))
        filas, rech = cb.leer_cartera(self.ruta)
        self.assertEqual((len(filas), rech), (2, 0))
        self.assertEqual(filas["F-002"]["monto"], Decimal("50.50"))

    def test_filas_invalidas_se_rechazan_una_a_una(self):
        malas = [fila(monto="-5.00"), fila(monto="abc"), fila(monto="10.123"), fila(monto="NaN"), fila(monto="1e3"), fila(monto="0.00"), fila(monto="1,000.00"), fila(monto="Infinity"),
                 fila(moneda="usd"), fila(vence="01/03/2027"), fila(vence="2027-13-45"), fila(estado="cobrada"), fila(email="x"),
                 fila(fid="../x"), fila(fid="=CMD"), fila(ref="=SUM(A1)"), fila(ref="cli-123456789"), fila(n="x"), fila(n="100"),
                 fila(ultimo="mañana")]
        for m in malas:
            self.escribir(csv_de(m, fila("OK-1")))
            filas, rech = cb.leer_cartera(self.ruta)
            self.assertEqual((list(filas), rech), (["OK-1"], 1), m)

    def test_id_repetido_deja_fuera_todas_sus_filas(self):
        self.escribir(csv_de(fila("F-1"), fila("F-1", monto="999.00"), fila("F-2")))
        filas, rech = cb.leer_cartera(self.ruta)
        self.assertEqual(list(filas), ["F-2"])
        self.assertEqual(rech, 2)  # las DOS filas del id repetido se descartan

    def test_id_repetido_con_una_fila_invalida_o_distinto_caso_tampoco_sobrevive(self):
        # hallazgo ALTO de Hermes: la fila "pagada" malformada no debe dejar viva a la fila "pendiente" del mismo id
        casos = [(fila("F-1"), fila("F-1", monto="abc", estado="pagada")),
                 (fila("F-1"), "F-1,solo,tres"),
                 (fila("F-1"), fila("f-1", estado="pagada")),
                 (fila("F-1"), fila("F-1", monto="10.00", vence="mal"))]
        for a, b in casos:
            self.escribir(csv_de(a, b, fila("F-2")))
            filas, rech = cb.leer_cartera(self.ruta)
            self.assertEqual(list(filas), ["F-2"], (a, b))
            self.assertGreaterEqual(rech, 1)

    def test_digitos_no_ascii_se_rechazan(self):
        for mala in (fila(n="٣"), fila(monto="١٠٠.00"), fila(vence="٢٠٢٧-02-20")):
            self.escribir(csv_de(mala, fila("OK-1")))
            self.assertEqual((list(cb.leer_cartera(self.ruta)[0]), cb.leer_cartera(self.ruta)[1]), (["OK-1"], 1), mala)

    def test_factura_id_con_numero_largo_se_rechaza(self):
        self.escribir(csv_de(fila("2026110012345"), fila("F-2")))
        self.assertEqual(list(cb.leer_cartera(self.ruta)[0]), ["F-2"])
        with self.assertRaises(ValueError):
            cb.validar_factura_id("2026110012345")

    def test_formato_y_tamano(self):
        for texto in ("factura_id,monto\nF-1,5\n", ENC + ",extra\n", ""):
            self.escribir(texto)
            with self.assertRaises(cb.CarteraInvalida):
                cb.leer_cartera(self.ruta)
        self.escribir(csv_de(*[fila(f"F-{i}") for i in range(cb.MAX_FILAS + 1)]))
        with self.assertRaises(cb.CarteraInvalida):
            cb.leer_cartera(self.ruta)
        self.escribir("x" * (cb.MAX_BYTES + 1))
        with self.assertRaises(cb.CarteraInvalida):
            cb.leer_cartera(self.ruta)
        self.escribir(b"\xff\xfe\x00bad")
        with self.assertRaises(cb.CarteraInvalida):
            cb.leer_cartera(self.ruta)
        with self.assertRaises(cb.CarteraInvalida):
            cb.leer_cartera(os.path.join(self.t.name, "no-existe.csv"))

    def test_filas_con_columnas_de_mas_o_de_menos_se_rechazan(self):
        self.escribir(ENC + "\nF-1,a\n" + fila("F-2") + ",sobrante\n" + fila("F-3") + "\n")
        filas, rech = cb.leer_cartera(self.ruta)
        self.assertEqual((list(filas), rech), (["F-3"], 2))

    def test_bom_utf8_aceptado(self):
        self.escribir(("﻿" + csv_de(fila())).encode("utf-8"))
        self.assertEqual(list(cb.leer_cartera(self.ruta)[0]), ["F-001"])


class Seguimientos(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.t.cleanup()

    def borrador(self, nombre, edad_dias, total="230.00", estado="borrador_pendiente_aprobacion"):
        ruta = os.path.join(self.t.name, nombre + ".json")
        with open(ruta, "w") as f:
            json.dump({"id": nombre, "total": total, "moneda": "USD", "estado": estado}, f)
        t = time.time() - edad_dias * 86400
        os.utime(ruta, (t, t))

    def test_lista_solo_antiguos_validos_y_ordenados(self):
        self.borrador("b-000000000001", 5)
        self.borrador("b-000000000002", 1)  # reciente
        self.borrador("b-000000000003", 9)
        self.borrador("b-000000000004", 9, estado="cerrado")
        self.borrador("otro", 9)
        r = cb.seguimientos(self.t.name, time.time())
        self.assertEqual([x["borrador"] for x in r], ["b-000000000003", "b-000000000001"])
        self.assertEqual(r[0]["referencia"], "seg-00000000")
        self.assertNotRegex(r[0]["referencia"], r"\d{9}")  # apta para escalar_a_humano

    def test_tope_de_veinte_y_carpeta_ausente(self):
        for i in range(25):
            self.borrador(f"b-{i:012x}", 5 + i)
        self.assertEqual(len(cb.seguimientos(self.t.name, time.time())), cb.MAX_SEGUIMIENTOS)
        self.assertEqual(cb.seguimientos(os.path.join(self.t.name, "nada"), time.time()), [])

    def test_archivo_corrupto_se_ignora(self):
        with open(os.path.join(self.t.name, "b-0000000000aa.json"), "w") as f:
            f.write("{no")
        self.assertEqual(cb.seguimientos(self.t.name, time.time() + 9 * 86400), [])


class MCP(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.raiz = os.path.join(self.t.name, "proy")
        os.makedirs(os.path.join(self.raiz, "workspace", "cartera"))
        self.reloj = datetime.datetime(2027, 3, 1, 12, tzinfo=datetime.timezone.utc).timestamp()
        self.lista_nc = os.path.join(self.t.name, "no_contactar.txt")  # lista de no contacto vacía (es obligatoria: falla cerrado)
        with open(self.lista_nc, "w", encoding="utf-8") as f:
            f.write("# vacía\n")
        self.s = servidor.Servicio(self.raiz, os.path.join(self.t.name, "estado"), os.path.join(RAIZ, "agente", "catalogo.json"),
                                   reloj=lambda: self.reloj, no_contactar_persona=self.lista_nc)
        self.cartera(csv_de(fila("F-001", vence="2027-02-20"), fila("F-002", vence="2027-02-28"), fila("F-003", estado="disputa"),
                            fila("F-004", monto="5000.00"), fila("F-005", estado="pagada"), fila("F-006", vence="2027-04-01")))

    def tearDown(self):
        self.t.cleanup()

    def cartera(self, texto):
        with open(os.path.join(self.raiz, "workspace", "cartera", "cartera.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(texto)

    def llamar(self, nombre, args):
        r = servidor.manejar(self.s, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": nombre, "arguments": args}})
        txt = r["result"]["content"][0]["text"]
        return r["result"]["isError"], (json.loads(txt) if not r["result"]["isError"] else txt)

    def test_evaluar_cada_caso(self):
        esperado = {"F-001": "firme", "F-002": "amable", "F-003": "escalar", "F-004": "escalar", "F-005": "sin_accion", "F-006": "al_dia"}
        for fid, etapa in esperado.items():
            err, r = self.llamar("cobranza_evaluar", {"factura_id": fid})
            self.assertFalse(err, r)
            self.assertEqual(r["etapa"], etapa, fid)
            self.assertNotIn("ana@taller.com", json.dumps(r))

    def test_preparar_deja_borrador_sin_enviar_y_no_devuelve_datos_personales(self):
        err, r = self.llamar("cobranza_preparar", {"factura_id": "F-001"})
        self.assertFalse(err, r)
        self.assertEqual((r["etapa"], r["enviado"]), ("firme", False))
        self.assertNotIn("ana@taller.com", json.dumps(r))
        ruta = os.path.join(self.raiz, "workspace", "borradores_cobranza", r["borrador"] + ".json")
        with open(ruta, encoding="utf-8") as f:
            b = json.load(f)
        self.assertEqual(b["destinatario"], "ana@taller.com")
        self.assertIn("USD 100.00", b["mensaje"])
        self.assertEqual(b["estado"], "borrador_pendiente_aprobacion")

    def test_no_se_duplica_ni_se_prepara_lo_que_no_corresponde(self):
        self.assertFalse(self.llamar("cobranza_preparar", {"factura_id": "F-001"})[0])
        self.assertTrue(self.llamar("cobranza_preparar", {"factura_id": "F-001"})[0])  # ya existe el borrador
        for fid in ("F-003", "F-004", "F-005", "F-006"):
            self.assertTrue(self.llamar("cobranza_preparar", {"factura_id": fid})[0], fid)
        carpeta = os.path.join(self.raiz, "workspace", "borradores_cobranza")
        self.assertEqual(len(os.listdir(carpeta)), 1)

    def test_amable_pendiente_bloquea_un_firme_posterior_sin_actualizar_el_csv(self):
        self.cartera(csv_de(fila("F-9", vence="2027-02-22")))  # 7 días: amable
        self.assertFalse(self.llamar("cobranza_preparar", {"factura_id": "F-9"})[0])
        self.reloj += 86400  # al día siguiente son 8 días: firme, pero el CSV sigue sin ultimo_recordatorio
        err, r = self.llamar("cobranza_evaluar", {"factura_id": "F-9"})
        self.assertEqual(r["etapa"], "firme")
        self.assertTrue(self.llamar("cobranza_preparar", {"factura_id": "F-9"})[0])
        self.assertEqual(len(os.listdir(os.path.join(self.raiz, "workspace", "borradores_cobranza"))), 1)

    def test_id_que_es_prefijo_de_otro_no_se_bloquea_por_error(self):
        self.cartera(csv_de(fila("F-1", vence="2027-02-22"), fila("F-1-2", vence="2027-02-22")))
        self.assertFalse(self.llamar("cobranza_preparar", {"factura_id": "F-1-2"})[0])
        self.assertFalse(self.llamar("cobranza_preparar", {"factura_id": "F-1"})[0])

    def test_filas_descartadas_se_informan(self):
        self.cartera(csv_de(fila("F-1"), fila("F-2", monto="mal")))
        err, r = self.llamar("cobranza_evaluar", {"factura_id": "F-1"})
        self.assertEqual(r["filas_descartadas_en_cartera"], 1)
        err, txt = self.llamar("cobranza_evaluar", {"factura_id": "F-2"})
        self.assertTrue(err)
        self.assertIn("1 fila", txt)

    def test_argumentos_y_ids_invalidos(self):
        for args in ({}, {"factura_id": "F-001", "x": 1}, {"factura_id": "../F"}, {"factura_id": 5}, {"factura_id": "NO-EXISTE"}):
            self.assertTrue(self.llamar("cobranza_evaluar", args)[0], args)
        self.assertTrue(self.llamar("seguimiento_pendientes", {"x": 1})[0])

    def test_cartera_ausente_o_corrupta_falla_sin_efectos(self):
        os.remove(os.path.join(self.raiz, "workspace", "cartera", "cartera.csv"))
        self.assertTrue(self.llamar("cobranza_evaluar", {"factura_id": "F-001"})[0])
        self.cartera("basura")
        self.assertTrue(self.llamar("cobranza_preparar", {"factura_id": "F-001"})[0])
        self.assertFalse(os.path.exists(os.path.join(self.raiz, "workspace", "borradores_cobranza")))

    def test_seguimiento_via_mcp(self):
        carpeta = os.path.join(self.raiz, "workspace", "borradores")
        os.makedirs(carpeta)
        ruta = os.path.join(carpeta, "b-1234abcd0000.json")
        with open(ruta, "w") as f:
            json.dump({"total": "230.00", "moneda": "USD", "estado": "borrador_pendiente_aprobacion"}, f)
        t = self.reloj - 5 * 86400
        os.utime(ruta, (t, t))
        err, r = self.llamar("seguimiento_pendientes", {})
        self.assertFalse(err, r)
        self.assertEqual(r["pendientes"][0]["referencia"], "seg-1234abcd")
        self.assertEqual(r["pendientes"][0]["dias"], 5)

    def test_la_cartera_no_puede_salir_por_un_enlace(self):
        # una cartera que sea un enlace simbólico hacia fuera del proyecto debe rechazarse (si el sistema permite crearlo)
        fuera = os.path.join(self.t.name, "fuera.csv")
        with open(fuera, "w") as f:
            f.write(csv_de(fila("F-099", vence="2027-02-20")))
        ruta = os.path.join(self.raiz, "workspace", "cartera", "cartera.csv")
        os.remove(ruta)
        try:
            os.symlink(fuera, ruta)
        except (OSError, NotImplementedError):
            self.skipTest("no se pueden crear enlaces simbólicos aquí")
        self.assertTrue(self.llamar("cobranza_evaluar", {"factura_id": "F-099"})[0])


if __name__ == "__main__":
    unittest.main()
