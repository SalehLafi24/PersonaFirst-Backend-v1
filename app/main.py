from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    affinities,
    behavioral_relationships,
    customer_recommendations,
    health,
    purchases,
    recommendations,
    recommendations_explorer,
    recommendations_v1,
    relationships,
    signal_strength,
    taxonomy_admin,
    users,
    workspaces,
)
from app.core.config import settings
from app.core.database import Base, engine

app = FastAPI(title=settings.app_name, debug=settings.debug)

Base.metadata.create_all(bind=engine)

app.include_router(health.router)
app.include_router(workspaces.router)
app.include_router(users.router)
app.include_router(affinities.router)
app.include_router(purchases.router)
app.include_router(recommendations.router)
app.include_router(relationships.router)
app.include_router(behavioral_relationships.router)
app.include_router(signal_strength.router)
app.include_router(recommendations_v1.router)
app.include_router(taxonomy_admin.router)
app.include_router(recommendations_explorer.router)
app.include_router(customer_recommendations.public_router)
app.include_router(customer_recommendations.admin_router)

# Static files for the Recommendation Explorer single-page UI.
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "explorer"
if _STATIC_DIR.exists():
    app.mount(
        "/admin/explorer/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="explorer_static",
    )
