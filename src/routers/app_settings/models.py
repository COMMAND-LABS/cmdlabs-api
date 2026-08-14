"""
Shared Pydantic models for the app settings router.
"""
from pydantic import BaseModel
from typing import Optional


class AppSettingsResponse(BaseModel):
    default_agent_id: Optional[int] = None
    elevenlabs_voice_id: Optional[str] = None


class UpdateAppSettingsRequest(BaseModel):
    """Partial update: only fields the client actually sent are applied.

    Explicit null clears a field (e.g. default_agent_id: null removes the
    default agent); an omitted field is left untouched. Handlers distinguish
    the two via model_fields_set.
    """
    default_agent_id: Optional[int] = None
    elevenlabs_voice_id: Optional[str] = None
