"""Pruebas de la bandeja de aprobación (agente/bandeja.py). Todo en directorios temporales: nada toca workspace/ real."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(AQUI, "..", "sandbox"))
import bandeja as B  # noqa: E402

AHORA = 1_800_000_000.0  # instante fijo para las pruebas
CREADO = "2027-01-15T07:00:00+00:00"  # 1 día antes de AHORA


def D(item_id, decision, raiz, **kw):
    """decidir() con el hash del contenido actual (lo que la persona vería): aprobar lo exige."""
    if decision == "aprobado" and "hash_esperado" not in kw:
        kw["hash_esperado"] = B.hash_contenido(B.ver(item_id, raiz)[0])
    kw.setdefault("ruta_lista_persona", os.path.join(raiz, "no_contactar.txt"))  # lista de no contacto de la prueba (vacía por defecto)
    return B.decidir(item_id, decision, raiz, **kw)


def iso(delta_dias=0):
    import datetime

    return datetime.datetime.fromtimestamp(AHORA - delta_dias * 86400, datetime.timezone.utc).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.raiz = self.t.name
        for c in ("borradores", "borradores_cobranza", "escalados"):
            os.makedirs(os.path.join(self.raiz, "workspace", c))
        self.lista_nc = os.path.join(self.raiz, "no_contactar.txt")
        self.lista("# vacía\n")

    def lista(self, texto):
        with open(self.lista_nc, "w", encoding="utf-8", newline="") as f:
            f.write(texto)

    def tearDown(self):
        self.t.cleanup()

    def ruta(self, carpeta, nombre):
        return os.path.join(self.raiz, "workspace", carpeta, nombre + ".json")

    def poner(self, carpeta, id_, cuerpo, sellar=True):
        d = B.sellar(cuerpo) if sellar else cuerpo
        with open(self.ruta(carpeta, id_), "w", encoding="utf-8") as f:
            json.dump(d, f)
        return d

    def cot(self, id_="b-0123456789ab", dias=1, sellar=True, **extra):
        return self.poner(
            "borradores",
            id_,
            {
                "id": id_,
                "moneda": "USD",
                "cliente_ref": "chat-A1",
                "lineas": [
                    {
                        "sku": "SRV-BASICO",
                        "nombre": "Servicio basico",
                        "cantidad": 1,
                        "precio_unitario": "910.00",
                        "total": "910.00",
                    }
                ],
                "subtotal": "910.00",
                "descuento_pct": "0",
                "descuento": "0.00",
                "impuesto": "0.00",
                "total": "910.00",
                "requiere_aprobacion_humana": False,
                "creado": iso(dias),
                "estado": "borrador_pendiente_aprobacion",
                **extra,
            },
            sellar,
        )

    def cob(self, id_="c-f001-amable", dias=1, sellar=True):
        return self.poner(
            "borradores_cobranza",
            id_,
            {
                "id": id_,
                "factura_id": "F001",
                "etapa": id_.rsplit("-", 1)[1],
                "destinatario": "a@b.com",
                "asunto": "x",
                "mensaje": "y",
                "creado": iso(dias),
                "estado": "borrador_pendiente_aprobacion",
            },
            sellar,
        )

    def esc(self, id_="e-0123456789ab", dias=1):
        return self.poner(
            "escalados",
            id_,
            {
                "id": id_,
                "creado": iso(dias),
                "motivo": "cliente molesto",
                "referencia": "chat-A1",
                "estado": "pendiente",
            },
            sellar=False,
        )


class Sello(unittest.TestCase):
    def test_sello_intacto_editado_y_ausente(self):
        d = B.sellar({"a": 1, "b": [1, 2]})
        self.assertEqual(B.estado_sello(d), "intacto")
        self.assertEqual(B.estado_sello({**d, "a": 2}), "editado")
        self.assertEqual(B.estado_sello({"a": 1}), "sin_sello")
        self.assertEqual(B.estado_sello({**d, "sello": 5}), "editado")
        self.assertEqual(B.estado_sello({**d, "sello": "0" * 64}), "editado")

    def test_sellar_es_idempotente_y_no_muta(self):
        original = {"a": 1}
        d = B.sellar(original)
        self.assertNotIn("sello", original)
        self.assertEqual(B.sellar(d), d)

    def test_el_formato_del_json_no_cuenta_como_edicion(self):
        d = B.sellar({"x": "ñ", "y": 1})
        reescrito = json.loads(json.dumps(d, indent=4, ensure_ascii=False))
        self.assertEqual(B.estado_sello(reescrito), "intacto")


class Pendientes(Base):
    def test_lista_las_tres_colas_con_el_mas_antiguo_primero(self):
        self.cot(dias=1)
        self.cob(dias=5)
        self.esc(dias=3)
        items = B.pendientes(self.raiz, AHORA)
        self.assertEqual([i["tipo"] for i in items], ["cobranza", "escalado", "cotizacion"])
        self.assertEqual(items[0]["categoria"], "cobranza_amable")
        self.assertEqual(items[2]["sello"], "intacto")
        self.assertFalse(any(i["vencido"] for i in items))

    def test_vencido_a_partir_de_siete_dias_pero_no_para_escalados(self):
        self.cot(dias=8)
        self.esc(dias=30)
        por_tipo = {i["tipo"]: i for i in B.pendientes(self.raiz, AHORA)}
        self.assertTrue(por_tipo["cotizacion"]["vencido"])
        self.assertFalse(por_tipo["escalado"]["vencido"])

    def test_archivos_danados_o_con_nombre_raro_salen_marcados_nunca_se_omiten(self):
        with open(self.ruta("borradores", "b-aaaaaaaaaaaa"), "w") as f:
            f.write("{no json")
        with open(self.ruta("borradores", "b-bbbbbbbbbbbb"), "w") as f:
            f.write("[1, 2]")
        with open(self.ruta("borradores", "raro"), "w") as f:
            f.write("{}")
        items = B.pendientes(self.raiz, AHORA)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i["ilegible"] for i in items))

    def test_las_escrituras_en_curso_se_ignoran(self):
        with open(os.path.join(self.raiz, "workspace", "borradores", "b-aaaaaaaaaaaa.json.abc.tmp"), "w") as f:
            f.write("{")
        self.assertEqual(B.pendientes(self.raiz, AHORA), [])

    def test_el_texto_con_controles_no_llega_a_la_terminal(self):
        self.poner(
            "escalados",
            "e-0123456789ab",
            {
                "id": "e-0123456789ab",
                "creado": iso(1),
                "motivo": "hola\x1b[31m rojo\x07",
                "referencia": "r",
            },
            sellar=False,
        )
        r = B.pendientes(self.raiz, AHORA)[0]["resumen"]
        self.assertNotIn("\x1b", r)
        self.assertNotIn("\x07", r)

    def test_un_enlace_simbolico_no_se_sigue(self):
        destino = os.path.join(self.raiz, "secreto.json")
        with open(destino, "w") as f:
            json.dump({"id": "b-0123456789ab"}, f)
        try:
            os.symlink(destino, self.ruta("borradores", "b-0123456789ab"))
        except (OSError, NotImplementedError):
            self.skipTest("este sistema no permite crear enlaces simbólicos")
        self.assertTrue(B.pendientes(self.raiz, AHORA)[0]["ilegible"])
        with self.assertRaises(FileNotFoundError):
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)


class Decidir(Base):
    def test_aprobar_archiva_con_hash_y_sello_y_saca_de_la_cola(self):
        d = self.cot()
        destino = D("b-0123456789ab", "aprobado", self.raiz, quien="ana", ahora=AHORA)
        self.assertFalse(os.path.exists(self.ruta("borradores", "b-0123456789ab")))
        with open(destino, encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual(
            (reg["estado"], reg["sello"], reg["decidido_por"], reg["categoria"]),
            ("aprobado", "intacto", "ana", "cotizacion"),
        )
        self.assertEqual(reg["contenido"], d)
        self.assertEqual(reg["hash_contenido"], B._sha(d))
        self.assertIn(os.path.join("archivo", "aprobados"), destino)
        self.assertEqual(B.pendientes(self.raiz, AHORA), [])

    def test_aprobar_un_borrador_editado_queda_marcado_editado(self):
        d = self.cot()
        d["cliente_ref"] = "chat-B2"  # una persona lo cambió a mano, el sello ya no coincide
        with open(self.ruta("borradores", "b-0123456789ab"), "w") as f:
            json.dump(d, f)
        with open(D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sello"], "editado")

    def test_aprobar_sin_sello_queda_sin_sello(self):
        self.cot(sellar=False)
        with open(D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sello"], "sin_sello")

    def test_rechazar_exige_motivo_y_lo_guarda(self):
        self.cob()
        for vacio in ("", "   ", None, "\x1b\x07"):
            with self.assertRaises(ValueError):
                D("c-f001-amable", "rechazado", self.raiz, motivo=vacio, ahora=AHORA)
        self.assertTrue(os.path.exists(self.ruta("borradores_cobranza", "c-f001-amable")))  # nada se movió
        destino = D("c-f001-amable", "rechazado", self.raiz, motivo="tono muy duro", ahora=AHORA)
        with open(destino, encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual((reg["estado"], reg["motivo"]), ("rechazado", "tono muy duro"))
        self.assertIn("rechazados", destino)

    def test_los_escalados_no_se_aprueban_ni_se_rechazan(self):
        self.esc()
        for d in ("aprobado", "rechazado"):
            with self.assertRaises(ValueError):
                D("e-0123456789ab", d, self.raiz, motivo="x", ahora=AHORA)

    def test_pendiente_vencido_no_se_aprueba_sin_forzar_pero_si_se_rechaza(self):
        self.cot(dias=9)
        with self.assertRaises(ValueError):
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))
        D("b-0123456789ab", "aprobado", self.raiz, forzar=True, ahora=AHORA)
        self.cot(dias=9)
        D("b-0123456789ab", "rechazado", self.raiz, motivo="viejo", ahora=AHORA + 5)

    def test_ids_peligrosos_o_de_otro_formato(self):
        for malo in (
            "../x",
            "b-123",
            "b-0123456789ab.json",
            "",
            None,
            5,
            "B-0123456789AB",
            "c-f001-otra",
            "c--x-amable",
            "b-0123456789ab\n",
            "b-0123456789ab/../../x",
            "c-" + "a" * 40 + "-amable",
        ):
            with self.assertRaises(ValueError, msg=repr(malo)):
                D(malo, "aprobado", self.raiz, ahora=AHORA)
        with self.assertRaises(FileNotFoundError):
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)

    def test_el_contenido_debe_corresponder_al_id(self):
        self.poner("borradores", "b-0123456789ab", {"id": "b-ffffffffffff", "creado": iso(1)})
        with self.assertRaises(ValueError):
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)

    def test_si_el_archivo_cambia_mientras_se_decide_no_se_mueve_nada(self):
        self.cot()
        ruta = self.ruta("borradores", "b-0123456789ab")

        class LogQueEdita:  # la edición ocurre entre la lectura y la toma del archivo (justo al registrar el intento)
            def registrar(self_, evento, datos=None):
                if evento.endswith("_intento"):
                    with open(ruta, "a") as f:
                        f.write(" ")

        with mock.patch.object(B, "_auditoria", lambda raiz: LogQueEdita()):
            with self.assertRaises(RuntimeError):
                D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertTrue(os.path.exists(ruta))  # devuelto a la cola, con la edición conservada
        self.assertTrue(open(ruta, encoding="utf-8").read().endswith(" "))
        self.assertEqual([f for f in os.listdir(os.path.dirname(ruta)) if f.endswith(".tomado")], [])
        carpeta = os.path.join(self.raiz, "workspace", "archivo", "aprobados")
        self.assertFalse(os.path.isdir(carpeta) and os.listdir(carpeta))

    def test_aprobar_exige_el_hash_mostrado_y_aborta_si_el_contenido_cambio(self):
        d = self.cot()
        with self.assertRaises(ValueError):
            B.decidir("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)  # sin hash_esperado
        with self.assertRaises(ValueError):
            B.decidir("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA, hash_esperado="x")
        visto = B.hash_contenido(d)
        d2 = {**d, "total": "999.00"}  # cambió después de que la persona lo vio
        with open(self.ruta("borradores", "b-0123456789ab"), "w") as f:
            json.dump(d2, f)
        with self.assertRaises(ValueError) as cm:
            B.decidir("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA, hash_esperado=visto)
        self.assertIn("cambió", str(cm.exception))
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_dos_decisiones_a_la_vez_sobre_el_mismo_id_no_pierden_el_registro(self):
        import threading

        self.cot()
        resultados = []

        def intentar():
            try:
                resultados.append(("ok", D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)))
            except Exception as e:  # el perdedor puede fallar de varias formas, pero NUNCA debe borrar el registro del ganador
                resultados.append(("error", type(e).__name__))

        hilos = [threading.Thread(target=intentar) for _ in range(4)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        oks = [r for r in resultados if r[0] == "ok"]
        self.assertEqual(len(oks), 1, resultados)
        self.assertTrue(os.path.isfile(oks[0][1]))
        self.assertFalse(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_si_no_se_puede_borrar_lo_tomado_vuelve_a_la_cola_sin_registro(self):
        self.cot()
        ruta = self.ruta("borradores", "b-0123456789ab")
        real = os.remove

        def remove(p, *a, **k):
            if str(p).endswith(".tomado"):
                raise PermissionError("antivirus")
            return real(p, *a, **k)

        with mock.patch.object(B.os, "remove", remove):
            with self.assertRaises(PermissionError):
                D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertTrue(os.path.exists(ruta))  # de vuelta en la cola
        carpeta = os.path.join(self.raiz, "workspace", "archivo", "aprobados")
        self.assertEqual(os.listdir(carpeta) if os.path.isdir(carpeta) else [], [])  # y sin registro: no hay duplicado
        D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA + 1)  # un segundo intento funciona

    def test_si_tampoco_se_puede_retirar_el_registro_el_borrador_no_vuelve_a_la_cola(self):
        self.cot()
        real = os.remove

        def remove(p, *a, **k):
            if str(p).endswith(".tomado") or os.sep + "aprobados" + os.sep in str(p):
                raise PermissionError("antivirus")
            return real(p, *a, **k)

        with mock.patch.object(B.os, "remove", remove):
            with self.assertRaises(PermissionError):
                D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        # el registro existe y el borrador NO está como pendiente válido: ningún duplicado aprobable
        self.assertFalse(os.path.exists(self.ruta("borradores", "b-0123456789ab")))
        self.assertEqual(len(os.listdir(os.path.join(self.raiz, "workspace", "archivo", "aprobados"))), 1)
        self.assertTrue(all(i["ilegible"] for i in B.pendientes(self.raiz, AHORA)))  # el sobrante sale marcado

    def test_con_la_cola_bloqueada_el_error_es_controlado(self):
        self.cot()
        os.makedirs(os.path.join(self.raiz, "estado", "bandeja"))
        with open(os.path.join(self.raiz, "estado", "bandeja", "bandeja.lock"), "w") as f:
            f.write("x")
        with mock.patch.object(B, "TIMEOUT_BLOQUEO", 0.2):
            with self.assertRaises(RuntimeError):
                D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_forzar_y_la_confirmacion_quedan_en_el_registro(self):
        self.cot(dias=9)
        with open(
            D(
                "b-0123456789ab",
                "aprobado",
                self.raiz,
                forzar=True,
                ahora=AHORA,
                confirmacion="interactiva",
            ),
            encoding="utf-8",
        ) as f:
            reg = json.load(f)
        self.assertEqual((reg["forzado"], reg["confirmacion"]), (True, "interactiva"))

    def test_sin_registro_de_auditoria_no_se_decide(self):
        self.cot()
        with mock.patch.object(B, "_auditoria", side_effect=RuntimeError("registro corrupto")):
            with self.assertRaises(RuntimeError):
                D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_las_decisiones_quedan_en_el_registro_encadenado(self):
        from audit_log import verificar

        self.cot()
        self.cob()
        D("b-0123456789ab", "aprobado", self.raiz, quien="ana", ahora=AHORA)
        D("c-f001-amable", "rechazado", self.raiz, motivo="no", ahora=AHORA)
        ruta = os.path.join(self.raiz, "estado", "bandeja", "decisiones.jsonl")
        self.assertEqual(verificar(ruta), (True, "ok"))
        with open(ruta, encoding="ascii") as f:
            eventos = [json.loads(x)["evento"] for x in f]
        self.assertEqual(eventos, ["aprobado_intento", "aprobado", "rechazado_intento", "rechazado"])

    def test_un_archivo_de_mas_de_200kb_se_rechaza(self):
        with open(self.ruta("borradores", "b-0123456789ab"), "w") as f:
            f.write('{"id":"b-0123456789ab","x":"' + "a" * 250_000 + '"}')
        with self.assertRaises(ValueError):
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)

    def test_el_nombre_del_usuario_se_limpia(self):
        self.assertEqual(B._quien("ana\x1b[31m\n"), "ana 31m")
        self.assertEqual(B._quien("\x1b"), "desconocido")


class Incidente(Base):
    def test_marca_el_ultimo_aprobado_y_no_se_repite(self):
        self.cob()
        D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)
        ruta = B.incidente("c-f001-amable", "el cliente ya había pagado", self.raiz, quien="ana", ahora=AHORA + 60)
        with open(ruta, encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual(reg["incidente"]["motivo"], "el cliente ya había pagado")
        with self.assertRaises(ValueError):
            B.incidente("c-f001-amable", "otra vez", self.raiz)

    def test_exige_motivo_y_un_aprobado_existente(self):
        with self.assertRaises(ValueError):
            B.incidente("c-f001-amable", "", self.raiz)
        with self.assertRaises(FileNotFoundError):
            B.incidente("c-f001-amable", "algo", self.raiz)
        self.cob()
        D("c-f001-amable", "rechazado", self.raiz, motivo="no", ahora=AHORA)
        with self.assertRaises(FileNotFoundError):  # un rechazado no es un incidente: nunca salió
            B.incidente("c-f001-amable", "algo", self.raiz)

    def test_un_registro_alterado_no_se_toca(self):
        self.cob()
        ruta = D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)
        with open(ruta, encoding="utf-8") as f:
            reg = json.load(f)
        reg["contenido"]["mensaje"] = "otro"
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(reg, f)
        with self.assertRaises(ValueError):
            B.incidente("c-f001-amable", "algo", self.raiz)


class Resumen(Base):
    def decidir_n(self, n, editar=0, rechazar=0, sin_sello=0, categoria="cobranza"):
        for i in range(n):
            id_ = f"b-{i:012x}"
            d = self.cot(id_, sellar=i >= sin_sello)
            if sin_sello <= i < sin_sello + editar:
                d["cliente_ref"] = "chat-B2"
                with open(self.ruta("borradores", id_), "w") as f:
                    json.dump(d, f)
            if i >= n - rechazar:
                D(id_, "rechazado", self.raiz, motivo="no", ahora=AHORA + i)
            else:
                D(id_, "aprobado", self.raiz, ahora=AHORA + i)

    def test_cumple_con_50_casos_95_por_ciento_y_sin_incidentes(self):
        self.decidir_n(50)
        c = B.resumen(self.raiz, 30, AHORA + 100)["categorias"]["cotizacion"]
        self.assertEqual((c["decididos"], c["sin_edicion"], c["tasa_sin_edicion"]), (50, 50, 1.0))
        self.assertTrue(c["cumple_criterio_numerico"])

    def test_no_cumple_con_pocos_casos_con_ediciones_con_incidente_o_sin_sello(self):
        self.decidir_n(49)
        self.assertFalse(B.resumen(self.raiz, 30, AHORA + 100)["categorias"]["cotizacion"]["cumple_criterio_numerico"])
        self.decidir_n(0)
        self.tearDown()
        self.setUp()
        self.decidir_n(50, editar=3)  # 47/50 = 94 %
        c = B.resumen(self.raiz, 30, AHORA + 100)["categorias"]["cotizacion"]
        self.assertEqual(c["editados"], 3)
        self.assertFalse(c["cumple_criterio_numerico"])
        self.tearDown()
        self.setUp()
        self.decidir_n(50)
        B.incidente("b-000000000000", "queja", self.raiz, ahora=AHORA + 200)
        self.assertFalse(B.resumen(self.raiz, 30, AHORA + 300)["categorias"]["cotizacion"]["cumple_criterio_numerico"])
        self.tearDown()
        self.setUp()
        self.decidir_n(50, sin_sello=1)
        c = B.resumen(self.raiz, 30, AHORA + 100)["categorias"]["cotizacion"]
        self.assertEqual(c["sin_sello"], 1)
        self.assertFalse(c["cumple_criterio_numerico"])

    def test_rechazos_cuentan_como_no_aprobados_sin_edicion(self):
        self.decidir_n(50, rechazar=3)
        c = B.resumen(self.raiz, 30, AHORA + 100)["categorias"]["cotizacion"]
        self.assertEqual((c["aprobados"], c["rechazados"]), (47, 3))
        self.assertEqual(c["tasa_sin_edicion"], round(47 / 50, 3))
        self.assertFalse(c["cumple_criterio_numerico"])

    def test_solo_cuenta_el_periodo(self):
        self.decidir_n(3)
        self.assertEqual(B.resumen(self.raiz, 30, AHORA + 40 * 86400)["categorias"], {})

    def test_registros_alterados_o_ilegibles_no_cuentan_y_se_informan(self):
        self.decidir_n(2)
        carpeta = os.path.join(self.raiz, "workspace", "archivo", "aprobados")
        archivos = sorted(os.listdir(carpeta))
        ruta = os.path.join(carpeta, archivos[0])
        with open(ruta, encoding="utf-8") as f:
            reg = json.load(f)
        reg["contenido"]["total"] = "0.01"  # alterado después de aprobarlo
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(reg, f)
        with open(os.path.join(carpeta, "roto.json"), "w") as f:
            f.write("{")
        r = B.resumen(self.raiz, 30, AHORA + 100)
        self.assertEqual((r["registros_alterados"], r["registros_ilegibles"]), (1, 1))
        self.assertEqual(r["categorias"]["cotizacion"]["decididos"], 1)

    def test_un_registro_en_la_carpeta_equivocada_se_marca_alterado(self):
        self.cot()
        ruta = D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        destino = os.path.join(self.raiz, "workspace", "archivo", "rechazados")
        os.makedirs(destino)
        os.replace(ruta, os.path.join(destino, os.path.basename(ruta)))
        self.assertEqual(B.resumen(self.raiz, 30, AHORA + 100)["registros_alterados"], 1)


class Falsificaciones(Base):
    """Cada campo que alimenta las métricas debe detectar la manipulación del registro archivado."""

    def registro(self, **editar):
        self.cot()
        ruta = D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        with open(ruta, encoding="utf-8") as f:
            reg = json.load(f)
        return ruta, reg

    def guardar(self, ruta, reg):
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(reg, f)

    def resumen(self):
        return B.resumen(self.raiz, 30, AHORA + 100)

    def test_un_registro_sin_tocar_cuenta(self):
        self.registro()
        r = self.resumen()
        self.assertEqual((r["registros_alterados"], r["categorias"]["cotizacion"]["decididos"]), (0, 1))

    def test_cambiar_sello_categoria_estado_decidido_o_id_se_detecta(self):
        ruta, reg = self.registro()
        cambios = {
            "sello": "sin_sello",
            "categoria": "cobranza_firme",
            "decidido": iso(0.5),
            "id": "b-ffffffffffff",
            "tipo": "cobranza",
            "decidido_por": "otra",
            "motivo": "x",
            "forzado": True,
            "confirmacion": "interactiva",
        }
        for campo, valor in cambios.items():
            falso = {**reg, campo: valor}
            self.guardar(ruta, falso)
            self.assertEqual(self.resumen()["registros_alterados"], 1, campo)
        self.guardar(ruta, reg)
        self.assertEqual(self.resumen()["registros_alterados"], 0)

    def test_marcar_editado_como_intacto_se_detecta(self):
        self.cot()
        d = json.load(open(self.ruta("borradores", "b-0123456789ab"), encoding="utf-8"))
        d["cliente_ref"] = "chat-B2"
        with open(self.ruta("borradores", "b-0123456789ab"), "w") as f:
            json.dump(d, f)
        ruta = D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        reg = json.load(open(ruta, encoding="utf-8"))
        self.assertEqual(reg["sello"], "editado")
        self.guardar(ruta, {**reg, "sello": "intacto"})
        r = self.resumen()
        self.assertEqual(r["registros_alterados"], 1)
        self.assertEqual(r["categorias"], {})

    def test_borrar_un_incidente_del_archivo_no_lo_borra_porque_esta_en_la_cadena(self):
        ruta, reg = self.registro()
        B.incidente("b-0123456789ab", "queja", self.raiz, ahora=AHORA + 5)
        with open(ruta, encoding="utf-8") as f:
            reg = json.load(f)
        self.assertTrue(reg["incidente"])
        reg["incidente"] = None  # lo borran a mano
        self.guardar(ruta, reg)
        self.assertEqual(self.resumen()["categorias"]["cotizacion"]["incidentes"], 1)
        with self.assertRaises(ValueError):  # y no se puede "re-marcar" como si nada hubiera pasado: el evento ya existe
            B.incidente("b-0123456789ab", "otra vez", self.raiz)

    def test_un_registro_fabricado_a_mano_sin_evento_en_la_cadena_no_cuenta(self):
        ruta, reg = self.registro()
        falso = json.loads(json.dumps(reg))
        falso["contenido"]["total"] = "5.00"
        falso["contenido"]["sello"] = B.sellar({k: v for k, v in falso["contenido"].items() if k != "sello"})["sello"]
        falso["hash_contenido"] = B.hash_contenido(falso["contenido"])
        falso["sello"] = B.estado_sello(falso["contenido"])
        falso["sello_registro"] = B._sello_registro(falso)  # lo recalcula quien conoce el formato
        falso["id"] = falso["contenido"]["id"] = "b-aaaaaaaaaaaa"  # otro borrador que nunca existió
        falso["contenido"] = B.sellar({k: v for k, v in falso["contenido"].items() if k != "sello"})
        falso["sello"] = B.estado_sello(falso["contenido"])
        falso["hash_contenido"] = B.hash_contenido(falso["contenido"])
        falso["categoria"] = "cotizacion"
        falso["sello_registro"] = B._sello_registro(falso)
        destino = os.path.join(os.path.dirname(ruta), B._nombre_esperado(falso))
        self.guardar(destino, falso)
        r = self.resumen()
        self.assertEqual(r["registros_sin_evento_en_cadena"], 1)
        self.assertEqual(r["categorias"]["cotizacion"]["decididos"], 1)  # solo el verdadero
        self.assertFalse(r["integro"])

    def test_cambiar_la_fecha_de_decision_aunque_se_recalcule_el_sello_no_sirve(self):
        ruta, reg = self.registro()
        reg["decidido"] = iso(0.01)  # dentro de la ventana y no futura: solo la cadena puede delatarlo
        reg["sello_registro"] = B._sello_registro(reg)
        nuevo = os.path.join(os.path.dirname(ruta), B._nombre_esperado(reg))  # incluso con el nombre corregido
        os.remove(ruta)
        self.guardar(nuevo, reg)
        r = self.resumen()
        self.assertEqual(r["registros_sin_evento_en_cadena"], 1)  # la cadena guarda la fecha original
        self.assertEqual(r["categorias"], {})

    def test_borrar_un_registro_se_detecta_porque_su_evento_sigue_en_la_cadena(self):
        for i in range(50):
            id_ = f"b-{i:012x}"
            self.cot(id_)
            D(id_, "aprobado", self.raiz, ahora=AHORA + i)
        self.assertTrue(self.resumen()["categorias"]["cotizacion"]["cumple_criterio_numerico"])
        carpeta = os.path.join(self.raiz, "workspace", "archivo", "aprobados")
        os.remove(os.path.join(carpeta, sorted(os.listdir(carpeta))[0]))
        r = self.resumen()
        self.assertEqual(r["eventos_sin_archivo"], 1)
        self.assertFalse(any(c["cumple_criterio_numerico"] for c in r["categorias"].values()))

    def test_una_copia_con_otro_nombre_no_cuenta_dos_veces(self):
        ruta, reg = self.registro()
        copia = os.path.join(os.path.dirname(ruta), "b-0123456789ab.20270101T000000Z.json")
        self.guardar(copia, reg)  # contenido idéntico y válido, nombre que no le corresponde
        r = self.resumen()
        self.assertEqual((r["registros_alterados"], r["categorias"]["cotizacion"]["decididos"]), (1, 1))

    def test_un_solo_registro_alterado_impide_el_cumple_de_todo(self):
        for i in range(51):
            id_ = f"b-{i:012x}"
            self.cot(id_)
            D(id_, "aprobado", self.raiz, ahora=AHORA + i)
        self.assertTrue(self.resumen()["categorias"]["cotizacion"]["cumple_criterio_numerico"])
        carpeta = os.path.join(self.raiz, "workspace", "archivo", "aprobados")
        with open(os.path.join(carpeta, "roto.json"), "w") as f:
            f.write("{")
        self.assertFalse(self.resumen()["categorias"]["cotizacion"]["cumple_criterio_numerico"])

    def test_la_tasa_se_compara_en_enteros_sin_redondear(self):
        self.assertFalse(B.tasa_cumple(189, 199))  # 94,97 %: redondeado a 3 decimales daría 0,95
        self.assertTrue(B.tasa_cumple(190, 200))
        self.assertFalse(B.tasa_cumple(0, 0))

    def test_cadena_rota_impide_el_cumple(self):
        for i in range(50):
            id_ = f"b-{i:012x}"
            self.cot(id_)
            D(id_, "aprobado", self.raiz, ahora=AHORA + i)
        self.assertTrue(self.resumen()["categorias"]["cotizacion"]["cumple_criterio_numerico"])
        ruta = os.path.join(self.raiz, "estado", "bandeja", "decisiones.jsonl")
        lineas = open(ruta, encoding="ascii").read().splitlines()
        lineas[3] = lineas[3].replace('"evento": "aprobado"', '"evento": "rechazado"')
        with open(ruta, "w", encoding="ascii") as f:
            f.write("\n".join(lineas) + "\n")
        r = self.resumen()
        self.assertFalse(r["cadena_ok"])
        self.assertFalse(any(c["cumple_criterio_numerico"] for c in r["categorias"].values()))

    def test_fecha_decidida_en_el_futuro_se_marca_alterada(self):
        ruta, reg = self.registro()
        reg["decidido"] = iso(-400)
        reg["sello_registro"] = B._sello_registro(reg)  # incluso con el sello recalculado
        self.guardar(ruta, reg)
        r = self.resumen()
        self.assertEqual(r["registros_alterados"], 1)
        self.assertEqual(r["categorias"], {})

    def test_truncado_impide_el_cumple_y_se_informa(self):
        for i in range(50):
            id_ = f"b-{i:012x}"
            self.cot(id_)
            D(id_, "aprobado", self.raiz, ahora=AHORA + i)
        with mock.patch.object(B, "MAX_REGISTROS", 10):
            r = self.resumen()
        self.assertTrue(r["truncado"])
        self.assertFalse(any(c["cumple_criterio_numerico"] for c in r["categorias"].values()))

    def test_rechazado_sin_sello_cuenta_como_sin_sello(self):
        self.cot(sellar=False)
        D("b-0123456789ab", "rechazado", self.raiz, motivo="x", ahora=AHORA)
        self.assertEqual(self.resumen()["categorias"]["cotizacion"]["sin_sello"], 1)

    def test_dias_fuera_de_rango_se_rechazan(self):
        for malo in (0, -1, 10**9, "30", None):
            with self.assertRaises(ValueError):
                B.resumen(self.raiz, malo, AHORA)

    def test_cola_enorme_se_avisa_y_no_se_omite_en_silencio(self):
        self.cot()
        with mock.patch.object(B, "MAX_ARCHIVOS", 0):
            items = B.pendientes(self.raiz, AHORA)
        self.assertTrue(any("AVISO" in i["resumen"] for i in items))

    def test_una_carpeta_que_resuelve_fuera_del_proyecto_se_rechaza(self):
        fuera = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(fuera, ignore_errors=True))
        os.rmdir(os.path.join(self.raiz, "workspace", "borradores"))
        try:
            os.symlink(fuera, os.path.join(self.raiz, "workspace", "borradores"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("este sistema no permite crear enlaces simbólicos")
        items = B.pendientes(self.raiz, AHORA)
        self.assertTrue(any(i["ilegible"] for i in items))
        with self.assertRaises(ValueError):
            B.ver("b-0123456789ab", self.raiz)


class JunctionWindows(Base):
    def test_una_junction_a_otra_carpeta_se_rechaza(self):
        if os.name != "nt":
            self.skipTest("las junctions son de Windows")
        import shutil
        import subprocess

        fuera = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(fuera, ignore_errors=True))
        with open(os.path.join(fuera, "b-0123456789ab.json"), "w") as f:
            json.dump({"id": "b-0123456789ab", "creado": iso(1)}, f)
        os.rmdir(os.path.join(self.raiz, "workspace", "borradores"))
        enlace = os.path.join(self.raiz, "workspace", "borradores")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", enlace, fuera], capture_output=True)
        if r.returncode != 0:
            self.skipTest("no se pudo crear la junction")
        self.addCleanup(lambda: subprocess.run(["cmd", "/c", "rmdir", enlace], capture_output=True))
        self.assertTrue(any(i["ilegible"] for i in B.pendientes(self.raiz, AHORA)))
        with self.assertRaises(ValueError):
            B.ver("b-0123456789ab", self.raiz)
        with self.assertRaises(ValueError):
            D("b-0123456789ab", "rechazado", self.raiz, motivo="x", ahora=AHORA)

    def test_una_junction_en_archivo_no_deja_crear_registros_fuera(self):
        if os.name != "nt":
            self.skipTest("las junctions son de Windows")
        import shutil
        import subprocess

        fuera = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(fuera, ignore_errors=True))
        enlace = os.path.join(self.raiz, "workspace", "archivo")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", enlace, fuera], capture_output=True)
        if r.returncode != 0:
            self.skipTest("no se pudo crear la junction")
        self.addCleanup(lambda: subprocess.run(["cmd", "/c", "rmdir", enlace], capture_output=True))
        self.cot()
        with self.assertRaises(ValueError):
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertEqual(os.listdir(fuera), [])  # nada se creó fuera del proyecto
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))


class RevisionEnBandeja(Base):
    """La revisión previa determinista y la lista de no contacto frenan la APROBACIÓN (solo pueden frenar)."""

    def editar(self, carpeta, id_, **cambios):
        ruta = self.ruta(carpeta, id_)
        with open(ruta, encoding="utf-8") as f:
            d = json.load(f)
        d.update(cambios)
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_una_cobranza_correcta_se_aprueba_y_queda_con_su_revision(self):
        self.cob()
        with open(D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA), encoding="utf-8") as f:
            reg = json.load(f)
        self.assertEqual((reg["estado"], reg["avisos"]), ("aprobado", []))

    def test_destinatario_en_la_lista_impide_aprobar_pero_no_rechazar(self):
        self.cob()
        self.lista("@b.com\n")
        with self.assertRaises(ValueError) as cm:
            D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)
        self.assertIn("no contacto", str(cm.exception))
        self.assertTrue(os.path.exists(self.ruta("borradores_cobranza", "c-f001-amable")))
        D("c-f001-amable", "rechazado", self.raiz, motivo="en la lista", ahora=AHORA)

    def test_forzar_no_salta_un_bloqueo(self):
        self.cob(dias=9)
        self.editar("borradores_cobranza", "c-f001-amable", mensaje="Pague en https://pagos.example/ya")
        with self.assertRaises(ValueError):
            D("c-f001-amable", "aprobado", self.raiz, forzar=True, ahora=AHORA)

    def test_variables_sin_rellenar_y_cifras_manipuladas_bloquean(self):
        self.cob()
        self.editar("borradores_cobranza", "c-f001-amable", mensaje="Hola {nombre}, debe XXX")
        with self.assertRaises(ValueError):
            D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)
        self.cot()
        self.editar("borradores", "b-0123456789ab", total="1.00")
        with self.assertRaises(ValueError) as cm:
            D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)
        self.assertIn("cifras", str(cm.exception))

    def test_sin_la_lista_de_no_contacto_la_cobranza_no_se_aprueba_pero_la_cotizacion_si(self):
        self.cob()
        self.cot()
        os.remove(self.lista_nc)
        with self.assertRaises(ValueError) as cm:
            D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)
        self.assertIn("no contacto", str(cm.exception))
        D("b-0123456789ab", "aprobado", self.raiz, ahora=AHORA)

    def test_los_avisos_no_bloquean_y_quedan_en_el_registro(self):
        self.cob()
        self.editar(
            "borradores_cobranza",
            "c-f001-amable",
            mensaje="Pague por transferencia al IBAN indicado. Gracias",
        )
        with open(D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA), encoding="utf-8") as f:
            reg = json.load(f)
        self.assertTrue(any("transferencia" in a for a in reg["avisos"]))
        self.assertEqual(reg["sello"], "editado")

    def test_el_registro_con_avisos_alterados_se_detecta(self):
        self.cob()
        self.editar("borradores_cobranza", "c-f001-amable", mensaje="Pague por transferencia")
        ruta = D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)
        with open(ruta, encoding="utf-8") as f:
            reg = json.load(f)
        reg["avisos"] = []  # esconden que se aprobó con avisos
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(reg, f)
        self.assertEqual(B.resumen(self.raiz, 30, AHORA + 100)["registros_alterados"], 1)

    def test_pendientes_informa_bloqueos_y_avisos(self):
        self.cob("c-f001-amable")
        self.cob("c-f002-firme")
        self.cot()
        self.editar("borradores_cobranza", "c-f002-firme", mensaje="Es su último aviso")
        self.lista("ana@a.com\n")
        por_id = {i["id"]: i for i in B.pendientes(self.raiz, AHORA, ruta_lista_persona=self.lista_nc)}
        self.assertEqual((por_id["c-f001-amable"]["bloqueos"], por_id["c-f001-amable"]["avisos"]), (0, 0))
        self.assertEqual((por_id["c-f002-firme"]["bloqueos"], por_id["c-f002-firme"]["avisos"]), (0, 1))
        self.assertEqual(por_id["b-0123456789ab"]["bloqueos"], 0)

    def test_pendientes_sin_lista_marca_la_cobranza_como_bloqueada(self):
        self.cob()
        os.remove(self.lista_nc)
        item = B.pendientes(self.raiz, AHORA, ruta_lista_persona=self.lista_nc)[0]
        self.assertGreaterEqual(item["bloqueos"], 1)


class CerrarRegistra(Base):
    def test_cerrar_deja_rastro_en_la_cadena_y_rechaza_archivos_enormes(self):
        self.esc()
        B.cerrar("e-0123456789ab", self.raiz)
        ruta = os.path.join(self.raiz, "estado", "bandeja", "decisiones.jsonl")
        self.assertIn('"cerrado"', open(ruta, encoding="ascii").read())
        with open(self.ruta("escalados", "e-aaaaaaaaaaaa"), "w") as f:
            f.write('{"id":"x","m":"' + "a" * 300_000 + '"}')
        with self.assertRaises(ValueError):
            B.cerrar("e-aaaaaaaaaaaa", self.raiz)
        with open(self.ruta("escalados", "e-bbbbbbbbbbbb"), "w") as f:
            f.write("[" * 5000)  # anidamiento extremo
        with self.assertRaises(ValueError):
            B.cerrar("e-bbbbbbbbbbbb", self.raiz)


class LineaDeComandos(Base):
    def correr(self, *args, entrada=""):
        with mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()) as err:
            rc = B.main(
                ["bandeja.py", *args],
                io.StringIO(entrada),
                self.raiz,
                ruta_lista_persona=self.lista_nc,
            )
        return rc, out.getvalue(), err.getvalue()

    def test_aprobar_pide_escribir_el_id_y_sin_confirmar_no_hace_nada(self):
        self.cot(dias=0)
        rc, out, err = self.correr("aprobar", "b-0123456789ab", entrada="no\n")
        self.assertEqual(rc, 1)
        self.assertIn("cancelado", err)
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))
        rc, out, err = self.correr("aprobar", "b-0123456789ab", entrada="b-0123456789ab\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("NO envía nada", out)
        self.assertFalse(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_sin_entrada_no_se_aprueba(self):
        self.cot(dias=0)
        self.assertEqual(self.correr("aprobar", "b-0123456789ab", entrada="")[0], 1)
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_listar_avisa_de_las_bajas_automaticas_y_nocontacto_las_muestra(self):
        import nocontacto

        auto = os.path.join(self.raiz, "estado", "agente", "no_contactar_auto.json")
        self.assertNotIn("automática", self.correr("listar")[1])  # sin bajas, ni una línea de más
        nocontacto.agregar_auto(auto, "ana@x.com", "baja por correo", 1_800_000_000)
        nocontacto.agregar_auto(auto, "luis@y.com", "baja por correo", 1_800_000_100)
        rc, out, _ = self.correr("listar")
        self.assertIn("2 baja(s) automática(s)", out)
        rc, out, err = self.correr("nocontacto")
        self.assertEqual(rc, 0, err)
        for esperado in ("ana@x.com", "luis@y.com", "baja por correo"):
            self.assertIn(esperado, out)

    def test_listar_avisa_si_hay_bajas_sin_registrar_y_la_cobranza_queda_bloqueada(self):
        import nocontacto

        self.cob()
        auto = os.path.join(self.raiz, "estado", "agente", "no_contactar_auto.json")
        nocontacto.marcar_bloqueo(auto, 2, 1_800_000_000)
        rc, out, err = self.correr("listar")
        self.assertIn("bajas pedidas por correo que NO se pudieron registrar", out)
        self.assertIn("BLOQUEADO", out)  # la lista no está disponible: la cobranza no se puede aprobar
        with self.assertRaises(ValueError):
            D("c-f001-amable", "aprobado", self.raiz, ahora=AHORA)

    def test_avisa_cuando_la_lista_automatica_esta_casi_llena(self):
        import nocontacto

        auto = os.path.join(self.raiz, "estado", "agente", "no_contactar_auto.json")
        with mock.patch.object(nocontacto, "MAX_ENTRADAS", 10):
            for i in range(7):
                nocontacto.agregar_auto(auto, f"u{i}@x.com", "baja por correo", 1_800_000_000)
            self.assertNotIn("casi llena", self.correr("listar")[1])
            nocontacto.agregar_auto(auto, "u7@x.com", "baja por correo", 1_800_000_000)
            self.assertIn("casi llena", self.correr("listar")[1])
            self.assertIn("casi llena", self.correr("nocontacto")[1])

    def test_nocontacto_con_la_lista_automatica_corrupta_da_error_controlado(self):
        auto = os.path.join(self.raiz, "estado", "agente", "no_contactar_auto.json")
        os.makedirs(os.path.dirname(auto))
        with open(auto, "w") as f:
            f.write("{roto")
        self.assertEqual(self.correr("nocontacto")[0], 1)
        self.assertIn("ATENCIÓN", self.correr("listar")[1])

    def test_aprobar_con_bloqueos_no_pregunta_ni_aprueba(self):
        self.cob()
        self.lista("a@b.com\n")
        rc, out, err = self.correr("aprobar", "c-f001-amable", entrada="c-f001-amable\n")
        self.assertEqual(rc, 1)
        self.assertIn("no contacto", out + err)
        self.assertNotIn("Para aprobar escriba", out)  # ni siquiera se pide el id
        self.assertTrue(os.path.exists(self.ruta("borradores_cobranza", "c-f001-amable")))

    def test_aprobar_con_avisos_los_muestra_antes_de_pedir_el_id(self):
        self.cob()
        with open(self.ruta("borradores_cobranza", "c-f001-amable"), encoding="utf-8") as f:
            d = json.load(f)
        d["mensaje"] = "Pague por transferencia"
        with open(self.ruta("borradores_cobranza", "c-f001-amable"), "w", encoding="utf-8") as f:
            json.dump(d, f)
        rc, out, err = self.correr("aprobar", "c-f001-amable", entrada="c-f001-amable\n")
        self.assertEqual(rc, 0, err)
        self.assertLess(out.index("AVISO"), out.index("Para aprobar escriba"))

    def test_ver_y_listar_muestran_la_revision(self):
        self.cob()
        self.lista("a@b.com\n")
        self.assertIn("BLOQUEO", self.correr("ver", "c-f001-amable")[1])
        self.assertIn("BLOQUEADO", self.correr("listar")[1])

    def test_aprobar_con_entrada_redirigida_se_rechaza(self):
        self.cot(dias=0)
        with mock.patch("sys.stdin", io.StringIO("b-0123456789ab\n")), mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as err:
            rc = B.main(["bandeja.py", "aprobar", "b-0123456789ab"], None, self.raiz)
        self.assertEqual(rc, 1)
        self.assertIn("interactiva", err.getvalue())
        self.assertTrue(os.path.exists(self.ruta("borradores", "b-0123456789ab")))

    def test_la_confirmacion_inyectada_queda_registrada_como_tal(self):
        self.cot(dias=0)
        self.correr("aprobar", "b-0123456789ab", entrada="b-0123456789ab\n")
        carpeta = os.path.join(self.raiz, "workspace", "archivo", "aprobados")
        reg = json.load(open(os.path.join(carpeta, os.listdir(carpeta)[0]), encoding="utf-8"))
        self.assertEqual(reg["confirmacion"], "inyectada")

    def test_no_existe_una_opcion_para_aprobar_sin_confirmar(self):
        for flag in ("--si", "--yes", "-y", "--sin-confirmar"):
            self.assertEqual(self.correr("aprobar", "b-0123456789ab", flag)[0], 2, flag)

    def test_listar_ver_y_resumen_funcionan(self):
        self.cot(dias=0)
        rc, out, _ = self.correr("listar")
        self.assertEqual(rc, 0)
        self.assertIn("1 pendiente(s)", out)
        rc, out, _ = self.correr("ver", "b-0123456789ab")
        self.assertEqual(rc, 0)
        self.assertIn("sello: intacto", out)
        self.assertEqual(self.correr("resumen")[0], 0)

    def test_una_consola_cp1252_no_tumba_el_comando_por_un_caracter_no_codificable(self):
        crudo = io.BytesIO()
        consola = io.TextIOWrapper(crudo, encoding="cp1252", errors="strict")
        with mock.patch("sys.stdout", consola):
            with self.assertRaises(UnicodeEncodeError):
                print("≥")
                consola.flush()
        consola = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with mock.patch("sys.stdout", consola), mock.patch("sys.stderr", io.StringIO()):
            B._salida_segura()
            print("≥ 50 casos")
            consola.flush()  # con errors="replace" no lanza

    def test_errores_controlados_devuelven_1_y_uso_incorrecto_2(self):
        self.assertEqual(self.correr("ver", "../x")[0], 1)
        self.assertEqual(self.correr("cerrar", "e-0123456789ab")[0], 1)
        self.assertEqual(self.correr()[0], 2)
        self.assertEqual(self.correr("rechazar", "b-0123456789ab")[0], 2)  # falta --motivo


if __name__ == "__main__":
    unittest.main()
