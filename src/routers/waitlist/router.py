import logging

from fastapi import APIRouter, Response, status, Request
from pydantic import BaseModel
from src.db.waitlist import Waitlist
from src.deps import db_dependency

logger = logging.getLogger(__name__)

router = APIRouter()

class JoinWaitlistRequestBody(BaseModel):
    email: str

@router.post("/join", deprecated=True)
async def create_account(db: db_dependency, body: JoinWaitlistRequestBody, request: Request):
    logger.warning("[DEPRECATED] POST /api/waitlist/join called")
    entry = Waitlist(email=body.email)
    db.add(entry)
    db.commit()

    return Response(status_code=status.HTTP_201_CREATED)
