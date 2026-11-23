"""Frenos deterministas previos a cualquier salida: marcado del texto de terceros y revisión de borradores. Solo biblioteca estándar.

Todo lo de este módulo solo puede FRENAR o ADVERTIR; nada amplía lo que otro código permite. Ante un error interno se retiene.
"""

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

import nocontacto

_ABRE, _CIERRA = "<<<EXTERNO", "EXTERNO>>>"
# `<` y `>` en cualquier forma: literal, de ancho completo (NFKC los normaliza antes) o como entidad HTML (con o sin ";"), para que nadie
# pueda fabricar una marca falsa dentro del texto marcado aunque algo posterior des-escape entidades.
_RE_ANGULOS = re.compile(r"[<>]|&(?:lt|gt|#0*(?:60|62)|#x0*(?:3c|3e));?", re.IGNORECASE)


def marcar_externo(texto):
    """Envuelve texto escrito por un TERCERO entre `<<<EXTERNO` y `EXTERNO>>>` para que el modelo lo trate como datos, nunca como
    instrucciones. Antes elimina cualquier `<`/`>` (y sus formas de entidad) y los caracteres de control o de formato. Es una
    mitigación, no una garantía: un modelo puede ignorar la marca."""
    if not isinstance(texto, str):
        raise ValueError("el texto debe ser una cadena")
    t = unicodedata.normalize("NFKC", texto)
    t = _RE_ANGULOS.sub(" ", t)
    t = "".join((" " if unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp") else c) for c in t)
    return f"{_ABRE} {' '.join(t.split())} {_CIERRA}" if t.strip() else f"{_ABRE}  {_CIERRA}"


# ---------------------------------------------------------------------------------------------------------------- revisión previa
MAX_ASUNTO, MAX_MENSAJE, MAX_LINEAS = 150, 1500, 50
_D2 = Decimal("0.01")
_RE_LLAVES = re.compile(r"[{}]")
# Sin IGNORECASE: "[nota]" o "todo el día" son texto normal. TODO/XXX solo cuentan SUELTOS: dentro de un identificador ("XXX-001", "TODO-12",
# "ABC-XXX-9") son parte de un número de factura real.
_RE_MARCADOR_MAYUS = re.compile(r"\[[A-Z][A-Z_ ]{2,30}\]|<[A-Z][A-Z_ ]{2,30}>|(?<![\w-])TODO(?![\w-])")
_RE_RELLENO = re.compile(r"(?i)(?<![\w-])X{3,}(?![\w-])|lorem ipsum")
_RE_HTML = re.compile(r"<\s*/?\s*[A-Za-z!?]")
_RE_ENLACE = re.compile(r"(?:\b(?:https?|ftp)://|\bwww\.)", re.IGNORECASE)
_RE_DOMINIO_SUELTO = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|\b[A-Za-z0-9\-]+\.(?:com|net|org|io|co|mx|es|ar|cl|pe|info|biz|app|dev)\b",
    re.IGNORECASE,
)
# re.ASCII: `\d` aceptaría dígitos de otros alfabetos. 18 enteros: validar_catalogo admite precios hasta 1e12 y el cotizador 10.000 unidades × 50 líneas.
_RE_DINERO = re.compile(r"\d{1,18}\.\d{2}", re.ASCII)
_RE_PCT = re.compile(r"\d{1,3}(?:\.\d{1,60})?", re.ASCII)  # el cotizador lo imprime tal cual lo recibe (0.1+0.2 = 0.30000000000000004; 1.5e-30 = 31 decimales)
# Frases YA normalizadas (sin acentos, minúsculas, solo [a-z0-9]). AVISOS, no bloqueos: una persona puede querer incluir datos de pago.
_FRASES_AVISO = (
    "transferencia",
    "transfiera",
    "transferir",
    "iban",
    "swift",
    "cuenta bancaria",
    "numero de cuenta",
    "deposite",
    "gift card",
    "tarjeta de regalo",
    "wire transfer",
    "western union",
    "bitcoin",
    "criptomoneda",
    "usdt",
    "ultimo aviso",
    "demanda",
    "embargo",
    "policia",
    "acciones legales",
    "consecuencias legales",
    "denuncia",
    "abogado",
    "tribunal",
    "cobro juridico",
    "urgente",
    "contrasena",
    "password",
    "tarjeta de credito",
    "cvv",
    "clave de acceso",
)
_FRASES_COMPACTAS = tuple(f.replace(" ", "") for f in _FRASES_AVISO if len(f.replace(" ", "")) >= 9)


def _normalizar_aviso(texto):
    t = unicodedata.normalize("NFKC", texto).casefold()
    t = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) not in ("Mn", "Cf"))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _frases_en(texto):
    """Frases de pago o presión presentes; la versión compacta (sin espacios) caza las letras separadas ("t r a n s f e r e n c i a")."""
    n = _normalizar_aviso(texto[:20000])
    rodeado = f" {n} "
    hallazgos = [f for f in _FRASES_AVISO if f" {f} " in rodeado]
    compacto = n.replace(" ", "")
    hallazgos += [f for f in _FRASES_COMPACTAS if f in compacto and f not in [h.replace(" ", "") for h in hallazgos]]
    return hallazgos


MAX_TEXTO_GENERICO = 5000  # tope de lo que se examina si el campo no tiene un máximo propio (las expresiones regulares no deben ver texto enorme)


def _texto_libre(nombre, valor, bloqueos, avisos, maximo=None, estricto=True, solo_avisos=False):
    """Reglas objetivas sobre un texto que va a leer un tercero o la persona. HTML y llaves sin resolver bloquean; los marcadores
    ([NOMBRE], TODO, XXX) y los enlaces bloquean si `estricto` (mensajes a terceros) y son solo avisos si no (texto corto de una cotización).
    `solo_avisos`: texto que escribe la persona en el catálogo (moneda, SKU, nombre): todo hallazgo es un aviso, porque `validar_catalogo`
    lo admite y un catálogo así no debe producir borradores imposibles de aprobar. Lo vacío o que no es texto bloquea siempre.
    """
    if not isinstance(valor, str) or not valor.strip():
        bloqueos.append(f"{nombre}: falta o no es texto")
        return
    firme = bloqueos if not solo_avisos else avisos  # dónde van los hallazgos que normalmente bloquean
    tope = maximo if maximo is not None else MAX_TEXTO_GENERICO
    if len(valor) > tope:  # sale ANTES de las expresiones regulares: un texto enorme no puede colgar la revisión
        firme.append(f"{nombre}: supera {tope} caracteres")
        return
    marcadores = _RE_MARCADOR_MAYUS.search(valor) or _RE_RELLENO.search(valor)
    if _RE_LLAVES.search(valor) or (marcadores and estricto):
        firme.append(f"{nombre}: variables o marcadores sin rellenar")
    elif marcadores:
        avisos.append(f"{nombre}: parece contener un marcador sin rellenar")
    if _RE_HTML.search(valor):
        firme.append(f"{nombre}: contiene HTML")
    if _RE_ENLACE.search(valor):
        (firme if estricto else avisos).append(f"{nombre}: contiene un enlace")
    elif _RE_DOMINIO_SUELTO.search(valor):
        avisos.append(f"{nombre}: parece contener un correo o un dominio")
    for f in _frases_en(valor):
        avisos.append(f'{nombre}: frase de pago o presión ("{f}")')


def _dinero(v):
    if not isinstance(v, str) or not _RE_DINERO.fullmatch(v):
        raise ValueError("cifra ilegible")
    return Decimal(v)


def _q(d):
    return d.quantize(_D2, rounding=ROUND_HALF_UP)


def _revisar_cotizacion(q, bloqueos, avisos):
    _texto_libre("moneda", q.get("moneda"), bloqueos, avisos, 10, estricto=False, solo_avisos=True)  # el catálogo admite cualquier texto no vacío
    _texto_libre("cliente_ref", q.get("cliente_ref") or "-", bloqueos, avisos, 60, estricto=False)
    lineas = q.get("lineas")
    if not isinstance(lineas, list) or not 1 <= len(lineas) <= MAX_LINEAS:
        bloqueos.append("líneas ausentes o fuera de rango")
        return
    try:
        subtotal = Decimal(0)
        for l in lineas:
            if not isinstance(l, dict):
                raise ValueError("línea ilegible")
            cant = l.get("cantidad")
            if type(cant) is not int or not 1 <= cant <= 10000:
                raise ValueError("cantidad inválida")
            for campo in ("sku", "nombre"):
                _texto_libre(
                    f"línea {campo}",
                    l.get(campo),
                    bloqueos,
                    avisos,
                    120,
                    estricto=False,
                    solo_avisos=True,
                )
            precio, total_l = _dinero(l.get("precio_unitario")), _dinero(l.get("total"))
            # el cotizador multiplica el precio SIN redondear y el borrador muestra el precio ya redondeado a 2 decimales: la diferencia
            # legítima puede llegar a medio céntimo por unidad (más medio céntimo del redondeo del total)
            if abs(precio * cant - total_l) > cant * Decimal("0.005") + Decimal("0.005"):
                raise ValueError("el total de una línea no cuadra")
            subtotal += total_l
        if not isinstance(q.get("descuento_pct"), str) or not _RE_PCT.fullmatch(q["descuento_pct"]) or Decimal(q["descuento_pct"]) > 100:
            raise ValueError("descuento_pct ilegible")
        if _dinero(q.get("subtotal")) != subtotal:
            raise ValueError("el subtotal no cuadra")
        descuento = _dinero(q.get("descuento"))
        if descuento != _q(subtotal * Decimal(q["descuento_pct"]) / 100):
            raise ValueError("el descuento no cuadra")
        base, impuesto, total = (
            subtotal - descuento,
            _dinero(q.get("impuesto")),
            _dinero(q.get("total")),
        )
        if impuesto > base or total != base + impuesto:
            raise ValueError("impuesto o total no cuadran")
    except (ValueError, ArithmeticError) as e:
        bloqueos.append(f"cifras: {e}")
    if q.get("requiere_aprobacion_humana") is True:
        avisos.append("el cotizador marcó que requiere aprobación humana (descuento, monto o número de líneas)")


def _revisar_cobranza(b, lista, bloqueos, avisos):
    dest = b.get("destinatario")
    try:
        nocontacto.normalizar(dest)
        formato_ok = True
    except ValueError:
        formato_ok = False
        bloqueos.append("destinatario ausente o con formato inválido")
    if lista is None:
        bloqueos.append("no se pudo consultar la lista de no contacto")
    elif formato_ok and lista.bloquea(dest):
        bloqueos.append("el destinatario está en la lista de no contacto")
    _texto_libre("asunto", b.get("asunto"), bloqueos, avisos, MAX_ASUNTO)
    _texto_libre("mensaje", b.get("mensaje"), bloqueos, avisos, MAX_MENSAJE)


def revisar(tipo, contenido, lista=None):
    """Reglas fijas sobre un borrador ANTES de aprobarlo. Devuelve {"bloqueos": [...], "avisos": [...]}: con algún bloqueo no debe
    aprobarse; los avisos los decide la persona. `lista` = lista de no contacto (necesaria en cobranza; sin ella se bloquea). Nunca
    lanza: si algo falla internamente, bloquea."""
    bloqueos, avisos = [], []
    try:
        if tipo == "escalado":
            return {"bloqueos": [], "avisos": []}
        if tipo not in ("cobranza", "cotizacion") or not isinstance(contenido, dict):
            bloqueos.append("tipo o contenido no reconocido")
        elif tipo == "cobranza":
            _revisar_cobranza(contenido, lista, bloqueos, avisos)
        else:
            _revisar_cotizacion(contenido, bloqueos, avisos)
    except Exception:
        bloqueos.append("la revisión falló: trátelo como no revisado")
    dedup = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    return {"bloqueos": dedup(bloqueos), "avisos": dedup(avisos)}
