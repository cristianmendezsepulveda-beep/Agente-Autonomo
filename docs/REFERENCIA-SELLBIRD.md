# Referencia: Sellbird (para futuras mejoras)

Fecha de la revisión: 2026-11-23. Repositorio: https://github.com/ethanplusai/sellbird (AGPL-3.0, versión 0.4.0 Beta, unos 7.000 estrellas, 544 commits al leerlo).
Es una guía de IDEAS, no un plan aprobado: **nada de esto está aplicado en nuestro código.**

## Cómo se obtuvo y qué vale
- Un subagente de solo lectura leyó 15 archivos por la web (README, `pyproject.toml`, `SECURITY.md`, `docs/how-it-works.md`, `docs/approval-and-sending.md`, y en `sellbird/`: `untrusted.py`, `dashboard_auth.py`, `public_http.py`, `claude_runtime.py`, `secrets.py`, `send_hooks.py`, `gate.py`, `mcp_server.py`, parte de `dashboard.py` y `codex.py`, más `Dockerfile` y `docker-compose.yml`). Nadie descargó ni ejecutó su código.
- Las severidades son estimaciones de lectura, sin reproducir. El árbol de archivos llegó truncado.
- **No leído:** `sender.py`, `handler.py`, `writer.py`, `state.py`, `send_window.py`, `usage.py`, `integrations/*`, `collectors/*` (scraping y Playwright), `web/app.js`, `agent_policy.py`, `tests/`, `.env.example`, `CLAUDE.md`. Cualquier afirmación sobre envío real, límites diarios y front del panel NO está verificada.
- **Licencia:** AGPL-3.0. Copiar código suyo obligaría a abrir el nuestro (también en uso por red). Tomar ideas es otra cosa; si algún día se reutiliza código, revisar la licencia primero.

## Qué es
Motor de prospección y correo en frío que corre en la máquina del usuario: descubre empresas, las perfila con señales de compra, identifica a quien decide, verifica correos, redacta con un modelo (CLI de Claude o Codex por suscripción), pone el borrador en una Outbox con aprobación humana por defecto (autopilot opcional) y envía por Gmail, Microsoft 365, SMTP o Instantly. Panel FastAPI local y servidor MCP.

## Ideas a considerar para nuestro proyecto (por prioridad propuesta)
1. **Lista de no contacto y bajas** (el hueco más importante). No tenemos ninguna: debería bloquear `crm_registrar_lead`, `correo_captar` y `cobranza_preparar`. Sellbird la aplica incluso a las respuestas, con coincidencia por dirección y por dominio, y exige confirmación explícita para reactivar una baja.
2. **Marcar como no confiable el texto de terceros** que ve el modelo (asunto y extracto de `correo_nuevos`, resultados de otras herramientas): envolverlo en marcas y eliminar marcas falsas, incluso en formas Unicode normalizadas o con entidades HTML. Ellos lo hacen en sus prompts (`untrusted.py`) y reconocen que es mitigación, no garantía. Su servidor MCP NO lo hace al devolver texto al agente: es el mismo hueco que tenemos.
3. **Revisión previa determinista, en código**: formato del destinatario, lista de no contacto, correo verificado para contacto en frío, límites de longitud, variables sin rellenar, frases prohibidas, máximo de enlaces, rechazo de HTML y, en frío, dirección postal. Útil si algún día se añade envío con aprobación.
4. **Las reglas solo pueden frenar**: ganchos de envío que retengan o cancelen, nunca envíen lo que un control interno detuvo; si una regla falla, se retiene.
5. **Frases de pago o presión en español**: sus patrones son en inglés ("wire transfer", IBAN) y se les escapa "transferencia bancaria". Si usamos heurísticas parecidas, que cubran español.
6. **Dos tokens separados** (persona y agente) si algún día hay un panel: el del agente no puede aprobar, enviar ni tocar claves. Autenticación por cabecera `Authorization` (sin cookies, así sin CSRF clásico), comparación en tiempo constante, validación de `Host` y `Origin`, escucha solo en 127.0.0.1.
7. **Comprobar las banderas de seguridad de la herramienta antes de cada uso** y no operar si falta alguna (ellos lo hacen con `claude --help`). Es el mismo principio de nuestros verificadores: fallar cerrado si el entorno cambió.
8. **Scraping seguro, solo si añadimos prospección**: validar la IP de cada salto de redirección y conectar a la IP ya validada (evita el rebinding de DNS), ignorar proxies del entorno, rechazar esquemas raros y credenciales en la URL, y fijar un límite de tamaño de respuesta (en lo leído no se vio).
9. **Verificación de correos** (verified, risky, guess, invalid) antes de contactar en frío.
10. **Límites de envío diarios y horario de oficina del destinatario**, y un botón de pausa general, si algún día se envía.
11. **Funciones que no tenemos y pueden valer como idea**: panel visual local, exportación CSV, búsqueda de empresas en OpenStreetMap, señales de compra por análisis del sitio web.
12. **Poner nuestro proyecto en git**: ellos tienen historial, CI y changelog; nosotros no tenemos ni historial de cambios.

## Debilidades de Sellbird que NO conviene copiar
- **Alta, condicional:** CSP con `script-src 'unsafe-inline'` y token del panel en `sessionStorage`: si `app.js` inserta texto de correos o webs sin escapar, un correo hostil podría tomar el panel. No se leyó `app.js`.
- **Media:** autopilot con huecos (dominios desnudos con TLD poco común, pagos solo en inglés, copia de instrucciones detectada solo con 12 palabras idénticas).
- **Media:** `docker-compose` con `network_mode: host`, el proyecto y `~/.claude` montados con escritura, sin `read_only` ni `cap_drop`.
- **Media-baja:** el Dockerfile instala el CLI de Claude descargando un script sin fijar versión ni suma de comprobación.
- **Baja:** el panel podría imprimir en consola una URL con el token (según un resumen, sin verificar), devuelve `str(e)` en algunas rutas y hace `SELECT *` de prospectos.
- **Plataforma:** su proveedor Codex solo funciona en macOS y Linux (este equipo es Windows).

## Dónde nuestro diseño ya es más estricto
Nuestro agente no envía nunca y corre en un contenedor sin red con solo el MCP como salida; cada fase pasó por una auditoría independiente de Hermes con sus FAIL documentados; hay verificadores propios del contenedor y de los canales; los topes y el estado fallan cerrados.

## Cómo usar este documento
Antes de aplicar cualquier punto: diseño aprobado por el usuario, pruebas, re-fijar el pin del MCP y auditoría de Hermes (reglas de `docs/GUARDRAILS.md` y del `CLAUDE.md` global). Si se quiere profundizar, leer primero los archivos "no leídos" de arriba antes de afirmar nada sobre ellos.
