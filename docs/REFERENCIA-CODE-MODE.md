# Referencia: Code Mode de Cloudflare (MCP)

Fecha: 2026-11-23. Fuente: https://blog.cloudflare.com/code-mode-mcp/ (publicada 2026-02-20, modificada 2026-07-15). Es una guía de IDEAS: **nada de esto está aplicado en nuestro código.**

## Cómo se obtuvo y qué vale
Un subagente de solo lectura leyó únicamente esa página. No se abrieron los repositorios enlazados (`cloudflare/mcp`, `cloudflare/agents` en `packages/codemode`, `workers-oauth-provider`), así que **licencia y detalles del SDK no están verificados**. Se ignoró una instrucción incrustada en la cabecera de la página.

## Qué propone
- El servidor MCP expone solo dos herramientas, `search` y `execute`, cada una con un parámetro `code` (función JavaScript asíncrona).
- `search` filtra la especificación OpenAPI (sin meterla entera en el contexto); `execute` hace las llamadas autenticadas y devuelve solo lo necesario.
- El código corre en un aislado V8 (Dynamic Worker) sin sistema de archivos ni variables de entorno; las peticiones salientes están deshabilitadas salvo manejadores explícitos.
- Autenticación OAuth 2.1 con permisos recortados al conectar; para CI, un token de API como bearer.
- Cifra: más de 2.500 endpoints; unos 1.000 tokens fijos frente a 1,17 millones (el resumen dice más de 2 millones: no lo concilian); 99,9% menos de tokens de entrada, medido con tiktoken sin especificar.

## Qué no cubre la página
Precisión, latencia, coste o tasa de éxito; límites de recursos y tiempos del aislado; defensa contra inyección por datos devueltos por la API; auditoría; aprobación humana para escrituras; licencia.

## Por qué no encaja ahora
- Nuestro MCP tiene 14 herramientas: el ahorro de contexto es mínimo.
- Fijamos la lista de herramientas a propósito (manifiesto con hash, cuotas, registro por llamada). Código libre del modelo contra una API es lo contrario de mínima agencia, y un correo hostil podría inducir cadenas de llamadas.
- En HubSpot elegimos un cliente estrecho (4 operaciones, token sin borrar ni exportar) justo para no ampliar la superficie.

## Ideas aprovechables (por prioridad propuesta)
1. Devolver solo lo necesario (ya lo hacemos).
2. Si el MCP crece mucho: búsqueda perezosa de herramientas en lugar de ampliar la lista fija.
3. Procesar lotes dentro del servidor con código determinista nuestro y devolver un resumen (código auditado por nosotros, no escrito por el modelo).
4. Solo si algún día se conecta una API muy grande: ejecutar el código en un entorno sin red y con lista de operaciones permitidas.

Antes de aplicar cualquier punto: diseño aprobado, pruebas, re-fijar el pin del MCP y auditoría de Hermes. Relacionado: `docs/REFERENCIA-SELLBIRD.md`.
