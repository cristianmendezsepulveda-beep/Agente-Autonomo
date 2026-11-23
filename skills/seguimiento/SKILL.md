---
name: seguimiento
description: Detecta cotizaciones en borrador sin decisión hace días con `seguimiento_pendientes` y las escala a una persona con `escalar_a_humano` para que haga el seguimiento. No contacta a nadie ni redacta mensajes.
---

# Seguimiento de cotizaciones

Herramientas del servidor MCP `agente-local`: `seguimiento_pendientes` y `escalar_a_humano`.

## Flujo
1. Llama a `seguimiento_pendientes`. Devuelve hasta 20 cotizaciones en borrador sin tocar hace 3 días o más (las más antiguas primero), con `referencia`, total, moneda y días.
2. Para cada una, llama a `escalar_a_humano` con `motivo` "cotización sin respuesta: hacer seguimiento" y la `referencia` que te dio la herramienta (no la modifiques).
3. Resume al operador cuántas hay y las más antiguas. Si la lista está vacía, dilo en una línea.

## Qué garantiza el código y qué depende de ti
- **El código garantiza:** que solo se listan borradores válidos por antigüedad del archivo, sin datos personales; que la referencia es un alias corto apto para escalar; que nada se envía.
- **El código NO garantiza** que "sin tocar" signifique "sin respuesta": mide la antigüedad del archivo, no si el cliente contestó. Una persona decide qué hacer y archiva o cierra los borradores ya resueltos.
- **Depende de que TÚ cumplas:**
  1. No contactes al cliente ni redactes mensajes: solo escalas.
  2. No escales dos veces la misma referencia en una misma sesión.
  3. Si `escalar_a_humano` falla, avisa al operador; no lo des por registrado hasta ver `"escalado": "e-…"`.

## Ahorro de tokens
Una llamada a `seguimiento_pendientes` y una a `escalar_a_humano` por elemento; resume.
