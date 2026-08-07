"""
Pydantic request/response models for credential access grants.
"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class CreateCredentialGrantRequest(BaseModel):
    """Share a credential with ONE named person, by email.

    A credential can be shared with a person and NEVER with a space. It is an
    API key with a bill attached: the org that owns it is the one being
    charged, and a space's audience crosses orgs by design. Enforced by
    space_resources' resource_type CHECK, which admits agents and knowledge
    bases only.
    """
    granteeEmail: str


class CredentialGrantResponse(BaseModel):
    id: int
    credential_id: int
    grantee_account_id: int
    # Human-readable label for display: the grantee's email.
    label: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
