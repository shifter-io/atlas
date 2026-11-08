"""The three AI answer sources of K-BP1 (D4), gathered from their modules; proxy_index.targets
registers them from here."""

from __future__ import annotations

from .ai import Gemini
from .ai_overview import AiOverview
from .chatgpt import ChatGpt

__all__ = ["AI_TARGET_TYPES", "AiOverview", "ChatGpt", "Gemini"]

AI_TARGET_TYPES = (AiOverview, ChatGpt, Gemini)
