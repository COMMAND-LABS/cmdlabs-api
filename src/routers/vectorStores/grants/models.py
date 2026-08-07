"""
Pydantic request/response models for knowledge-base (vector store) access grants.
"""
from pydantic import BaseModel, ConfigDict, model_validator
from datetime import datetime


class CreateVectorStoreGrantRequest(BaseModel):
    """Share a knowledge base with ONE named person, at a given role.

    role is 'read' (view) or 'write' (ingest/edit). Sharing with a set of
    people is putting the knowledge base in a SPACE, which is always read-only:
    a space share offers something to be consulted, never reconfigured.
    """
    index_name: str
    granteeEmail: str
    role: str = "read"

    @model_validator(mode="after")
    def _validate(self):
        if self.role not in ("read", "write"):
            raise ValueError("role must be 'read' or 'write'")
        return self


class VectorStoreAccessGrantResponse(BaseModel):
    id: int
    owner_account_id: int
    index_name: str
    grantee_account_id: int
    label: str
    role: str         # 'read' | 'write'
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SharedVectorStore(BaseModel):
    """A knowledge base the caller can reach without owning it.

    By a grant naming them, or by membership of a space it was shared into.
    `can_write` is only ever true for a grant — see vector_store_access.
    """

    owner_account_id: int
    index_name: str
    can_write: bool
