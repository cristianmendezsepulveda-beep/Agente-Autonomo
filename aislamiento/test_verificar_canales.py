import unittest

import verificar_canales as v

TOOLSETS = ["web", "browser", "terminal", "file", "code_execution", "vision", "video", "image_gen", "video_gen", "x_search", "tts",
            "stt", "skills", "todo", "kanban", "memory", "context_engine", "session_search", "connections", "clarify", "delegation",
            "cronjob", "computer_use"]


def salida(plataforma, habilitados=("clarify",), plugin_habilitado=False, mcp=("agente-local  all tools enabled",)):
    lineas = [f"Built-in toolsets ({plataforma}):"]
    for t in TOOLSETS:
        lineas.append(f"  {'✓ enabled' if t in habilitados else '✗ disabled'}  {t}  icono Descripción")
    lineas += ["", f"Plugin toolsets ({plataforma}):", f"  {'✓ enabled' if plugin_habilitado else '✗ disabled'}  a2a  🔌 A2A", "", "MCP servers:"]
    lineas += ["  " + m for m in mcp]
    return "\n".join(lineas) + "\n"


PLATAFORMAS = "cli, telegram, whatsapp"
ERROR_ZZZ = f"✗ Unknown platform 'zzz'. Valid: {PLATAFORMAS}\n"


def runner(overrides=None, cli=("clarify", "file", "terminal", "todo")):
    overrides = overrides or {}

    def correr(perfil, plataforma):
        if plataforma == "zzz":
            return overrides.get("zzz", (0, ERROR_ZZZ))
        if plataforma in overrides:
            return overrides[plataforma]
        return 0, salida(plataforma, cli if plataforma == "cli" else ("clarify",))
    return correr


class Parseo(unittest.TestCase):
    def test_parsea_habilitados_y_mcp(self):
        h, m, ok = v.parsear(salida("telegram"))
        self.assertEqual((h, m, ok), ({"clarify"}, {"agente-local"}, True))

    def test_plugin_habilitado_cuenta(self):
        self.assertIn("a2a", v.parsear(salida("telegram", plugin_habilitado=True))[0])

    def test_formato_inesperado_es_no_ok(self):
        for malo in ("", None, "hola", "Built-in toolsets (x):\n", "MCP servers:\n  a  b\n"):
            self.assertFalse(v.parsear(malo)[2], repr(malo))

    def test_linea_mcp_rara_es_no_ok(self):
        self.assertFalse(v.parsear(salida("x", mcp=("???",)))[2])

    def test_marcador_o_nombre_raro_en_un_toolset_no_se_ignora(self):
        # hallazgos 1 y 2 de Hermes: antes estas líneas se ignoraban y el resultado era CORRECTO
        base = salida("telegram")
        for extra in ("  ✔ enabled  terminal", "  ~ partial  terminal", "  ✓ on  terminal", "  ✓ enabled  clarify.extra  x",
                      "  ✓ enabled  mcp:foo  x", "basura suelta"):
            malo = base.replace("\nMCP servers:", "\n" + extra + "\nMCP servers:")
            self.assertFalse(v.parsear(malo)[2], extra)
            self.assertFalse(v.evaluar("telegram", malo)[0], extra)
        # nombre + descripción con espacio: se lee como un toolset llamado "term" (no permitido) y falla igual
        malo = base.replace("\nMCP servers:", "\n  ✓ enabled  term inal\nMCP servers:")
        self.assertFalse(v.evaluar("telegram", malo)[0])

    def test_faltan_toolsets_conocidos_es_formato_inesperado(self):
        sin_terminal = "\n".join(l for l in salida("telegram").splitlines() if " terminal " not in l)
        self.assertFalse(v.evaluar("telegram", sin_terminal)[0])

    def test_mcp_con_estado_distinto_falla(self):
        # hallazgo 3: antes solo se leía el nombre del servidor
        for estado in ("✗ disabled", "3/9 tools", "all tools disabled"):
            ok, d = v.evaluar("telegram", salida("telegram", mcp=("agente-local  " + estado,)))
            self.assertFalse(ok, estado)

    def test_crlf_y_toolset_repetido(self):
        crlf = salida("telegram").replace("\n", "\r\n")
        self.assertTrue(v.evaluar("telegram", crlf)[0])
        # un toolset que aparece deshabilitado y luego habilitado (o al revés) cuenta como habilitado
        doble = salida("telegram").replace("\nMCP servers:", "\n  ✓ enabled  terminal  x\nMCP servers:")
        self.assertFalse(v.evaluar("telegram", doble)[0])

    def test_nombres_de_plataforma_con_guion_o_mayuscula(self):
        self.assertEqual(v.descubrir_plataformas("Valid: cli, api-server, WhatsApp2"), ["cli", "api-server", "WhatsApp2"])
        self.assertIsNone(v.descubrir_plataformas("Valid: cli, -x"))

    def test_perfil_que_empieza_por_guion_se_rechaza(self):
        self.assertEqual(v.main(["--perfil=--help"]), 2)  # con "=" argparse lo entrega tal cual y lo para nuestra regex

    def test_regresion_discord_y_discord_admin(self):
        # los dos toolsets propios de discord que el verificador real encontró habilitados
        s = salida("discord").replace("\nPlugin toolsets", "\n  ✓ enabled  discord  💬 Discord\n  ✓ enabled  discord_admin  🛡️ Admin\n\nPlugin toolsets")
        ok, d = v.evaluar("discord", s)
        self.assertFalse(ok)
        self.assertIn("discord_admin", d)


class Evaluacion(unittest.TestCase):
    def test_canal_correcto(self):
        self.assertTrue(v.evaluar("telegram", salida("telegram"))[0])

    def test_cli_permite_su_conjunto_y_un_canal_no(self):
        s = salida("x", ("clarify", "file", "terminal", "todo"))
        self.assertTrue(v.evaluar("cli", s)[0])
        ok, detalle = v.evaluar("telegram", s)
        self.assertFalse(ok)
        self.assertIn("file", detalle)

    def test_cada_toolset_de_mas_en_un_canal_falla(self):
        for t in TOOLSETS:
            if t == "clarify":
                continue
            ok, d = v.evaluar("whatsapp", salida("whatsapp", ("clarify", t)))
            self.assertFalse(ok, t)
            self.assertIn(t, d)

    def test_plugin_habilitado_falla(self):
        self.assertFalse(v.evaluar("telegram", salida("telegram", plugin_habilitado=True))[0])

    def test_mcp_ajeno_o_ausente_falla(self):
        self.assertFalse(v.evaluar("telegram", salida("telegram", mcp=("agente-local  all tools enabled", "otro  all tools enabled")))[0])
        self.assertFalse(v.evaluar("telegram", salida("telegram", mcp=("otro  all tools enabled",)))[0])
        self.assertFalse(v.evaluar("telegram", salida("telegram", mcp=()))[0])

    def test_salida_ilegible_falla_cerrado(self):
        self.assertFalse(v.evaluar("telegram", "")[0])


class Descubrimiento(unittest.TestCase):
    def test_lista(self):
        self.assertEqual(v.descubrir_plataformas(ERROR_ZZZ), ["cli", "telegram", "whatsapp"])

    def test_no_legible(self):
        for malo in ("", None, "sin lista", "Valid: telegram, whatsapp", "Valid: cli, TELE GRAM", "Valid: cli, ../x"):
            self.assertIsNone(v.descubrir_plataformas(malo), repr(malo))


class Verificacion(unittest.TestCase):
    def test_todo_correcto(self):
        veredicto, res = v.verificar("agente", runner())
        self.assertEqual(veredicto, "CORRECTO", res)
        self.assertEqual([r[0] for r in res], ["cli", "telegram", "whatsapp"])

    def test_un_canal_con_terminal_falla_y_se_nombra(self):
        veredicto, res = v.verificar("agente", runner({"whatsapp": (0, salida("whatsapp", ("clarify", "terminal")))}))
        self.assertEqual(veredicto, "FALLÓ")
        self.assertEqual([r[0] for r in res if not r[1]], ["whatsapp"])

    def test_comando_que_falla_o_sin_descubrimiento_falla_cerrado(self):
        # rc != 0 aunque la salida sea PERFECTA: el código de salida cuenta por sí solo (hallazgo 5: antes el test usaba salida vacía)
        self.assertEqual(v.verificar("agente", runner({"telegram": (1, salida("telegram"))}))[0], "FALLÓ")
        self.assertEqual(v.verificar("agente", runner({"zzz": (1, ERROR_ZZZ)}))[0], "FALLÓ")
        self.assertEqual(v.verificar("agente", runner({"telegram": (1, "")}))[0], "FALLÓ")
        self.assertEqual(v.verificar("agente", runner({"zzz": (0, "otra cosa")}))[0], "FALLÓ")
        self.assertEqual(v.verificar("agente", runner({"zzz": (127, "hermes no está en el PATH")}))[0], "FALLÓ")

    def test_plataforma_nueva_se_verifica_aunque_no_la_conociera(self):
        def correr(perfil, p):
            if p == "zzz":
                return 0, "✗ Unknown platform 'zzz'. Valid: cli, nueva_red\n"
            return 0, salida(p, ("clarify", "terminal") if p == "nueva_red" else ("clarify", "file", "terminal", "todo"))
        veredicto, res = v.verificar("agente", correr)
        self.assertEqual(veredicto, "FALLÓ")
        self.assertEqual([r[0] for r in res if not r[1]], ["nueva_red"])

    def test_perfil_invalido(self):
        self.assertEqual(v.main(["--perfil", "../x"]), 2)


if __name__ == "__main__":
    unittest.main()
