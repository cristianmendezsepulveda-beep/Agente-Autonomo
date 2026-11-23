# Vendedor autónomo con HubSpot (MVP): diseño

Estado: BORRADOR para aprobación. Nada de esto está construido.

## Intención
El agente local pasa de contestar y cotizar a **captar clientes y registrarlos en HubSpot**, y a **preparar prospección (SDR)**. El objetivo final es un vendedor 100% autónomo en producción; **ahora es un MVP** y se prueba solo en una cuenta de desarrollador de HubSpot con datos sintéticos.

## Objetivos
- Cada consulta o cotización entrante genera, sin intervención humana, un contacto, un trato y una nota en HubSpot, sin duplicados.
- La prospección en frío produce tareas y borradores dentro de HubSpot para una persona. No se envía nada.
- El token de HubSpot nunca entra al contenedor ni a los prompts.

## No objetivos (MVP)
- Enviar correos, WhatsApp o llamadas. Cobrar. Borrar o exportar datos del CRM.
- Producción real y datos de clientes reales.
- Buscar prospectos por internet o scraping: los prospectos los carga una persona (CSV).
- Cualquier canal emparejado (WhatsApp, etc.).

## Antiobjetivos (fracaso aunque "funcione")
- Que el modelo pueda borrar, exportar o leer masivamente el CRM.
- Que el token aparezca en un prompt, log, informe o carpeta montada.
- Contactar a una persona sin su consentimiento.

## Restricciones
- El agente corre en un contenedor sin red: todo acceso a HubSpot pasa por el MCP `agente-local` en el host, que es la única vía hacia afuera.
- El MCP tiene pin de hashes, Compuerta, presupuesto y registro de auditoría. Cada cambio exige re-fijar el pin y reiniciar el gateway.
- Solo biblioteca estándar de Python (sin dependencias de terceros).
- Contactos en frío: riesgo legal (consentimiento y protección de datos), por eso solo borradores.

## Enfoques considerados
- **A. Herramientas `crm_*` fijas en `agente-local` (ELEGIDO por el usuario):** control total de campos, volumen y auditoría; coste: código propio que mantener.
- B. MCP oficial de HubSpot en el perfil `agente`: menos código, pero expone borrar/exportar y es una dependencia de terceros sin límite por campos.
- C. Solo el conector de Claude: no es autónomo; se usa únicamente para configurar la cuenta e inspeccionar resultados.
- Se descartó B por el riesgo de cadena de suministro y la imposibilidad de limitar herramientas. C queda como apoyo, no como solución.

## Diseño
**Herramientas nuevas (solo estas):**
1. `crm_buscar_contacto(email)`: devuelve id, nombre, empresa, etapa. Nunca devuelve listas ni más de 1 resultado.
2. `crm_registrar_lead(nombre, empresa, email, telefono?, origen, resumen)`: crea o actualiza el contacto (clave: email, sin duplicados) y crea un trato en la etapa "Nuevo". Idempotente.
3. `crm_registrar_nota(email, texto)`: añade una nota a un contacto existente.
4. `crm_crear_tarea(email, tipo, borrador)`: crea una tarea para una persona. `tipo` ∈ {seguimiento, contacto_frio, revisar_cotizacion}. El borrador queda en el cuerpo; no se envía.

**Seguridad (mi decisión, corregible):**
- Token en un archivo fuera del proyecto (`%LOCALAPPDATA%\agente-local\hubspot.token`) que crea el usuario; el servidor lo lee al arrancar y nunca lo imprime. Una variable de entorno es la alternativa.
- HubSpot "private app" con alcances mínimos: leer/escribir contactos, tratos y notas/tareas. **Sin alcances de borrado ni exportación**: ese es el control real, no el código.
- Salida de red solo a `api.hubapi.com` por HTTPS, fijado en el código (no en la política). Sin redirecciones; tiempo máximo de espera; tamaño de respuesta acotado.
- Lista blanca de campos y longitudes máximas; los textos del cliente se truncan y se limpian de caracteres de control. Aviso: lo que escribe el modelo lo leerán personas en HubSpot (inyección hacia el operador), igual que `escalar_a_humano`.
- Tope propio: 100 escrituras/día y 30/hora; se registra cada llamada (sin token ni cuerpo completo).
- Si HubSpot falla, la herramienta lo dice y el skill escala a persona; nunca reintenta en bucle.

**Skill nuevo `captacion`:** instruye al modelo a registrar el lead tras una consulta o cotización, y a crear tarea (no enviar) para prospección. Los datos personales solo viajan a HubSpot, no a `workspace/`.

**Pruebas:** servidor HTTP falso local que imita la API de HubSpot (sin llamadas reales en tests); tests de lista blanca, duplicados, límites, token ausente y fallo de red. Después, prueba real mínima en la cuenta de desarrollador y auditoría de Hermes.

## Etapas
1. Herramientas CRM + skill + tests con servidor falso.
2. Prueba real en cuenta de desarrollador (el usuario crea la cuenta y el token; yo no toco credenciales).
3. Auditoría de Hermes; corregir.
4. (Fuera del MVP) Producción: requiere resolver almacenamiento por conversación, consentimiento, y revisión humana del pin.

## Pendiente para el constructor / decisiones mías
- Nombre exacto de etapas y propiedades personalizadas de HubSpot: las configuro con el conector de Claude cuando exista la cuenta (mi decisión).
- Dónde guardar el token (archivo vs variable): propuesto archivo; el usuario puede cambiarlo.
- Cómo se cargan los prospectos (CSV en `workspace/prospectos/`): propuesto; sin scraping.
- No verificado: que el gateway multiplexado pase bien las 4 herramientas nuevas (se reinicia el gateway tras cambiar la lista).
