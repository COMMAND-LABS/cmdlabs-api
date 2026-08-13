"""Shared exceptions for the tools package."""


class CredentialError(Exception):
    """Raised when a credential required by a tool is missing, invalid, or inaccessible."""
