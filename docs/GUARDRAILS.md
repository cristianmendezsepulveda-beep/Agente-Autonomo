# Guardarraíles (pruebas cerradas y locales)

- Todo corre local, SALVO el backend `hermes` del simulador y, desde la Fase C (sin activar), la salida del MCP a `api.hubapi.com` (ver Fase C; el "egress denegado" vale para el contenedor, no para el MCP en el host): envía datos de clientes sintéticos al proveedor del modelo y no pasa por `Compuerta.red`. Solo se usa con `--backend hermes` explícito. Nada se publica, envía ni cobra sin aprobación humana explícita.
- Egress denegado por defecto; credenciales nunca dentro del sandbox ni en estos archivos.
- Topes de tokens y acciones (`sandbox/budget_guard.py`): son duros solo sobre lo que el llamador declara; no miden el consumo real. Parada de emergencia: `hermes pause`.
- MCP y skills: solo locales y revisados; sin dependencias de terceros sin auditar.
- Los 100 clientes son SINTÉTICOS: señal blanda para ordenar segmentos, no demanda real ni precios absolutos.

## Alcance real de los controles (actualizado 2026-11-22)
- `sandbox/` es una **capa de política voluntaria**: solo controla lo que pase por `Compuerta`. El terminal propio de Hermes
  NO pasa por ella, así que no impide nada por sí sola. No es aislamiento.
- Lo que hace: lectura confinada al proyecto, escritura solo en `workspace/` (sin extensiones ejecutables), bloqueo de rutas
  sensibles (todos los componentes), tope de tokens/acciones con bloqueo entre procesos, y aprobación humana para
  enviar/publicar/cobrar/borrar/red/ejecutar. `ejecutar` está bloqueado: permitir `python` equivale a código arbitrario.
- El registro con cadena de hashes **detecta ediciones torpes o accidentales**; no resiste a quien pueda reescribirlo entero
  ni detecta que lo trunquen. Haría falta HMAC con clave fuera del alcance del agente y un ancla externa.
- El presupuesto solo cuenta lo que el llamador declara. `simulador.py --backend hermes` cobra una estimación antes de cada lote.
- Límite conocido: TOCTOU entre comprobar y abrir una ruta. La política y el estado deben vivir fuera de `workspace/`.
- **Aislamiento real (actualizado 2026-11-22):** SÍ hay aislamiento para el perfil `agente` de Hermes: terminal y herramientas de
  archivos corren en un contenedor Docker (WSL2) sin red, con tope de memoria/procesos y solo 14 montajes del host de solo
  lectura. Detalle, evidencia y huecos en `docs/AISLAMIENTO.md`. (Lo que antes decía este punto, "virtualización desactivada", era
  un diagnóstico erróneo: faltaba el componente Virtual Machine Platform.) NO es absoluto, y el perfil `default` de Hermes, el
  MCP en el host y `sandbox/` siguen siendo capas de política sin contenedor.
- **Perfil `agente` de Hermes** (`hermes -p agente chat -s consultas,cotizaciones`, con el PATH de Docker refrescado): solo terminal,
  file, todo, clarify + MCP `agente-local`; web, browser, computer_use, cronjob, memory, delegation, skills y demás deshabilitados
  (plataforma `cli`; desde 2026-11-23 los canales de mensajería del perfil `agente` solo tienen `clarify` y el MCP: verificar con
  `python aislamiento/verificar_canales.py`, ver `docs/AISLAMIENTO.md`). Verificar con `python aislamiento/verificar_aislamiento.py --inspeccionar <contenedor>` (fase 2).
- El ranking sintético es señal blanda: ordenar hipótesis, no medir demanda. Con n bajo por sector no es concluyente.
- Backend `hermes` (ejecutado con los 100 clientes sintéticos; el gasto real de tokens NO se pudo medir): usa `-t todo` (sin terminal, navegador, archivos ni código), `--ignore-rules`
  (sin memoria/skills inyectados), `--max-turns 1` (`--safe-mode` y `--run-budget` no sirven en hermes 0.23.2); cobra estimación + 15.000 tokens de overhead SIN calibrar, así que el gasto real puede exceder lo cobrado;
  se corta tras 3 lotes fallidos seguidos. `hermes pause` como parada de emergencia no está verificado.
- Bloqueos huérfanos: se retiran tras 30 s, retiro no atómico (riesgo bajo en Windows, no probado; en POSIX habría carrera).
- Escritura: lista blanca estricta (.txt .json .md .csv) y rechazo de `~`, `token`, `secret` aunque a veces sobrebloquee.

## Fase B: consultas y cotizaciones (MCP local `mcp/servidor.py`, skills en `skills/`)
**Estado real (2026-11-22):** el MCP `agente-local` SÍ está registrado en la configuración global de Hermes
(4 herramientas) y los skills `consultas` y `cotizaciones` SÍ están instalados en `%LOCALAPPDATA%\hermes\skills\agente-local\`.
Hay una superficie expuesta: cualquier sesión de Hermes (y cualquier plataforma emparejada del gateway) puede usarlas.
- **Qué hace el servidor** (4 herramientas de consultas/cotización; desde 2026-11-23 el código tiene 4 `crm_*` más, ver Fase C): FAQ de un catálogo fijo (`consultar_catalogo`), lista de SKU y precios en solo
  lectura (`listar_catalogo`, sin descuentos ni umbrales: hace falta para que el modelo pueda cotizar), cotizaciones con
  aritmética decimal que quedan como BORRADOR (`cotizar`), y escalados para una persona (`escalar_a_humano`). Nunca envía nada. Texto fijo de salida: un falso negativo solo cambia el enrutamiento.
- **`agente/consultas.py` es una ayuda de enrutamiento, NO un control de seguridad.** Lista blanca de caracteres, patrones con
  límite de palabra, números largos, correos. No cubre sustituciones arbitrarias ("ign9ra") ni símbolos raros; escala de más
  a propósito. Hermes ya leyó el texto crudo antes de llamar al servidor.
- **Pin (`mcp/manifiesto.sha256`)**: hashes del manifiesto de herramientas, de servidor/políticas/agente/sandbox/catálogo y
  detección de archivos o carpetas nuevos en el código; se verifica antes de importar. Los `.pyc` existentes se ignoran
  (`sys.pycache_prefix` apunta a un directorio temporal NUEVO y vacío en cada arranque, borrado al salir). **Quien pueda editar código y pin puede re-fijarlo** (`--fijar` pide escribir FIJAR). Lo fijó el
  autor (Claude) por stdin tras cada cambio: conviene que una persona revise y lo vuelva a fijar. No cubre `estado/` ni
  paquetes del sitio de Python; lanzar con `python -I`.
- **Escalados:** compuerta, presupuesto, registro y límite de tasa propios; si el estado general (`estado/agente`) está
  corrupto el servidor arranca igual y solo `escalar_a_humano` funciona. Guardan fecha, origen, motivo y referencia
  (obligatoria; se rechaza si trae 9+ dígitos, correo o caracteres de control), NO el mensaje del cliente. La
  independencia es de ejecución: un `estado/agente/escalados` corrupto sí impediría escalar. Si fallan por error de
  herramienta la respuesta empieza por "ESCALADO NO REGISTRADO"; los errores de protocolo o un servidor caído no llevan
  ese prefijo: el SKILL dice que solo cuenta como registrado lo que devuelve `"escalado": "e-…"`. Una persona los
  gestiona con `python agente/bandeja.py listar|cerrar <id>`; esa herramienta no pasa por la Compuerta ni deja registro
  de auditoría (solo el campo `cerrado` del archivo).
- **Cotizaciones:** el código acepta cualquier descuento de 0 a 100 y solo lo marca para aprobación; no impide fraccionar
  un pedido para quedar bajo el umbral.
- **Topes:** `mcp/policy.json` fija 100.000 acciones de por vida (sin reinicio automático), 120 llamadas/min por proceso
  y 2.000 archivos por bandeja. No limitan el gasto de tokens de Hermes.
- Sin rotación del registro de auditoría; se verifica entero al arrancar.
- Compatibilidad con la revisión MCP 2026-07-28 NO verificada (se implementa el protocolo clásico initialize/tools).
- **La política de descuentos NO es indeducible.** `cotizar` ya no devuelve cifras (los `motivos` son genéricos y los números
  van solo a `detalle_operador` del borrador), pero `requiere_aprobacion_humana` sigue siendo una respuesta sí/no: subiendo
  `descuento_pct` por bisección (≈7 llamadas con un SKU) quien llame deduce el máximo exacto; igual con el umbral de total
  (el `total` también se devuelve) y con el máximo de líneas. Solo lo frenan el límite de tasa y los topes de acciones.
  Cerrarlo de verdad exigiría que `cotizar` no revele qué regla se disparó, o sacar descuentos y umbrales del alcance del
  modelo. El modelo no lo hizo en la prueba real, pero eso no es un control.
- **El borrador no está oculto al modelo, salvo en el perfil `agente`:** `detalle_operador` queda en `workspace/borradores/*.json`,
  legible por las herramientas de archivos de un perfil de Hermes que NO esté en contenedor (p. ej. `default`, que corre en el
  host). En el perfil `agente` sus herramientas de archivos operan dentro del contenedor y, por inferencia, no ven `workspace\` del
  host: se probó que NO pudo leer `C:\...\docs\GUARDRAILS.md` ("File not found") y que SÍ leyó `/etc/os-release` del contenedor, pero
  no se probó directamente con `workspace\borradores`. "No se devuelve por el MCP" es lo garantizado en todos los casos.

## Fase C: captación y CRM (HubSpot) — CONSTRUIDA, NO ACTIVADA (2026-11-23)
**Estado real:** el código (`agente/crm.py`, 4 herramientas `crm_*` en `mcp/servidor.py`, skill `skills/captacion/`) está escrito y probado
**solo contra un servidor HubSpot falso local** (31 tests). Nunca se ha llamado a HubSpot real, no hay token ni cuenta, el skill NO está
instalado en Hermes y el gateway NO se reinició (Hermes sigue mostrando las 4 herramientas antiguas desde el gateway). OJO: el MCP se
lanza desde disco, así que una sesión NUEVA de `hermes chat` ya vería las `crm_*`; sin token fallan con `CrmNoConfigurado`, y en cuanto
exista el token quedan activas en toda sesión que cargue el MCP (el registro es global, también en el perfil `default`).
**Auditoría de Hermes (2026-11-23):** PASS condicionado (solo estático, un único auditor, sin ejecutar nada). Corregidos 1–5, 8, 9
(parcial), 10–13 y 16: registro de intento y error, reserva del lote de escrituras (un lead nuevo no queda a medias por cuota; si falla un
paso posterior el error dice "LEAD A MEDIAS"), tope propio de búsquedas (60/h, 300/día), `crm_buscar_contacto` devuelve solo `existe` e id,
HTML escapado y URLs neutralizadas en textos libres, sin proxies del entorno, nombre en blanco, contadores inválidos fallan cerrado.
- **Qué hace:** busca un contacto por email exacto, registra un lead (contacto + trato + nota; si el email ya existe solo añade una nota,
  sin pisar datos ni crear otro trato), añade notas y crea TAREAS con borradores para una persona. No envía nada.
- **El control real es el token**, no el código: debe ser una "private app" de HubSpot sin alcances de borrado ni exportación. Si el
  token los tiene, el código no puede impedir que se usen por otra vía. El código solo expone 4 operaciones fijas.
- **Token:** archivo `%LOCALAPPDATA%\agente-local\hubspot.token` (fuera del proyecto, lo crea la persona; se lee en el primer uso y se
  cachea). El servidor corre en el host; el token nunca entra al contenedor del perfil `agente`. No se imprime ni registra; los errores no
  llevan cuerpo de respuesta. **Pero el perfil `default` corre en el host con terminal y archivos: una sesión `default` que procese texto
  no confiable podría leer ese archivo.** Mitigar: ACL del archivo solo para tu usuario (lo hace la persona) y no usar `default` con
  entrada no confiable.
- **Red:** el MCP pasa a tener una salida real a `api.hubapi.com` (HTTPS, sin redirecciones, 10 s, respuesta ≤200 KB); el host se fija
  en el código, no en `policy.json` (`hosts_permitidos` sigue vacío): la Compuerta NO controla esta salida.
- **Topes:** 30 escrituras/hora y 100/día, y 60 búsquedas/hora y 300/día, persistidos en `estado/agente/crm_cuota.json` (un lead nuevo
  gasta 2 escrituras, o 3 con resumen; si el contacto ya existe, 0 o 1). Sin bloqueo entre procesos: dos procesos simultáneos podrían
  pasarse un poco. El archivo vive en `estado/`, fuera del pin: quien lo edite puede anular el tope. Notas y tareas NO son idempotentes
  (cada llamada crea otra; solo las frena el tope): el contacto y el trato sí no se duplican.
- **Etapa del trato:** se usa `appointmentscheduled` (la primera del pipeline por defecto de HubSpot, cuya etiqueta es "Appointment
  Scheduled", no "Nuevo"): ajustar a la etapa inicial real de la cuenta antes de usar los reportes.
- **Datos personales:** viajan a HubSpot (un tercero) en nombre, empresa, email y teléfono; los textos libres se limpian (números largos y
  correos ocultos). El registro de auditoría guarda solo un hash corto del email. **El consentimiento del cliente no lo verifica nada.**
- **Texto inyectado:** lo que el modelo escribe en notas y tareas lo leerán personas en HubSpot; es la misma vía que `escalar_a_humano`.
- **NO verificado:** los identificadores de asociación (3, 202, 204), la etapa por defecto del pipeline (`appointmentscheduled`) y el
  formato real de las respuestas. Falta la prueba real en una cuenta de desarrollador y la auditoría de Hermes de esta fase.
- **Fuera del MVP:** producción 100% autónoma, prospección por internet, cualquier envío, canales de mensajería.

## Fase D: seguimiento y cobranza — CONSTRUIDA, NO ACTIVADA (2026-11-23)
**Estado real:** `agente/cobranza.py`, 3 herramientas en `mcp/servidor.py` (`seguimiento_pendientes`, `cobranza_evaluar`,
`cobranza_preparar`) y skills `skills/seguimiento/` y `skills/cobranza/` (NO instalados en Hermes; gateway sin reiniciar). Probado solo con
una cartera de prueba y reloj fijo (22 tests, 1 saltado: el de enlaces simbólicos, que este equipo no permite crear). Diseño en
`docs/superpowers/specs/2026-11-23-seguimiento-cobranza-design.md` (cambios respecto al diseño: la cartera lleva una columna más,
`ultimo_recordatorio`, para el espaciado de 7 días; el seguimiento escala con `escalar_a_humano` en vez de crear tareas en HubSpot).
- **Qué hace:** lee `workspace/cartera/cartera.csv` (lo carga y mantiene una PERSONA; ≤1 MB, ≤2000 filas, columnas exactas), decide la etapa con
  reglas fijas y deja un BORRADOR en `workspace/borradores_cobranza/c-<factura>-<etapa>.json` con una plantilla fija. **No envía nada ni cobra.**
- **Reglas fijas (propuestas, ajustables en `cobranza.py`):** 1–7 días amable, 8–30 firme; más de 30 días, monto sobre 2000, 3 recordatorios o
  disputa → escalar; 1 recordatorio por factura cada 7 días; pagadas y anuladas nunca. Las cifras salen de la cartera; el modelo no calcula.
- **Datos personales:** el email del cliente solo se guarda dentro del borrador; no vuelve al modelo POR LAS HERRAMIENTAS MCP. **Pero
  `workspace/cartera/cartera.csv` (email, monto y estado de deuda de cada cliente) y los borradores con `destinatario` están en el host:
  un perfil con herramientas de archivos en el host (p. ej. `default`) puede leerlos.** El perfil `agente` no los ve (su terminal y archivos
  están en el contenedor), igual que `workspace\borradores`. Cada fila inválida se descarta y su número se devuelve en
  `filas_descartadas_en_cartera`. Un `factura_id` repetido (sin distinguir mayúsculas, aunque la otra fila sea inválida) deja fuera TODAS sus
  filas; `factura_id` no admite números de 9+ dígitos (así se puede usar como referencia al escalar).
- **Auditoría de Hermes (2026-11-23):** ronda 1 FAIL por un alto real (una fila malformada "pagada" no contaba como id repetido y dejaba viva
  la fila "pendiente"); corregido con tests, junto con: un borrador amable pendiente ahora bloquea uno firme de la misma factura (hasta que una
  persona lo archive), conteo de filas descartadas, ids largos. **Abiertos y aceptados:** carrera entre comprobar y escribir el borrador con dos
  procesos MCP (sobrescribe un borrador casi idéntico); "hoy" se calcula en UTC (en UTC-3, de 21:00 a 24:00 el día cuenta uno de más y puede
  adelantar un borde de etapa); `seguimientos` no confina la carpeta con la Compuerta; el correo no se neutraliza si alguien exporta los
  borradores a una hoja de cálculo; las plantillas no incluyen identificación del emisor ni medio de pago (la persona debe añadirlos).
  **Ronda 2: PASS estático** (alto cerrado). Corregido después: dígitos no ASCII rechazados. **Medio abierto:** si una persona mueve o borra un
  borrador sin actualizar el CSV (`recordatorios_enviados` y `ultimo_recordatorio`), el agente puede volver a preparar el mismo recordatorio;
  el único bloqueo es el propio borrador pendiente. Antes de enviar, cotejar el estado de la factura en la cartera (un borrador ya preparado no
  se invalida si la factura pasa a pagada). El umbral de monto no distingue monedas. Pruebas faltantes señaladas por Hermes: filas duplicadas
  por el MCP, `espera` y 3 recordatorios por el MCP, bandeja llena, enlaces simbólicos (prueba saltada en este equipo).
- **Límites que NO resuelve el código:** nadie verifica el consentimiento ni que la cartera esté al día; el conteo de recordatorios depende de que
  la persona actualice el CSV al enviar (si no lo hace, el agente puede volver a preparar el mismo recordatorio tras archivar el borrador); las
  plantillas son de ejemplo y una persona debe revisarlas contra la ley de su país; `seguimiento_pendientes` mide la antigüedad del archivo, no si
  el cliente contestó; el nombre del borrador usa el id en minúsculas, así que `F-1` y `f-1` colisionarían (se rechaza el segundo).
- **No verificado:** ninguna prueba con un humano revisando borradores; ninguna llamada real desde Hermes a estas herramientas.

## Fase E: correo IMAP y captación — CONSTRUIDA, NO ACTIVADA (2026-11-23)
**Estado real:** `agente/correo.py`, 3 herramientas en `mcp/servidor.py` (`correo_nuevos`, `correo_captar`, `correo_descartar`) y skill
`skills/correo/` (NO instalado en Hermes; gateway sin reiniciar). Probado SOLO contra un servidor IMAP falso en memoria: **nunca se ha
conectado a un servidor IMAP real**, no existe el archivo de credenciales y el comportamiento con un proveedor real (cabeceras de
autenticación, límites, UIDVALIDITY) no está verificado. Diseño: `docs/superpowers/specs/2026-11-23-correo-imap-design.md`.
- **Qué hace:** lee INBOX por IMAP con TLS en SOLO LECTURA (`EXAMINE`, `BODY.PEEK`: no marca, no mueve, no borra, no envía; cabeceras ≤16 KB y
  cuerpo ≤8 KB, sin adjuntos ni HTML). Al modelo solo le llegan alias, asunto y extracto limpios (sin correos ni números largos, HTML escapado,
  URLs neutralizadas); **nunca el remitente**: `correo_captar` lo toma el código de la cabecera y llama a `crm.registrar_lead`.
- **Credenciales:** `%LOCALAPPDATA%\agente-local\imap.json` (host, usuario, clave —mejor una contraseña de aplicación—, puerto, `dominios_propios`
  y **`authserv_id`**, el nombre con el que TU servidor firma `Authentication-Results`, p. ej. el de tu proveedor; sin él no se verifica
  ningún mensaje), que crea una persona; el perfil `default` (host) podría leerlo. La clave no se imprime ni registra; los errores no llevan respuestas del servidor.
- **Descartes por código:** `noreply`/`mailer-daemon`/`bounce`, `List-Id`, `Auto-Submitted` ≠ `no`, `Precedence` bulk/list/junk, dominios propios,
  más de un `From`, y los SIN DKIM o SPF en `pass` EXACTAMENTE alineado con el dominio del remitente. Se usa SOLO la cabecera
  `Authentication-Results` que sea la primera Y cuyo authserv-id coincida con `authserv_id`, y debe haber una única con ese id (dos = imitación);
  se quitan los comentarios entre paréntesis y cada resultado se evalúa por separado. **Si no hay `authserv_id` o el proveedor no añade esa
  cabecera, no se registrará ningún mensaje.** Límite: si el proveedor NO antepone la suya y el remitente inventa una con tu mismo authserv-id,
  no se puede distinguir; depende de que tu proveedor la añada siempre. La alineación estricta deja fuera a remitentes legítimos que firman
  con otro dominio (p. ej. un servicio de envíos masivos): no se registran.
- **Auditoría de Hermes (2026-11-23), ronda 1: FAIL por un alto** (cabecera `Authentication-Results` inventada por el remitente, sin comprobar
  quién la añadió). Corregido con `authserv_id` y tests de regresión, junto con: alineación estricta, comentarios, respuestas vacías del
  servidor (ya no se descartan para siempre), fusión del estado al guardar (menos pérdidas por carreras; sigue sin haber un bloqueo), alias solo con
  letras, tope aplicado tras escapar, nombre con números largos no sube a HubSpot, tope de 45 s por llamada y alias en el registro. **Abiertos:**
  carrera residual entre procesos (ventana pequeña; si cambia UIDVALIDITY no se fusiona), `ana (at) cliente.com` pasa al modelo (no al remitente
  real), `List-Unsubscribe` sin `List-Id` no descarta, cada llamada de correo gasta una acción del presupuesto de consultas, y el tope de
  45 s se comprueba entre mensajes.
- **Ronda 2: FAIL por otro alto** (texto del remitente dentro de un valor entrecomillado de `smtp.mailfrom` que fabricaba un `dkim=pass`
  alineado). Corregido: se rechaza TODA cabecera de autenticación con comillas o barras invertidas; cada resultado debe EMPEZAR por
  `dkim=pass`/`spf=pass` y todo `ptype.propiedad=valor` posterior debe tener valor de caracteres seguros (si no, esa cláusula no cuenta);
  DKIM usa `header.d` o `header.i` (Gmail usa `header.i`), SPF `smtp.mailfrom`. Medios corregidos: cabeceras de 16 KB o más (truncadas) se
  descartan; separadores de línea raros o líneas sueltas sin ":" descartan el mensaje; los no vistos van antes que los ya listados (un
  remitente que inunda ya no acapara la lista); NFKC antes de ocultar correos (la "＠" de ancho completo).
- **Ronda 3: FAIL por dos altos** (uno condicionado a cómo escriba el proveedor): `header.i` alineado bastaba aunque `header.d` fuera ajeno, y
  espacios Unicode, paréntesis sueltos o propiedades repetidas permitían partir tokens de la cabecera. Corregido: si existe `header.d` manda
  él (`header.i` solo cuando falta, caso Gmail); la cabecera debe ser ASCII, sin controles, con paréntesis balanceados y separada solo por
  espacio y tabulador; una propiedad repetida invalida la cláusula; un `\r` suelto descarta el mensaje; el resultado informa descartes
  por motivo (`descartados_por_motivo`). **Esta cuarta versión NO se ha vuelto a auditar.** Tres rondas seguidas encontraron fallos reales en
  el análisis de `Authentication-Results`: tratarlo como poco fiable hasta probarlo con las cabeceras de TU proveedor real (que no se han
  visto nunca) y con una auditoría posterior. Falsos descartes posibles con un proveedor real: cabeceras con `reason="…"` (comillas),
  sin authserv-id (p. ej. algunos de Microsoft 365) o de 16 KB o más (muchas firmas ARC) se descartan o no se verifican.
- **Topes y estado:** 60 lecturas/h y 20 captaciones/h, UIDs procesados y alias pendientes en `estado/agente/correo_estado.json` (fuera del pin:
  quien lo edite puede anular topes o reprocesar); estado ilegible o inválido falla cerrado; si cambia `UIDVALIDITY` se reinicia.
- **Límites que NO resuelve el código:** nadie verifica el consentimiento de quien escribe; el asunto y el extracto siguen siendo texto
  hostil que lee el modelo (inyección: reducida, no eliminada); un remitente con DKIM/SPF válidos puede ser un atacante legítimo;
  el modelo decide qué es una consulta comercial; las captaciones usan las cuotas y la lógica de HubSpot (también sin probar con la API real).
- **No hace:** responder, reenviar, mover, borrar, abrir adjuntos o enlaces, leer otras carpetas. Los borradores de respuesta quedan fuera.

## Fase F: bandeja de aprobación (jefatura de ventas, nivel 1) — CONSTRUIDA (2026-11-23)
Diseño: `docs/superpowers/specs/2026-11-23-jefatura-ventas-design.md`. Código: `agente/bandeja.py` (reescrito; conserva `listar`/`cerrar`),
pruebas `agente/test_bandeja_aprobacion.py` (65, 2 saltadas por no poder crear enlaces simbólicos) y una de extremo a extremo en
`mcp/test_mcp.py`. **No es una herramienta del MCP**: la ejecuta una persona en su terminal; el MCP solo sella (`sello`) los borradores de
`cotizar` y `cobranza_preparar` y ninguna de sus 14 herramientas aprueba, rechaza ni envía. **Sigue sin existir envío**: "aprobado" significa
que una persona validó el borrador y lo enviará por su cuenta (en cobranza, anotándolo en `cartera.csv`).
- **Qué hace:** `listar` (escalados + cotizaciones + cobranza, con sello, antigüedad, VENCIDO/EDITADO/ILEGIBLE), `ver`, `aprobar`, `rechazar`
  (exige motivo), `cerrar` (escalados), `incidente` y `resumen` (métricas por categoría contra el criterio propuesto: ≥50 casos, ≥95 % sin
  edición, 0 incidentes; **solo informa, no sube ni baja ningún nivel**). Registros en `workspace/archivo/aprobados|rechazados/` y cadena de
  hashes en `estado/bandeja/decisiones.jsonl`.
- **Controles:** `aprobar` pasa el hash de lo mostrado y aborta si el archivo cambió; exige terminal interactiva y escribir el id (no hay
  `--si`); toma el archivo con renombrado atómico y compara bytes antes de archivar; todo bajo bloqueo entre procesos; falla cerrado si la
  cadena no está disponible; ids con regex estricta y categoría derivada del id; lectura de 200 KB como máximo; enlaces y junctions fuera del
  proyecto rechazados (también al crear carpetas); control/bidi neutralizados al imprimir; un pendiente de más de 7 días no se aprueba sin
  `--forzar` (queda anotado). `resumen` solo cuenta un registro si su contenido, sello, categoría, carpeta y nombre son coherentes **y** su evento
  (con la misma fecha) está en la cadena, y concilia al revés (evento sin archivo = registro borrado); cualquier alterado, ilegible, copia,
  cadena rota o truncado impide TODO "CUMPLE"; la tasa se compara en enteros (189/199 no cumple).
- **Auditoría Hermes:** ronda 1 FAIL (4 altos reales: lo mostrado no ligado a lo aprobado, sello/incidente fuera del hash, carrera entre dos
  terminales), ronda 2 FAIL (1 alto: borrar un registro o cambiar su fecha con solo escribir en `workspace/`, más tasa redondeada antes de
  comparar y confinamiento solo de carpetas existentes), ronda 3 **PASS estático** con 8 hallazgos bajos. Es una revisión sin terminal y esa
  vez el auditor hizo las tres lentes él mismo (no independientes); el PASS vale para el código tal como se auditó.
- **Hallazgos bajos abiertos (no corregidos tras el PASS, para no cambiar el código auditado):** un Ctrl-C a mitad deja el borrador como `.tomado`
  (visible como ilegible, no se pierde); un `_intento` cuenta como prueba aunque haya un `decision_fallida` posterior; los `.tmp` huérfanos se
  ignoran en silencio; `resumen` no distingue aprobaciones `programatica`/`inyectada` de `interactiva` (la garantía de interacción vale para la CLI);
  `cerrar` no toma el bloqueo; `listar` de escalados sin tope de memoria; `rechazar` no exige el hash mostrado; un carácter suelto no UTF-8 (`\ud800`)
  hace fallar la aprobación con un mensaje poco claro.
- **Límite honesto del modelo de amenaza:** la cadena no usa HMAC con clave fuera de alcance. Con escritura solo en `workspace/` no se puede esconder
  nada sin que `resumen` lo señale; con escritura también en `estado/` se puede reescribir la cadena y recalcular todo. Detecta ediciones honestas,
  accidentes y falsificaciones torpes, no a un atacante con acceso al host. `decidido_por` es el usuario del sistema, no una identidad
  autenticada; la fecha de creación del borrador es editable (la caducidad es una ayuda, no un control).
- **Fuera del nivel 1 (diseñado, no hecho):** editar y aprobar en un paso, nivel vigente por categoría, descenso automático por incidente, herramienta de
  envío (diseño y auditoría propios), pantalla local en lugar de línea de comandos, prerrequisitos de nivel 2 (lista de no contacto, texto de terceros
  marcado como no confiable, revisión previa determinista, medición de consumo, pruebas con HubSpot e IMAP reales). Nada probado con datos reales.
- **Operativa:** tras aprobar un recordatorio de cobranza, anotar el envío en `cartera.csv`; si no, el agente puede preparar otro borrador de la
  misma factura. Las decisiones sobre `workspace/` real nunca se han ejecutado todavía (solo pruebas en directorios temporales y un `listar`/`resumen` vacío).

## Fase G: prerrequisitos del nivel 2 (no contacto, texto externo, revisión previa) — CONSTRUIDA (2026-11-23)
Diseño: `docs/superpowers/specs/2026-11-23-prerrequisitos-nivel2-design.md`. Código: `agente/nocontacto.py`, `agente/revision.py` (nuevos y en el pin),
cambios en `agente/correo.py`, `agente/bandeja.py`, `mcp/servidor.py` y `skills/correo/SKILL.md` (skill NO instalada en Hermes). Pruebas nuevas:
`test_nocontacto.py` (29), `mcp/test_nocontacto_mcp.py` (22), `test_correo_bajas.py` (21), `test_revision.py` (30) y ampliaciones en
`test_bandeja_aprobacion.py` (81) y `test_correo.py` (42). Todo con servidores falsos: **nada probado con IMAP, HubSpot ni correos reales**.
- **Lista de no contacto:** archivo de la PERSONA `%LOCALAPPDATA%\agente-local\no_contactar.txt` (correo o `@dominio` por línea; UTF-8; **debe existir**,
  puede estar vacío; una línea inválida invalida todo; el archivo se relee en cada uso) más la lista AUTOMÁTICA `estado/agente/no_contactar_auto.json`
  (la escribe solo el código). Frena `crm_registrar_lead`, `crm_crear_tarea`, `correo_captar`, `cobranza_evaluar`/`cobranza_preparar` (pasa a `escalar`) y la
  aprobación en la bandeja. `crm_buscar_contacto` y `crm_registrar_nota` siguen permitidas. Comparación: NFKC, minúsculas, sin `+etiqueta`, puntos de
  Gmail ignorados y `googlemail.com` = `gmail.com`; lo que la lista no entiende cuenta como bloqueado y `analizar` descarta a los remitentes que el CRM
  aceptaría y la lista no (`+a@x.com`, `ana@-x.com`).
- **Bajas automáticas:** `correo_nuevos` detecta frases fijas en español e inglés (también en correos solo HTML, sin citas) SOLO en mensajes verificados; el código
  añade al remitente a la lista automática y al modelo solo le dice cuántas. Si una baja no se puede escribir (lista llena, corrupta, sin permisos) el mensaje
  no se lista, se crea `estado/agente/no_contactar_auto.json.bloqueo` y **todo lo que contacte o registre personas se deniega** (también en memoria hasta
  reiniciar el servidor) hasta que una persona repare la lista y borre el marcador. Las bajas gastan plazas del tope de 10 por llamada.
  Visibilidad: `python agente/bandeja.py nocontacto` lista las bajas automáticas (falsos positivos posibles: "opt out", "remove me"… aparecen en
  consultas normales) y `listar` avisa de su número, de que está casi llena (80 %) y de bajas sin registrar.
- **Texto externo:** asunto y extracto de `correo_nuevos` salen entre `<<<EXTERNO` y `EXTERNO>>>`, sin `<`/`>` ni sus entidades dentro. Una mitigación,
  no una garantía: un desconocido puede imitar la marca con otros símbolos (`〈〈〈`) y el modelo puede ignorarla.
- **Revisión previa** (`bandeja aprobar`/`listar`/`ver`): bloquea destinatario inválido o en la lista, llaves y marcadores sin rellenar (`[NOMBRE]`, `TODO`/`XXX`
  sueltos), HTML, enlaces, longitudes y cifras de cotización que no cuadran (con tolerancia al redondeo del precio unitario); ni `--forzar` salta un bloqueo.
  Frases de pago o presión (`transferencia`, `IBAN`, `último aviso`…) son avisos y quedan en el registro. Los textos del catálogo (moneda, SKU, nombre) solo
  generan avisos para no volver inaprobable un catálogo válido.
- **Auditoría Hermes (3 rondas, todas FAIL, todas con hallazgos reales):** ronda 1: alto por palabras codificadas RFC 2047 en `Authentication-Results` que
  fabricaban `dkim=pass` (reproducido por mí: un remitente que no firmó nada salía verificado); ronda 2: alto por direcciones que el CRM acepta y la lista no,
  más CPU con comentarios anidados, cuerpo que no llega y falsos bloqueos; ronda 3: alto porque una baja que no se podía escribir dejaba a esa persona captable
  (corregido con el marcador de bloqueo) y medio por importes de más de 13 dígitos. **La última versión corregida NO se ha vuelto a auditar.**
- **Abiertos, conocidos:** (1) quien tenga un dominio propio con firma válida puede ir llenando la lista automática (20.000 entradas, ~12 MB máximo): lo
  detectas por el aviso del 80 % y, si se llena, el marcador de bloqueo frena todo hasta que lo repares (disponibilidad, no seguridad); (2) borrar
  `no_contactar_auto.json` o vaciar `no_contactar.txt` anula la lista sin aviso (el diseño lo admite; hace falta un ancla externa o HMAC); (3) la verificación
  asume que TODO el correo entra por tu servidor, que es quien añade `Authentication-Results`: si hay un MX secundario o un reenvío que no lo añade, una cabecera
  forjada con tu `authserv_id` pasaría como única; comprobarlo en el servidor real; (4) la comparación no cubre sub-direccionamiento con guion (Yahoo) ni los
  puntos de dominios Google Workspace; (5) la baja pedida solo como imagen o adjunto, o en otro idioma, no se detecta; (6) `crm_crear_tarea` y su borrador no
  pasan por `revision.py`; (7) `procesados` se recorta a 5.000 UIDs (un correo muy antiguo puede reaparecer); (8) `self.ce = Compuerta(...)` de escalados queda fuera
  del `try` de arranque (cambio ajeno: un estado corrupto de escalados tumba el arranque con traza); (9) el mensaje "inválido o en la lista" y un hash corto de la
  dirección en el registro permiten sondear si una dirección está en la lista (oráculo leve).
- **Operativa:** crear el archivo de la lista antes de activar nada (`New-Item`/`Set-Content -Encoding utf8`; un `echo x > archivo` de PowerShell 5 lo guarda en
  UTF-16 y se rechaza con un mensaje claro). Otra sesión de Claude trabajó en paralelo en esta carpeta (`cotizador.validar_catalogo`, `servidor.py`, pruebas);
  sus cambios no los revisé a fondo más allá de lo que Hermes dijo.

## Integración con Hermes (qué hay que hacer y qué no está verificado)
- **Registro:** `hermes mcp add agente-local --command <python.exe> --args -I <ruta>\mcp\servidor.py` (ruta absoluta; pide
  confirmar con Y). Hay copias de seguridad de `config.yaml` junto al original (`.bak-antes-de-agente-local`,
  `.bak-antes-de-reregistrar`).
- **Al cambiar la lista de herramientas o el manifiesto hay que REINICIAR EL GATEWAY** (`hermes gateway restart`): el gateway
  conserva el registro hasta reiniciarse, y `hermes mcp add/remove` no basta ni apartar `cache/mcp_schema_cache.json`.
- **Tras cada cambio de código o datos del MCP hay que re-fijar el pin** (`python mcp/servidor.py --fijar`, escribir FIJAR);
  lo hizo el autor (Claude) por stdin: una persona debe revisarlo. Si el servidor sale con código 3, el código o los datos
  difieren del pin: revisar el cambio y re-fijar solo si es legítimo.
- **Los skills instalados son COPIAS** de `skills/`: tras editar un `SKILL.md` hay que recopiarlo y comparar hashes.
- **Superficie:** el registro es global. WhatsApp está "habilitado pero no emparejado" (desde agosto), así que hoy no hay
  canal externo; emparejarlo (o cualquier otra plataforma) amplía la superficie y exige una nueva auditoría.
- **No verificado:** que `-t agente-local` apague las demás herramientas de Hermes, y qué conjuntos de herramientas carga
  el gateway. Solo se probó con `hermes chat`. Los escalados y borradores de prueba siguen en `workspace/`.
