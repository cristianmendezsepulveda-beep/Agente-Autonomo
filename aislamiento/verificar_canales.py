"""Verifica que los CANALES de mensajería del perfil `agente` de Hermes solo tengan las herramientas permitidas.

Qué comprueba: para cada plataforma que Hermes declara válida (se descubre, no se supone), `hermes -p <perfil> tools list --platform <p>`
debe mostrar habilitado COMO MÁXIMO `clarify` (y `clarify`, `file`, `terminal`, `todo` en `cli`, que ya estaba recortada y usa el contenedor),
ningún toolset de plugin habilitado y como único servidor MCP `agente-local`. Falla cerrado: salida vacía, ilegible o con un formato
inesperado, plataformas que no se pueden descubrir, o un comando que falla, dan FALLÓ.

Qué NO comprueba: el comportamiento de un mensaje real (no hay canal emparejado); que el gateway en ejecución haya recargado la
configuración (hay que reiniciarlo); herramientas individuales del MCP; que una versión futura de Hermes no reabra herramientas.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys

PERMITIDOS = {"cli": {"clarify", "file", "terminal", "todo"}}
PERMITIDOS_CANAL = {"clarify"}
MCP_PERMITIDOS = {"agente-local"}
_RE_VALIDAS = re.compile(r"Valid:\s*(.+)$", re.M)
_RE_TOOLSET = re.compile(r"^\s{2}(✓ enabled|✗ disabled)\s+([A-Za-z0-9_\-]+)(\s.*)?$")
_RE_MCP = re.compile(r"^\s{2}([A-Za-z0-9_\-]+)\s{2,}(.+)$")
MCP_ESTADO_OK = "all tools enabled"
TOOLSETS_OBLIGATORIOS = {
    "clarify",
    "terminal",
    "file",
    "web",
    "browser",
}  # deben aparecer (habilitados o no): si faltan, el formato no es el esperado
_RE_PLATAFORMA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,39}")


def parsear(salida):
    """PURO. Devuelve (toolsets_habilitados, servidores_mcp, formato_ok). formato_ok=False si faltan las secciones esperadas."""
    if not isinstance(salida, str) or "Built-in toolsets" not in salida or salida.count("MCP servers:") != 1:
        return set(), set(), False
    cuerpo, _, mcp = salida.partition("MCP servers:")
    habilitados, todos = set(), set()
    for linea in cuerpo.splitlines():
        if not linea.strip() or re.match(r"^(Built-in|Plugin) toolsets \(", linea):
            continue
        m = _RE_TOOLSET.match(linea)
        if not m:
            return (
                set(),
                set(),
                False,
            )  # una línea con otro marcador o nombre raro NO se ignora: falla cerrado
        todos.add(m.group(2))
        if m.group(1).startswith("✓"):
            habilitados.add(m.group(2))
    servidores = set()
    for linea in mcp.splitlines():
        if linea.strip():
            m = _RE_MCP.match(linea)
            if not m:
                return set(), set(), False
            if m.group(2).strip() != MCP_ESTADO_OK:
                habilitados.add("mcp:" + m.group(1) + " (estado: " + m.group(2).strip()[:30] + ")")  # estado distinto de "all tools enabled": no esperado
            servidores.add(m.group(1))
    return habilitados, servidores, TOOLSETS_OBLIGATORIOS <= todos


def evaluar(plataforma, salida):
    """PURO. (ok, detalle) para una plataforma."""
    habilitados, mcp, ok = parsear(salida)
    if not ok:
        return False, "salida vacía o con formato inesperado (falla cerrado)"
    permitidos = PERMITIDOS.get(plataforma, PERMITIDOS_CANAL)
    sobran = sorted(habilitados - permitidos)
    faltan_mcp = MCP_PERMITIDOS - mcp
    otros_mcp = sorted(mcp - MCP_PERMITIDOS)
    problemas = []
    if sobran:
        problemas.append("toolsets habilitados de más: " + ", ".join(sobran))
    if otros_mcp:
        problemas.append("servidores MCP no esperados: " + ", ".join(otros_mcp))
    if faltan_mcp:
        problemas.append("falta el MCP: " + ", ".join(sorted(faltan_mcp)))
    return (not problemas), ("; ".join(problemas) if problemas else "solo " + ", ".join(sorted(permitidos | MCP_PERMITIDOS)))


def descubrir_plataformas(salida):
    """PURO. Lista de plataformas válidas según el mensaje de error de Hermes; None si no se puede leer."""
    m = _RE_VALIDAS.search(salida or "")
    if not m:
        return None
    lista = [p.strip() for p in m.group(1).split(",")]
    if not lista or not all(_RE_PLATAFORMA.fullmatch(p) for p in lista) or "cli" not in lista:
        return None
    return lista


def correr(perfil, plataforma, timeout=60):
    exe = shutil.which("hermes")
    if not exe:
        return 127, "hermes no está en el PATH"
    try:
        p = subprocess.run(
            [exe, "-p", perfil, "tools", "list", "--platform", plataforma],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 124, type(e).__name__
    return p.returncode, p.stdout


def verificar(perfil, correr_fn=None):
    """Devuelve (veredicto, resultados [(plataforma, ok, detalle)])."""
    correr_fn = correr_fn or correr
    rc, sal = correr_fn(perfil, "zzz")
    plataformas = descubrir_plataformas(sal) if rc == 0 else None
    if not plataformas:
        return "FALLÓ", [
            (
                "(descubrimiento de plataformas)",
                False,
                "no se pudo obtener la lista de plataformas de Hermes",
            )
        ]
    resultados = []
    for p in plataformas:
        rc, sal = correr_fn(perfil, p)
        if rc != 0:
            resultados.append((p, False, f"el comando falló (rc={rc})"))
        else:
            ok, detalle = evaluar(p, sal)
            resultados.append((p, ok, detalle))
    return ("CORRECTO" if all(ok for _, ok, _ in resultados) else "FALLÓ"), resultados


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--perfil", default="agente")
    ap.add_argument("--salida", default=None)
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,39}", a.perfil):
        print("perfil inválido", file=sys.stderr)
        return 2
    veredicto, resultados = verificar(a.perfil)
    for p, ok, d in resultados:
        print(("OK    " if ok else "FALLA ") + p + " -> " + d)
    print("Veredicto canales:", veredicto)
    if a.salida:
        with open(a.salida, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "perfil": a.perfil,
                    "veredicto": veredicto,
                    "resultados": [list(r) for r in resultados],
                },
                f,
                ensure_ascii=False,
                indent=1,
            )
    return 0 if veredicto == "CORRECTO" else 1


if __name__ == "__main__":
    sys.exit(main())
