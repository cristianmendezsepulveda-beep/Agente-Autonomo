"""Respuesta a consultas sin LLM: coincidencia por palabras clave contra un catálogo de FAQ de confianza.

Ahorro de tokens: lo repetitivo se resuelve con código. Lo que no encaja, es sensible, parece manipulación,
trae caracteres fuera del español normal, números largos, correos o varias intenciones, se ESCALA a un humano.
El texto del cliente nunca se repite en la respuesta ni se trata como instrucción.

QUÉ ES ESTO: una ayuda de ENRUTAMIENTO, no un control de seguridad. Las respuestas son textos fijos del catálogo,
así que el daño de un falso negativo se limita a que una consulta rara reciba una FAQ en vez de escalarse. No detecta
toda manipulación (el modelo ya leyó el texto crudo antes de llamar aquí): la protección real es que el servidor
solo devuelve texto fijo, deja borradores y nunca envía nada.

Coincidencia: cada patrón se compila con sus letras separables por un espacio opcional ("c v v", "pa-go"), con
límite de palabra al inicio (no coincide dentro de otra palabra: "reciban" no activa "iban") y, para patrones de
hasta 4 letras, también al final (admite plural). Se prueba además con sustituciones tipo "1gnora"/"rec1amo".
Límites conocidos: no cubre símbolos arbitrarios ("ign#ra"); "varias intenciones" = 2 o más FAQ con coincidencias.
"""

import functools
import re
import unicodedata

MAX_TEXTO = 2000
# Sustituciones frecuentes. "1" y "|" pueden ser i o l: se prueban las dos variantes.
LEET_A = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "i",
    }
)
LEET_B = str.maketrans(
    {
        "0": "o",
        "1": "l",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "l",
    }
)
# LISTA BLANCA de caracteres: ASCII imprimible, espacios, letras y signos del español y tipografía común.
# Todo lo demás (homoglifos, ø ı đ, ancho cero, bidi, emojis) escala en vez de intentar normalizarlo.
PERMITIDOS = set("áéíóúüñÁÉÍÓÚÜÑ¿¡") | set("…—–“”‘’«»€°ºª·") | {chr(10), chr(9), chr(13)}
NUMEROS = re.compile(r"(?:\d\D{0,3}){9,}")
CORREO = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _base(s):
    """NFKD, sin marcas combinantes ni caracteres de formato (ancho cero, guion blando), en minúsculas."""
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c) and unicodedata.category(c) != "Cf").lower()


def normalizar(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", _base(s))).strip()


@functools.lru_cache(maxsize=512)
def _patron(palabra):
    letras = normalizar(palabra).replace(" ", "")
    cuerpo = " ?".join(re.escape(c) for c in letras)
    fin = r"(?:s|es)?\b" if len(letras) <= 4 else ""  # palabras cortas: completas (con plural)
    return re.compile(r"\b" + cuerpo + fin)


def _contiene(textos, palabra):
    p = _patron(palabra)
    return any(p.search(t) for t in (textos if isinstance(textos, tuple) else (textos,)))


def _caracteres_inusuales(texto):
    return any(not (0x20 <= ord(c) <= 0x7E or c in PERMITIDOS) for c in texto)


def responder(cat, texto):
    if not isinstance(texto, str) or not texto.strip():
        raise ValueError("texto vacío o no es cadena")
    if len(texto) > MAX_TEXTO:
        return {"tipo": "escalar", "motivo": "mensaje demasiado largo"}
    texto = unicodedata.normalize("NFC", texto)  # "á" descompuesta cuenta como "á"
    if _caracteres_inusuales(texto):
        return {"tipo": "escalar", "motivo": "caracteres inusuales (posible ofuscación)"}
    if NUMEROS.search(texto) or CORREO.search(texto):
        return {"tipo": "escalar", "motivo": "datos personales (números largos o correo)"}
    b = _base(texto)
    t1 = normalizar(b)
    variantes = (
        t1,
        normalizar(b.translate(LEET_A)),
        normalizar(b.translate(LEET_B)),
    )  # "1gnora", "rec1amo"
    if any(_contiene(variantes, p) for p in cat["patrones_manipulacion"]):
        return {"tipo": "escalar", "motivo": "posible intento de manipulación"}
    if any(_contiene(variantes, p) for p in cat["palabras_sensibles"]):
        return {
            "tipo": "escalar",
            "motivo": "tema sensible (pagos, reclamos, datos personales o urgencia)",
        }
    puntos = [sum(_contiene(t1, w) for w in f["palabras"]) for f in cat["faq"]]
    con_coincidencia = [i for i, p in enumerate(puntos) if p > 0]
    if not con_coincidencia:
        return {"tipo": "escalar", "motivo": "consulta no reconocida"}
    if len(con_coincidencia) > 1:
        return {"tipo": "escalar", "motivo": "la consulta mezcla varias intenciones"}
    return {"tipo": "faq", "respuesta": cat["faq"][con_coincidencia[0]]["respuesta"]}
