
"""
Database Read Tool

Provides read access to external database tables via stored credentials.
Allows agents to query structured data from user-configured databases.
"""
import asyncio
import logging
import math
import uuid
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, TypedDict

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from src.services.credential_access import load_credential_for_use
from src.routers.credentials.encryption import decrypt_credential_data
from src.agent_runtime.tools.exceptions import CredentialError

logger = logging.getLogger(__name__)


# Type definitions for database read results
class DbReadResult(TypedDict):
    """A single row from database query."""
    data: dict[str, Any]


class DbReadSuccess(TypedDict):
    """Successful database read result."""
    results: list[DbReadResult]
    table: str
    count: int


class DbReadError(TypedDict):
    """Error result from database read."""
    error: str


def serialize_value(value: Any) -> Any:
    """Serialize a database value to JSON-compatible format.

    Tool results are json.dumps'd on the way to the LLM and into the SSE
    stream, so anything that survives this function must be a JSON primitive
    or a container of them. Driver types with no JSON equivalent — Decimal
    (NUMERIC), UUID, Interval, bytes, Enum — are converted here; the previous
    ``hasattr(value, '__dict__')`` catch-all missed all of them, because none
    of those types carry a ``__dict__``, and they were returned raw only to
    blow up later as "Object of type X is not JSON serializable".
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON — json.dumps emits them anyway and
        # the browser's JSON.parse then rejects the whole SSE frame.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        # NUMERIC columns (e.g. deals.amount). Emitted as a JSON number to
        # match how the REST API models the same columns (deals: float), so
        # the agent and the dashboard see one shape. Beyond ~15 significant
        # digits float64 cannot hold the value; those fall back to a string
        # rather than silently rounding.
        if not value.is_finite():
            return str(value)
        as_float = float(value)
        return as_float if Decimal(repr(as_float)) == value else str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, 'isoformat'):  # date / time / datetime
        return value.isoformat()
    if isinstance(value, timedelta):  # INTERVAL
        return str(value)
    if isinstance(value, Enum):
        return serialize_value(value.value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode('utf-8', errors='replace')
    if isinstance(value, dict):  # JSON/JSONB, or ARRAY elements
        return {str(k): serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serialize_value(v) for v in value]
    # Anything left (custom driver objects, ranges, network types) is rendered
    # as text — lossy, but always serializable.
    return str(value)


def get_connection_string(credential_id: int, account_id: int, db: Session) -> str:
    """
    Retrieve and decrypt the connection string from a stored credential.

    Args:
        credential_id: ID of the credential to look up
        account_id: Account ID for security (must own the credential)
        db: Database session

    Returns:
        Decrypted connection string

    Raises:
        CredentialError: If credential not found, unauthorized, wrong type, or decryption fails
    """
    # Look up the credential by usage access (owned or shared with the account).
    credential = load_credential_for_use(db, account_id, credential_id)

    if not credential:
        raise CredentialError(
            f"Credential with ID {credential_id} not found. "
            f"It may have been deleted or you don't have access to it."
        )

    if credential.auth_type != "db_connection":
        raise CredentialError(
            f"Credential {credential_id} is not a database connection. "
            f"Expected type 'db_connection', got '{credential.auth_type}'."
        )

    try:
        # Decrypt the credential data
        credential_data = decrypt_credential_data(credential.encrypted_data)

        # Get the connection string
        connection_string = credential_data.get("connection_string")
        if not connection_string:
            raise CredentialError(
                f"Credential {credential_id} does not contain a 'connection_string'. "
                f"Available keys: {list(credential_data.keys())}"
            )

        return connection_string
    except CredentialError:
        raise
    except Exception as e:
        raise CredentialError(f"Failed to decrypt credential {credential_id}: {e}") from e


def _connect_and_reflect_read(
    connection_string: str,
    credential_id: int,
    table_name: str,
    allowed_columns: list[str],
) -> tuple[Any, list[str]]:
    """Build the external engine, reflect the table schema, and validate columns.

    This is the slow part of tool setup: blocking network I/O (connect + schema
    reflection), often several seconds. It is extracted as a plain sync function
    so it can run via ``asyncio.to_thread`` and overlap with other tools being
    built concurrently. It touches only its own engine — never the shared
    request ``Session`` — so it is safe to run off the event-loop thread.

    Returns the live engine and the resolved list of selectable columns.
    """
    # Use NullPool to avoid creating persistent connection pools for each tool
    # Connections are created/closed on each query - better for tools that run infrequently
    try:
        external_engine = create_engine(
            connection_string,
            poolclass=NullPool,  # No persistent pool - connections close after each use
            pool_pre_ping=True
        )
        logger.info(f"[DB READ TOOL] Created connection to external database for table: {table_name}")
    except Exception as e:
        raise CredentialError(
            f"Failed to connect to database using credential {credential_id}: {e}"
        ) from e

    # Validate the table exists and get its columns
    try:
        with external_engine.connect():
            inspector = inspect(external_engine)
            tables = inspector.get_table_names()

            if table_name not in tables:
                available = tables[:10]
                raise ValueError(
                    f"Table '{table_name}' not found in database. "
                    f"Available tables: {available}{'...' if len(tables) > 10 else ''}"
                )

            # Get actual column names from the table
            table_columns = [col['name'] for col in inspector.get_columns(table_name)]
            logger.debug(f"[DB READ TOOL] Table '{table_name}' columns: {table_columns}")

            # Validate allowed_columns exist in the table
            if allowed_columns:
                invalid_columns = [col for col in allowed_columns if col not in table_columns]
                if invalid_columns:
                    raise ValueError(
                        f"Invalid columns specified: {invalid_columns}. "
                        f"Available columns in '{table_name}': {table_columns}"
                    )
                selected_columns = allowed_columns
            else:
                # If no columns specified, use all columns (not recommended for security)
                logger.warning("[DB READ TOOL] ⚠️ Warning: No columns specified, exposing all columns")
                selected_columns = table_columns

    except (CredentialError, ValueError):
        raise
    except Exception as e:
        raise ValueError(f"Failed to validate table '{table_name}': {e}") from e

    return external_engine, selected_columns


async def create_db_read_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs
) -> StructuredTool:
    """
    Create a database read tool for querying external database tables.

    Args:
        tool_config: Tool configuration including:
            - credentialId: ID of stored credential with connection string
            - table: Table name to query
            - name: Optional custom tool name
            - description: Description for the LLM
            - columns: List of columns to expose (required for security)
            - maxLimit: Maximum rows per query
        account_id: Account ID for credential lookup
        db: Database session (for credential lookup)
        auth_token: Authentication token (unused)
        **kwargs: Additional context (unused)

    Returns:
        StructuredTool for database queries, or None if setup fails

    Example tool_config:
        {
            "type": "dbTableRead",
            "credentialId": 6,
            "table": "users",
            "description": "Query user records from the users table",
            "columns": ["id", "name", "email", "created_at"],
            "maxLimit": 100
        }
    """
    credential_id = tool_config.get('credentialId')
    table_name = tool_config.get('table', '').strip()
    description = tool_config.get('description', f"Query data from {table_name} table")
    allowed_columns = tool_config.get('columns', [])
    max_limit = tool_config.get('maxLimit', 100)

    # Validate required fields
    if not credential_id:
        raise CredentialError("Missing required field 'credentialId' in dbTableRead tool configuration")

    if not table_name:
        raise ValueError("Missing required field 'table' in dbTableRead tool configuration")

    # For shared agents, use the agent owner's credentials so shared users
    # can read from the owner's database.  Write tools (db_write) intentionally
    # do NOT do this — write access requires the caller's own credentials.
    credential_account_id = kwargs.get('agent_owner_account_id', account_id)

    # Get the connection string from the credential (raises CredentialError if fails).
    # This uses the shared request Session, so it stays on the event-loop thread.
    connection_string = get_connection_string(credential_id, credential_account_id, db)

    if allowed_columns:
        # Config-trust path (the common, recommended case): the agent config
        # already declares which columns to expose, fixed when the agent was
        # configured. We skip the live schema reflection entirely and build the
        # engine lazily — no connect, no inspect — so there is no multi-second
        # round trip on the request path. An invalid table/column surfaces as a
        # clear error on first query. Needs no per-instance cache, so it behaves
        # identically on every Cloud Run instance and across cold starts.
        try:
            external_engine = create_engine(
                connection_string,
                poolclass=NullPool,
                pool_pre_ping=True,
            )
        except Exception as e:
            raise CredentialError(
                f"Failed to connect to database using credential {credential_id}: {e}"
            ) from e
        selected_columns = allowed_columns
    else:
        # No column allow-list configured: we must reflect the table to discover
        # its columns. This blocking connect+reflect runs in a worker thread so
        # tools still build concurrently (see create_tools_from_agent_config).
        external_engine, selected_columns = await asyncio.to_thread(
            _connect_and_reflect_read,
            connection_string,
            credential_id,
            table_name,
            allowed_columns,
        )

    logger.info(f"[DB READ TOOL] Tool 'db_table_read' ready for table: {table_name} (columns: {selected_columns})")

    # Define the query implementation
    async def query_impl(
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0
    ) -> DbReadSuccess | DbReadError:
        """Query the external database table with optional filters."""
        # DEBUG: Tool invocation
        logger.debug("[DB READ TOOL] 🚀 TOOL INVOKED: db_table_read")
        logger.debug(f"[DB READ TOOL] 📊 Table: {table_name}")
        logger.debug(f"[DB READ TOOL] 🔍 Filters: {filters}")
        logger.debug(f"[DB READ TOOL] 📈 Limit: {limit}, Offset: {offset}")

        try:
            # Enforce max limit
            if limit > max_limit:
                limit = max_limit
                logger.warning(f"[DB READ TOOL] ⚠️ Limit capped to max: {max_limit}")

            # Build the SELECT query with only allowed columns
            columns_sql = ", ".join([f'"{col}"' for col in selected_columns])
            query_sql = f'SELECT {columns_sql} FROM "{table_name}"'

            # Add WHERE clause for filters
            params = {}
            if filters:
                where_clauses = []
                for i, (column_name, value) in enumerate(filters.items()):
                    # Only allow filtering on allowed columns
                    if column_name in selected_columns:
                        param_name = f"p{i}"
                        where_clauses.append(f'"{column_name}" = :{param_name}')
                        params[param_name] = value
                        logger.debug(f"[DB READ TOOL] 🔍 Applied filter: {column_name} = {value}")
                    else:
                        logger.warning(f"[DB READ TOOL] ⚠️ Ignoring filter on non-allowed column: {column_name}")

                if where_clauses:
                    query_sql += " WHERE " + " AND ".join(where_clauses)

            # Add LIMIT and OFFSET
            query_sql += " LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = offset

            logger.debug(f"[DB READ TOOL] 📡 Executing query: {query_sql}")

            # Execute query
            with external_engine.connect() as conn:
                result = conn.execute(text(query_sql), params)
                rows = result.fetchall()
                column_names = result.keys()

            logger.info(f"[DB READ TOOL] ✅ Query complete: {len(rows)} rows returned")

            # Format results
            formatted_results = []
            for row in rows:
                row_data = {}
                for col_name, value in zip(column_names, row, strict=False):
                    row_data[col_name] = serialize_value(value)
                formatted_results.append({"data": row_data})

            logger.debug(f"[DB READ TOOL] 🎯 Returning {len(formatted_results)} results")

            return {
                "results": formatted_results,
                "table": table_name,
                "count": len(formatted_results)
            }

        except Exception as e:
            logger.exception("[DB READ TOOL] ❌❌❌ EXCEPTION CAUGHT ❌❌❌")
            logger.error(f"[DB READ TOOL] Error: {e}")
            logger.error(f"[DB READ TOOL] Type: {type(e).__name__}")
            return {"error": str(e)}

    # Define the Pydantic schema for the tool arguments
    class QueryInput(BaseModel):
        filters: dict[str, Any] | None = Field(
            default=None,
            description=f"Optional filters to apply. Allowed columns: {selected_columns}"
        )
        limit: int = Field(
            default=50,
            description=f"Maximum number of results to return (max: {max_limit})",
            ge=1,
            le=max_limit
        )
        offset: int = Field(
            default=0,
            description="Number of results to skip (for pagination)",
            ge=0
        )

    tool_name_suffix = table_name.lower().replace(" ", "_")
    unique_tool_name = f"db_table_read_{tool_name_suffix}"
    return StructuredTool(
        func=query_impl,
        coroutine=query_impl,
        name=unique_tool_name,
        description=description,
        args_schema=QueryInput
    )
