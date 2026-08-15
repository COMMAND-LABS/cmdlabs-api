"""Shared agent context preparation.

Extracts the common setup logic used by the streaming agent endpoints
(the generic agent stream and the contact-chat stream) into a single place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from langchain_classic.agents import (
    AgentExecutor,
    create_openai_tools_agent,
    create_tool_calling_agent,
)
from langchain_classic.memory import ConversationBufferMemory
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import StructuredTool

from src.db.database import SessionLocal
from src.db.models import Account, Agent, ChatMessage, ChatSession, OrganizationMember
from src.db.retry import db_retry_once
from src.services.agent_access import load_agent_with_access_check
from src.services.org_scope import OrgScope
from src.agent_runtime.contact_agent_config import (
    CONTACT_AGENT_NAME,
    contact_session_required,
)
from src.agent_runtime.helpers import (
    build_message_history,
    create_llm,
    extract_auth_token,
    get_model_config,
    get_required_credential_type,
    store_ai_message,
    store_user_message,
)
from src.routers.credentials.encryption import get_credential_value
from src.services.credential_access import (
    resolve_default_credential,
    can_use_credential,
    load_credential_for_use,
)
from src.agent_runtime.skills import (
    build_skills_guidance,
    create_load_skill_tool,
    load_agent_skills,
)
from src.agent_runtime.tools import CredentialError, create_tools_from_agent_config
from src.agent_runtime.tools.think import THINK_SYSTEM_GUIDANCE
from src.utils.pdf_to_images import (
    build_document_message,
    build_image_message,
    build_pdf_message,
)
from src.utils.template_variables import (
    build_variable_context,
    resolve_template_variables,
)



def _resolve_org_scope(db, account_id: int, agent) -> OrgScope:
    """The organization this run acts in.

    Taken from the AGENT, not the caller: an agent belongs to one org and its
    tools read that org's data, so a caller reaching a shared agent must be
    acting inside the agent's tenant or not at all.

    The membership check is what makes that safe — without it, a caller who
    obtained an agent id could have its tools run against an org they do not
    belong to. It is the same rule ai-api's get_org_context enforces per
    request; the agent runtime needs its own because tools do not run on the
    request session.

    On the code-defined (contact-chat) path there is no agent row, so the
    caller's default org is used. That path is already gated by the
    session<->contact ownership check made at session creation, and the tenancy
    predicate then fails closed: a contact outside this org simply is not found.

    The membership check is the whole point and is why this reads the
    membership table rather than just trusting the agent's org_id.
    """
    org_id = getattr(agent, "org_id", None) if agent is not None else None
    if org_id is None:
        org_id = db.query(Account.default_org_id).filter(
            Account.id == account_id).scalar()
    if org_id is None:
        raise AgentSetupError(
            "No organization",
            "Your account is not a member of any organization.")

    member = (
        db.query(OrganizationMember.id)
        .filter(
            OrganizationMember.org_id == org_id,
            OrganizationMember.account_id == account_id,
        )
        .first()
    )
    if member is None:
        raise AgentSetupError(
            "Agent not found",
            "The specified agent was not found or you do not have access.")

    return OrgScope(account_id=account_id, org_id=org_id)


@dataclass
class AgentContext:
    """Everything the streaming endpoints need after setup."""

    agent: Agent | None  # None on the code-defined (override) path
    account_id: int
    provider: str
    model_name: str
    llm: BaseChatModel
    tools: list[StructuredTool]
    prompt_template: ChatPromptTemplate
    memory: ConversationBufferMemory
    message_history: ChatMessageHistory
    agent_executor: AgentExecutor | None
    agent_input: Any
    chat_session_id: int
    session_uuid: uuid.UUID
    user_email: str
    prompt: str
    pdf_filename: str | None
    # GCS-backed attachment reference persisted onto the chat message, or None.
    attachment_ref: dict | None
    callbacks: list


class AgentSetupError(Exception):
    """Raised when agent setup fails with a user-facing message."""

    def __init__(self, title: str, detail: str):
        self.title = title
        self.detail = detail
        super().__init__(f"{title}: {detail}")


async def prepare_agent_context(
    *,
    agent_id: int | None = None,
    session_id: str,
    prompt: str,
    db,
    auth: dict,
    request: Request,
    callbacks: list,
    pdf_base64: str | None = None,
    pdf_filename: str | None = None,
    pdf_use_vision: bool = False,
    image_base64: str | None = None,
    document_text: str | None = None,
    attachment_filename: str | None = None,
    attachment_content_type: str | None = None,
    gcs_bucket: str | None = None,
    gcs_file_path: str | None = None,
    agent_config_override: dict | None = None,
) -> AgentContext:
    """Build the full agent context shared by the streaming agent endpoints.

    Raises ``AgentSetupError`` for any user-facing failure.
    """
    account_id = auth["id"]

    if agent_config_override is not None:
        # Server-fixed agent (e.g. contact-chat). It is NOT user-selected, so
        # the generic per-account access check does not apply: authorization
        # for this path is the session<->contact ownership gate (validated at
        # session creation in ai-api) plus the per-tool account_id filter.
        # We deliberately skip load_agent_with_access_check here.
        agent = None
        agent_config = agent_config_override
        agent_name = CONTACT_AGENT_NAME
        agent_owner_account_id = account_id
    else:
        agent = db_retry_once(
            db, "load agent",
            lambda: load_agent_with_access_check(db, account_id, agent_id),
        )
        if not agent:
            raise AgentSetupError("Agent not found", "The specified agent was not found or you do not have access.")
        if not agent.config:
            raise AgentSetupError("Invalid agent configuration", "Agent configuration is missing.")
        agent_config = agent.config
        agent_name = agent.name
        agent_owner_account_id = agent.account_id

    org_scope = _resolve_org_scope(db, account_id, agent)

    config_data = agent_config.get("data", {})

    # --- Turn-completion credential principal ---
    # Which account's stored LLM provider credential funds this run's turn
    # completions. The owner always runs on their own. A non-owner (group member
    # accessing via an access-group grant) runs on the owner's credential only
    # when the owner opted in via `shareOwnerCredentials`; otherwise the member
    # uses their own. When the caller IS the owner, owner == caller so the flag
    # is a no-op.
    #
    # Tool credentials are resolved separately, inside the tool builders, which
    # already apply a deliberate per-tool policy (read/pinecone/email use the
    # owner's credentials so shared members can use them; db_write uses the
    # caller's). That policy is intentionally NOT governed by this flag.
    share_owner_credentials = bool(config_data.get("shareOwnerCredentials", False))
    completion_credential_account_id = (
        agent_owner_account_id if share_owner_credentials else account_id
    )

    system_prompt_raw = config_data.get("systemPrompt", "You are a helpful assistant.")
    var_context = build_variable_context(agent_name=agent_name)
    system_prompt = resolve_template_variables(system_prompt_raw, var_context).replace("{", "{{").replace("}", "}}")

    model_config = get_model_config(agent_config)
    provider = model_config["provider"]
    model_name = model_config["model"]

    # --- Credentials ---
    required_credential_type = get_required_credential_type(provider)
    credentials: dict[str, str] = {}
    if required_credential_type:
        # Explicit binding wins: if the agent config pins a credentialId AND the
        # funding account can use it (owner's own key, or one shared with a member
        # running on the owner's credentials), use exactly that — no drift. Else
        # fall back to the funding account's default for the provider type.
        pinned_credential_id = model_config.get("credentialId")

        def _load_completion_credential():
            if pinned_credential_id is not None and can_use_credential(
                db, completion_credential_account_id, pinned_credential_id
            ):
                return load_credential_for_use(
                    db, completion_credential_account_id, pinned_credential_id
                )
            return resolve_default_credential(
                db, completion_credential_account_id, required_credential_type
            )

        credential = db_retry_once(
            db, "load provider credential", _load_completion_credential
        )
        if not credential:
            # If this run is funded by the owner's credentials (shared agent) but
            # the owner has not configured the provider key, the member cannot fix
            # it — point them at the owner instead of "your account settings".
            uses_owner_credentials = completion_credential_account_id != account_id
            if uses_owner_credentials:
                detail = (
                    f"This shared agent runs on the owner's credentials, but the owner "
                    f"has not configured a {provider.title()} API key for {model_name}. "
                    f"Ask the agent owner to add it."
                )
            else:
                detail = (
                    f"Please add your {provider.title()} API key in account settings "
                    f"to use {model_name}."
                )
            raise AgentSetupError(f"{provider.title()} API key required", detail)
        try:
            credentials[provider] = get_credential_value(credential, "api_key")
        except Exception as exc:
            raise AgentSetupError("Failed to retrieve API key", str(exc)) from exc

    # --- LLM ---
    try:
        llm, _ = create_llm(
            model_config=model_config,
            credentials=credentials,
            temperature=0,
        )
    except ValueError as exc:
        raise AgentSetupError("LLM initialization failed", str(exc)) from exc

    # --- Session ---
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        raise AgentSetupError("Invalid sessionId format", "The sessionId must be a valid UUID format.") from None

    session = db_retry_once(
        db, "load chat session",
        lambda: db.query(ChatSession).filter(
            ChatSession.session_id == session_uuid,
            ChatSession.account_id == account_id,
        ).first(),
    )
    if not session:
        try:
            session = ChatSession(
                session_id=session_uuid,
                agent_id=agent_id,
                account_id=account_id,
                title=f"Chat with Agent {agent_id}",
            )
            db.add(session)
            db.commit()
            db.refresh(session)
        except Exception as exc:
            db.rollback()
            raise AgentSetupError("Failed to create session", f"Could not create chat session: {exc}") from exc

    # --- Contact scope (fail closed) ---
    # The session<->contact binding is the server-trusted scope. If the agent
    # declares contact-scoped tools but the session has no bound contact, the
    # agent must refuse rather than run unscoped.
    contact_id = session.contact_id
    if contact_session_required(config_data) and contact_id is None:
        raise AgentSetupError(
            "Contact context required",
            "This agent's tools require a contact-bound chat session.",
        )

    # --- History ---
    db_messages = db_retry_once(
        db, "load chat messages",
        lambda: db.query(ChatMessage).filter(
            ChatMessage.chat_session_id == session.id,
        ).order_by(ChatMessage.created_at.asc()).all(),
    )
    message_history = build_message_history(db_messages)
    auth_token = extract_auth_token(request, auth)

    # --- Tools ---
    try:
        tools = await create_tools_from_agent_config(
            agent_config=agent_config,
            account_id=account_id,
            db=db,
            auth_token=auth_token,
            request=request,
            chat_session_id=session_uuid,
            agent_id=agent_id,
            chat_session_id_pk=session.id,
            agent_owner_account_id=agent_owner_account_id,
            org_scope=org_scope,
            contact_id=contact_id,
        )
    except CredentialError as exc:
        raise AgentSetupError("Tool configuration error", str(exc)) from exc
    except ValueError as exc:
        raise AgentSetupError("Invalid tool configuration", str(exc)) from exc

    # --- Skills (progressive disclosure) ---
    # Index in the prompt, body behind the load_skill tool — see
    # agent_runtime/skills.py. Placed before the tools/no-tools branch below
    # because attaching a skill is what may give a previously toolless agent
    # its first tool, which flips it onto the AgentExecutor path.
    # build_skills_guidance returns pre-escaped text (the same {→{{ treatment
    # the base prompt got above); the override path (contact-chat) has no
    # agent row and therefore no skills.
    attached_skills = load_agent_skills(db, agent, org_scope)
    if attached_skills:
        system_prompt = system_prompt + build_skills_guidance(attached_skills)
        tools = tools + [create_load_skill_tool(attached_skills)]

    # --- Prompt template + agent ---
    # The model only takes multiple steps if invited to: without this nudge,
    # most models answer in one shot and the think tool sits unused.
    if any(t.name == "think" for t in tools):
        system_prompt = system_prompt + THINK_SYSTEM_GUIDANCE

    if tools:
        prompt_template = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        tagged_llm = llm.with_config({"tags": ["agent_llm"]})
        if provider == "openai":
            agent_langchain = create_openai_tools_agent(tagged_llm, tools, prompt_template)
        else:
            agent_langchain = create_tool_calling_agent(tagged_llm, tools, prompt_template)
    else:
        prompt_template = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{input}"),
        ])
        agent_langchain = None

    memory = ConversationBufferMemory(
        memory_key="chat_history",
        chat_memory=message_history,
        return_messages=True,
        output_key="output" if tools else None,
    )

    user_email = auth.get("email", "unknown")
    agent_executor: AgentExecutor | None = None
    if tools and agent_langchain:
        agent_executor = AgentExecutor(
            agent=agent_langchain,
            tools=tools,
            memory=memory,
            max_iterations=25,
        ).with_config({
            "run_name": "Agent",
            "callbacks": callbacks,
            "metadata": {"user_email": user_email, "agent_id": agent_id, "session_id": str(session_uuid)},
            "tags": [f"user:{user_email}", f"agent:{agent_id}"],
        })

    # --- Agent input (text or attachment) ---
    # The current turn's attachment content rides inline to the model. The
    # durable copy already lives in the account's GCS bucket (referenced by
    # attachment_ref below); we do not re-download it here.
    if pdf_base64:
        # PyMuPDF rasterization/text extraction is CPU-bound (seconds for a
        # large PDF). Run it on the threadpool so it never stalls the event
        # loop — one blocking parse here would freeze every other request on
        # this instance, including in-flight SSE streams.
        agent_input = await run_in_threadpool(
            build_pdf_message,
            prompt=prompt,
            pdf_base64=pdf_base64,
            pdf_filename=pdf_filename,
            use_vision=pdf_use_vision,
            max_pages=10 if pdf_use_vision else 50,
        )
    elif image_base64:
        agent_input = build_image_message(
            prompt=prompt,
            image_base64=image_base64,
            content_type=attachment_content_type or "image/png",
            filename=attachment_filename,
        )
    elif document_text:
        agent_input = build_document_message(
            prompt=prompt,
            document_text=document_text,
            filename=attachment_filename,
        )
    else:
        agent_input = prompt

    # Build the persisted attachment reference (GCS-backed) for the chat message.
    attachment_ref: dict | None = None
    if gcs_bucket and gcs_file_path:
        if pdf_base64:
            attachment_type = "pdf"
        elif image_base64:
            attachment_type = "image"
        else:
            attachment_type = "document"
        attachment_ref = {
            "type": attachment_type,
            "filename": attachment_filename or pdf_filename,
            "gcs_bucket": gcs_bucket,
            "gcs_file_path": gcs_file_path,
            "content_type": attachment_content_type,
        }

    # Release the DB connection before the long-running LLM call
    chat_session_id = session.id
    db.close()

    return AgentContext(
        agent=agent,
        account_id=account_id,
        provider=provider,
        model_name=model_name,
        llm=llm,
        tools=tools,
        prompt_template=prompt_template,
        memory=memory,
        message_history=message_history,
        agent_executor=agent_executor,
        agent_input=agent_input,
        chat_session_id=chat_session_id,
        session_uuid=session_uuid,
        user_email=user_email,
        prompt=prompt,
        pdf_filename=pdf_filename,
        attachment_ref=attachment_ref,
        callbacks=callbacks,
    )


# ---------------------------------------------------------------------------
# Short-lived DB session wrappers for message persistence
# ---------------------------------------------------------------------------

def persist_user_message(
    chat_session_id: int,
    prompt: str,
    pdf_filename: str | None = None,
    attachment_ref: dict | None = None,
):
    """Write user message using a short-lived DB session."""
    db = SessionLocal()
    try:
        store_user_message(db, chat_session_id, prompt, pdf_filename, attachment_ref=attachment_ref)
    finally:
        db.close()


def persist_ai_message(chat_session_id: int, content: str, tool_calls=None, blocks=None):
    """Write AI message using a short-lived DB session."""
    db = SessionLocal()
    try:
        store_ai_message(db, chat_session_id, content, tool_calls, blocks=blocks)
    finally:
        db.close()
