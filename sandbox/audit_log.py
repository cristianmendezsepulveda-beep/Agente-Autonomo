"""Registro de auditoría append-only con cadena de hashes.

ALCANCE REAL: detecta modificaciones accidentales y ediciones torpes. NO resiste a quien pueda escribir el
archivo (puede recalcular toda la cadena) ni detecta que lo borren o trunquen al final. Para eso haría falta
HMAC con clave fuera del alcance del agente y un ancla externa del último hash.
No registrar secretos: los textos se truncan y no se guarda contenido de archivos.
"""
import hashlib
import json
import os
import time

from _lock import bloqueo

GENESIS = "0" * 64
CLAVES = {"n", "ts", "evento", "datos", "prev", "hash"}


def _h(prev, cuerpo):
    # ensure_ascii=True: un sustituto suelto (\ud800) no rompe la codificación
    return hashlib.sha256((prev + json.dumps(cuerpo, sort_keys=True, ensure_ascii=True)).encode("ascii")).hexdigest()


def _ultimo(ruta):
    """(n, hash) de la última línea; (0, GENESIS) si no existe. Lanza RuntimeError si está truncada."""
    if not os.path.exists(ruta) or os.path.getsize(ruta) == 0:
        return 0, GENESIS
    with open(ruta, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 65536))
        cola = f.read()
    if not cola.endswith(b"\n"):
        raise RuntimeError("registro truncado (última línea incompleta)")
    try:
        r = json.loads(cola.rstrip(b"\n").split(b"\n")[-1].decode("utf-8"))
        return r["n"], r["hash"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("registro corrupto (última línea)")


class AuditLog:
    def __init__(self, ruta):
        self.ruta = ruta
        os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
        if os.path.exists(ruta):
            ok, motivo = verificar(ruta)
            if not ok:
                raise RuntimeError(f"registro corrupto: {motivo}")

    def registrar(self, evento, datos=None):
        # máx. 20 claves: mantiene cada línea muy por debajo de los 64 KB que lee _ultimo
        datos = {str(k)[:60]: str(v)[:300] for k, v in list((datos or {}).items())[:20]}
        with bloqueo(self.ruta):  # relee la cola dentro del bloqueo: varias instancias no bifurcan la cadena
            n, prev = _ultimo(self.ruta)
            cuerpo = {"n": n + 1, "ts": round(time.time(), 3), "evento": str(evento)[:60], "datos": datos}
            linea = json.dumps({**cuerpo, "prev": prev, "hash": _h(prev, cuerpo)}, ensure_ascii=True)
            with open(self.ruta, "a", encoding="ascii") as f:
                f.write(linea + "\n")
                f.flush()
                os.fsync(f.fileno())


def verificar(ruta):
    prev, n = GENESIS, 0
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
        if datos and not datos.endswith(b"\n"):
            return False, "última línea incompleta"
        for linea in datos.decode("utf-8").splitlines():
            n += 1
            r = json.loads(linea)
            if set(r) != CLAVES:
                return False, f"línea {n}: claves"
            cuerpo = {k: r[k] for k in ("n", "ts", "evento", "datos")}
            if r["n"] != n or r["prev"] != prev or r["hash"] != _h(prev, cuerpo):
                return False, f"línea {n}"
            prev = r["hash"]
    except (OSError, ValueError, TypeError) as e:
        return False, f"línea {n}: {type(e).__name__}"
    return True, "ok"
