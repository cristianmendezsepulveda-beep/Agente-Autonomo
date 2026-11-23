# Procedencia de las skills instaladas aquí (para quien trabaja en este proyecto con Claude Code)

Estas skills NO son las del agente de ventas (esas están en `skills/` y se instalan en Hermes). Estas son para el desarrollador
(Claude Code) cuando construye o depura código de este proyecto. Instaladas el 2026-11-23 a petición del usuario.

- Origen: https://github.com/mattpocock/skills (licencia MIT, © 2026 Matt Pocock), rama `main`, commit
  `abeeaf95660ba9e8b7c753eb38f441ca707a8823`.
- Instalación: COPIA de archivos fijos de ese commit (no es el plugin: no se actualiza solo). Se descargó con `git clone --depth 1`
  en una carpeta aparte, sin ejecutar ningún script del repositorio. Se leyeron COMPLETOS los 9 archivos copiados antes de
  instalarlos: son prompts de texto; no hay llamadas de red ni instrucciones fuera de lo que dicen. El único script
  (`diagnosing-bugs/scripts/hitl-loop.template.sh`) es una plantilla interactiva de bash que no se ejecuta sola.
- No se copiaron las carpetas `agents/` (son para otro agente) ni ninguna otra skill del repositorio.
- Para actualizar: revisar el diff de la versión nueva leyendo los archivos, volver a copiar, y actualizar este registro.

| Archivo | sha256 |
|---|---|
| code-review/SKILL.md | 36847f010579f3bccd54c49793e1fd043f10cc49b4607bac5a3067522e528b91 |
| codebase-design/SKILL.md | 362b0bec828219d9f4b08ca7c466164e67253bdd817359258e60d2e9c5692a8f |
| codebase-design/DEEPENING.md | a67260cb384580b60430486edeb47a6e656ea5f8a42b7196b24461459031f00c |
| codebase-design/DESIGN-IT-TWICE.md | 43e788220bde5ce60c5f8932b6bde7a758ed63e22ddc0d658dbf6460d8950a6c |
| diagnosing-bugs/SKILL.md | aa87b0ac245c6d1b203c4678eb34dc101af02291448780c55c5e58ccc1619bd7 |
| diagnosing-bugs/scripts/hitl-loop.template.sh | 063ef14e7212113c7778258172e7c76e963926963e448934048f505d6e576e8f |
| tdd/SKILL.md | 969583080caf842ae86a53501a0091eced13fd89cad65d3aa75c09112318ff33 |
| tdd/mocking.md | 73a10b6a26b56197c738a764c1e4205af98c74553f4a713795997f6bc35b917d |
| tdd/tests.md | 0b401f98bd3684d2fea6440d6946afcf8f5f68ac6a65599dd7a2b90dd9f02eb5 |

## Límites de uso en ESTE proyecto (no es un repositorio git)
- `code-review` espera un punto fijo de git (`git diff <ref>...HEAD`) y un gestor de incidencias; aquí no hay git. Se aplica su idea
  de dos ejes (estándares vs. especificación) revisando contra `docs/GUARDRAILS.md` y el diseño aprobado, y la auditoría
  independiente sigue siendo la de Hermes (`hermes-audit.ps1`); estas skills NO la sustituyen.
- `tdd` pide acordar los "seams" antes de escribir pruebas: se enuncian en el plan de cada tarea y se confirman con el usuario
  cuando la decisión no es obvia. Las pruebas de este proyecto usan `unittest` y servidores o IMAP falsos locales.
- Si un hallazgo de estas skills contradice una regla del `CLAUDE.md` del usuario o de `docs/GUARDRAILS.md`, manda la regla.
