from fastapi import APIRouter

from . import events, schemas, service, tracks

router = APIRouter()
for group in (schemas, tracks, events, service):
    router.include_router(group.router)
