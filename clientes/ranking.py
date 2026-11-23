"""Ranking por (sector, variante) con intervalos de confianza y estabilidad por remuestreo de clientes.

viabilidad = 0.6*intencion + 0.4*disposicion_pago     (SOLO datos de las respuestas)
El ranking ordena por el LÍMITE INFERIOR del IC95 de la viabilidad (intervalo de Wilson: ancho con pocos clientes).
intencion = %likert>=4 ; disposicion_pago = % cuyo precio_max >= precio de la variante.
`prior_sector` (facilidad de automatizar, riesgo*autonomia) se informa APARTE y no entra en el orden,
para no contar dos veces supuestos que el simulador heurístico ya usa.
Señal sintética: sirve para ORDENAR hipótesis, no para estimar demanda real. Diferencias dentro del
intervalo de confianza no son ordenables.
"""

import argparse
import json
import random
from collections import defaultdict

MIN_N_MEDIA, MIN_N_ALTA = 10, 20


def _wilson(p, n, z=1.96):
    """Intervalo de Wilson para una proporción (o media en [0,1]): se ensancha con n pequeño y no es
    degenerado cuando p=0 o p=1 (a diferencia del bootstrap con pocos clientes)."""
    d = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / d
    mitad = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round(max(0.0, centro - mitad), 3), round(min(1.0, centro + mitad), 3)


def ranking(clientes, vars_, resp, seed=0, unicos=True):
    cmap = {c["id"]: c for c in clientes}
    vmap = {v["id"]: v for v in vars_}
    g, vistos = defaultdict(list), set()
    for r in resp:
        if r["cliente"] not in cmap or r["variante"] not in vmap:
            raise ValueError(f"respuesta con id desconocido: {r['cliente']}/{r['variante']}")
        par = (r["cliente"], r["variante"])
        if unicos and par in vistos:
            raise ValueError(f"par duplicado: {par}")  # inflaría n y la confianza
        vistos.add(par)
        g[(cmap[r["cliente"]]["sector"], r["variante"])].append(r)
    filas = []
    for (sec, vid), rs in g.items():
        n = len(rs)
        c0 = cmap[rs[0]["cliente"]]
        bin_i = [int(r["likert"] >= 4) for r in rs]
        bin_p = [int(r["precio_max_usd"] >= vmap[vid]["precio_mensual_usd"]) for r in rs]
        interes, pago = sum(bin_i) / n, sum(bin_p) / n
        lo, hi = _wilson(0.6 * interes + 0.4 * pago, n)
        filas.append(
            {
                "sector": sec,
                "variante": vid,
                "n": n,
                "intencion": round(interes, 3),
                "disposicion_pago": round(pago, 3),
                "viabilidad": round(0.6 * interes + 0.4 * pago, 3),
                "ic95_viabilidad": (lo, hi),
                "prior_sector": round(
                    0.5 * c0["facilidad_automatizar_sector"] - 0.5 * c0["riesgo_regulatorio_sector"] * vmap[vid]["autonomia"],
                    3,
                ),
                "confianza": "alta" if n >= MIN_N_ALTA else "media" if n >= MIN_N_MEDIA else "baja",
            }
        )
    # orden CONSERVADOR: por el límite inferior del IC95 (las celdas con pocos clientes bajan solas)
    return sorted(filas, key=lambda f: (-f["ic95_viabilidad"][0], -f["viabilidad"]))


def _rangos(vals):
    orden = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(orden):
        j = i
        while j + 1 < len(orden) and vals[orden[j + 1]] == vals[orden[i]]:
            j += 1
        for k in range(i, j + 1):
            r[orden[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 2 or len(a) != len(b):
        return 0.0
    ra, rb = _rangos(a), _rangos(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def estabilidad(r1, r2):
    """Correlación entre dos corridas sobre los MISMOS clientes: mide solo el ruido del simulador."""
    d1 = {(f["sector"], f["variante"]): f["viabilidad"] for f in r1}
    d2 = {(f["sector"], f["variante"]): f["viabilidad"] for f in r2}
    claves = [k for k in d1 if k in d2]
    return spearman([d1[k] for k in claves], [d2[k] for k in claves])


def estabilidad_bootstrap(clientes, vars_, resp, B=100, seed=0, k=5):
    """Remuestrea clientes con reemplazo y compara con el ranking completo. Devuelve
    {"spearman": media, "top_k_solape": media de |top-k ∩ top-k base|/k}.
    Es una cota OPTIMISTA: el remuestreo sale de la misma muestra que el ranking base."""
    rng = random.Random(seed)
    base_r = ranking(clientes, vars_, resp)
    base = {(f["sector"], f["variante"]): f["viabilidad"] for f in base_r}
    top_base = {(f["sector"], f["variante"]) for f in base_r[:k]}
    por_cliente = defaultdict(list)
    for r in resp:
        por_cliente[r["cliente"]].append(r)
    ids = [c["id"] for c in clientes if c["id"] in por_cliente]
    cs, solapes = [], []
    for _ in range(B):
        muestra = [r for cid in (rng.choice(ids) for _ in ids) for r in por_cliente[cid]]
        rk_r = ranking(clientes, vars_, muestra, unicos=False)
        rk = {(f["sector"], f["variante"]): f["viabilidad"] for f in rk_r}
        claves = [c for c in base if c in rk]
        cs.append(spearman([base[c] for c in claves], [rk[c] for c in claves]))
        solapes.append(len(top_base & {(f["sector"], f["variante"]) for f in rk_r[:k]}) / k)
    return {"spearman": sum(cs) / B, "top_k_solape": sum(solapes) / B}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clientes", default="clientes.json")
    ap.add_argument("--variantes", default="../productos/variantes.json")
    ap.add_argument("--resp", nargs="+", default=["respuestas.json"])
    a = ap.parse_args()
    with open(a.clientes, encoding="utf-8") as f:
        cl = json.load(f)
    with open(a.variantes, encoding="utf-8") as f:
        vs = json.load(f)
    resps = []
    for p in a.resp:
        with open(p, encoding="utf-8") as f:
            resps.append(json.load(f))
    rks = [ranking(cl, vs, r) for r in resps]
    with open("ranking.json", "w", encoding="utf-8") as f:
        json.dump(rks[0], f, ensure_ascii=False, indent=1)
    for fila in rks[0][:10]:
        print(
            fila["sector"],
            fila["variante"],
            fila["viabilidad"],
            "IC95 viabilidad",
            fila["ic95_viabilidad"],
            f"n={fila['n']}",
            fila["confianza"],
        )
    est = estabilidad_bootstrap(cl, vs, resps[0])
    print(
        "Estabilidad por remuestreo (cota optimista): Spearman",
        round(est["spearman"], 3),
        "| solape del top-5",
        round(est["top_k_solape"], 3),
    )
    if len(rks) > 1:
        print(
            "Spearman entre corridas (solo ruido del simulador):",
            round(estabilidad(rks[0], rks[1]), 3),
        )
