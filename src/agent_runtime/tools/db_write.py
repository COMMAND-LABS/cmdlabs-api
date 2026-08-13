"""
Database Write Tool

Provides write access to external database tables via stored credentials.
Allows agents to insert records into user-configured databases.
"""
import logging
from typing import Any, Optional, TypedDict

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .db_read import get_connection_string, serialize_value
from .exceptions import CredentialError

logger = logging.getLogger(__name__)


# Type definitions for database write results
class DbWriteSuccess(TypedDict):
    """Successful database write result."""
    success: bool
    table: str
    inserted: dict[str, Any]
    message: str


class DbWriteError(TypedDict):
    """Error result from database write."""
    error: str


async def create_db_write_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs
) -> StructuredTool:
    """
    Create a database write tool for inserting records into external database tables.

    Args:
        tool_config: Tool configuration including:
            - credentialId: ID of stored credential with connection string
            - table: Table name to write to
            - description: Description for the LLM
            - columns: List of columns that can be written (required for security)
            - requiredColumns: List of columns that must be provided
            - injectAccountId: If true, automatically inject the account_id column
            - injectChatSessionId: If true, automatically inject the chat_session_id column
        account_id: Account ID for credential lookup and auto-injection
        db: Database session (for credential lookup)
        auth_token: Authentication token (unused)
        **kwargs: Additional context including:
            - chat_session_id: UUID of the current chat session (for injectChatSessionId)

    Returns:
        StructuredTool for database inserts

    Raises:
        CredentialError: If credential is invalid or missing
        ValueError: If configuration is invalid

    Example tool_config:
        {
            "type": "dbTableWrite",
            "credentialId": 6,
            "table": "leads",
            "description": "Create a new lead record with contact information",
            "columns": ["name", "email", "phone", "description"],
            "requiredColumns": ["name"],
            "injectAccountId": true,
            "injectChatSessionId": true
        }
    """
    credential_id = tool_config.get('credentialId')
    table_name = tool_config.get('table', '').strip()
    description = tool_config.get('description', f"Insert a record into {table_name} table")
    allowed_columns = tool_config.get('columns', [])
    required_columns = tool_config.get('requiredColumns', [])
    inject_account_id = tool_config.get('injectAccountId', False)
    inject_chat_session_id = tool_config.get('injectChatSessionId', False)

    # Get chat_session_id from kwargs (passed by stream.py)
    chat_session_id = kwargs.get('chat_session_id')
    logger.debug(f"[DB WRITE TOOL] 🔍 kwargs received: {list(kwargs.keys())}")
    logger.debug(f"[DB WRITE TOOL] 🔍 chat_session_id from kwargs: {chat_session_id}")
    logger.debug(f"[DB WRITE TOOL] 🔍 injectChatSessionId config: {inject_chat_session_id}")

    # Validate required fields
    if not credential_id:
        raise CredentialError("Missing required field 'credentialId' in dbTableWrite tool configuration")

    if not table_name:
        raise ValueError("Missing required field 'table' in dbTableWrite tool configuration")

    if not allowed_columns:
        raise ValueError("Missing required field 'columns' in dbTableWrite tool configuration. "
                        "You must specify which columns can be written for security.")

    # Validate requiredColumns are in allowed_columns
    invalid_required = [col for col in required_columns if col not in allowed_columns]
    if invalid_required:
        raise ValueError(f"requiredColumns contains columns not in allowed columns: {invalid_required}")

    # Get the connection string from the credential (raises CredentialError if fails).
    connection_string = get_connection_string(credential_id, account_id, db)

    # Config-trust path: dbTableWrite always declares its writable columns (see
    # the validation above), fixed when the agent was configured. We skip the
    # live schema reflection and build the engine lazily — no connect/inspect on
    # the request path. An invalid table/column surfaces as a clear error on the
    # first insert. No per-instance cache needed; identical on every Cloud Run
    # instance and across cold starts.
    try:
        external_engine = create_engine(
            connection_string,
            poolclass=NullPool,  # No persistent pool - connections close after each use
            pool_pre_ping=True,
        )
    except Exception as e:
        raise CredentialError(
            f"Failed to connect to database using credential {credential_id}: {e}"
        ) from e

    logger.info(f"[DB WRITE TOOL] Tool 'db_table_write' ready for table: {table_name}")
    logger.debug(f"[DB WRITE TOOL] Allowed columns: {allowed_columns}")
    logger.debug(f"[DB WRITE TOOL] Required columns: {required_columns}")
    logger.debug(f"[DB WRITE TOOL] Inject account_id: {inject_account_id}")
    logger.debug(f"[DB WRITE TOOL] Inject chat_session_id: {inject_chat_session_id}")

    # Define the insert implementation that accepts **kwargs for flat schema
    async def insert_impl(**kwargs) -> DbWriteSuccess | DbWriteError:
        """Insert a record into the external database table."""
        logger.debug(f"[DB WRITE TOOL] 🚀 TOOL INVOKED: db_table_write on table {table_name}")
        logger.debug(f"[DB WRITE TOOL] 📝 Input kwargs: {kwargs}")

        try:
            # Validate required columns are present
            missing_required = [col for col in required_columns if col not in kwargs or kwargs[col] is None]
            if missing_required:
                return {"error": f"Missing required columns: {missing_required}"}

            # Filter data to only allowed columns (should already be filtered by schema, but double-check)
            filtered_data = {}
            for col in allowed_columns:
                if col in kwargs and kwargs[col] is not None:
                    filtered_data[col] = kwargs[col]

            # Auto-inject account_id if configured
            # This ensures the record is associated with the authenticated user's account
            if inject_account_id:
                filtered_data['account_id'] = account_id
                logger.debug(f"[DB WRITE TOOL] 🔐 Auto-injected account_id: {account_id}")

            # Auto-inject chat_session_id if configured
            # This links the record to the chat session where it was created
            if inject_chat_session_id and chat_session_id:
                filtered_data['chat_session_id'] = str(chat_session_id)
                logger.debug(f"[DB WRITE TOOL] 🔐 Auto-injected chat_session_id: {chat_session_id}")

            if not filtered_data:
                return {"error": "No valid columns provided. "
                               f"Allowed columns: {allowed_columns}"}

            # Build the INSERT query
            columns = list(filtered_data.keys())
            columns_sql = ", ".join([f'"{col}"' for col in columns])
            placeholders = ", ".join([f":{col}" for col in columns])

            query_sql = f'INSERT INTO "{table_name}" ({columns_sql}) VALUES ({placeholders}) RETURNING *'

            logger.debug(f"[DB WRITE TOOL] 📡 Executing query: {query_sql}")
            logger.debug(f"[DB WRITE TOOL] 📝 Parameters: {filtered_data}")

            # Execute insert
            with external_engine.connect() as conn:
                result = conn.execute(text(query_sql), filtered_data)
                inserted_row = result.fetchone()
                column_names = result.keys()
                conn.commit()

            # Format the inserted row
            inserted_data = {}
            if inserted_row:
                for col_name, value in zip(column_names, inserted_row, strict=False):
                    # Only return allowed columns in the response
                    if col_name in allowed_columns or col_name == 'id':
                        inserted_data[col_name] = serialize_value(value)

            logger.info("[DB WRITE TOOL] ✅ Insert complete")
            logger.debug(f"[DB WRITE TOOL] 🎯 Inserted: {inserted_data}")

            return {
                "success": True,
                "table": table_name,
                "inserted": inserted_data,
                "message": f"Successfully inserted record into {table_name}"
            }

        except Exception as e:
            logger.exception(f"[DB WRITE TOOL] ❌ Insert failed: {e}")
            return {"error": str(e)}

    # Dynamically create a Pydantic model with each column as a direct field
    # This creates a flat schema that LLMs understand better than nested Dict fields
    field_definitions = {}
    for col in allowed_columns:
        # Required columns are not Optional, optional columns are Optional with None default
        if col in required_columns:
            field_definitions[col] = (
                str,  # Type - using str as it's most common, values get converted by DB
                Field(..., description=f"Value for column '{col}' (required)")
            )
        else:
            field_definitions[col] = (
                Optional[str],  # Optional type
                Field(default=None, description=f"Value for column '{col}' (optional)")
            )

    # Create the dynamic Pydantic model
    InsertInput = create_model(
        f"InsertInput_{table_name}",
        **field_definitions
    )

    # Update the model's docstring for better LLM understanding
    required_str = f" Required fields: {required_columns}." if required_columns else ""
    InsertInput.__doc__ = f"Input schema for inserting a record into {table_name}.{required_str}"

    logger.debug(f"[DB WRITE TOOL] Created dynamic schema with fields: {list(field_definitions.keys())}")

    tool_name_suffix = table_name.lower().replace(" ", "_")
    unique_tool_name = f"db_table_write_{tool_name_suffix}"
    return StructuredTool(
        func=insert_impl,
        coroutine=insert_impl,
        name=unique_tool_name,
        description=description,
        args_schema=InsertInput
    )
