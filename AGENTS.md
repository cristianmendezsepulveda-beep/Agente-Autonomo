# AGENTS.md

## Propósito
Este archivo guía a los agentes autónomos que trabajan en el proyecto **Agente Autónomo**. Contiene punteros de navegación, alcance permitido y comandos esenciales.

## Punteros de navegación
- **Normas de código** → [`CODING_STANDARDS.md`](./CODING_STANDARDS.md)
- **Documentación del proyecto** → [`docs/`](./docs/)
- **Skills de agente** → [`.claude/skills/`](./.claude/skills/)
- **Datos de trabajo** → [`workspace/`](./workspace/)

## Alcance permitido
- **Permitido modificar**: `src/`, `docs/`, `.claude/skills/`, `workspace/`.
- **Prohibido modificar**: `infra/`, `legacy/`, cualquier archivo fuera del árbol del proyecto.

## Comandos esenciales (para CI / pre‑commit)
```bash
# Instalar dependencias (Python)
pip install -r requirements.txt

# Lint de código
flake8 .

# Tests
pytest
```

## Calidad y definición de "Done"
1. El código compila / interpreta sin errores.
2. Todos los tests pasan y la cobertura es ≥ 80 %.
3. No hay errores de lint.
4. Commits atómicos siguiendo Conventional Commits.

---
Este archivo será cargado automáticamente por los agentes al iniciar una sesión en este repositorio.
