# Almacenamiento y canales: diseño (opción C = B ahora, A si hace falta)

Estado: BORRADOR para aprobación. Nada de esto está aplicado.

## Intención
Antes de emparejar un canal real (WhatsApp u otro), evitar que un cliente pueda llegar a datos de otro cliente a través del almacenamiento compartido del perfil `agente`. El usuario eligió "B ahora, A (un perfil por cliente) solo si algún cliente lo exige".

## Hallazgos de la recon (2026-11-23)
- Hermes monta en el contenedor las **cachés del perfil** (documentos, imágenes, audio, adjuntos, web, capturas, delegación, skills) en solo lectura (`get_cache_directory_mounts`, `docker.py`). Son por **perfil**, no por conversación: todos los clientes de un canal las compartirían.
- **El hueco es real y mayor de lo documentado:** `hermes -p agente tools list --platform whatsapp` muestra habilitados `terminal`, `file`, `web`, `browser`, `code_execution`, `vision`, `image_gen`, `tts`, `skills`, `memory`, `session_search`, `connections`, `delegation`, `cronjob` y `computer_use`. El recorte hecho antes se aplicó solo a `cli`. Un mensaje externo tendría el conjunto completo.
- **El MCP no puede aislar por conversación:** habla por stdio y no recibe la identidad de la conversación. Cualquier conversación puede llamar `crm_buscar_contacto` o `cobranza_evaluar` con el email o el id de otro cliente. Lo único que lo limita hoy es que esas herramientas devuelven lo mínimo (existe/id, etapa) y los topes de tasa y de lecturas.

## Objetivos
- Un canal de mensajería del perfil `agente` solo puede usar las herramientas del MCP `agente-local` (más `clarify`): sin terminal, archivos, web, navegador, ejecución de código, visión, generación de imágenes, voz, skills, memoria, búsqueda de sesiones, conexiones, delegación, cron ni computer_use.
- Comprobarlo con `hermes tools list --platform <p>` para cada plataforma y dejarlo en un verificador repetible.
- Documentar con honestidad lo que sigue abierto.

## No objetivos
- Emparejar ningún canal real. Crear perfiles por cliente (opción A). Cambiar el perfil `default`.
- Aislamiento por conversación dentro del MCP (no es posible sin identidad de Hermes).

## Antiobjetivos
- Dar por cerrado el riesgo porque la lista de herramientas esté recortada: el recorte lo aplica la configuración, no el kernel; un cambio de Hermes o de configuración lo deshace.

## Enfoques (ya elegido C)
- **B (ahora):** recorte de herramientas por plataforma en el perfil `agente` + verificador + documentación. Barato, reversible (copia de `config.yaml`).
- **A (después, si un cliente lo exige):** un perfil por cliente con cachés propias. Fuerte pero caro en RAM (7,7 GB) y sin enrutamiento por conversación comprobado.

## Diseño
1. **Inventario:** listar las plataformas que Hermes conoce para el perfil `agente` (`hermes -p agente tools list --platform <p>` para cada una; las plataformas salen de la ayuda o la configuración de Hermes).
2. **Recorte (con permiso, copia previa de `profiles\agente\config.yaml`):** `hermes -p agente tools disable <toolset> --platform <p>` para todos los toolsets salvo `clarify` y el MCP, en cada plataforma de mensajería (no `cli`, que ya está recortada). Se aplica en el perfil `agente`; `default` queda intacto.
3. **Verificador `aislamiento/verificar_canales.py`** (fuera de `mcp/agente/sandbox`: el pin rechaza `.py` nuevos ahí): ejecuta `tools list` por plataforma y falla si algún toolset fuera de la lista blanca aparece habilitado; con pruebas de lógica sobre salidas simuladas (incluida salida vacía o ilegible = falla cerrado).
4. **Gateway:** tras el recorte, reiniciar el gateway (con permiso) y comprobar de nuevo. No se empareja ningún canal.
5. **Documentación:** `docs/AISLAMIENTO.md` y `docs/GUARDRAILS.md`: qué se cerró, qué sigue abierto (el MCP sin identidad de conversación; las cachés siguen siendo compartidas por perfil, solo que ningún canal tiene cómo leerlas; adjuntos que el propio Hermes inyecte en el contexto de una conversación; cambios futuros de Hermes).
6. **Regla operativa:** no emparejar un canal hasta que el verificador dé CORRECTO y una persona lo haya revisado.

## Pruebas y verificación
- Tests del verificador con salidas simuladas de `hermes tools list` (todo bien, un toolset habilitado de más, plataforma desconocida, salida vacía, error del comando).
- Ejecución real del verificador sobre el perfil `agente` antes y después del recorte (el "antes" debe fallar, el "después" debe dar CORRECTO).
- Auditoría de Hermes del verificador y de la documentación.
- **No se puede probar de extremo a extremo** sin un canal emparejado: lo que se verifica es la configuración, no el comportamiento de un mensaje real.

## Pendiente para el constructor / decisiones mías
- Qué plataformas se recortan: todas las que `hermes tools list` acepte salvo `cli`; la lista exacta la fija el inventario.
- Si `clarify` se mantiene (propuesto sí: no toca archivos ni red).
- Si el MCP deberá recibir algún día la identidad de la conversación (cambio grande; fuera de este alcance).
