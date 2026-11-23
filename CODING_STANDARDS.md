# CODING_STANDARDS.md

## Propósito
Definir normas de código para el proyecto **Agente Autónomo** que serán usadas por el agente de revisión y por los desarrolladores humanos.

## Convenciones de nombres
- **Clases / Interfaces**: PascalCase (e.g., `CustomerService`).
- **Funciones / Métodos**: camelCase (e.g., `processOrder`).
- **Variables / Constantes**: snake_case para variables locales, UPPER_SNAKE_CASE para constantes.
- **Archivos**: kebab-case (`customer-service.py`).

## Formato de imports
- Agrupar imports de librerías estándar, de terceros y locales en bloques separados.
- Orden alfabético dentro de cada bloque.
- Evitar imports con `*`.

## Longitud de línea
- Máximo 100 caracteres.

## Documentación
- Cada módulo debe tener un docstring descriptivo.
- Cada función pública debe tener un docstring con descripción, args y return.

## Linter / Checks
- **Python**: `flake8` (configurado en `setup.cfg`).
- **TypeScript / JavaScript**: `eslint` con reglas de `standard`.

## Tests
- Todo código nuevo debe incluir al menos un test unitario.
- Cobertura mínima del 80%.

## Navegación y apuntadores
Si este archivo crece > 1 000 líneas, añadir **pointers** en `AGENTS.md` hacia secciones específicas.

---
Este archivo será leído por el skill `code-review` y por los agentes de CI para aplicar automáticamente estas normas.
