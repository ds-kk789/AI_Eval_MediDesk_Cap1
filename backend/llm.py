"""Groq client with automatic failover across API keys.

The free tier caps each key at 200k tokens per day. A second key
(GROQ_API_KEY_2) doubles the budget, but only if every call site knows to
move to it when the first is exhausted. This module is that one place.

    from backend.llm import groq
    completion = groq.chat(model=..., messages=[...])

`chat()` tries the active key; on a daily-quota 429 it rotates to the next
key and retries once. Per-minute 429s are left to the SDK's own retry, since
switching keys would not help and would burn the second key's minute too.
"""

import os
import re
import threading

from dotenv import load_dotenv
from groq import Groq, RateLimitError

load_dotenv()


KEY_ENV_NAMES = ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3")

# Groq's daily-limit body reads "...on tokens per day (TPD): Limit 200000..."
DAILY_LIMIT = re.compile(r"per day|TPD", re.IGNORECASE)


def _daily_quota_exhausted(error):
    return bool(DAILY_LIMIT.search(str(error)))


class GroqPool:

    def __init__(self, keys=None):
        self.keys = [
            key for key in (keys or [os.getenv(n) for n in KEY_ENV_NAMES])
            if key
        ]

        if not self.keys:
            raise RuntimeError("No GROQ_API_KEY configured.")

        self.index = 0
        self._lock = threading.Lock()
        self._clients = {}

    # ------------------------------------------------------------
    # KEYS
    # ------------------------------------------------------------

    @property
    def active_key(self):
        return self.keys[self.index]

    @property
    def active_label(self):
        return KEY_ENV_NAMES[self.index] if self.index < len(KEY_ENV_NAMES) else f"key#{self.index}"

    def client(self, key=None):
        key = key or self.active_key

        if key not in self._clients:
            self._clients[key] = Groq(api_key=key)

        return self._clients[key]

    def rotate(self, reason=""):
        """Move to the next key. Returns False when none are left."""
        with self._lock:
            if self.index + 1 >= len(self.keys):
                return False

            self.index += 1
            print(
                f"[groq] {KEY_ENV_NAMES[self.index - 1]} exhausted"
                f"{' (' + reason + ')' if reason else ''}; "
                f"switching to {self.active_label}"
            )
            return True

    # ------------------------------------------------------------
    # CALLS
    # ------------------------------------------------------------

    def chat(self, **kwargs):
        """chat.completions.create with daily-quota failover."""
        while True:
            try:
                return self.client().chat.completions.create(**kwargs)
            except RateLimitError as error:
                if _daily_quota_exhausted(error) and self.rotate("daily quota"):
                    continue
                raise

    def probe_key(self):
        """Advance past any key whose daily quota is already gone.

        Costs one ~10-token call per exhausted key. Used by processes that
        hand the key to a third-party client (LiteLLM in the ADK lane) and
        therefore cannot intercept the 429 themselves.
        """
        while True:
            try:
                self.client().chat.completions.create(
                    model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
                    messages=[{"role": "user", "content": "ok"}],
                    max_completion_tokens=8,
                )
                return self.active_key
            except RateLimitError as error:
                if _daily_quota_exhausted(error) and self.rotate("daily quota"):
                    continue
                raise


# Process-wide pool: one rotation covers every call site in this process.
groq = GroqPool()
