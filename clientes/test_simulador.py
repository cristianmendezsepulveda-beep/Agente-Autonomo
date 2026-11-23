import json
import os
import sys
import tempfile
import types
import unittest

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "..", "sandbox"))
from generar_clientes import build
from simulador import (
    clientes_pendientes,
    simular,
    parsear,
    prompt_lote,
    cobertura,
    correr_hermes,
    PRECIO_TOPE,
)
from ranking import ranking, spearman, estabilidad, estabilidad_bootstrap
from budget_guard import BudgetGuard

with open(os.path.join(AQUI, "..", "productos", "variantes.json"), encoding="utf-8") as f:
    V = json.load(f)


def fila(c, v, l=4, p=50, extra=""):
    return '{"cliente":"%s","variante":"%s","likert":%s,"precio_max_usd":%s%s}' % (
        c,
        v,
        l,
        p,
        extra,
    )


def resp(cid, vid, l, p):
    return {
        "cliente": cid,
        "variante": vid,
        "likert": l,
        "precio_max_usd": p,
        "objecion": "",
        "backend": "x",
    }


class T(unittest.TestCase):
    def setUp(self):
        self.c = build(42)

    # --- heurístico ---
    def test_heuristico_completo_y_determinista(self):
        r = simular(self.c, V, 1)
        self.assertEqual(len(r), 100 * len(V))
        self.assertTrue(all(1 <= x["likert"] <= 5 for x in r))
        self.assertEqual(r, simular(self.c, V, 1))
        self.assertEqual(cobertura(r, self.c, V), [])

    def test_prompt_solo_incluye_el_lote(self):
        p = prompt_lote(self.c[:10], V)
        self.assertIn("C010", p)
        self.assertNotIn("C011", p)

    # --- parser: la salida del modelo es dato no confiable ---
    def test_descarta_filas_ajenas_invalidas_y_no_objetos(self):
        t = (
            "basura ["
            + ",".join(
                [
                    fila("C001", "V1"),
                    fila("HACK", "V1", 5),
                    fila("C001", "V2", 9),
                    fila("C001", "V3", '"abc"'),
                    '{"cliente":["C001"],"variante":"V1","likert":3}',
                    "7",
                    '{"cliente":"C001","variante":"V4","likert":3.9}',
                ]
            )
            + "] fin"
        )
        out = parsear(t, {"C001"}, {"V1", "V2", "V3", "V4"})
        self.assertEqual([(o["cliente"], o["variante"]) for o in out], [("C001", "V1")])

    def test_corchetes_alrededor_y_sin_json(self):
        t = "nota [1] antes\n[" + fila("C001", "V1") + "]\n y después [fin]"
        self.assertEqual(len(parsear(t, {"C001"}, {"V1"})), 1)
        with self.assertRaises(ValueError):
            parsear("sin json", {"C001"}, {"V1"})

    def test_elige_la_lista_con_mas_filas_validas_no_la_primera(self):
        eco = "[" + fila("C001", "V1", 5) + "]"  # eco/inyección al inicio con 1 fila
        buena = "[" + fila("C001", "V1", 2) + "," + fila("C001", "V2", 3) + "]"
        out = parsear(eco + "\n" + buena, {"C001"}, {"V1", "V2"})
        self.assertEqual(len(out), 2)

    def test_nan_infinity_overflow_recursion_y_tope(self):
        with self.assertRaises(ValueError):  # NaN: la lista no se acepta
            parsear("[" + fila("C001", "V2", "NaN") + "]", {"C001"}, {"V2"})
        enorme = "[" + fila("C001", "V1", 4, "1" + "0" * 400) + "]"  # 10**400: antes lanzaba OverflowError
        out = parsear(enorme, {"C001"}, {"V1"})
        self.assertEqual(out[0]["precio_max_usd"], PRECIO_TOPE)
        with self.assertRaises(ValueError):  # 1e999 => inf: fila descartada, sin filas válidas
            parsear("[" + fila("C001", "V1", 4, "1e999") + "]", {"C001"}, {"V1"})
        with self.assertRaises(ValueError):
            parsear("[" * 100000, {"C001"}, {"V1"})  # anidamiento profundo: error controlado, no RecursionError

    def test_precio_ausente_se_descarta_no_cuenta_como_cero(self):
        t = '[{"cliente":"C001","variante":"V1","likert":4},' + fila("C001", "V2") + "]"
        out = parsear(t, {"C001"}, {"V1", "V2"})
        self.assertEqual([o["variante"] for o in out], ["V2"])

    def test_deduplica_y_sanea_objecion(self):
        t = "[" + fila("C001", "V1", 5) + "," + fila("C001", "V1", 1) + "," + fila("C002", "V1", 3, 10, ',"objecion":"a\\u001b[31mrojo\\n"') + "]"
        out = parsear(t, {"C001", "C002"}, {"V1"})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["likert"], 5)
        self.assertNotIn("\x1b", out[1]["objecion"])

    # --- backend hermes (con llamada simulada: no gasta tokens) ---
    def _falso(self, salidas):
        it = iter(salidas)

        def llamar(prompt):
            s = next(it)
            if isinstance(s, Exception):
                raise s
            return types.SimpleNamespace(returncode=0, stdout=s, stderr="")

        return llamar

    def _lote_ok(self, clientes):
        return "[" + ",".join(fila(c["id"], v["id"]) for c in clientes for v in V) + "]"

    def test_hermes_exige_guard(self):
        with self.assertRaises(RuntimeError):
            correr_hermes(self.c, V, None)

    def test_lote_fallido_no_descarta_los_pagados_y_se_guarda_cada_lote(self):
        import subprocess

        with tempfile.TemporaryDirectory() as d:
            g = BudgetGuard(os.path.join(d, "b.json"), 10**9, 1000)
            ok = lambda i: self._lote_ok(self.c[i * 10 : (i + 1) * 10])
            # lotes: 0 ok, 1 timeout, 2 sin json (2 fallos seguidos), 3 ok, 4 archivo ausente (suelto), 5-9 ok
            salidas = [
                ok(0),
                subprocess.TimeoutExpired("hermes", 1),
                "sin json",
                ok(3),
                FileNotFoundError("hermes"),
                ok(5),
                ok(6),
                ok(7),
                ok(8),
                ok(9),
            ]
            guardados = []
            res, corte = correr_hermes(self.c, V, g, guardados.append, self._falso(salidas))
            self.assertIsNone(corte)  # nunca 3 fallos seguidos
            self.assertEqual(len(guardados), 10)  # persistió tras cada lote
            self.assertEqual(len(res), 7 * 10 * len(V))  # 3 lotes perdidos, 7 conservados
            self.assertEqual(g.acciones, 10)

    def test_limite_detiene_antes_de_gastar_y_devuelve_lo_pagado(self):
        with tempfile.TemporaryDirectory() as d:
            g = BudgetGuard(os.path.join(d, "b.json"), 10**9, 2)  # solo 2 lotes
            llamadas = []

            def llamar(p):
                llamadas.append(1)
                return types.SimpleNamespace(returncode=0, stdout=self._lote_ok(self.c[0:10]), stderr="")

            res, corte = correr_hermes(self.c, V, g, None, llamar)
            self.assertEqual(len(llamadas), 2)
            self.assertIn("acciones", corte)
            self.assertTrue(res)

    def test_corta_tras_tres_fallos_seguidos_y_cobra_overhead(self):
        with tempfile.TemporaryDirectory() as d:
            g = BudgetGuard(os.path.join(d, "b.json"), 10**9, 1000)
            llamadas = []

            def llamar(p):
                llamadas.append(1)
                return types.SimpleNamespace(returncode=2, stdout="", stderr="flag desconocido")

            res, corte = correr_hermes(self.c, V, g, None, llamar)
            self.assertEqual((len(llamadas), res), (3, []))
            self.assertIn("seguidos", corte)
            self.assertGreaterEqual(g.tokens, 3 * (6000 + 15000))  # incluye el overhead por llamada

    def test_la_llamada_real_restringe_herramientas(self):
        import simulador

        capturado = {}

        def falso(args, **kw):
            capturado["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

        original = simulador.subprocess.run
        simulador.subprocess.run = falso
        try:
            simulador._llamar_hermes("prueba")
        finally:
            simulador.subprocess.run = original
        a = capturado["args"]
        self.assertEqual(a[a.index("-t") + 1], "todo")
        for f in ("--ignore-rules", "--max-turns", "--query-file"):
            self.assertNotIn("--run-budget", a)
            self.assertIn(f, a)

    def test_reanudar_solo_pide_los_clientes_incompletos(self):
        completos = [resp(c["id"], v["id"], 3, 10) for c in self.c[:80] for v in V]
        parcial = completos + [resp(self.c[80]["id"], "V1", 3, 10)]  # un cliente con 1 de 4 variantes
        pend = clientes_pendientes(parcial, self.c, V)
        self.assertEqual([c["id"] for c in pend], [c["id"] for c in self.c[80:]])
        self.assertEqual(clientes_pendientes(simular(self.c, V, 1), self.c, V), [])

    # --- ranking ---
    def test_ranking_ordenado_por_limite_inferior_y_ic(self):
        r1 = ranking(self.c, V, simular(self.c, V, 1))
        inf = [f["ic95_viabilidad"][0] for f in r1]
        self.assertEqual(inf, sorted(inf, reverse=True))
        for f in r1:
            lo, hi = f["ic95_viabilidad"]
            self.assertLessEqual(lo, f["viabilidad"] + 1e-9)
            self.assertGreaterEqual(hi, f["viabilidad"] - 1e-9)

    def test_confianza_en_los_umbrales(self):
        esperado = {9: "baja", 10: "media", 19: "media", 20: "alta"}
        # clientes sintéticos del mismo sector hasta tener n exacto
        base = dict(self.c[0])
        for n, conf in esperado.items():
            cl = [dict(base, id=f"X{i:03d}") for i in range(n)]
            rs = [resp(c["id"], "V1", 4, 100) for c in cl]
            fila_ = ranking(cl, V, rs)[0]
            self.assertEqual((fila_["n"], fila_["confianza"]), (n, conf))

    def test_pocos_clientes_no_ganan_por_suerte(self):
        base = dict(self.c[0])
        chico = [dict(base, id=f"A{i}", sector="agro") for i in range(4)]
        grande = [dict(base, id=f"B{i}", sector="turismo") for i in range(40)]
        rs = [resp(c["id"], "V1", 5, 500) for c in chico]  # 4 de 4: viabilidad 1.0 con n=4
        rs += [resp(c["id"], "V1", 5 if i < 34 else 1, 500 if i < 34 else 0) for i, c in enumerate(grande)]  # 85%
        r = ranking(chico + grande, V, rs)
        self.assertEqual(r[0]["sector"], "turismo")  # el orden conservador premia la evidencia

    def test_duplicados_rechazados_y_id_desconocido(self):
        r = resp(self.c[0]["id"], "V1", 4, 100)
        with self.assertRaises(ValueError):
            ranking(self.c, V, [r, dict(r)])
        with self.assertRaises(ValueError):
            ranking(self.c, V, [resp("ZZZ", "V1", 3, 1)])

    def test_estabilidades(self):
        rs = simular(self.c, V, 1)
        self.assertGreater(estabilidad(ranking(self.c, V, rs), ranking(self.c, V, simular(self.c, V, 2))), 0.8)
        e = estabilidad_bootstrap(self.c, V, rs, B=10)
        self.assertTrue(0.0 < e["spearman"] < 1.0)
        self.assertTrue(0.0 <= e["top_k_solape"] <= 1.0)

    def test_spearman_basico(self):
        self.assertAlmostEqual(spearman([1, 2, 3], [10, 20, 30]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3], [3, 2, 1]), -1.0)
        self.assertEqual(spearman([], []), 0.0)


if __name__ == "__main__":
    unittest.main()
