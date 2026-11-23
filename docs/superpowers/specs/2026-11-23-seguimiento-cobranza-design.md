# Seguimiento y cobranza (MVP): diseño

Estado: BORRADOR para aprobación. Nada de esto está construido. Lo propuse yo sin entrevista (el usuario pidió "propón un diseño directamente"): las decisiones son mías y corregibles.

## Intención
Completar el flujo consulta → cotización → **seguimiento → cobranza**. El agente detecta qué cotizaciones se enfriaron y qué facturas están atrasadas, y **prepara borradores de mensaje** para que una persona los revise y los envíe. Mismo principio que el resto del proyecto: el código decide y calcula, el modelo no inventa cifras ni plazos.

## Objetivos
- Seguimiento: listar cotizaciones en borrador sin decisión tras N días y preparar un recordatorio.
- Cobranza: dado el estado de una factura, decidir la etapa (amable, firme, escalar) con reglas deterministas y generar el mensaje con plantillas fijas y cifras calculadas por el código.
- Dejar el resultado como borrador local y, si HubSpot está activo, como tarea (`crm_crear_tarea`).
- Escalar a una persona lo que el agente no debe manejar: disputas, montos altos, reclamos, tercera insistencia.

## No objetivos (MVP)
- Enviar mensajes, cobrar, mover dinero, generar enlaces de pago, ni aceptar promesas de pago o planes de cuotas.
- Recibir datos bancarios o de tarjeta (jamás en el CSV ni en las herramientas).
- Contactar a terceros (familiares, empleadores) ni publicar nada.
- Leer facturas desde un sistema de facturación o desde HubSpot (queda para una etapa posterior).

## Antiobjetivos
- Que el agente amenace, mienta, presione fuera de horario o prometa algo (descuentos, quitas, plazos).
- Que un cliente con una disputa abierta o ya pagado reciba un recordatorio.
- Que el modelo cambie un monto o una fecha.

## Restricciones
- No hay fuente de deuda hoy: la fuente del MVP es un **CSV que carga una persona** en `workspace/cartera/cartera.csv`. Sin esa persona, el agente no sabe nada.
- Reclamar dinero a personas tiene reglas legales (trato, horarios, protección de datos) que dependen del país; el MVP las trata con reglas conservadoras y deja el cumplimiento al operador.
- El MCP tiene pin: cada cambio exige re-fijar y reiniciar el gateway. Solo biblioteca estándar.

## Enfoques considerados
- **A. Motor de etapas determinista + plantillas fijas en código (ELEGIDO).** El código calcula días de atraso, etapa y monto; el mensaje sale de plantillas revisadas por una persona; el modelo solo elige qué factura procesar y recibe el resultado. Coste: menos "natural"; ventaja: auditable, sin promesas ni tono impredecible, barato en tokens.
- B. El modelo redacta libremente a partir de los datos. Más fluido, pero puede amenazar, prometer o equivocar cifras; riesgo legal y de reputación. Descartado.
- C. HubSpot como fuente de facturas. Evita el CSV, pero exige objetos de facturación y permisos adicionales de la API, sin verificar y sin cuenta de prueba aún. Para una etapa posterior.

## Diseño
**Datos (CSV cargado por una persona, ≤2000 filas, ≤1 MB, solo `.csv`):** `factura_id`, `cliente_ref` (alias corto, sin datos personales), `email`, `monto`, `moneda`, `fecha_vencimiento` (ISO), `estado` ∈ {pendiente, pagada, disputa, anulada}, `recordatorios_enviados` (lo actualiza la persona al enviar). Todo se valida con Decimal y fechas ISO; una fila inválida se rechaza entera y se informa cuál.

**Herramientas nuevas (3):**
1. `seguimiento_pendientes()`: lista (máx. 20) los borradores de cotización con más de N días sin cerrar (N=3), con su id, total y antigüedad. Solo lectura.
2. `cobranza_evaluar(factura_id)`: devuelve días de atraso, etapa y si requiere persona. Etapas: `al_dia` (no vence), `amable` (1–7 días), `firme` (8–30), `escalar` (más de 30, o monto sobre el umbral, o `disputa`, o ya 3 recordatorios). Facturas pagadas, anuladas o en disputa: nunca producen mensaje.
3. `cobranza_preparar(factura_id)`: si la etapa es `amable` o `firme`, genera el mensaje con plantilla fija (monto, moneda, fecha y atraso calculados por el código; sin amenazas, sin promesas, con una línea para pedir que respondan si ya pagaron o hay un problema, e indicación de cómo dejar de recibir recordatorios) y lo guarda como borrador en `workspace/borradores_cobranza/` (nunca enviado). Con HubSpot activo, el skill crea además una tarea con `crm_crear_tarea`.

**Reglas de seguridad (mis decisiones):**
- Máximo 1 recordatorio por factura cada 7 días y 3 en total; luego `escalar`. El conteo sale del CSV, no del modelo.
- Los datos personales no vuelven al modelo: las herramientas devuelven `factura_id`, montos y etapa; el email solo se usa dentro del borrador guardado.
- El texto de la plantilla es código revisado, no editable por el modelo; `escalar_a_humano` para disputas y reclamos del cliente.
- Tope de borradores (2000 archivos) como el resto de bandejas; registro de auditoría con la etapa decidida y sin PII.
- Pin, Compuerta y límite de tasa como las demás herramientas.

**Skills nuevos:** `seguimiento` y `cobranza` (instrucciones y reglas: no inventar cifras, no prometer, escalar ante disputa, no enviar). Se instalan en Hermes solo con permiso.

**Pruebas:** tests del motor de etapas con fechas fijas (bordes 0, 1, 7, 8, 30, 31 días), montos Decimal, estados que bloquean mensaje, límite de 3 recordatorios, CSV inválido (columnas, decimales, fechas, tamaño, inyección de fórmulas `=`/`+`/`@` en celdas), que las plantillas no admitan texto libre y que no se filtre el email. Luego auditoría de Hermes.

## Etapas
1. Lector de cartera + motor de etapas + plantillas + tests.
2. 3 herramientas en el MCP + skills + documentación, pin re-fijado.
3. Auditoría de Hermes; corregir.
4. (Fuera del MVP) Fuente de facturas real, canal de envío con aprobación humana, cumplimiento legal por país.

## Pendiente para el constructor / decisiones mías
- Umbral de monto para escalar y plazos de las etapas (7/30 días, 3 recordatorios): propuestos, el operador debe ajustarlos a su negocio.
- Texto exacto de las plantillas en español neutro: lo redacto yo; una persona debe revisarlo (tono y requisitos legales de su país).
- Dónde se guarda el CSV y cómo lo actualiza la persona al enviar un recordatorio (campo `recordatorios_enviados` a mano).
- No verificado: que el consentimiento de contacto exista; nada lo comprueba.
