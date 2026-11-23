"""Pruebas de la lista de no contacto (agente/nocontacto.py). Todo en directorios temporales."""

import json
import os
import sys
import tempfile
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(AQUI, "..", "sandbox"))
import nocontacto as N  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.persona = os.path.join(self.t.name, "no_contactar.txt")
        self.auto = os.path.join(self.t.name, "estado", "no_contactar_auto.json")

    def tearDown(self):
        self.t.cleanup()

    def escribir(self, texto, ruta=None, modo="w"):
        with open(ruta or self.persona, modo, encoding="utf-8", newline="") as f:
            f.write(texto)

    def lista(self):
        return N.cargar(self.persona, self.auto)


class ArchivoDeLaPersona(Base):
    def test_archivo_ausente_falla_cerrado(self):
        with self.assertRaises(N.NoContactoNoConfigurado):
            self.lista()

    def test_archivo_solo_con_comentarios_es_una_lista_vacia_valida(self):
        self.escribir("# vacía a propósito\n\n   \n")
        self.assertFalse(self.lista().bloquea("ana@empresa.com"))

    def test_correo_exacto_y_mayusculas(self):
        self.escribir("Ana@Empresa.COM\n")
        L = self.lista()
        self.assertTrue(L.bloquea("ana@empresa.com"))
        self.assertTrue(L.bloquea(" ANA@empresa.com "))
        self.assertFalse(L.bloquea("otra@empresa.com"))

    def test_dominio_bloquea_subdominios_pero_no_dominios_parecidos(self):
        self.escribir("@empresa.com\n")
        L = self.lista()
        self.assertTrue(L.bloquea("a@empresa.com"))
        self.assertTrue(L.bloquea("a@sub.empresa.com"))
        self.assertFalse(L.bloquea("a@miempresa.com"))  # sufijo de texto, no de dominio
        self.assertFalse(L.bloquea("a@empresa.com.mx"))

    def test_la_etiqueta_mas_se_ignora_en_los_dos_lados(self):
        self.escribir("ana@gmail.com\nluis+ventas@gmail.com\n")
        L = self.lista()
        self.assertTrue(L.bloquea("ana+cualquiera@gmail.com"))
        self.assertTrue(L.bloquea("luis@gmail.com"))

    def test_formas_unicode_se_normalizan(self):
        self.escribir("ana@empresa.com\n")
        self.assertTrue(self.lista().bloquea("ana＠empresa.com"))  # arroba de ancho completo
        self.assertTrue(self.lista().bloquea("ａna@empresa.com"))  # 'a' de ancho completo

    def test_un_destinatario_con_formato_invalido_se_considera_bloqueado(self):
        self.escribir("# nada\n")
        L = self.lista()
        for malo in (
            "",
            "sin-arroba",
            "a@b",
            "a b@c.com",
            None,
            5,
            "a@@b.com",
            "a@b.com\n.evil",
            "x" * 400 + "@a.com",
        ):
            self.assertTrue(L.bloquea(malo), repr(malo))

    def test_linea_invalida_rechaza_todo_e_indica_la_linea(self):
        for malo in (
            "ana@",
            "@",
            "@dominio",
            "a b@c.com",
            "ana@empresa.com extra",
            "*@empresa.com",
            "ana@@x.com",
            "@.com",
            "é@x.com",
        ):
            self.escribir(f"# c\nbueno@x.com\n{malo}\n")
            with self.assertRaises(N.NoContactoInvalido, msg=malo) as cm:
                self.lista()
            self.assertIn("línea 3", str(cm.exception), malo)

    def test_limites_de_tamano_y_de_lineas(self):
        self.escribir("a@x.com\n" * 20_001)
        with self.assertRaises(N.NoContactoInvalido):
            self.lista()
        self.escribir("# " + "a" * 1_100_000)
        with self.assertRaises(N.NoContactoInvalido):
            self.lista()

    def test_bytes_que_no_son_utf8_fallan_cerrado(self):
        with open(self.persona, "wb") as f:
            f.write(b"ana@x.com\n\xff\xfe\n")
        with self.assertRaises(N.NoContactoInvalido):
            self.lista()

    def test_bom_y_finales_de_linea_de_windows(self):
        with open(self.persona, "wb") as f:
            f.write(b"\xef\xbb\xbf# nota\r\nana@x.com\r\n")
        self.assertTrue(self.lista().bloquea("ana@x.com"))

    def test_una_carpeta_o_un_enlace_en_lugar_del_archivo_falla_cerrado(self):
        os.makedirs(self.persona)
        with self.assertRaises(N.NoContactoInvalido):
            self.lista()


class BloqueoPorBajasSinRegistrar(Base):
    """Si una baja pedida por correo no se pudo escribir, esa persona NO está en la lista: mientras tanto TODO contacto debe frenarse."""

    def setUp(self):
        super().setUp()
        self.escribir("# vacía\n")

    def test_el_marcador_hace_fallar_cerrado_a_cargar_hasta_que_una_persona_lo_borre(self):
        self.assertFalse(N.bloqueo_activo(self.auto))
        N.marcar_bloqueo(self.auto, 3, 1_800_000_000)
        self.assertTrue(N.bloqueo_activo(self.auto))
        with self.assertRaises(N.NoContactoInvalido) as cm:
            self.lista()
        self.assertIn("bajas sin registrar", str(cm.exception))
        os.remove(self.auto + ".bloqueo")
        self.assertFalse(self.lista().bloquea("ana@x.com"))

    def test_el_marcador_no_se_pisa_y_acumula_la_cuenta(self):
        N.marcar_bloqueo(self.auto, 2, 1)
        N.marcar_bloqueo(self.auto, 3, 2)
        datos = json.load(open(self.auto + ".bloqueo", encoding="utf-8"))
        self.assertEqual((datos["bajas_sin_registrar"], datos["desde"]), (5, 1))

    def test_un_marcador_corrupto_tambien_bloquea(self):
        os.makedirs(os.path.dirname(self.auto))
        self.escribir("{roto", self.auto + ".bloqueo")
        with self.assertRaises(N.NoContactoInvalido):
            self.lista()


class FormasDelMismoBuzon(Base):
    def test_gmail_ignora_puntos_y_googlemail_es_gmail(self):
        self.escribir("ana.lopez@gmail.com\n")
        L = self.lista()
        for igual in (
            "analopez@gmail.com",
            "a.n.a.l.o.p.e.z@gmail.com",
            "ana.lopez+x@googlemail.com",
            "ANALOPEZ@GoogleMail.com",
        ):
            self.assertTrue(L.bloquea(igual), igual)
        self.assertFalse(L.bloquea("ana.lopez@empresa.com"))
        self.assertFalse(L.bloquea("otra@gmail.com"))

    def test_otros_dominios_conservan_los_puntos(self):
        self.escribir("ana.lopez@empresa.com\n")
        L = self.lista()
        self.assertTrue(L.bloquea("ana.lopez@empresa.com"))
        self.assertFalse(L.bloquea("analopez@empresa.com"))

    def test_un_dominio_googlemail_en_la_lista_bloquea_gmail(self):
        self.escribir("@googlemail.com\n")
        self.assertTrue(self.lista().bloquea("x@gmail.com"))

    def test_un_archivo_de_la_persona_en_utf16_da_un_mensaje_claro(self):
        with open(self.persona, "wb") as f:
            f.write("# nota\r\nana@x.com\r\n".encode("utf-16"))  # lo que produce `echo x > archivo` en PowerShell 5
        with self.assertRaises(N.NoContactoInvalido) as cm:
            self.lista()
        self.assertIn("UTF-16", str(cm.exception))


class ArchivoAutomatico(Base):
    def setUp(self):
        super().setUp()
        self.escribir("# vacía\n")

    def test_ausente_es_vacio_y_agregar_lo_crea(self):
        self.assertFalse(self.lista().bloquea("ana@x.com"))
        N.agregar_auto(self.auto, "Ana+t@X.com", "baja", 1_800_000_000)
        self.assertTrue(self.lista().bloquea("ana@x.com"))

    def test_agregar_es_idempotente_y_solo_anade(self):
        N.agregar_auto(self.auto, "ana@x.com", "baja", 1)
        N.agregar_auto(self.auto, "ana@x.com", "baja", 2)
        N.agregar_auto(self.auto, "luis@x.com", "baja", 3)
        datos = json.load(open(self.auto, encoding="utf-8"))
        self.assertEqual(sorted(datos["entradas"]), ["ana@x.com", "luis@x.com"])
        self.assertEqual(datos["entradas"]["ana@x.com"]["fecha"], 1)  # no se pisa la primera

    def test_archivo_corrupto_falla_cerrado_y_no_se_sobrescribe(self):
        os.makedirs(os.path.dirname(self.auto))
        self.escribir("{no json", self.auto)
        with self.assertRaises(N.NoContactoInvalido):
            self.lista()
        with self.assertRaises(N.NoContactoInvalido):
            N.agregar_auto(self.auto, "ana@x.com", "baja", 1)
        self.assertEqual(open(self.auto, encoding="utf-8").read(), "{no json")

    def test_estructura_inesperada_falla_cerrado(self):
        os.makedirs(os.path.dirname(self.auto))
        for malo in (
            "[]",
            '{"entradas": []}',
            '{"entradas": {"no-es-correo": {}}}',
            '{"version": 2, "entradas": {}}',
            '{"entradas": {"a@x.com": 5}}',
            "5",
        ):
            self.escribir(malo, self.auto)
            with self.assertRaises(N.NoContactoInvalido, msg=malo):
                self.lista()

    def test_direccion_invalida_no_se_agrega(self):
        for malo in ("", "sin-arroba", "a@b", None, "a b@c.com"):
            with self.assertRaises(ValueError, msg=repr(malo)):
                N.agregar_auto(self.auto, malo, "baja", 1)
        self.assertFalse(os.path.exists(self.auto))

    def test_tope_de_entradas(self):
        with unittest_mock_patch(N, "MAX_ENTRADAS", 2):
            N.agregar_auto(self.auto, "a@x.com", "baja", 1)
            N.agregar_auto(self.auto, "b@x.com", "baja", 1)
            with self.assertRaises(N.NoContactoInvalido):
                N.agregar_auto(self.auto, "c@x.com", "baja", 1)
            N.agregar_auto(self.auto, "a@x.com", "baja", 9)  # una ya existente no cuenta como nueva

    def test_una_lista_automatica_llena_sigue_siendo_legible(self):
        """Con 20.000 entradas el archivo supera 1 MB: no debe volverse ilegible (eso denegaría TODO el sistema)."""
        entradas = {
            f"usuario{i:05d}.con.un.nombre.bastante.largo@dominio-de-ejemplo-{i % 50}.com": {
                "motivo": "baja por correo",
                "fecha": 1_800_000_000.5,
            }
            for i in range(N.MAX_ENTRADAS)
        }
        os.makedirs(os.path.dirname(self.auto))
        with open(self.auto, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entradas": entradas}, f, indent=1)  # formato antiguo, con sangría
        self.assertGreater(os.path.getsize(self.auto), 1_000_000)
        L = self.lista()
        self.assertTrue(L.bloquea("usuario00007.con.un.nombre.bastante.largo@dominio-de-ejemplo-7.com"))
        with self.assertRaises(N.NoContactoInvalido):  # llena: no admite más, pero SIGUE leyéndose
            N.agregar_auto(self.auto, "nuevo@x.com", "baja", 1)

    def test_el_archivo_nuevo_se_escribe_compacto(self):
        N.agregar_auto(self.auto, "a@x.com", "baja", 1)
        self.assertNotIn("\n ", open(self.auto, encoding="utf-8").read())

    def test_el_motivo_se_limpia(self):
        N.agregar_auto(self.auto, "a@x.com", "baja\x1b[31m\n" + "x" * 500, 1)
        m = json.load(open(self.auto, encoding="utf-8"))["entradas"]["a@x.com"]["motivo"]
        self.assertNotIn("\x1b", m)
        self.assertLessEqual(len(m), 60)

    def test_dos_hilos_agregando_no_pierden_entradas(self):
        import threading

        correos = [f"u{i}@x.com" for i in range(12)]
        hilos = [threading.Thread(target=N.agregar_auto, args=(self.auto, c, "baja", 1)) for c in correos]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(sorted(json.load(open(self.auto, encoding="utf-8"))["entradas"]), sorted(correos))


def unittest_mock_patch(obj, nombre, valor):
    from unittest import mock

    return mock.patch.object(obj, nombre, valor)


if __name__ == "__main__":
    unittest.main()
