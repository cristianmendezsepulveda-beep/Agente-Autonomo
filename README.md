# Agente Autónomo

![CI](https://github.com/cristianmendezsepulveda-beep/Agente-Autonomo/actions/workflows/ci.yml/badge.svg)

Este repositorio contiene el proyecto **Agente Autónomo**, una herramienta en Python que usa pre‑commit (Black) y está preparada para CI con GitHub Actions.

## Requisitos
- Python 3.14 (o compatible)
- `pip install -r requirements.txt`
- `pre-commit install`

## Uso
```bash
# Ejecutar pruebas con cobertura
pytest --cov=. --cov-fail-under=80

# Formatear código
black .
```
