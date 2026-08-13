from fastapi import APIRouter, Response, status, Request
from pydantic import BaseModel
from src.db.waitlist import Waitlist
from src.deps import db_dependency

router = APIRouter()

class JoinWaitlistRequestBody(BaseModel):
    email: str

@router.post("/join")
async def create_account(db: db_dependency, body: JoinWaitlistRequestBody, request: Request):
    entry = Waitlist(email=body.email)
    db.add(entry)
    db.commit()

    return Response(status_code=status.HTTP_201_CREATED)
