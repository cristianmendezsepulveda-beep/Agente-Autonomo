"""Bajas automáticas: detección determinista de "no me contacten" en correos VERIFICADOS. Buzón IMAP falso, sin red."""

import os
import sys
import tempfile
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(AQUI, "..", "mcp"))
import correo as co  # noqa: E402
import test_correo as tc  # noqa: E402


class Detector(unittest.TestCase):
    def baja(self, asunto="Hola", cuerpo="texto"):
        return co.detectar_baja(asunto, cuerpo.encode("utf-8"), tc.cabecera(asunto=asunto))

    def test_frases_en_espanol_e_ingles(self):
        positivos = [
            "Quiero darme de baja de su lista",
            "Por favor, no me contacten más",
            "NO ME ESCRIBAN",
            "Dejen de enviarme correos",
            "no quiero recibir más mensajes",
            "Solicito la baja inmediata",
            "cancelar mi suscripción",
            "no me llamen por favor",
            "Please unsubscribe me",
            "remove me from your list",
            "stop emailing me",
            "Do not contact me again",
            "don't contact me",
            "I want to opt-out",
            "Opt out",
            "take me off your list",
            "I no longer wish to receive these emails",
        ]
        for texto in positivos:
            self.assertTrue(self.baja(cuerpo=texto), texto)

    def test_ruido_tipografico_no_la_esconde(self):
        for texto in (
            "Quiero  darme\n\nde   baja",
            "quiero DARME DE BAJA",
            "darme de baja",
            "ｕｎｓｕｂｓｃｒｉｂｅ",
            "opt­out",
            "no me​ contacten",
            "Darme de bája",
            "NO ME CONTÁCTEN",
        ):
            self.assertTrue(self.baja(cuerpo=texto), repr(texto))

    def test_el_asunto_tambien_cuenta(self):
        self.assertTrue(self.baja(asunto="UNSUBSCRIBE", cuerpo="."))

    def test_texto_que_no_es_una_baja(self):
        for texto in (
            "Hola, necesito una cotización de sus servicios",
            "¿Cuál es el horario de atención?",
            "No puedo recibir su factura por correo, ¿pueden reenviarla?",
            "El precio de baja temporada me interesa",
            "Quiero contactarlos para cotizar",
            "stop by our office tomorrow",
        ):
            self.assertFalse(self.baja(cuerpo=texto), texto)

    def test_la_frase_citada_de_un_mensaje_anterior_no_cuenta(self):
        self.assertFalse(self.baja(cuerpo="Gracias, me interesa.\r\n> Si desea darse de baja responda unsubscribe\r\n> saludos"))

    def test_busca_mas_alla_de_los_primeros_300_caracteres_pero_con_tope(self):
        self.assertTrue(self.baja(cuerpo="Hola. " + "relleno " * 150 + "por favor darme de baja"))
        self.assertFalse(self.baja(cuerpo="relleno " * 600 + "darme de baja"))  # más allá del tope de lectura (2000 caracteres)

    def test_entrada_enorme_o_rara_no_se_cuelga_ni_lanza(self):
        import time

        inicio = time.time()
        for cuerpo in ("a" * 500_000, "\x00" * 10_000, "darme de " * 50_000):
            self.baja(cuerpo=cuerpo)
        self.assertLess(time.time() - inicio, 3.0)
        self.assertFalse(co.detectar_baja("x", b"\xff\xfe\xfa", b"no es una cabecera"))


class DireccionesQueLaListaNoEntiende(tc.Base):
    """El CRM acepta `+a@x.com` o `ana@-x.com` pero la lista no las entiende: un remitente así nunca debe llegar a captarse ni a dar de baja
    (antes, su baja fallaba, activaba la alerta y el mensaje acaparaba una plaza de candidatos durante 7 días).
    """

    def test_se_descartan_como_remitente_en_analizar(self):
        for de in (
            "x <+a@cliente.com>",
            "x <...@gmail.com>",
            "x <ana@-cliente.com>",
            "x <ana@cliente-.com>",
        ):
            d, razon = co.analizar(tc.cabecera(de=de), tc.CFG["dominios_propios"], tc.CFG["authserv_id"])
            self.assertEqual((d, razon), (None, "remitente"), de)

    def test_no_acaparan_ni_activan_la_alerta(self):
        mensajes = [tc.mensaje(i, de=f"x <+{i}@cliente.com>", cuerpo="darme de baja") for i in range(1, 6)]
        mensajes.append(tc.mensaje(9, de="Luis <luis@cliente.com>", cuerpo="unsubscribe"))
        bajas = []
        r = co.nuevos(tc.CFG, self.estado, tc.AHORA, tc.fab(tc.FalsoIMAP(dict(mensajes))), baja=bajas.append)
        self.assertEqual(bajas, ["luis@cliente.com"])
        self.assertEqual((r["bajas_registradas"], r["bajas_no_registradas"]), (1, 0))
        self.assertNotIn("alerta", r)
        self.assertEqual(r["descartados_por_motivo"].get("remitente"), 5)


class Cabeceras(unittest.TestCase):
    def test_comentarios_anidados_hasta_el_limite_verifican_y_mas_se_rechazan_rapido(self):
        import time

        ok = "mx.ejemplo.test; dkim=pass header.d=cliente.com (a (b (c)))"
        self.assertTrue(co.analizar(tc.cabecera(auth=ok), (), tc.CFG["authserv_id"])[0]["verificado"])
        profundo = "mx.ejemplo.test; dkim=pass header.d=cliente.com " + "(" * 7000 + "x" + ")" * 7000
        inicio = time.time()
        d, _ = co.analizar(tc.cabecera(auth=profundo), (), tc.CFG["authserv_id"])
        self.assertLess(time.time() - inicio, 1.0)
        self.assertFalse(d is not None and d["verificado"])
        cuatro = "mx.ejemplo.test; dkim=pass header.d=cliente.com ((((x))))"
        d, _ = co.analizar(tc.cabecera(auth=cuatro), (), tc.CFG["authserv_id"])
        self.assertFalse(d is not None and d["verificado"])


class SinCuerpo(tc.FalsoIMAP):
    """El servidor no entrega el cuerpo (error transitorio): la detección de bajas no puede mirar solo el asunto."""

    def uid(self, cmd, *args):
        if cmd == "FETCH" and "TEXT" in args[1]:
            self.comandos.append((cmd,) + args)
            return "NO", [None]
        return super().uid(cmd, *args)


class CuerpoYHtml(tc.Base):
    def test_si_el_cuerpo_no_llega_el_mensaje_no_se_lista_y_se_reintenta(self):
        falso = SinCuerpo(dict([tc.mensaje(1, cuerpo="darme de baja")]))
        bajas = []
        r = co.nuevos(tc.CFG, self.estado, tc.AHORA, tc.fab(falso), baja=bajas.append)
        self.assertEqual((r["nuevos"], r["no_leidos_por_error"], bajas), ([], 1, []))
        r = co.nuevos(
            tc.CFG,
            self.estado,
            tc.AHORA,
            tc.fab(tc.FalsoIMAP(dict([tc.mensaje(1, cuerpo="darme de baja")]))),
            baja=bajas.append,
        )
        self.assertEqual((r["bajas_registradas"], bajas), (1, ["ana@cliente.com"]))  # al llegar el cuerpo, se registra

    def test_un_cuerpo_realmente_vacio_si_se_lista(self):
        falso = tc.FalsoIMAP(dict([tc.mensaje(1, cuerpo="")]))
        r = co.nuevos(tc.CFG, self.estado, tc.AHORA, tc.fab(falso), baja=lambda m: None)
        self.assertEqual((len(r["nuevos"]), r["no_leidos_por_error"]), (1, 0))

    def test_una_baja_en_un_correo_solo_html_tambien_se_detecta(self):
        cab = tc.cabecera().replace(b"text/plain", b"text/html")
        for html in (
            b"<p>Por favor <b>darme de baja</b></p>",
            b"<div>darme <i>de</i> baja</div>",
            b"<p>unsubscribe&nbsp;me</p>",
            b"<html><body><p>No me <a href='x'>contacten</a> m\xc3\xa1s</p></body></html>",
        ):
            self.assertTrue(co.detectar_baja("Hola", html, cab), html)
        self.assertFalse(co.detectar_baja("Hola", b"<p>Quiero una <b>cotizaci\xc3\xb3n</b></p>", cab))
        self.assertFalse(co.detectar_baja("Hola", b"<a href='https://x.test/unsubscribe-demo-no-es-texto'>ver</a>", cab))  # el atributo no es texto visible


class EnNuevos(tc.Base):
    def setUp(self):
        super().setUp()
        self.bajas = []

    def nuevos_con_baja(self, falso, callback=None):
        return co.nuevos(tc.CFG, self.estado, tc.AHORA, tc.fab(falso), baja=callback or self.bajas.append)

    def test_una_baja_verificada_se_registra_no_se_lista_y_se_da_por_procesada(self):
        falso = tc.FalsoIMAP(dict([tc.mensaje(1, cuerpo="Quiero darme de baja, gracias"), tc.mensaje(2)]))
        r = self.nuevos_con_baja(falso)
        self.assertEqual(self.bajas, ["ana@cliente.com"])
        self.assertEqual(r["bajas_registradas"], 1)
        self.assertEqual(len(r["nuevos"]), 1)
        self.assertNotIn("ana@cliente.com", str(r))
        self.assertNotIn("baja", str(r["nuevos"]).lower())
        r2 = self.nuevos_con_baja(falso)
        self.assertEqual(self.bajas, ["ana@cliente.com"])  # no se registra dos veces
        self.assertEqual(r2["bajas_registradas"], 0)

    def test_un_mensaje_sin_verificar_no_puede_dar_de_baja_a_nadie(self):
        falso = tc.FalsoIMAP(dict([tc.mensaje(1, cuerpo="darme de baja", auth=None)]))
        r = self.nuevos_con_baja(falso)
        self.assertEqual((self.bajas, r["bajas_registradas"], r["sin_verificar"]), ([], 0, 1))

    def test_sin_callback_el_comportamiento_anterior_no_cambia(self):
        falso = tc.FalsoIMAP(dict([tc.mensaje(1, cuerpo="darme de baja")]))
        r = co.nuevos(tc.CFG, self.estado, tc.AHORA, tc.fab(falso))
        self.assertEqual(len(r["nuevos"]), 1)
        self.assertNotIn("bajas_registradas", r)

    def test_si_no_se_puede_registrar_la_baja_el_mensaje_no_se_lista_ni_se_pierde(self):
        falso = tc.FalsoIMAP(dict([tc.mensaje(1, cuerpo="darme de baja")]))

        def falla(email):
            raise RuntimeError("lista llena")

        r = self.nuevos_con_baja(falso, falla)
        self.assertEqual((r["nuevos"], r["bajas_registradas"], r["bajas_no_registradas"]), ([], 0, 1))
        r = self.nuevos_con_baja(falso)  # se reintenta en la siguiente llamada
        self.assertEqual((r["bajas_registradas"], self.bajas), (1, ["ana@cliente.com"]))

    def test_tras_el_primer_fallo_no_se_reintenta_y_ninguna_baja_llega_al_modelo(self):
        falso = tc.FalsoIMAP(
            dict(
                [
                    tc.mensaje(1, cuerpo="darme de baja"),
                    tc.mensaje(2, de="Luis <luis@cliente.com>", cuerpo="unsubscribe"),
                    tc.mensaje(3, de="Eva <eva@cliente.com>", cuerpo="Quiero una cotización"),
                ]
            )
        )
        llamadas = []

        def falla(email):
            llamadas.append(email)
            raise RuntimeError("lista llena")

        r = self.nuevos_con_baja(falso, falla)
        self.assertEqual(len(llamadas), 1)  # no se insiste con una lista rota
        self.assertEqual((r["bajas_registradas"], r["bajas_no_registradas"]), (0, 2))
        self.assertEqual(len(r["nuevos"]), 1)  # solo el correo normal; NINGUNA baja se lista aunque no se pudiera registrar

    def test_las_bajas_cuentan_para_el_tope_por_llamada(self):
        """Antes una llamada podía procesar hasta 30 bajas (los candidatos) sin gastar plazas: inundar con bajas era barato."""
        falso = tc.FalsoIMAP(dict(tc.mensaje(i, de=f"u <u{i}@cliente.com>", cuerpo="unsubscribe") for i in range(1, 16)))
        r = self.nuevos_con_baja(falso)
        self.assertEqual((r["bajas_registradas"], len(self.bajas)), (co.MAX_POR_LLAMADA, co.MAX_POR_LLAMADA))
        r = self.nuevos_con_baja(falso)
        self.assertEqual(r["bajas_registradas"], 5)  # el resto, en la siguiente llamada

    def test_un_auto_submitted_o_precedence_repetido_no_enga_a_el_filtro(self):
        for extra in (
            "Auto-Submitted: no\r\nAuto-Submitted: auto-generated\r\n",
            "Precedence: normal\r\nPrecedence: bulk\r\n",
            "Auto-Submitted: auto-replied\r\nAuto-Submitted: no\r\n",
        ):
            d, razon = co.analizar(tc.cabecera(extra=extra), tc.CFG["dominios_propios"], tc.CFG["authserv_id"])
            self.assertEqual((d, razon), (None, "automatico"), extra)

    def test_dos_remitentes_distintos_se_registran_cada_uno(self):
        falso = tc.FalsoIMAP(
            dict(
                [
                    tc.mensaje(1, cuerpo="unsubscribe"),
                    tc.mensaje(2, de="Luis <luis@cliente.com>", cuerpo="no me contacten"),
                ]
            )
        )
        r = self.nuevos_con_baja(falso)
        self.assertEqual(
            (sorted(self.bajas), r["bajas_registradas"]),
            (["ana@cliente.com", "luis@cliente.com"], 2),
        )


if __name__ == "__main__":
    unittest.main()
