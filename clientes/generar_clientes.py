"""Genera 100 clientes sintéticos de forma determinista (sin LLM, sin red).

Uso: python generar_clientes.py [--seed 42] [--out clientes.json]
Solo biblioteca estándar. Mismo seed => mismo resultado.
"""

import argparse
import json
import math
import random

N = 100
SKEPTIC_MIN = 25  # mínimo de clientes difíciles: escépticos, obsesionados con el costo o baja tolerancia

SECTORES = {  # sector: (peso, facilidad_automatizar 0-1, riesgo_regulatorio 0-1)
    "comercio minorista": (18, 0.8, 0.2),
    "servicios profesionales": (14, 0.7, 0.4),
    "gastronomía": (10, 0.6, 0.3),
    "construcción": (9, 0.4, 0.4),
    "salud privada": (8, 0.5, 0.9),
    "educación": (8, 0.7, 0.4),
    "logística": (8, 0.6, 0.3),
    "inmobiliaria": (7, 0.7, 0.5),
    "manufactura liviana": (7, 0.5, 0.3),
    "turismo": (6, 0.7, 0.3),
    "agro": (5, 0.4, 0.3),
}
CARACTERES = [  # (nombre, peso, sesgo de apertura -1..1)
    ("pragmático", 25, 0.2),
    ("escéptico", 22, -0.7),
    ("entusiasta tecnológico", 12, 0.8),
    ("conservador", 15, -0.4),
    ("obsesionado con el costo", 14, -0.3),
    ("miedo a perder control", 12, -0.5),
]
CRITERIOS = [
    "precio",
    "confianza",
    "facilidad de uso",
    "integración con lo que ya uso",
    "privacidad de datos",
    "soporte humano",
    "ahorro de tiempo demostrable",
]
OBJECIONES = {
    "precio": "Es caro para lo que factura mi negocio.",
    "confianza": "No confío en que un agente actúe sin que yo lo revise.",
    "facilidad de uso": "No tengo tiempo de aprender otra herramienta.",
    "integración con lo que ya uso": "Tiene que funcionar con mi WhatsApp/planilla actual.",
    "privacidad de datos": "No quiero que datos de mis clientes salgan de mi control.",
    "soporte humano": "Si falla, quiero hablar con una persona.",
    "ahorro de tiempo demostrable": "Muéstrame horas ahorradas, no promesas.",
}


def pick(rng, items, weights):
    return rng.choices(items, weights=weights, k=1)[0]


def build(seed):
    rng = random.Random(seed)
    sectores = list(SECTORES)
    sw = [SECTORES[s][0] for s in sectores]
    clientes = []
    for i in range(N):
        sector = pick(rng, sectores, sw)
        ingresos = round(math.exp(rng.gauss(math.log(60000), 1.0)), -2)  # USD/año, lognormal
        ingresos = max(8000, min(ingresos, 3_000_000))
        empleados = max(1, int(ingresos / rng.uniform(25000, 70000)))
        car = pick(rng, CARACTERES, [c[1] for c in CARACTERES])
        pct = {"obsesionado con el costo": 0.004}.get(car[0], rng.uniform(0.004, 0.02))
        presupuesto_mensual = round(ingresos * pct / 12, 0)
        tolerancia = round(min(1, max(0, 0.5 + car[2] * 0.4 + rng.gauss(0, 0.15))), 2)
        criterios = rng.sample(CRITERIOS, 3)
        clientes.append(
            {
                "id": f"C{i + 1:03d}",
                "sector": sector,
                "ingresos_anuales_usd": ingresos,
                "empleados": empleados,
                "caracter": car[0],
                "tolerancia_riesgo": tolerancia,
                "presupuesto_mensual_usd": presupuesto_mensual,
                "criterios_decision": criterios,
                "objecion_principal": OBJECIONES[criterios[0]],
                "facilidad_automatizar_sector": SECTORES[sector][1],
                "riesgo_regulatorio_sector": SECTORES[sector][2],
                "nota": "SINTÉTICO: señal blanda, no demanda real",
            }
        )

    # garantizar mínimo de escépticos / presupuesto bajo
    def dificil(c):
        return c["caracter"] in ("escéptico", "obsesionado con el costo") or c["tolerancia_riesgo"] < 0.35

    faltan = SKEPTIC_MIN - sum(dificil(c) for c in clientes)
    if faltan > 0:  # se eligen al azar (con el mismo seed), no por orden de ID
        candidatos = [c for c in clientes if not dificil(c)]
        for c in rng.sample(candidatos, min(faltan, len(candidatos))):
            c["caracter"], c["tolerancia_riesgo"] = "escéptico", 0.2
    return clientes


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="clientes.json")
    a = ap.parse_args()
    data = build(a.seed)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"{len(data)} clientes -> {a.out}")
