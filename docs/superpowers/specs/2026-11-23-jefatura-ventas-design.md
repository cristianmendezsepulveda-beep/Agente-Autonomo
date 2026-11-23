# Jefatura de ventas: diseño

Estado: APROBADO por el usuario (2026-11-23). **Construido solo el nivel 1** (bandeja de aprobación en `agente/bandeja.py`; ver "Fase F" de `docs/GUARDRAILS.md`). Desviaciones respecto a este diseño: la bandeja usa las carpetas que ya existían (`workspace/borradores`, `borradores_cobranza`, `escalados`) y guarda registros en `workspace/archivo/aprobados|rechazados/` en lugar de `workspace/bandeja/…`; es línea de comandos, sin pantalla; no existe "editar y aprobar" ni nivel vigente por categoría ni descenso automático (el descenso es manual); no hay envío. Todo lo demás (niveles 2 a 4, revisor, prerrequisitos) sigue sin construir. Las decisiones de detalle (umbrales, nombres, niveles) son mías y corregibles. Los umbrales de ascenso no están probados con datos reales: son un punto de partida.

## Intención
El objetivo final es un vendedor 100% autónomo en producción; hoy estamos en MVP con aprobación humana para todo lo que sale hacia fuera. La jefatura es la capa que decide **qué puede hacer el agente solo, qué debe aprobar una persona y cómo se sube la autonomía** sin saltos de fe. Funciona como un departamento de ventas: un jefe humano, un vendedor (el agente), un revisor opcional y reglas fijas que ni el vendedor ni el revisor pueden saltarse.

## Principios
1. **La autonomía se gana por categoría de acción, no de forma global.** Se puede ser autónomo en "recordatorio amable de 1-7 días" y seguir con aprobación en "descuento".
2. **Los topes y las reglas viven en código, no en un modelo.** Un agente revisor solo puede frenar o escalar; nunca ampliar lo que el código permite.
3. **La aprobación se ata al contenido** (hash). Si el borrador cambia después de aprobado, la aprobación deja de valer.
4. **Quien propone no aprueba.** El agente solo escribe en la bandeja de pendientes; no puede marcar nada como aprobado.
5. **Todo baja de nivel solo, nada sube solo.** Un incidente degrada la categoría automáticamente; subir requiere decisión de la persona.
6. **Fallar cerrado:** si falta una regla, un dato o el revisor no responde, la acción queda retenida y se escala.

## Puestos
| Puesto | Quién | Qué puede |
|---|---|---|
| Jefe de ventas | Persona (tú) | Aprueba o rechaza, fija topes y umbrales, sube o baja niveles, pausa todo, recibe el informe diario |
| Vendedor | Hermes, perfil `agente` | Capta, hace seguimiento y prepara cobranza con las 14 herramientas del MCP; escribe solo en la bandeja de pendientes |
| Revisor (desde nivel 3) | Agente de otro modelo (propuesto: Gemini vía Antigravity, o Hermes con otro modelo) | Solo lee el borrador y las reglas aplicadas; puede **frenar o escalar**, nunca aprobar fuera de lo que las reglas permiten. Su entrada se trata como texto no confiable |
| Compuerta de reglas | Código (no es un agente) | Lista de no contacto, topes, horario, formato, importes, frases prohibidas; decide qué categoría es cada acción |
| Auditor | Hermes y consejo de auditoría, aparte | Revisa **cambios de código**, no operaciones; no forma parte del flujo en producción |

## Catálogo de acciones y su categoría
| Acción | Hoy | Control |
|---|---|---|
| Leer correo, consultar seguimiento, evaluar cartera | Autónoma | Solo lectura; cuotas de lectura |
| Registrar lead, nota o tarea en HubSpot | Autónoma con tope | Cuotas 30/h y 100/d; remitente verificado por cabeceras; token sin borrar ni exportar |
| Borrador de recordatorio de cobranza | Genera borrador | La persona revisa y envía ella misma |
| Borrador de respuesta o cotización | Genera borrador | Igual |
| **Enviar** cualquier mensaje a un tercero | **No existe** | Requeriría una herramienta nueva con diseño y auditoría propios (ver "Envío" abajo) |
| Descuentos, condiciones fuera de catálogo, promesas | Nunca autónomo | Siempre persona |
| Disputas, quejas, bajas, datos personales, >30 días, monto >2000, 3 recordatorios | Siempre escala | Ya está en `cobranza.py` y en las reglas |

## Niveles de autonomía (por categoría)
- **Nivel 0 (hoy):** el agente solo prepara borradores; la persona decide y envía por su cuenta.
- **Nivel 1:** igual, pero con **bandeja de aprobación**: cada borrador entra como pendiente, la persona aprueba, edita o rechaza, y todo queda registrado con el hash. Sigue sin haber envío por el agente.
- **Nivel 2:** existe una herramienta de envío; cada mensaje lo aprueba una persona en la bandeja y solo entonces se envía. Requiere diseñar y auditar esa herramienta.
- **Nivel 3:** el revisor y la compuerta aprueban categorías de **bajo riesgo** definidas (p. ej. primer recordatorio amable, importe bajo, sin disputa, destinatario verificado y fuera de la lista de no contacto). Todo lo demás sigue a la persona, que además audita una muestra.
- **Nivel 4:** autonomía dentro de topes (importe, cantidad por día, horario, categorías), con la persona como excepción y con informe diario.

Se puede estar en niveles distintos por categoría: p. ej. captación en 4, recordatorios en 2, descuentos siempre en 1.

## Criterios de ascenso y descenso (propuesta)
- **Subir** una categoría un nivel cuando: al menos 50 casos en esa categoría en el nivel actual, 95% aprobados sin edición, 0 incidentes, y la persona lo decide en una confirmación explícita. Ningún ascenso automático.
- **Bajar** automáticamente un nivel (o a 0) ante: un mensaje aprobado que generó queja, un incumplimiento de regla, una violación de tope, un fallo de la compuerta o un hallazgo alto verificado en una auditoría. Reanudar requiere decisión de la persona.
- **Parada general:** una orden detiene todo (pausa de Hermes, parada de la pasarela, y si hace falta revocar el token de HubSpot).

## Siempre a la persona (nunca autónomo, en ningún nivel)
Disputas, quejas, bajas o solicitudes sobre datos personales, descuentos o condiciones fuera de catálogo, promesas legales o de plazo, importes sobre el tope, clientes marcados como sensibles, remitentes sin autenticación verificada, cualquier mensaje con enlaces o adjuntos que el agente no haya generado, y cualquier texto que la compuerta no sepa clasificar.

## Bandeja de aprobación
- Cola de archivos en `workspace/bandeja/` con carpetas `pendientes`, `aprobados`, `rechazados`, `enviados` (si existe envío). Cada ítem: id, tipo, categoría, nivel vigente, borrador, motivos y reglas aplicadas, hash del contenido, quién decidió, cuándo, caducidad.
- El agente solo puede crear en `pendientes`. Mover a otra carpeta lo hace un proceso fuera del contenedor, con la identidad de la persona.
- Una pantalla local o línea de comandos para revisar: ver borrador, contexto, regla que lo retiene, aprobar, editar y aprobar, rechazar con motivo.
- Caducidad: un pendiente viejo vuelve a revisión antes de enviarse (los datos de cartera cambian).
- Lo que alimenta la bandeja hoy: `workspace/borradores_cobranza/` y los futuros borradores de cotización y seguimiento.

## Envío (explícitamente fuera de este diseño, requiere el suyo)
No hay herramienta para enviar mensajes. Antes de nivel 2 hace falta un diseño aparte con: canal (SMTP o API), cuentas con permisos mínimos, límites diarios y horario, lista de no contacto obligatoria, texto fijado por hash tras la aprobación, registro, y auditoría independiente (consejo). Las ideas de Sellbird (`docs/REFERENCIA-SELLBIRD.md`) aplican aquí.

## Prerrequisitos antes de nivel 2 o superior
1. Lista de no contacto y bajas, aplicada a captación, correo y cobranza (hoy no existe).
2. Texto de terceros marcado como no confiable en lo que el MCP devuelve al modelo (asunto y extracto del correo).
3. Revisión previa determinista (formato, longitud, variables sin rellenar, enlaces, frases de presión y de pago, en español).
4. Medición real de consumo del agente (hoy no se puede medir en Hermes).
5. Pruebas reales con HubSpot e IMAP (hoy solo hay pruebas con simulaciones).

## Cadencia y informes
- **Tareas recurrentes** (lectura de correo, seguimientos, evaluación de cartera): las dispara un planificador. Opciones: cron de Hermes, o Paperclip si se adopta. Solo llaman herramientas de lectura y generan borradores.
- **Informe diario al jefe:** leads captados y descartados (con motivo), borradores pendientes y su antigüedad, cartera vencida, escalamientos, incidentes, cuotas usadas, cambios de nivel. Texto generado por código a partir del registro; el modelo no lo redacta.
- **Revisión semanal:** tasa de aprobación sin edición por categoría, para decidir ascensos.

## Dónde encaja Paperclip (opcional, después)
| Concepto de la jefatura | En Paperclip |
|---|---|
| Jefe | La "junta" |
| Vendedor / revisor | Agentes con adaptadores de Hermes y de CLI |
| Bandeja | Puertas de aprobación |
| Tareas recurrentes | Routines |
| Topes | Presupuestos (ciegos para Hermes mientras no se mida su gasto) |
El diseño no depende de Paperclip: la bandeja propia da lo esencial. Se evalúa aparte (leer su código y probarlo en entorno aislado).

## No objetivos
No implementa envío ni nuevas herramientas del MCP; no sustituye las reglas de `docs/GUARDRAILS.md`; no automatiza ascensos; no decide precios.

## Etapas propuestas
1. **Aprobar este diseño** y fijar umbrales (importe, cantidad, horario, categorías de bajo riesgo).
2. **Bandeja de aprobación mínima (nivel 1):** módulo local, fuera de `mcp/`, `agente/` y `sandbox/` si es posible; el MCP solo gana una operación "crear pendiente" (nuevo manifiesto, re-fijado, auditoría de Hermes).
3. **Prerrequisitos 1 a 3** (no contacto, texto no confiable, revisión determinista), cada uno con su diseño corto y auditoría.
4. **Informe diario** generado por código.
5. Ensayo real con HubSpot e IMAP cuando haya credenciales.
6. Diseño del envío y evaluación de nivel 2.

## Decisiones abiertas para ti
- ¿Eres el único jefe, o habrá varios (p. ej. cobranza y ventas separadas)?
- ¿Los umbrales de ascenso (50 casos, 95%, 0 incidentes) te parecen razonables?
- ¿Qué categorías consideras de bajo riesgo para el nivel 3?
- ¿Quién será el revisor: Gemini vía Antigravity, Hermes con otro modelo, o ninguno al principio?
- ¿Bandeja en pantalla local o por línea de comandos al inicio?

## No verificado
Que los umbrales produzcan buenas decisiones (sin datos reales); que un agente revisor detecte los casos hostiles (se ensayaría con el consejo); que Paperclip pueda ejecutar Hermes con el perfil `agente` y su terminal en Docker; que el consumo del agente se pueda limitar mientras no se mida.
