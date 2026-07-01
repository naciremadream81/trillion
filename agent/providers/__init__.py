"""
Provider factory.

Imports are lazy — done inside get_provider() rather than at module load — so
that using one provider (or just importing the neutral seam types from
providers.base) doesn't require *every* provider's SDK to be installed. Run
with only the anthropic SDK, or only aiohttp for Ollama, and it's fine.
"""


def get_provider(name: str):
    """
    Factory. Returns an initialized provider instance.

    Switch providers by setting TRILLION_PROVIDER in your .env:
        TRILLION_PROVIDER=claude   (default)
        TRILLION_PROVIDER=openai   (also works for OpenRouter)
        TRILLION_PROVIDER=ollama   (local, Raspberry Pi)
    """
    name = name.lower().strip()

    if name == "claude":
        from .claude import ClaudeProvider
        return ClaudeProvider()
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider()
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider()

    raise ValueError(
        f"Unknown provider '{name}'. "
        "Set TRILLION_PROVIDER to one of: claude, openai, ollama"
    )


__all__ = ["get_provider"]
