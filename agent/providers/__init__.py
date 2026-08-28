"""
Provider factory.

Imports are lazy — done inside get_provider() rather than at module load — so
that using one provider (or just importing the neutral seam types from
providers.base) doesn't require *every* provider's SDK to be installed. Run
with only the anthropic SDK, or only aiohttp for Ollama, and it's fine.
"""


def get_provider(name: str, model: str | None = None):
    """
    Factory. Returns an initialized provider instance.

    Switch providers by setting TRILLION_PROVIDER in your .env:
        TRILLION_PROVIDER=claude   (default)
        TRILLION_PROVIDER=openai   (also works for OpenRouter)
        TRILLION_PROVIDER=ollama   (local, Raspberry Pi)

    `model` overrides the provider's env-configured default for this instance
    only. It exists for orchestration.md Tier 2's "a declared model per agent":
    a spawned specialist doing cheap, narrow work has no business burning the
    same model as the main conversation. None means "use the env default",
    which is what the main agent always passes.
    """
    name = name.lower().strip()

    if name == "claude":
        from .claude import ClaudeProvider
        return ClaudeProvider(model)
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(model)
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(model)

    raise ValueError(
        f"Unknown provider '{name}'. "
        "Set TRILLION_PROVIDER to one of: claude, openai, ollama"
    )


__all__ = ["get_provider"]
