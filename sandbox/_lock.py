"""Bloqueo entre procesos por creación exclusiva de archivo, con recuperación de bloqueos huérfanos.

Las secciones críticas duran milisegundos: un .lock con más de EDAD_HUERFANO segundos se considera de un
proceso muerto y se retira. Falla cerrado (BloqueoNoObtenido) si no se logra obtener a tiempo.
"""

import contextlib
import os
import time

EDAD_HUERFANO = 30.0


class BloqueoNoObtenido(Exception):
    pass


def _retirar_si_huerfano(lock):
    try:
        if time.time() - os.path.getmtime(lock) > EDAD_HUERFANO:
            os.remove(lock)
    except OSError:
        pass  # otro proceso lo retiró o lo está usando: se reintenta


@contextlib.contextmanager
def bloqueo(ruta, timeout=5.0):
    lock = ruta + ".lock"
    limite = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):  # Windows: borrado pendiente => PermissionError
            _retirar_si_huerfano(lock)
            if time.monotonic() > limite:
                raise BloqueoNoObtenido(lock)
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.remove(lock)
        except OSError:
            pass  # si no se pudo borrar, la recuperación de huérfanos lo retirará
