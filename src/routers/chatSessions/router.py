import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, status, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.services.org_scope import get_scoped_or_404
from src.db.models import ChatSession, ChatMessage, Contact
from src.services.agent_access import can_access_agent
import uuid
from datetime import datetime

from src.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

# Pydantic models for request/response
class ChatSessionCreate(BaseModel):
    # agentId is optional: contact-scoped sessions have no DB agent (the
    # contact-chat endpoint injects a code-defined config instead).
    agentId: Optional[int] = None
    title: Optional[str] = None
    contactId: Optional[int] = None

class ChatSessionUpdate(BaseModel):
    """Partial update — each field applies only when the client sent it.

    A PATCH carrying just agentId must not clear the title (and vice versa),
    so the handler reads model_fields_set rather than treating None as a value.
    """
    # Bounded so a pathological title cannot break the sidebar / list rendering.
    # The column itself is an unbounded String; this is the product-level cap.
    title: Optional[str] = Field(default=None, max_length=200)
    # Switch which agent this session runs. The session (and its transcript)
    # stays; only the agent answering the next turn changes.
    agentId: Optional[int] = None

def to_camel(s: str) -> str:
    parts = s.split('_')
    return parts[0] + ''.join(p.title() for p in parts[1:])

class ChatMessageResponse(BaseModel):
    id: int
    role: str
    content: str
    createdAt: datetime
    toolCalls: Optional[List[dict]] = None

class ChatSessionResponse(BaseModel):
    id: int
    sessionId: uuid.UUID
    agentId: Optional[int] = None
    accountId: int
    createdAt: datetime
    title: Optional[str] = None
    contactId: Optional[int] = None

class ChatSessionListResponse(BaseModel):
    """Paginated envelope for the sessions list.

    Mirrors the contacts/deals contract ({items, total, limit, offset,
    has_more}) so the frontend can drive the shared PaginationFooter with a
    real total instead of guessing from a bare array's length.
    """
    sessions: List[ChatSessionResponse]
    total: int
    limit: int
    offset: int
    has_more: bool

class ChatSessionWithMessagesResponse(BaseModel):
    id: int
    sessionId: uuid.UUID
    agentId: Optional[int] = None
    accountId: int
    createdAt: datetime
    title: Optional[str] = None
    contactId: Optional[int] = None
    messages: List[ChatMessageResponse] = []

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

# CRUD Operations for ChatSession

@router.post("/sessions", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_session(
    sessionData: ChatSessionCreate, 
    db: db_dependency, 
    jwt: jwt_dependency,
    org: org_dependency, 
    request: Request
):
    """Create a new chat session"""
    account_id = account_id_from_claims(jwt)

    # Verify the caller can access the requested agent
    if sessionData.agentId and not can_access_agent(
        db, account_id, sessionData.agentId, org_id=org.org_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this agent"
        )

    # Contact ownership gate: a session may only be bound to a contact the
    # caller's account owns. This is the layer-2 control that makes the
    # contact binding a trustworthy scope (404, not 403, to avoid leaking
    # the existence of other accounts' contact ids).
    if sessionData.contactId is not None:
        get_scoped_or_404(db, Contact, sessionData.contactId, org)

    # Generate a new UUID for the session
    session_uuid = str(uuid.uuid4())

    # Create the session
    new_session = ChatSession(
        session_id=session_uuid,
        agent_id=sessionData.agentId,
        account_id=jwt['id'],
        title=sessionData.title,
        contact_id=sessionData.contactId
    )

    db.add(new_session)
    db.commit()
    db.refresh(new_session)

    return {
        "id": new_session.id,
        "sessionId": new_session.session_id,
        "agentId": new_session.agent_id,
        "accountId": new_session.account_id,
        "createdAt": new_session.created_at,
        "title": new_session.title,
        "contactId": new_session.contact_id
    }

@router.get("/sessions", response_model=ChatSessionListResponse)
@limiter.limit("30/minute")
async def get_sessions(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
    agent_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=500, description="Number of sessions to return"),
    offset: int = Query(0, ge=0, description="Number of sessions to skip")
):
    """Get sessions for the authenticated user (server-side paginated).

    Contact-bound sessions are scoped artifacts of the contact drawer, not
    general chat history: they are excluded by default and returned only when
    an explicit ``contact_id`` is requested. This keeps them out of the global
    agent-chat history (where they would render with no agent).

    Returns a paginated envelope ({sessions, total, limit, offset, has_more});
    ``total`` counts the filtered set before pagination.
    """
    query = db.query(ChatSession).filter(ChatSession.account_id == jwt['id'])

    # Optionally filter by agent_id
    if agent_id is not None:
        query = query.filter(ChatSession.agent_id == agent_id)

    # Contact-bound sessions are hidden unless explicitly requested.
    if contact_id is not None:
        query = query.filter(ChatSession.contact_id == contact_id)
    else:
        query = query.filter(ChatSession.contact_id.is_(None))

    # Total before pagination, then the requested slice.
    total = query.count()
    rows = query.order_by(ChatSession.created_at.desc()).offset(offset).limit(limit).all()

    sessions = [{
        "id": s.id,
        "sessionId": s.session_id,
        "agentId": s.agent_id,
        "accountId": s.account_id,
        "createdAt": s.created_at,
        "title": s.title,
        "contactId": s.contact_id
    } for s in rows]

    return {
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }

@router.get("/sessions/{session_id}", response_model=ChatSessionWithMessagesResponse)
@limiter.limit("30/minute")
async def get_session(
    session_id: str,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """Get a specific session by session_id with its messages"""
    try:
        # Convert string to UUID for database query
        session_uuid = uuid.UUID(session_id)
        
        session = db.query(ChatSession).filter(
            ChatSession.session_id == session_uuid,
            ChatSession.account_id == jwt['id']
        ).first()
        
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        
        # Get all messages for this session
        messages = db.query(ChatMessage).filter(
            ChatMessage.chat_session_id == session.id
        ).order_by(ChatMessage.created_at.asc()).all()

        def _normalize_content(content) -> str:
            """Coerce Anthropic-style content block lists to a plain string."""
            if isinstance(content, list):
                return "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            return content if isinstance(content, str) else str(content)

        def convert_shape_of_message(msg):

            message_data = {
                "id": msg.id,
                "role": msg.message['role'],
                "content": _normalize_content(msg.message['content']),
                "createdAt": msg.created_at
            }
            
            # Include toolCalls if present in the message
            if 'toolCalls' in msg.message and msg.message['toolCalls']:
                message_data["toolCalls"] = msg.message['toolCalls']
            
            return message_data

        messages = [convert_shape_of_message(m) for m in messages]
        
        # Create response with session and messages
        response_data = {
            "id": session.id,
            "sessionId": session.session_id,
            "agentId": session.agent_id,
            "accountId": session.account_id,
            "createdAt": session.created_at,
            "title": session.title,
            "contactId": session.contact_id,
            "messages": messages
        }
        
        return response_data
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session ID format")
@router.patch("/sessions/{session_id}", response_model=ChatSessionResponse)
@limiter.limit("30/minute")
async def update_session(
    session_id: str,
    payload: ChatSessionUpdate,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """Rename a session and/or switch the agent it runs.

    Titles are what make a session list human-readable — without one the UI can
    only fall back to "Agent #<id>". A blank/whitespace-only title clears the
    field back to NULL so the fallback chain takes over again, rather than
    persisting an empty string that renders as a nameless row.

    Switching agentId keeps the session and its transcript; only the agent
    answering from the next turn on changes. Gated exactly like session
    creation: the caller must be able to access the new agent in this org.

    Rate-limited above the other mutations (30/min vs 10/min) because renaming
    is a title-only UPDATE and the sessions list invites several in a row.
    """
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session ID format"
        )

    session = db.query(ChatSession).filter(
        ChatSession.session_id == session_uuid,
        ChatSession.account_id == jwt['id']
    ).first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    if 'agentId' in payload.model_fields_set:
        if payload.agentId is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agentId cannot be cleared; send a new agent id"
            )
        # Contact-bound sessions have no DB agent by design: the contact-chat
        # endpoint injects a code-defined config, and the contact binding is a
        # server-trusted tool scope. Re-pointing one at an arbitrary agent
        # would silently drop that scope.
        if session.contact_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Contact-bound sessions cannot switch agents"
            )
        account_id = account_id_from_claims(jwt)
        if not can_access_agent(db, account_id, payload.agentId, org_id=org.org_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this agent"
            )
        session.agent_id = payload.agentId

    if 'title' in payload.model_fields_set:
        title = payload.title.strip() if payload.title is not None else None
        session.title = title or None

    db.commit()
    db.refresh(session)

    return {
        "id": session.id,
        "sessionId": session.session_id,
        "agentId": session.agent_id,
        "accountId": session.account_id,
        "createdAt": session.created_at,
        "title": session.title,
        "contactId": session.contact_id
    }

@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def delete_session(
    session_id: str, 
    db: db_dependency, 
    jwt: jwt_dependency,
    org: org_dependency, 
    request: Request
):
    """Delete a session and all its messages"""
    try:
        # Convert string to UUID for database query
        session_uuid = uuid.UUID(session_id)
        
        session = db.query(ChatSession).filter(
            ChatSession.session_id == session_uuid,
            ChatSession.account_id == jwt['id']
        ).first()
        
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        
        db.delete(session)
        db.commit()
        
        return None
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session ID format")
@router.delete("/sessions/{session_id}/messages", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def clear_session_messages(
    session_id: str, 
    db: db_dependency, 
    jwt: jwt_dependency,
    org: org_dependency, 
    request: Request
):
    """Clear all messages from a session without deleting the session itself"""
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid session ID format"
        )
        
    session = db.query(ChatSession).filter(
        ChatSession.session_id == session_uuid,
        ChatSession.account_id == jwt['id']
    ).first()
        
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Session not found"
        )
        
    deleted_count = db.query(ChatMessage).filter(
        ChatMessage.chat_session_id == session.id
    ).delete()
        
    db.commit()
        
    logger.info("[CLEAR MESSAGES] Deleted %d messages from session %s", deleted_count, session_id)
        
    return None
