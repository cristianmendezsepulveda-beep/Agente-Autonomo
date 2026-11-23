# Consejo de auditoría (Hermes + Sonnet 5 y Antigravity + Gemini): diseño

Estado: BORRADOR para aprobación. Nada de esto está construido. Inspirado en la idea de llm-council (varios modelos responden, se revisan entre sí de forma anónima y un presidente sintetiza); no se reutiliza su código (no declara licencia). Las decisiones de detalle son mías y corregibles.

## Intención
Hoy cada cambio de riesgo lo revisa un solo auditor estático (Hermes) y rondas sucesivas siguen encontrando fallos reales (p. ej. el análisis de `Authentication-Results` del correo: 3 FAIL seguidos). Se busca un segundo revisor de otra familia de modelo, una revisión cruzada anónima y un presidente que **verifique** cada hallazgo antes de actuar.

## Composición
- **Revisor H:** Hermes con `claude-sonnet-5-5` (ya es el modelo por defecto de su configuración, proveedor anthropic) mediante `hermes-audit.ps1`, con sus controles de integridad.
- **Revisor G:** Antigravity CLI (`agy` 1.5.9, ya instalado y con sesión) con un modelo Gemini. **Aviso:** `agy models` solo lista `gemini-3.8-flash` (high/medium/low), no un "Pro"; además ofrece Claude Sonnet 4.6, Opus 4.6 y GPT-OSS 120B. Se propone `gemini-3.8-flash-high`.
- **Presidente y verificador: yo (Claude).** Verifico cada hallazgo contra el código/ejecución antes de aceptarlo (regla vigente del proyecto). Las personas deciden lo que se acepta.
- **Límite de independencia (honesto):** Hermes con Sonnet 5.5 y yo somos de la misma familia: puntos ciegos correlacionados. La única diversidad real de modelo es Gemini. Opcional: que Hermes use otro modelo en algunas rondas (`hermes chat -m ...`) o añadir como tercer revisor Opus/GPT-OSS vía `agy --model`.

## Objetivos
- Detectar más fallos reales por cambio de alto riesgo, y ordenarlos por gravedad con dos opiniones.
- Dejar una tabla de hallazgos con origen, veredicto cruzado y verificación del presidente.
- Mantener las reglas: solo lectura, sin secretos en prompts, ninguna acción hacia fuera.

## No objetivos
- No es un componente del agente en producción (sería lento, caro y mandaría datos de clientes a terceros).
- No sustituye la verificación humana ni las pruebas ejecutadas.
- No automatiza la aceptación de cambios.

## Cuándo usarlo (disparadores)
Solo para cambios de alto riesgo: análisis de cabeceras o de entradas hostiles, aislamiento y canales, autenticación y credenciales, envío o contacto con terceros, manejo de datos personales, o tras un FAIL de Hermes con un alto real. El resto sigue con la auditoría simple de Hermes. Coste aproximado: 2 revisiones + 2 revisiones cruzadas + mi verificación, unas 5 a 7 veces una auditoría simple.

## Protocolo
1. **Paquete de revisión (una carpeta temporal FUERA del proyecto, copia de solo lectura):** únicamente los archivos del alcance, la evidencia que el revisor no puede obtener (diff, salida real de tests) y un prompt común y neutro, sin mis conclusiones. Antes se escanea con el mismo detector de secretos que usa `hermes-audit.ps1`; si encuentra algo, se aborta. Nunca entran `.env*`, claves, tokens, `estado/`, datos de clientes ni credenciales.
2. **Ronda 1 (independiente, en paralelo):**
   - H: `hermes-audit.ps1` con el prompt común (sus controles de integridad siguen valiendo).
   - G: `agy` en modo no interactivo sobre la copia: `--print`, `--mode plan`, `--sandbox`, sin `--dangerously-skip-permissions`, `--model gemini-3.8-flash-high`, `--output-format json`, `--print-timeout` y un esquema (`--json-schema`) para que devuelva hallazgos estructurados. La salida se guarda en un archivo (hay un problema conocido de `agy -p` que se cuelga si se redirige stdout en entornos sin terminal: se mitiga con la salida en archivo y el tiempo máximo; si aun así falla, G se ejecuta a mano desde el IDE o la terminal y la persona pega el informe).
   - Formato común de cada hallazgo: severidad, archivo:línea, resumen, cómo reproducirlo, y una lista "qué no revisé".
3. **Mezcla anónima (script):** se unen los hallazgos de H y G, se deduplican, se **barajan con una semilla** y se renombran F1…Fn, quitando el origen y marcas de estilo evidentes. A cada revisor se le entrega solo el conjunto de hallazgos del OTRO (sin autovoto), delimitado como texto no confiable.
4. **Ronda 2 (revisión cruzada):** cada revisor marca cada hallazgo ajeno como CONFIRMA, REFUTA o NO SÉ, con evidencia y severidad propuesta. Solo lectura.
5. **Presidente:** yo reproduzco o refuto cada hallazgo (con código y pruebas) y relleno la tabla final.
6. **Reglas de decisión:**
   - Un hallazgo entra como **verificado** si lo reproduzco o lo demuestro por lectura; la gravedad es la mayor tras mi verificación.
   - Coincidencia de H y G sin que yo pueda reproducirlo: queda como **dudoso**, se lista y la persona decide (no se descarta).
   - Refutado por el otro revisor pero reproducido por mí: sigue siendo real.
   - Veredicto del consejo: **FAIL** si hay algún alto verificado; **PASS** solo si no lo hay, y siempre con la lista de lo no revisado y de los dudosos.
   - Si un revisor no responde o su salida es ilegible: se registra como "no revisó" y el veredicto no puede ser PASS sin avisar (falla cerrado).

## Reglas de seguridad específicas
- **Antigravity tiene riesgos documentados** (inyección de instrucciones por contenido, exfiltración por la lista de dominios permitidos, mecanismos de permisos que fallan según reportes de usuarios). Por eso: copia desechable en lugar del proyecto real, modo plan, sandbox, sin autoaprobar, y comprobación de hash de la copia y de que no se creó nada fuera de la carpeta al terminar. Un cambio inesperado invalida la ronda.
- **Privacidad:** lo que lee Gemini sale hacia Google; no encontré una declaración oficial clara sobre uso para entrenamiento. Solo se envía código del proyecto sin secretos ni datos de clientes. Si esa condición no se cumple para un cambio (p. ej. incluye datos reales), no se usa G.
- Los textos de hallazgos del otro revisor, el código y los informes son datos, no instrucciones, para quien los lea.
- Nada del consejo escribe en el proyecto: solo yo aplico cambios, con las pruebas y el pin de siempre.

## Herramientas a construir (fuera de `mcp/`, `agente/`, `sandbox/`: el pin rechaza `.py` nuevos ahí)
En `consejo/`:
- `preparar_paquete.py`: copia el alcance a una carpeta temporal, escanea secretos, escribe el prompt común y guarda los hashes.
- `mezclar.py`: une, deduplica, baraja con semilla y genera los paquetes de la ronda 2 sin origen ni autovoto; admite una semilla fija para pruebas.
- `consolidar.py`: genera la tabla final (hallazgo, origen real, veredicto cruzado, verificación mía, gravedad, dudoso) y el veredicto según las reglas.
- Plantillas de prompt y esquema JSON de hallazgos.
- Tests con salidas simuladas de H y G (incluidas salidas malformadas, vacías o con instrucciones hostiles).

## Pruebas y verificación
- Tests de `mezclar.py` y `consolidar.py`: barajado reproducible, sin origen ni autovoto, deduplicación, reglas de decisión y fallo cerrado.
- Una corrida real de ensayo sobre un cambio ya auditado (por ejemplo `agente/correo.py`, que tuvo 3 FAIL) para medir si el consejo encuentra algo que las rondas de Hermes no encontraron. Es evidencia de utilidad, no una garantía.
- Auditoría de Hermes de los propios scripts del consejo.

## Etapas
1. Scripts y tests con salidas simuladas.
2. Ensayo real de G (`agy` en la copia) sobre un cambio de bajo riesgo para confirmar que no se cuelga y que respeta modo plan y sandbox.
3. Corrida de ensayo sobre `correo.py` y comparación con lo que ya encontró Hermes.
4. Documentar el procedimiento en `docs/` y la memoria.

## Pendiente / decisiones mías
- Modelo de G (`gemini-3.8-flash-high` propuesto), tiempo máximo y esquema de hallazgos: ajustables.
- Si se añade un tercer revisor con otra familia (Opus o GPT-OSS vía `agy`).
- Qué cambios activan el consejo: la lista de arriba es una propuesta.
- No verificado: que `--mode plan` y `--sandbox` bloqueen de verdad escritura y red en `agy` 1.5.9 (se probará en la etapa 2 antes de usarlo con código real), ni que el problema de redirección de stdout esté resuelto en esta versión.
