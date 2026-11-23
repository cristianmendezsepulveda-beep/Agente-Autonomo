import unittest
from generar_clientes import build, N, SKEPTIC_MIN


class T(unittest.TestCase):
    def test_cantidad_e_ids_unicos(self):
        d = build(42)
        self.assertEqual(len(d), N)
        self.assertEqual(len({c["id"] for c in d}), N)

    def test_determinista(self):
        self.assertEqual(build(7), build(7))
        self.assertNotEqual(build(7), build(8))

    def test_minimo_escepticos(self):
        for seed in range(20):
            d = build(seed)
            n = sum(c["caracter"] in ("escéptico", "obsesionado con el costo") or c["tolerancia_riesgo"] < 0.35 for c in d)
            self.assertGreaterEqual(n, SKEPTIC_MIN)

    def test_diversidad(self):
        d = build(42)
        self.assertGreaterEqual(len({c["sector"] for c in d}), 8)
        ing = sorted(c["ingresos_anuales_usd"] for c in d)
        self.assertGreater(ing[-1] / ing[0], 20)  # amplio rango de ingresos
        self.assertTrue(all(0 <= c["tolerancia_riesgo"] <= 1 for c in d))


if __name__ == "__main__":
    unittest.main()
