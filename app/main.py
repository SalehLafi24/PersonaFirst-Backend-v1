from fastapi import FastAPI

from app.api.routes import affinities, behavioral_relationships, health, purchases, recommendations, relationships, users, workspaces
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
