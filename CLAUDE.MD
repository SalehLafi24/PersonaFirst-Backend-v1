# PersonaFirst Backend Instructions

## Stack
- Python 3.12
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL
- pytest

## Rules
- Always use workspace_id for multi-tenancy
- Keep routes thin, logic in services
- Create migrations for DB changes
- Add tests for every feature
- Do not modify unrelated files
- Explain what you changed

## Structure
- app/models
- app/schemas
- app/services
- app/api/routes
- app/core
- tests/

## Commands
- Run server: uvicorn app.main:app --reload
- Run tests: pytest