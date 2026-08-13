"""Shared LangSmith tracing setup."""

import os

if not os.getenv("LANGCHAIN_API_KEY") and os.getenv("LANGSMITH_API_KEY"):
    os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGSMITH_API_KEY")


def get_langsmith_callbacks(project_name: str) -> list:
    """Return a ``[LangChainTracer]`` list when LangSmith is configured, else ``[]``."""

    # return []

    api_key = os.getenv("LANGSMITH_API_KEY")
    if not api_key:
        return []

    from langchain_core.tracers import LangChainTracer
    from langsmith import Client

    return [
        LangChainTracer(
            project_name=project_name,
            client=Client(
                api_url=os.getenv("LANGSMITH_ENDPOINT"),
                api_key=api_key,
            ),
        )
    ]
