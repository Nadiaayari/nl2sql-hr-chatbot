"""
Client LLM avec fallback automatique.
Essaie Groq (modèle principal) puis bascule sans interruption vers des modèles alternatifs
en cas de limite de tokens / rate limit.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

try:
    from groq import Groq
    from groq import APIStatusError as GroqAPIStatusError
except ImportError:
    raise ImportError("pip install groq")

try:
    from openai import OpenAI, APIStatusError as OpenAIAPIStatusError
except ImportError:
    OpenAI = None  # type: ignore
    OpenAIAPIStatusError = Exception  # type: ignore


# ─── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

MODEL_PRIMARY = os.getenv("GROQ_MODEL_PRIMARY", "llama-3.3-70b-versatile")
MODEL_FAST = os.getenv("GROQ_MODEL_FAST", "llama-3.1-8b-instant")

OPENROUTER_MODEL_PRIMARY = os.getenv(
    "OPENROUTER_MODEL_PRIMARY", "meta-llama/llama-3.3-70b-instruct:free"
)
OPENROUTER_MODEL_FAST = os.getenv(
    "OPENROUTER_MODEL_FAST", "meta-llama/llama-3.1-8b-instruct:free"
)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Chaînes de fallback par modèle Groq demandé : (provider, model_id)
FALLBACK_CHAINS: Dict[str, List[Tuple[str, str]]] = {
    MODEL_PRIMARY: [
        ("groq", MODEL_PRIMARY),
        ("groq", MODEL_FAST),
        ("openrouter", OPENROUTER_MODEL_PRIMARY),
        ("openrouter", OPENROUTER_MODEL_FAST),
        ("gemini", GEMINI_MODEL),
    ],
    MODEL_FAST: [
        ("groq", MODEL_FAST),
        ("openrouter", OPENROUTER_MODEL_FAST),
        ("gemini", GEMINI_MODEL),
    ],
}

_RATE_LIMIT_HINTS = (
    "rate limit", "rate_limit", "tokens limit", "token limit",
    "quota", "capacity", "too many requests", "429",
    "insufficient", "exceeded", "limit reached",
)


def _is_rate_or_quota_error(exc: Exception) -> bool:
    """True si l'erreur indique une limite de tokens / rate limit."""
    status = getattr(exc, "status_code", None)
    if status in (429, 503):
        return True
    msg = str(exc).lower()
    return any(h in msg for h in _RATE_LIMIT_HINTS)


class LLMClient:
    """Client multi-fournisseur avec bascule automatique."""

    def __init__(self):
        if not GROQ_API_KEY:
            raise ValueError("GROQ_API_KEY manquant dans .env")

        self._groq = Groq(api_key=GROQ_API_KEY)
        self._openrouter: Optional[Any] = None
        self._gemini: Optional[Any] = None

        if OPENROUTER_API_KEY and OpenAI is not None:
            self._openrouter = OpenAI(
                api_key=OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
            )
        elif OPENROUTER_API_KEY and OpenAI is None:
            logger.warning("[LLM] OPENROUTER_API_KEY défini mais 'openai' non installé")

        if GEMINI_API_KEY and OpenAI is not None:
            self._gemini = OpenAI(
                api_key=GEMINI_API_KEY,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            )
        elif GEMINI_API_KEY and OpenAI is None:
            logger.warning("[LLM] GEMINI_API_KEY défini mais 'openai' non installé")

        self._active_provider = "groq"
        self._active_model = MODEL_PRIMARY

        fallbacks = []
        if self._openrouter:
            fallbacks.append("openrouter")
        if self._gemini:
            fallbacks.append("gemini")
        fb_str = ", ".join(fallbacks) if fallbacks else "groq/" + MODEL_FAST + " uniquement"
        logger.info(
            f"[LLM] Primary: groq/{MODEL_PRIMARY} | Fallbacks: {fb_str}"
        )

    @property
    def active_provider(self) -> str:
        return self._active_provider

    @property
    def active_model(self) -> str:
        return self._active_model

    def _provider_client(self, provider: str):
        if provider == "groq":
            return self._groq
        if provider == "openrouter":
            return self._openrouter
        if provider == "gemini":
            return self._gemini
        return None

    def _call_provider(
        self,
        provider: str,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
    ):
        client = self._provider_client(provider)
        if client is None:
            raise RuntimeError(f"Provider '{provider}' non configuré")

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if provider == "gemini":
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        return client.chat.completions.create(**kwargs)

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: str = MODEL_PRIMARY,
        temperature: float = 0.0,
        max_tokens: int = 400,
    ) -> Any:
        """
        Appelle le LLM avec fallback automatique.
        Retourne l'objet response complet (compatible Groq/OpenAI).
        """
        chain = FALLBACK_CHAINS.get(model, [("groq", model)])
        last_error: Optional[Exception] = None

        for provider, provider_model in chain:
            client = self._provider_client(provider)
            if client is None:
                continue

            try:
                response = self._call_provider(
                    provider, provider_model, messages, temperature, max_tokens
                )
                if provider != chain[0][0] or provider_model != chain[0][1]:
                    logger.warning(
                        f"[LLM FALLBACK] Bascule → {provider}/{provider_model} "
                        f"(demandé: groq/{model})"
                    )
                self._active_provider = provider
                self._active_model = provider_model
                return response

            except (GroqAPIStatusError, OpenAIAPIStatusError) as e:
                last_error = e
                if _is_rate_or_quota_error(e):
                    logger.warning(
                        f"[LLM] {provider}/{provider_model} indisponible "
                        f"(limite): {str(e)[:120]}"
                    )
                    continue
                raise

            except Exception as e:
                last_error = e
                if _is_rate_or_quota_error(e):
                    logger.warning(
                        f"[LLM] {provider}/{provider_model} indisponible: {str(e)[:120]}"
                    )
                    continue
                raise

        raise RuntimeError(
            f"Tous les modèles LLM sont indisponibles (limite de tokens atteinte). "
            f"Dernière erreur: {last_error}"
        )


class _CompletionsWrapper:
    """Adaptateur compatible client.chat.completions.create()."""

    def __init__(self, llm: "LLMClient"):
        self._llm = llm

    def create(
        self,
        messages: List[Dict[str, str]],
        model: str = MODEL_PRIMARY,
        temperature: float = 0.0,
        max_tokens: int = 400,
        **kwargs,
    ):
        return self._llm.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


class _ChatWrapper:
    def __init__(self, llm: "LLMClient"):
        self.completions = _CompletionsWrapper(llm)


class LLMClientCompat(LLMClient):
    """LLMClient avec interface Groq-compatible (client.chat.completions.create)."""

    def __init__(self):
        super().__init__()
        self.chat = _ChatWrapper(self)


# Instance globale — remplace l'ancien client Groq direct
llm_client = LLMClientCompat()
