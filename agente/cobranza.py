"""Seguimiento de cotizaciones y cobranza (borradores de recordatorio). Solo biblioteca estándar.

Qué es y qué NO es:
- Un motor DETERMINISTA: el código lee la cartera (CSV que carga una persona), decide la etapa y arma el mensaje con plantillas
  fijas. El modelo no redacta ni calcula nada: solo pide evaluar o preparar una factura por su id.
- NUNCA envía, cobra ni mueve dinero: deja un borrador para que una persona lo revise y lo envíe por su cuenta.
- La fuente de deuda es `workspace/cartera/cartera.csv`; si una persona no lo mantiene, el agente no sabe nada. El conteo de
  recordatorios y la fecha del último salen del CSV (los actualiza la persona al enviar), no del modelo.
- Facturas pagadas, anuladas o en disputa NUNCA generan mensaje. Una disputa, un monto alto, más de 30 días de atraso o 3
  recordatorios ya enviados se escalan a una persona.
- Los datos personales no vuelven al modelo: el email solo se guarda dentro del borrador (para la persona).
- Las plantillas son texto de ejemplo en español neutro: una persona debe revisarlas (tono y requisitos legales de su país).
  Nada verifica el consentimiento del cliente para ser contactado.
"""
import csv
import datetime
import io
import json
import os
import re
from decimal import Decimal, InvalidOperation

import crm  # solo para validar correos con las mismas reglas del CRM

MAX_BYTES, MAX_FILAS = 1_000_000, 2000
COLUMNAS = ("factura_id", "cliente_ref", "email", "monto", "moneda", "fecha_vencimiento", "estado",
            "recordatorios_enviados", "ultimo_recordatorio")
ESTADOS = ("pendiente", "pagada", "disputa", "anulada")
UMBRAL_MONTO = Decimal("2000")  # sobre este monto siempre decide una persona (propuesto; ajustar al negocio)
MAX_RECORDATORIOS = 3
DIAS_ENTRE_RECORDATORIOS = 7
DIAS_AMABLE, DIAS_FIRME = 7, 30  # 1..7 amable, 8..30 firme, más de 30 escalar
SEGUIMIENTO_DIAS = 3
MAX_SEGUIMIENTOS = 20
_RE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{0,29}")
_RE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,59}")
_RE_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_RE_MONEDA = re.compile(r"[A-Z]{3}")
_RE_BORRADOR = re.compile(r"b-[0-9a-f]{12}")


class CarteraInvalida(ValueError):
    """La cartera no se pudo leer o no tiene el formato esperado (el mensaje no incluye datos de los clientes)."""


def _fecha(s):
    if not _RE_FECHA.fullmatch(s or ""):
        raise ValueError("fecha no ISO (AAAA-MM-DD)")
    return datetime.date.fromisoformat(s)


def _fila(f):
    fid = f["factura_id"].strip()
    if not _RE_ID.fullmatch(fid) or re.search(r"\d{9,}", fid, re.ASCII):
        raise ValueError("factura_id inválido")  # sin números largos: se usa como referencia al escalar y escalar los oculta
    ref = f["cliente_ref"].strip()
    if not _RE_REF.fullmatch(ref) or re.search(r"\d{9,}", ref, re.ASCII):
        raise ValueError("cliente_ref inválido")
    email = crm.validar_email(f["email"])
    m = f["monto"].strip()
    if not re.fullmatch(r"\d{1,10}(\.\d{1,2})?", m, re.ASCII):  # sin signo, exponente, NaN ni separadores: solo dígitos y hasta 2 decimales
        raise ValueError("monto inválido")
    monto = Decimal(m)
    if monto <= 0:
        raise ValueError("monto inválido")
    moneda = f["moneda"].strip()
    if not _RE_MONEDA.fullmatch(moneda):
        raise ValueError("moneda inválida")
    estado = f["estado"].strip()
    if estado not in ESTADOS:
        raise ValueError("estado inválido")
    n = f["recordatorios_enviados"].strip() or "0"
    if not re.fullmatch(r"\d{1,2}", n, re.ASCII):
        raise ValueError("recordatorios_enviados inválido")
    ult = f["ultimo_recordatorio"].strip()
    return {"factura_id": fid, "cliente_ref": ref, "email": email, "monto": monto.quantize(Decimal("0.01")), "moneda": moneda,
            "vence": _fecha(f["fecha_vencimiento"].strip()), "estado": estado, "recordatorios": int(n),
            "ultimo": _fecha(ult) if ult else None}


def leer_cartera(ruta):
    """Devuelve (facturas por id, número de filas rechazadas). Un id repetido deja FUERA todas sus filas (ambiguo)."""
    try:
        with open(ruta, "rb") as fh:
            crudo = fh.read(MAX_BYTES + 1)
    except OSError:
        raise CarteraInvalida("no se pudo leer la cartera (¿existe workspace/cartera/cartera.csv?)")
    if len(crudo) > MAX_BYTES:
        raise CarteraInvalida("la cartera supera el tamaño máximo (1 MB)")
    try:
        texto = crudo.decode("utf-8-sig")
        lector = csv.DictReader(io.StringIO(texto, newline=""), strict=True)
        if tuple(lector.fieldnames or ()) != COLUMNAS:
            raise CarteraInvalida("las columnas de la cartera no son las esperadas")
        validas, conteo, rechazadas, n = [], {}, 0, 0
        for fila in lector:
            n += 1
            if n > MAX_FILAS:
                raise CarteraInvalida("la cartera supera las 2000 filas")
            # el id CRUDO de TODA fila (válida o no) cuenta para detectar repetidos: si no, una fila malformada que dice
            # "pagada" dejaría viva a otra fila con el mismo id que dice "pendiente"
            crudo_id = (fila.get("factura_id") or "").strip().lower()
            conteo[crudo_id] = conteo.get(crudo_id, 0) + 1
            if None in fila or any(v is None for v in fila.values()):
                rechazadas += 1
                continue
            try:
                validas.append(_fila(fila))
            except ValueError:
                rechazadas += 1
    except (UnicodeError, csv.Error):
        raise CarteraInvalida("la cartera no es un CSV UTF-8 válido")
    filas = {}
    for d in validas:
        if conteo[d["factura_id"].lower()] > 1:
            rechazadas += 1  # id repetido (incluso solo por mayúsculas o con otra fila inválida): ninguna fila se usa
        else:
            filas[d["factura_id"]] = d
    return filas, rechazadas


def validar_factura_id(valor):
    if not isinstance(valor, str) or not _RE_ID.fullmatch(valor) or re.search(r"\d{9,}", valor, re.ASCII):
        raise ValueError("factura_id inválido")
    return valor


def evaluar(f, hoy):
    """PURO. Devuelve (etapa, dias_atraso). Etapas: sin_accion, al_dia, espera, amable, firme, escalar."""
    if f["estado"] in ("pagada", "anulada"):
        return "sin_accion", 0
    dias = (hoy - f["vence"]).days
    if f["estado"] == "disputa":
        return "escalar", max(dias, 0)  # una persona decide; jamás se manda recordatorio
    if dias <= 0:
        return "al_dia", 0
    if f["recordatorios"] >= MAX_RECORDATORIOS or f["monto"] > UMBRAL_MONTO or dias > DIAS_FIRME:
        return "escalar", dias
    if f["ultimo"] is not None and (hoy - f["ultimo"]).days < DIAS_ENTRE_RECORDATORIOS:
        return "espera", dias
    return ("amable" if dias <= DIAS_AMABLE else "firme"), dias


_CIERRE = ("Si ya realizó el pago, o si hay algún problema con esta factura, por favor respóndanos y lo revisamos. "
           "Si prefiere no recibir más recordatorios, indíquelo en su respuesta y lo atenderemos.")
PLANTILLAS = {
    "amable": ("Recordatorio de la factura {factura_id}",
               "Hola. Le escribimos para recordarle que la factura {factura_id} por {moneda} {monto}, con vencimiento el {vence}, "
               "figura pendiente desde hace {dias} día(s). " + _CIERRE),
    "firme": ("Factura {factura_id} pendiente de pago",
              "Hola. La factura {factura_id} por {moneda} {monto} venció el {vence} y sigue pendiente desde hace {dias} días. "
              "Le pedimos regularizarla o contactarnos para que podamos revisarla juntos. " + _CIERRE),
}


def preparar(f, etapa, dias, creado):
    """PURO. Borrador (dict) a partir de una plantilla fija; las cifras salen de la cartera, nunca del modelo."""
    if etapa not in PLANTILLAS:
        raise ValueError("esta etapa no genera mensaje")
    asunto, cuerpo = PLANTILLAS[etapa]
    datos = {"factura_id": f["factura_id"], "moneda": f["moneda"], "monto": f"{f['monto']:.2f}", "vence": f["vence"].isoformat(),
             "dias": dias}
    return {"id": f"c-{f['factura_id']}-{etapa}".lower(), "factura_id": f["factura_id"], "etapa": etapa, "destinatario": f["email"],
            "asunto": asunto.format(**datos), "mensaje": cuerpo.format(**datos), "creado": creado,
            "estado": "borrador_pendiente_aprobacion", "nota": "NO enviado: una persona debe revisarlo y enviarlo"}


def seguimientos(carpeta_borradores, ahora, dias=SEGUIMIENTO_DIAS):
    """Cotizaciones en borrador con más de `dias` días sin tocar (por fecha de modificación del archivo), las más antiguas primero.
    Devuelve [{borrador, referencia, total, moneda, dias}] (máx. MAX_SEGUIMIENTOS). `referencia` es un alias corto apto para escalar."""
    salida = []
    try:
        entradas = list(os.scandir(carpeta_borradores))
    except FileNotFoundError:
        return []
    for e in entradas[:2000]:
        nombre = e.name[:-5] if e.name.endswith(".json") else ""
        if not _RE_BORRADOR.fullmatch(nombre) or not e.is_file(follow_symlinks=False):
            continue
        antig = (ahora - e.stat().st_mtime) / 86400
        if antig < dias:
            continue
        try:
            with open(e.path, "rb") as fh:
                q = json.loads(fh.read(200_001).decode("utf-8"))
            if q.get("estado") != "borrador_pendiente_aprobacion" or not isinstance(q.get("total"), str):
                continue
            total, moneda = str(Decimal(q["total"]).quantize(Decimal("0.01"))), str(q.get("moneda", ""))[:3]
        except (OSError, ValueError, InvalidOperation, AttributeError, UnicodeError):
            continue
        salida.append({"borrador": nombre, "referencia": "seg-" + nombre[2:10], "total": total, "moneda": moneda, "dias": int(antig)})
    salida.sort(key=lambda x: -x["dias"])
    return salida[:MAX_SEGUIMIENTOS]
