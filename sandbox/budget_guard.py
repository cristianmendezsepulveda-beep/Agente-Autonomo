"""Tope duro de tokens y acciones. Estado persistente con bloqueo; al superar el límite lanza LimiteExcedido.

Solo cuenta lo que el llamador declara: quien invoque un modelo debe cobrar una estimación ANTES de llamar.
"""

import json
import os
import uuid

from _lock import bloqueo


class LimiteExcedido(Exception):
    pass


def _rechazar_constante(c):
    raise ValueError(f"constante no válida: {c}")


def _entero(x):
    return type(x) is int and x >= 0


class BudgetGuard:
    def __init__(self, ruta_estado, tokens_max, acciones_max):
        if not (_entero(tokens_max) and _entero(acciones_max)):
            raise ValueError("límites inválidos")
        self.ruta, self.tokens_max, self.acciones_max = ruta_estado, tokens_max, acciones_max
        self.tokens = self.acciones = 0
        with bloqueo(self.ruta):
            self._leer()

    def _leer(self):
        if not os.path.exists(self.ruta):
            self.tokens = self.acciones = 0
            return
        try:
            with open(self.ruta, encoding="utf-8") as f:
                e = json.load(f, parse_constant=_rechazar_constante)
            t, a = e["tokens"], e["acciones"]
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as ex:
            raise RuntimeError(f"estado de presupuesto inválido: {type(ex).__name__}")  # falla cerrado
        if not (_entero(t) and _entero(a)):
            raise RuntimeError("estado de presupuesto inválido: valores")
        self.tokens, self.acciones = t, a

    def _guardar(self):
        tmp = f"{self.ruta}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tokens": self.tokens, "acciones": self.acciones}, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.ruta)

    def cobrar_accion(self, tokens=0):
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens debe ser entero >= 0")
        with bloqueo(self.ruta):
            self._leer()  # relee dentro de la sección crítica: ve lo que cobraron otros procesos
            if self.acciones + 1 > self.acciones_max:
                raise LimiteExcedido("acciones")
            if self.tokens + tokens > self.tokens_max:
                raise LimiteExcedido("tokens")
            self.acciones += 1
            self.tokens += tokens
            self._guardar()
