"""ChatGPT, an AI answer source of K-BP1 (D4); its shared base is ai.ChatSource."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import AI_CHATGPT
from ..judge import Mark, text_selector
from .ai import ChatSource

TOO_MANY = "too many requests"


@dataclass(frozen=True, slots=True)
class ChatGpt(ChatSource):
    """ChatGPT logged out: ok when the assistant's reply has at least MIN_CHARS characters
    and at least 2 sentences."""

    id: str = AI_CHATGPT
    url: str = "https://chatgpt.com/"

    PROMPT = "#prompt-textarea"
    ANSWER = ('[data-message-author-role="assistant"]',)
    LOGIN_HOSTS = ("auth.openai.com", "auth0.openai.com")
    LOGIN_PATHS = ("/auth/login",)
    LOGIN_DIALOGS = (
        'div[role="dialog"]:has([data-testid="login-button"])',
        'div[role="dialog"]:has-text("Log in or sign up")',
    )
    STOPS = (text_selector("Too many requests"), "#challenge-form")
    DISMISS = ('button:has-text("Reject non-essential")', 'button:has-text("Accept all")')
    MIN_SENTENCES = 2
    MARKS = (
        Mark("challenge", "challenge_page", found=("#challenge-form",)),
        Mark("blocked", "too_many_requests", text=(TOO_MANY,)),
    )
