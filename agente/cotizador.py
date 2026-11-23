"""Cotizador determinista (sin LLM): mismo input => mismo resultado, con aritmética decimal.

Nunca envía nada: devuelve un borrador y marca si requiere aprobación humana.
"""

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

D2 = Decimal("0.01")
MAX_CANT = 10000
MAX_LINEAS = 50
CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
NUMEROS = re.compile(r"(?:\d\D{0,3}){9,}")  # 9+ dígitos con cualquier separador corto (tarjetas, cuentas, documentos)
CORREO = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _q(x):
    return x.quantize(D2, rounding=ROUND_HALF_UP)


# Topes de magnitud: un valor finito pero enorme (p. ej. 1e26 o "1e999999") pasaría una simple comprobación de
# signo y luego desbordaría la aritmética Decimal (contexto de 28 dígitos) en `cotizar`/`_q`, devolviendo el mismo
# error opaco que esta validación quiere evitar. Con estos topes, el peor caso (precio × MAX_CANT × MAX_LINEAS) cabe
# de sobra en 28 dígitos.
_MAX_PRECIO = Decimal("1e12")
_MAX_UMBRAL = Decimal("1e12")
_CIEN = Decimal(100)


def _decimal_rango(v, maximo):
    """Decimal finito en [0, maximo] a partir de un número o un texto numérico (como lo convierte `cotizar` con
    Decimal(str(...))). Lanza ValueError si no se puede o se sale del rango; nunca InvalidOperation ni Overflow.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise ValueError
    if not d.is_finite() or d < 0 or d.is_signed() or d > maximo:  # la comparación no hace aritmética: no desborda aunque d sea enorme; is_signed descarta -0
        raise ValueError
    return d


def _lista_de_textos(v):
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x.strip() for x in v)


def _validar_faq(cat):
    """Valida las claves que usa el módulo de consultas (consultas.responder): patrones y palabras de enrutamiento
    (listas de texto NO vacías: una lista vacía apagaría la detección de manipulación/temas sensibles sin avisar) y
    la lista de FAQ (cada entrada con 'palabras' y 'respuesta'). Cada término debe conservar algo tras
    consultas.normalizar (solo [a-z0-9]): un término como '!!!' o '€' normaliza a vacío y su regex casaría con casi
    cualquier texto (escalaría todo, o una entrada de FAQ respondería a cualquier consulta), un fallo silencioso.
    """
    from consultas import (
        normalizar,
    )  # import local: el módulo está en el pin; evita cualquier orden de carga raro

    def enrutables(v):
        # >= 3 caracteres tras normalizar: un término de 1 letra ("\ba\b") casa con casi cualquier consulta
        return _lista_de_textos(v) and all(len(normalizar(x).replace(" ", "")) >= 3 for x in v)

    for campo in ("patrones_manipulacion", "palabras_sensibles"):
        if not enrutables(cat.get(campo)):
            raise ValueError(f"catálogo: '{campo}' debe ser una lista no vacía de textos de al menos 3 letras o dígitos")
    faq = cat.get("faq")
    if not isinstance(faq, list) or not faq:
        raise ValueError("catálogo: 'faq' debe ser una lista con al menos una entrada")
    for i, f in enumerate(faq, 1):
        if not (isinstance(f, dict) and enrutables(f.get("palabras")) and isinstance(f.get("respuesta"), str) and f["respuesta"].strip()):
            raise ValueError(f"catálogo: faq #{i} debe tener 'palabras' (lista de textos de >= 3 letras/dígitos) y 'respuesta' (texto)")


def validar_catalogo(cat):
    """Valida la estructura del catálogo (agente/catalogo.json) de la que dependen `cotizar`, `listar_catalogo` y el
    módulo de consultas, para fallar al ARRANCAR con un mensaje claro en vez de con un KeyError/InvalidOperation
    opaco en la primera llamada. Devuelve `cat` o lanza ValueError."""
    if not isinstance(cat, dict):
        raise ValueError("el catálogo debe ser un objeto JSON")
    if not isinstance(cat.get("moneda"), str) or not cat["moneda"].strip():
        raise ValueError("catálogo: 'moneda' debe ser un texto no vacío")
    for campo, tope in (("iva_pct", _CIEN), ("umbral_aprobacion_total", _MAX_UMBRAL)):
        try:
            _decimal_rango(cat.get(campo), tope)
        except ValueError:
            raise ValueError(f"catálogo: '{campo}' debe ser un número entre 0 y {tope:f}")
    if type(cat.get("max_lineas_auto")) is not int or not 1 <= cat["max_lineas_auto"] <= MAX_LINEAS:
        raise ValueError(f"catálogo: 'max_lineas_auto' debe ser un entero entre 1 y {MAX_LINEAS}")
    items = cat.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("catálogo: 'items' debe ser una lista con al menos un producto")
    vistos = set()
    for i, it in enumerate(items, 1):
        donde = f"catálogo: item #{i}"
        if not isinstance(it, dict):
            raise ValueError(f"{donde} debe ser un objeto")
        sku = it.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError(f"{donde}: 'sku' debe ser un texto no vacío")
        if sku in vistos:
            raise ValueError(f"catálogo: sku duplicado: {sku!r}")
        vistos.add(sku)
        if not isinstance(it.get("nombre"), str) or not it["nombre"].strip():
            raise ValueError(f"{donde} ({sku!r}): 'nombre' debe ser un texto no vacío")
        try:
            _decimal_rango(it.get("precio_unitario"), _MAX_PRECIO)
        except ValueError:
            raise ValueError(f"{donde} ({sku!r}): 'precio_unitario' debe ser un número entre 0 y {_MAX_PRECIO:f}")
        try:
            _decimal_rango(it.get("descuento_max_pct"), _CIEN)
        except ValueError:
            raise ValueError(f"{donde} ({sku!r}): 'descuento_max_pct' debe ser un número entre 0 y 100")
    _validar_faq(cat)
    return cat


def _porcentaje(v):
    try:
        ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    except OverflowError:  # entero gigantesco
        ok = False
    if not ok:
        raise ValueError("descuento_pct debe ser un número finito")
    d = Decimal(str(v))
    if not Decimal(0) <= d <= Decimal(100):
        raise ValueError("descuento_pct fuera de 0..100")
    return d


def limpiar_texto(s, maximo=60):
    """Texto de un tercero para un archivo que leerá una persona: sin controles, sin formato invisible
    (ancho cero, inversión bidireccional) y con números largos ocultos."""
    if not isinstance(s, str):
        raise ValueError("el texto debe ser una cadena")
    s = s[: maximo * 4]  # ANTES de las regex: CORREO es cuadrática y 40.000 letras tardaban 7 s
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    s = NUMEROS.sub("[numero oculto]", CORREO.sub("[correo oculto]", CTRL.sub(" ", s)))
    return s[:maximo].strip()


def cotizar(cat, lineas, descuento_pct=0, cliente_ref=""):
    if not isinstance(lineas, list) or not 1 <= len(lineas) <= MAX_LINEAS:
        raise ValueError(f"lineas debe ser una lista de 1 a {MAX_LINEAS} elementos")
    items = {i["sku"]: i for i in cat["items"]}
    d = _porcentaje(descuento_pct)
    detalle, subtotal, desc_max = [], Decimal(0), Decimal(100)
    for l in lineas:
        if not isinstance(l, dict) or set(l) != {"sku", "cantidad"}:
            raise ValueError("cada línea debe tener exactamente sku y cantidad")
        sku, cant = l["sku"], l["cantidad"]
        if not isinstance(sku, str) or sku not in items:
            raise ValueError("sku desconocido")
        if type(cant) is not int or not 1 <= cant <= MAX_CANT:
            raise ValueError(f"cantidad debe ser un entero de 1 a {MAX_CANT}")
        it = items[sku]
        total_linea = _q(Decimal(str(it["precio_unitario"])) * cant)
        subtotal += total_linea
        desc_max = min(desc_max, Decimal(str(it["descuento_max_pct"])))
        detalle.append(
            {
                "sku": sku,
                "nombre": it["nombre"],
                "cantidad": cant,
                "precio_unitario": str(_q(Decimal(str(it["precio_unitario"])))),
                "total": str(total_linea),
            }
        )
    descuento = _q(subtotal * d / 100)
    base = subtotal - descuento
    impuesto = _q(base * Decimal(str(cat["iva_pct"])) / 100)
    total = base + impuesto
    # `motivos` es lo que recibe el MODELO: genérico, sin cifras de la política (si no, probando descuentos se
    # deduciría el máximo por producto). `detalle_operador` lleva los números y solo queda en el borrador para la persona.
    motivos, detalle_operador = [], []
    if d > desc_max:
        motivos.append("el descuento solicitado requiere aprobación humana")
        detalle_operador.append(f"descuento {d}% supera el máximo permitido {desc_max}% para estas líneas")
    if total > Decimal(str(cat["umbral_aprobacion_total"])):
        motivos.append("total supera el umbral de aprobación")
        detalle_operador.append(f"total {_q(total)} supera el umbral {cat['umbral_aprobacion_total']}")
    if len(lineas) > cat["max_lineas_auto"]:
        motivos.append("demasiadas líneas para cotización automática")
        detalle_operador.append(f"{len(lineas)} líneas; el máximo automático es {cat['max_lineas_auto']}")
    return {
        "moneda": cat["moneda"],
        "cliente_ref": limpiar_texto(cliente_ref),
        "lineas": detalle,
        "subtotal": str(_q(subtotal)),
        "descuento_pct": format(d, "f"),
        "descuento": str(descuento),
        "impuesto": str(impuesto),
        "total": str(_q(total)),
        "requiere_aprobacion_humana": bool(motivos),
        "motivos": motivos,
        "detalle_operador": detalle_operador,
        "estado": "borrador_pendiente_aprobacion",
    }
