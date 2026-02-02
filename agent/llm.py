"""LLM factory module for multi-provider support."""

import importlib
import os
from typing import Callable, Literal, Optional

import httpx
from langchain_openai import ChatOpenAI, AzureChatOpenAI

from core.logging import get_logger

logger = get_logger(__name__)

LLMTier = Literal["default", "fast"]

# Default models for each tier
DEFAULT_MODELS = {
    "default": "gpt-4o",
    "fast": "gpt-4o-mini",
}


def _get_http_clients() -> tuple[Optional[httpx.Client], Optional[httpx.AsyncClient]]:
    """Get HTTP clients with SSL verification settings."""
    ssl_verify = os.getenv("LLM_SSL_VERIFY", "true").lower()
    if ssl_verify in ("false", "0", "no"):
        return httpx.Client(verify=False), httpx.AsyncClient(verify=False)
    return None, None


def _load_function(func_path: str) -> Callable[[], str]:
    """
    Load a function from a module path.

    Args:
        func_path: Dot-separated path like 'mymodule.submodule.get_key'

    Returns:
        The callable function.
    """
    parts = func_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(
            f"Invalid function path: {func_path}. Expected format: 'module.function'"
        )
    module_path, func_name = parts
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    if not callable(func):
        raise ValueError(f"{func_path} is not callable")
    return func


def _get_api_key(env_var: str, func_env_var: str) -> str:
    """
    Get API key from environment variable or by calling a function.

    Args:
        env_var: Name of the static API key environment variable
        func_env_var: Name of the function path environment variable

    Returns:
        The API key string.
    """
    # Static key takes precedence
    api_key = os.getenv(env_var)
    if api_key:
        return api_key

    # Try function-based key
    func_path = os.getenv(func_env_var)
    if func_path:
        func = _load_function(func_path)
        return func()

    raise ValueError(
        f"API key not configured. Set {env_var} or {func_env_var} environment variable."
    )


def _get_openai_model(tier: LLMTier) -> ChatOpenAI:
    """Get OpenAI model for the specified tier."""
    model_name = os.getenv(
        f"LLM_MODEL_{'DEFAULT' if tier == 'default' else 'FAST'}",
        DEFAULT_MODELS[tier]
    )
    api_key = _get_api_key("OPENAI_API_KEY", "LLM_API_KEY_FUNCTION")
    http_client, async_http_client = _get_http_clients()

    kwargs = {"model": model_name, "api_key": api_key}
    if http_client:
        kwargs["http_client"] = http_client
    if async_http_client:
        kwargs["http_async_client"] = async_http_client

    return ChatOpenAI(**kwargs)


def _get_azure_model(tier: LLMTier) -> AzureChatOpenAI:
    """Get Azure OpenAI model for the specified tier."""
    deployment_env = f"AZURE_OPENAI_DEPLOYMENT_{'DEFAULT' if tier == 'default' else 'FAST'}"
    deployment = os.getenv(deployment_env)

    if not deployment:
        raise ValueError(
            f"Azure deployment not configured. Set {deployment_env} environment variable."
        )

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise ValueError(
            "Azure endpoint not configured. Set AZURE_OPENAI_ENDPOINT environment variable."
        )

    api_key = _get_api_key("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_KEY_FUNCTION")
    # Tier-specific API version with fallback to shared version
    version_env = f"AZURE_OPENAI_API_VERSION_{'DEFAULT' if tier == 'default' else 'FAST'}"
    api_version = os.getenv(version_env) or os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
    http_client, async_http_client = _get_http_clients()

    kwargs = {
        "azure_deployment": deployment,
        "azure_endpoint": endpoint,
        "api_key": api_key,
        "api_version": api_version,
    }
    if http_client:
        kwargs["http_client"] = http_client
    if async_http_client:
        kwargs["http_async_client"] = async_http_client

    return AzureChatOpenAI(**kwargs)


def get_llm(tier: LLMTier = "default") -> ChatOpenAI | AzureChatOpenAI:
    """
    Get an LLM instance for the specified tier.

    Args:
        tier: "default" for heavy tasks (planner, builder, generator)
              "fast" for light tasks (classifier, reporter)

    Returns:
        Configured LLM instance based on LLM_PROVIDER environment variable.
        Defaults to OpenAI if not specified.
    """
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "azure":
        llm = _get_azure_model(tier)
        deployment = os.getenv(f"AZURE_OPENAI_DEPLOYMENT_{'DEFAULT' if tier == 'default' else 'FAST'}")
        logger.debug(f"LLM: provider=azure, tier={tier}, deployment={deployment}")
        return llm
    elif provider == "openai":
        llm = _get_openai_model(tier)
        model_name = os.getenv(
            f"LLM_MODEL_{'DEFAULT' if tier == 'default' else 'FAST'}",
            DEFAULT_MODELS[tier]
        )
        logger.debug(f"LLM: provider=openai, tier={tier}, model={model_name}")
        return llm
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. Supported: openai, azure"
        )


def _has_api_key(env_var: str, func_env_var: str) -> bool:
    """Check if API key is available via env var or function."""
    if os.getenv(env_var):
        return True
    func_path = os.getenv(func_env_var)
    if func_path:
        # Validate the function can be loaded (but don't call it yet)
        try:
            _load_function(func_path)
            return True
        except (ImportError, AttributeError, ValueError):
            return False
    return False


def validate_config() -> None:
    """
    Validate LLM configuration at startup.

    Raises:
        ValueError: If required configuration is missing.
    """
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if provider == "azure":
        missing = []
        if not _has_api_key("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_KEY_FUNCTION"):
            missing.append("AZURE_OPENAI_API_KEY or AZURE_OPENAI_API_KEY_FUNCTION")
        if not os.getenv("AZURE_OPENAI_ENDPOINT"):
            missing.append("AZURE_OPENAI_ENDPOINT")
        if not os.getenv("AZURE_OPENAI_DEPLOYMENT_DEFAULT"):
            missing.append("AZURE_OPENAI_DEPLOYMENT_DEFAULT")
        if not os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST"):
            missing.append("AZURE_OPENAI_DEPLOYMENT_FAST")

        if missing:
            raise ValueError(
                f"Azure OpenAI configuration incomplete. Missing: {', '.join(missing)}"
            )
    elif provider == "openai":
        if not _has_api_key("OPENAI_API_KEY", "LLM_API_KEY_FUNCTION"):
            raise ValueError(
                "OpenAI configuration incomplete. Set OPENAI_API_KEY or LLM_API_KEY_FUNCTION."
            )
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. Supported: openai, azure"
        )
