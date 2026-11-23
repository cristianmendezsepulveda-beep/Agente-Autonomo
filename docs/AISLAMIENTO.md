# Aislamiento real: WSL2 + Docker, terminal de Hermes en contenedor

Estado (2026-11-22): **IMPLEMENTADO y verificado en este equipo** para el perfil `agente` de Hermes (ver "RESULTADO REAL" al final).
Los pasos 1-3 los hizo la persona (cambios de sistema). Las secciones de plan de abajo se conservan como registro de cómo se llegó
aquí; **donde contradigan "RESULTADO REAL", manda "RESULTADO REAL"** (p. ej. el plan decía "nada del host montado" y "sin
docker_extra_args", y planteaba tocar la configuración global: al final se usó un perfil dedicado y hay 14 montajes de solo lectura).

## Qué se quiere conseguir y qué NO cubre
- **Objetivo:** que lo que Hermes ejecute con su terminal/archivos corra dentro de un contenedor (kernel de la VM de WSL2),
  sin ver el disco ni las credenciales del host y sin red salvo que se abra a propósito.
- **Cerebro en el host:** el modelo de Hermes y sus claves de API siguen en el host. El contenedor nunca recibe las claves
  (`docker_forward_env: []`; si algún día hace falta salir a internet, usar `hermes egress`, que inyecta credenciales fuera
  del sandbox).
- **El MCP `agente-local` sigue en el host:** es código propio, fijado por pin; escribe solo en `workspace/` (Compuerta).
- **NO cubre:** que el modelo decida mal con lo que SÍ puede hacer (cotizar, escalar); fugas por el propio proveedor del
  modelo; un escape de contenedor/VM (riesgo bajo, no nulo); herramientas de Hermes que no pasen por el backend de terminal
  (navegador, etc.: hay que comprobar cuáles). Un contenedor no es una garantía absoluta.

## Restricciones del equipo
Windows 11 Pro, 7,7 GB de RAM (≈0,7 libres con Claude/Hermes abiertos), 68 GB de disco. La RAM es el límite real: limitar la
VM de WSL2 y el contenedor, y cerrar aplicaciones al probar.

## Paso 1 (persona, PowerShell COMO ADMINISTRADOR) y reinicio
Evidencia de que falta el componente: hay hipervisor activo y VBS en ejecución (el firmware ya virtualiza), pero no existen
los servicios `vmcompute`/`vmms` y WSL2 dice "virtualización no habilitada".
```powershell
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
Restart-Computer
```
Comprobación tras reiniciar (sin administrador): `wsl --status` ya no debe decir "virtualización no habilitada".

## Paso 2 (persona): límites de memoria de WSL2
Crear `C:\Users\PC\.wslconfig` (valores iniciales; ajustar si el equipo se ahoga):
```ini
[wsl2]
memory=3GB
processors=2
swap=2GB
```
Luego `wsl --shutdown`.

## Paso 3 (persona): Docker
Docker Desktop (genera `docker.exe` en el PATH, que es lo que Hermes busca con `shutil.which("docker")`; si no está, usa
`podman`). Descarga de ~0,5 GB y aceptación de su licencia: lo decide y hace la persona. Con 7,7 GB de RAM, desactivar
"Start Docker Desktop when you sign in" y cerrarlo cuando no se use. Comprobación: `docker version` sin errores.

## Paso 4 (Claude, con confirmación): comprobar el aislamiento ANTES de usarlo
Ejecutar `python aislamiento/verificar_aislamiento.py --imagen <imagen>` (ya escrito y probado en sus evaluadores; vive en
`aislamiento/` y no en `sandbox/` porque el pin del MCP rechaza archivos `.py` nuevos en las carpetas de código). No
descarga nada sin `--permitir-descarga`. Debe demostrar, con un contenedor con las banderas de abajo y un control
negativo por comprobación (si el control no difiere, el resultado es INVÁLIDO), que: no hay red; no se ve el disco del host (ni `C:\Users`); el sistema de
archivos raíz es efímero (el contenedor real NO es de solo lectura y corre como root con 5 capacidades: ver "Estado actual"); no hay socket de Docker dentro; hay tope de
memoria y de procesos. **Si alguna comprobación falla, no se activa el backend.**

## Paso 5 (Claude, con confirmación): un PERFIL DEDICADO de Hermes, no la configuración global
**No cambiar `terminal.backend` en el perfil `default`.** Con el backend `docker` el terminal (y, hay que comprobarlo, las
herramientas de archivos) opera dentro del contenedor, sin ver el disco del host: las auditorías con Hermes, que leen el
proyecto del host, dejarían de funcionar. Hermes tiene perfiles ("instancias aisladas"):
- Crear el perfil `agente` (`hermes profile create agente --clone`: copia config.yaml, .env, SOUL.md y skills del perfil
  activo; las credenciales del modelo se comparten salvo `--isolated-auth`). El cerebro y sus claves siguen en el host.
- En ESE perfil: `terminal.backend: docker` con la configuración de abajo, el MCP `agente-local` y los skills. El perfil
  `default` queda en `local` y, cuando se decida, sin el MCP registrado.
- Arrancar el agente con `hermes -p agente ...`. El gateway de `default` no cambia.
Cambia la configuración del PERFIL `agente` (hacer copia de su `config.yaml` antes; en el clon `terminal.backend` es `local`):
```yaml
terminal:
  backend: docker
  docker_image: python:3.12-slim          # a confirmar (descarga). NO dejar el valor por defecto de Hermes
  docker_mount_cwd_to_workspace: false   # nada del host montado
  docker_volumes: []
  docker_network: false                   # sin red
  docker_forward_env: []                  # ninguna clave dentro
  container_cpu: 1
  container_memory: 1536                  # MB; el valor por defecto (5120) no cabe en este equipo
  container_persistent: false             # efímero
  # SIN docker_extra_args: Hermes ya aplica cap-drop ALL (+CHOWN, DAC_OVERRIDE, FOWNER), no-new-privileges, tmpfs en
  # /tmp y /var/tmp, tope de procesos (2048) y de memoria. Repetirlos duplicaría banderas y chocaría con su
  # comprobación de colisiones.
```
**Imagen:** el valor por defecto de Hermes es `nousresearch/hermes-sandbox:desktop`, una imagen con escritorio (Xvnc, Xfce,
Chromium), demasiado pesada para 7,7 GB de RAM. Hay que fijar una imagen pequeña a propósito. `python:3.12-slim` es solo
una propuesta: Hermes puede necesitar utilidades (git, curl) que no trae; se prueba con una sesión real.

**Dos fases de verificación:** (1) `verificar_aislamiento.py` con una réplica de las banderas de Hermes demuestra que ESTA
máquina sabe aislar (red, montajes, capacidades, topes); (2) tras activar el backend hay que inspeccionar el contenedor REAL
que cree Hermes (`docker inspect`: red `none`, sin montajes, capacidades, memoria, entorno) porque la réplica puede derivar.
Verificar con una sesión real mínima: que el terminal de Hermes corre en el contenedor, que no ve `C:\`, que el MCP
`agente-local` sigue funcionando y que `escalar_a_humano` sigue escribiendo en `workspace/` del host.

## ESTADO ACTUAL Y RESULTADO REAL (2026-11-22)
Esta sección es la que manda. Todo lo anterior es el plan original.

### Configuración vigente (verificada)
- **Perfil `agente` de Hermes**, creado con `hermes profile create agente --clone --no-alias` (copia config, `.env`, SOUL.md y skills del
  perfil `default`; los logins se comparten). Backend `docker`, imagen `python@sha256:5e2bdb1b…fa5b` (python:3.12-slim), sin red,
  1536 MB, 1 CPU, contenedor no persistente entre sesiones (Hermes lo retira; el inspect muestra `AutoRemove: false`), sin `docker_extra_args` (Hermes ya aplica cap-drop ALL + CHOWN, DAC_OVERRIDE, FOWNER,
  SETGID, SETUID, no-new-privileges, tmpfs y tope de procesos 2048).
- **Herramientas del perfil (plataforma `cli`):** solo `terminal`, `file`, `todo`, `clarify` y el MCP `agente-local`. Deshabilitadas: web, browser,
  code_execution, vision, image_gen, tts, memory, session_search, connections, delegation, cronjob, computer_use y skills (porque `skill_manage`
  escribe skills en el directorio del HOST). Las skills `consultas` y `cotizaciones` van precargadas: **lanzar siempre**
  `hermes -p agente chat -s consultas,cotizaciones` desde una shell con el PATH de Docker refrescado.
- **Perfil `default`:** intacto (backend local), con el MCP registrado, salvo que Hermes añadió `gateway: multiplex_profiles: true`.
- Copias de seguridad de las configuraciones junto a cada `config.yaml` (`.bak-*`).

### Evidencia verificada (informes en `audits/`, generados por el verificador FINAL)
- `aislamiento-final-fase1.json`: fase 1 con alpine:3.20: 8 comprobaciones + 7 controles negativos → CORRECTO.
- `aislamiento-final-fase2-a.json` y `aislamiento-final-fase2-b.json`: fase 2 sobre el `docker inspect` crudo de DOS contenedores reales de
  Hermes → CORRECTO (22 entradas cada uno: 21 de `evaluar_contenedor` más la de enlaces; el informe guarda el inspect y su SHA-256). Un tercer contenedor se evaluó en vivo con
  una versión intermedia del verificador (`aislamiento-fase2-contenedor3-en-vivo.json`).
- Los demás informes (`aislamiento-fase1-alpine`, `…contenedor1`, `…contenedor2`, `…fase1-v2`, `…fase2-v2`) son de versiones ANTERIORES
  del verificador (los primeros sin inspect crudo ni hash): históricos, no se cuentan como evidencia vigente.
- Tests: sandbox 21, clientes 24, agente 36, mcp 60, aislamiento 38; el pin del MCP verifica.
- Sesiones reales del perfil `agente`: el terminal corre en el contenedor (Debian 13, uid 0, solo interfaz `lo`, sin git ni curl); las
  herramientas de archivos operan dentro del contenedor (no pudieron leer `C:\Users\PC\Desktop\Agente Autonomo\docs\GUARDRAILS.md`,
  "File not found"; sí leyeron `/etc/os-release` del contenedor); el MCP (consultar, listar, cotizar = 230,00) funciona con el terminal en
  Docker; `tool_search` con 7 búsquedas (browser, web, computer use, cron, memory, delegate, skill manage) devolvió vacío: el meta-buscador no
  alcanza los toolsets deshabilitados; el terminal ve solo AI_AGENT, GIT_PAGER, GPG_KEY, HERMES_AGENT, HOME, HOSTNAME, LANG, OLDPWD, PAGER,
  PATH, PWD, PYTHON_SHA256, PYTHON_VERSION, SHLVL y `_` (sin credenciales). `hermes -p agente mcp list`: solo `agente-local`.
- En el host (comprobaciones manuales de sesión; solo los informes JSON de `audits/` son evidencia archivada, y de ellos solo consta "14 carpetas revisadas, sin enlaces"): 609 archivos en las carpetas montadas, 0 enlaces/junctions; las skills clonadas solo contienen marcadores de ejemplo
  (`ghp_xxx…`, `sk-xxx…`); el daemon de Docker no escucha en 2375/2376; Enhanced Container Isolation en su valor por defecto (no activado).
- Hermes retiró por sí solo el contenedor idle de la última sesión (observado una vez); antes de eso queda vivo unos minutos (`sleep infinity`).

### Qué NO es verdad de lo que decía el plan (correcciones)
- No es cierto que "no haya montajes del host": el contenedor real tiene **14 montajes de SOLO LECTURA** de carpetas del perfil `agente`
  (cachés de documentos/imágenes/audio/web/delegación/capturas, adjuntos y `skills`). No monta `.env`, `config.yaml`, `auth.json`, ni nada
  del perfil `default` ni de tus archivos (el verificador exige una lista blanca exacta).
- Hermes añade 5 capacidades (CHOWN, DAC_OVERRIDE, FOWNER, **SETGID, SETUID**), no 3, y el contenedor corre como root dentro.
- No se debe cambiar `terminal.backend` en el perfil `default`: rompería las auditorías (leen el host).

### Huecos que SIGUEN ABIERTOS (el aislamiento NO es absoluto)
- **Un contenedor no es una garantía:** la superficie restante es el kernel de la VM de WSL2 (escape de contenedor/VM: riesgo bajo, no nulo),
  agravado por root con SETUID/SETGID dentro del contenedor.
- **Montajes de lectura compartidos entre clientes:** con un canal real, un cliente malicioso podría lograr que el modelo lea y devuelva
  adjuntos o documentos de OTROS clientes (el canal de salida legítimo es la respuesta). **No emparejar ningún canal sin separar el
  almacenamiento por conversación.**
- **El MCP corre en el HOST** (código propio, con pin y Compuerta, solo escribe `workspace\*.json`): es la vía prevista del agente hacia el
  host; `motivo` y `referencia` de `escalar_a_humano` pueden transportar texto inyectado hasta el operador (solo se sanean números largos,
  correos y controles). Sigue registrado también en el perfil `default`.
- **Gateway multiplexado** (`multiplex_profiles: true`, añadido por Hermes): no se ha probado a qué perfil enruta un mensaje externo; si
  llegara a `default` tendría terminal del host, navegador, escritorio y el MCP. Hoy es latente (WhatsApp "habilitado pero no emparejado").
  **El recorte de canales se hizo el 2026-11-23 SOLO en el perfil `agente`** (ver "Canales y almacenamiento"): el perfil `default` sigue con todas
  las herramientas en todas las plataformas, así que el riesgo de enrutamiento hacia `default` sigue ABIERTO.
- **Recursos:** cada sesión crea contenedores de 1,5 GB sobre una VM de 3 GB en un equipo de 7,7 GB; dos o tres sesiones a la vez pueden
  provocar OOM.
- **Fuera de lo verificado:** configuración de file sharing y de WSL2 (automount/interop); cualquier `docker run` manual (no pasa por Hermes ni
  por el verificador); el gasto de tokens; un solo equipo y pocas sesiones reales.
- **"Falla cerrado" se vio en UN caso** (Docker no visible en el PATH del gateway: el modelo se quedó sin terminal ni archivos, no cayó al host).
  No está probado con Docker Desktop apagado a mitad de sesión, fallo al arrancar el contenedor, OOM o contenedor retirado por antigüedad.
- **Límites del verificador:** la fase 2 evalúa el entorno de CREACIÓN y el momento de la inspección (no hay protección contra cambios
  posteriores ni contra variables inyectadas después; se comprobó a mano que el comando de Hermes solo exporta AI_AGENT, HERMES_AGENT,
  GIT_PAGER y PAGER); es una lista blanca, así que una versión nueva de Docker/Hermes puede dar FALLÓ por falsos positivos (p. ej.
  `IpcMode: shareable` o un `tmpfs` en `Mounts`): eso falla cerrado y obliga a revisarlo; la comprobación de enlaces exige acceso al host y
  compara `realpath` con la ruta esperada (cualquier diferencia, incluidos nombres 8.3 o redirecciones legítimas, falla); la detección de
  credenciales es heurística (nombres y formatos conocidos).

### Historial de auditorías de este trabajo
- **Ronda 1 (FAIL):** 3 altos del evaluador (los `--mount` no se evaluaban, se aceptaba montar la raíz del perfil, `no-new-privileges` por
  subcadena), 6 medios (campos sin evaluar, imagen sin digest exacto, credenciales solo por nombre, subcadenas, 3 de 7 controles, documentación
  obsoleta). Corregidos con tests.
- **Ronda 2 (FAIL):** 1 alto real, reproducido: si la propia carpeta montada, o un directorio padre, era una junction/symlink (p. ej.
  `skills → C:\Users\PC\.ssh`), `buscar_enlaces` daba "ninguno" mientras Docker montaba otra carpeta. Corregido con `realpath` sobre la
  carpeta y toda su cadena de padres. Medios corregidos: carpetas ausentes parciales, errores de lectura tragados, test de "falla cerrado" que no
  probaba lo que decía, controles probados por búsqueda de cadenas, y documentación contradictoria. Bajos: un `docker run` fallido ya no se da
  por bueno, propagación `shared/slave`, más formatos de credenciales, solo se recorren las fuentes de la lista blanca, y más tests de campos.
- **Ronda 3 (PASS estático, 0 altos):** 6 medios y varios bajos. Corregidos en el código: los controles de la fase 1 ahora exigen que su contenedor
  se ejecute (rc 0) y devuelva datos; la fase 2 exige exactamente 1 contenedor; el informe redacta los VALORES de `Config.Env`. Corregidos en la
  documentación: afirmaciones sobre root/solo lectura, 22 entradas, evidencia manual frente a archivada, `AutoRemove`.
  **Siguen abiertos (aceptados, no corregidos):** campos de `HostConfig` sin evaluar (`Tmpfs` con `exec`, `ReadonlyRootfs`, `Ulimits`, `Dns`, `State.Running`,
  destino de los montajes), fixture de tests circular (sin test de regresión con los `inspect` reales), hardlinks no detectados, `inspect_sha256`
  del crudo y no del guardado, tests de junction que se saltan si no se pueden crear enlaces. Fue una revisión estática de un único auditor.

### Canales y almacenamiento (2026-11-23)
Diseño: `docs/superpowers/specs/2026-11-23-almacenamiento-canales-design.md` (opción C: recortar canales ahora; un perfil por cliente solo si
alguien lo exige).
- **Hallazgo previo al recorte:** en `whatsapp` del perfil `agente` estaban habilitados terminal, archivos, web, navegador, ejecución de código,
  visión, memoria, delegación, cron y computer_use (18 de 26 toolsets). Lo observé con `tools list --platform whatsapp` ANTES de recortar.
- **Qué se hizo (con permiso; copia `profiles\agente\config.yaml.bak-antes-de-canales`):** `hermes -p agente tools disable --platform <p> ...`
  en las 30 plataformas válidas salvo `cli`, y se eliminaron de `platform_toolsets` las 13 entradas compuestas `hermes-<plataforma>` (en `sms`
  el comando "disable" NO bastaba: seguía todo habilitado por esa entrada compuesta, y en `discord` había dos toolsets propios,
  `discord` y `discord_admin`). Esos dos fallos los detectó el verificador, no yo. Quedan entradas vacías para `homeassistant` y `yuanbao`
  (plataformas que Hermes ya no reconoce).
- **Verificador:** `python aislamiento/verificar_canales.py [--salida informe.json]` descubre las plataformas de Hermes y exige que cada canal
  tenga habilitado solo `clarify` más el MCP `agente-local` (y `cli`: clarify, file, terminal, todo). Falla cerrado ante salida ilegible.
  Resultado: la primera ejecución real dio FALLÓ (discord y sms); tras corregir, CORRECTO en las 31 plataformas, ejecutado antes y después de
  reiniciar el gateway. `audits/canales-despues.json` guarda SOLO la última ejecución (sin fecha ni hash). El estado previo al recorte no se
  midió con el verificador (no existía): solo consta mi observación manual de arriba. Tras la auditoría de Hermes el analizador es más
  estricto (cualquier línea de toolset con marcador o nombre raro, un MCP con estado distinto de "all tools enabled" o la falta de
  toolsets conocidos dan FALLÓ); `tools list` solo muestra la configuración resuelta por Hermes, no la del gateway en ejecución, y no
  cubre herramientas individuales del MCP ni las plataformas obsoletas `homeassistant` y `yuanbao`.
- **Qué NO está probado:** ningún mensaje real (no hay canal emparejado); que el gateway use este perfil para un mensaje entrante; que una
  actualización de Hermes no reponga las entradas `hermes-*`. Volver a ejecutar el verificador tras actualizar Hermes o emparejar un canal.
- **Las cachés montadas en el contenedor siguen siendo compartidas por perfil** (solo lectura); ahora ningún canal del perfil `agente` tiene
  terminal ni archivos para leerlas, pero Hermes puede inyectar adjuntos en el contexto de la conversación que los recibe.
- **El MCP no recibe la identidad de la conversación:** cualquier conversación puede consultar un email o una factura ajenos
  (`crm_buscar_contacto`, `cobranza_evaluar`); lo frenan solo los topes y que devuelven lo mínimo. Separar por cliente exigiría que Hermes
  pase esa identidad, o un perfil por cliente (opción A, no hecha).
- **El gateway se reinició** (PID nuevo) y Hermes reafirmó el elemento de inicio de Windows `Hermes_Gateway.vbs` (ya existía desde el 19-11).

### Cómo operar
- Antes de usar el agente: `python aislamiento/verificar_aislamiento.py --imagen alpine:3.20` (plataforma) y, con un contenedor de Hermes
  en marcha, `python aislamiento/verificar_aislamiento.py --inspeccionar <id>` (fase 2). Si falla algo, **no usar el agente** hasta revisarlo.
- Si Hermes cambia sus montajes o banderas, la fase 2 fallará a propósito: revisar a mano y, solo si es legítimo, actualizar
  `MONTAJES_PERMITIDOS`/`CAPS_HERMES` en el verificador y repetir la auditoría.
- Tras reinstalar/actualizar Docker o cambiar el PATH, reiniciar el gateway desde una shell con el PATH refrescado.
- **Revertir:** quitar el perfil (`hermes profile delete agente`), o `terminal.backend: local` en su `config.yaml` (hay copias `.bak-*`), y reiniciar
  el gateway. Para desinstalar la plataforma: Docker Desktop, `.wslconfig` y el componente Virtual Machine Platform los gestiona la persona.
