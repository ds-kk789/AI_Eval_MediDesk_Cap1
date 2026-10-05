"""A Groq-backed judge model for DeepEval.

DeepEval defaults to OpenAI, which this project has no key for. Its metrics
ask the judge for structured JSON and, when a pydantic `schema` is supplied,
expect an instance of it back -- so `generate` handles both the plain-string
and schema-validated call shapes.
"""

import asyncio
import hashlib
import json
import os
import re
import time

from deepeval.models import DeepEvalBaseLLM
from dotenv import load_dotenv
from groq import BadRequestError

from backend import config
from backend.llm import groq

load_dotenv()


JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Judge verdicts are cached on disk, keyed by a hash of the exact prompt.
# This matters more than it looks: the API key allows ~30 calls per day, and
# a single DeepEval metric asks the judge several questions per test case.
# With this cache, only the first evaluation of a given case costs anything --
# re-running the suite, or demonstrating the gate, is free.
JUDGE_CACHE = config.REPORTS_DIR / "_judge_cache"

# Groq's free tier allows 8,000 tokens per minute; judge prompts carry the
# full answer plus retrieval context, so uncached calls are spaced out.
PACE_SECONDS = float(os.getenv("LLM_PACE_SECONDS", "30"))
_last_call = {"at": 0.0}


class GroqJudge(DeepEvalBaseLLM):
    """DeepEval judge backed by Groq."""

    def __init__(self, model_name=None):
        self.model_name = model_name or config.GROQ_JUDGE_MODEL
        super().__init__(model=self.model_name)

    def load_model(self):
        # The shared pool, so a daily-quota 429 rotates to the backup key.
        return groq

    def get_model_name(self):
        return f"Groq {self.model_name}"

    # ------------------------------------------------------------
    # GENERATION
    # ------------------------------------------------------------

    @staticmethod
    def _cache_path(prompt, model_name, force_json):
        digest = hashlib.sha256(
            f"{model_name}|{force_json}|{prompt}".encode("utf-8")
        ).hexdigest()[:32]

        return JUDGE_CACHE / f"{digest}.json"

    def _complete(self, prompt, force_json):
        cache_file = self._cache_path(prompt, self.model_name, force_json)

        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))["text"]

        wait = PACE_SECONDS - (time.time() - _last_call["at"])
        if wait > 0:
            time.sleep(wait)
        _last_call["at"] = time.time()

        kwargs = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            # gpt-oss reasons before it answers. In JSON mode Groq counts that
            # reasoning against the completion budget and returns
            # `json_validate_failed: max completion tokens reached` if the
            # JSON never arrives. Low effort + a generous cap keeps the
            # verdict inside the budget; judge prompts do not need deep
            # deliberation.
            "reasoning_effort": "low",
            "max_completion_tokens": 4096,
        }

        if force_json:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            completion = self.model.chat(**kwargs)
            text = completion.choices[0].message.content or ""
        except BadRequestError as error:
            text = self._recover_json(error, kwargs)

        JUDGE_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"prompt": prompt, "text": text}, ensure_ascii=False),
            encoding="utf-8",
        )

        return text

    def generate(self, prompt, schema=None, **kwargs):
        if schema is None:
            return self._complete(prompt, force_json=False)

        raw = self._complete(
            prompt
            + "\n\nRespond with a single JSON object and nothing else.",
            force_json=True,
        )

        return self._parse(raw, schema)

    async def a_generate(self, prompt, schema=None, **kwargs):
        # The Groq SDK call is synchronous; run it off the event loop so
        # DeepEval's async metric execution is not blocked.
        return await asyncio.to_thread(
            self.generate, prompt, schema, **kwargs
        )

    def _recover_json(self, error, kwargs):
        """Salvage a verdict Groq's strict JSON validator rejected.

        gpt-oss occasionally appends a stray `,""` after an otherwise
        complete object. Groq returns 400 json_validate_failed but includes
        the text as `failed_generation`; repairing that beats spending
        another call. If nothing salvageable came back, retry once without
        JSON mode and let _parse extract the object from free text.
        """
        body = getattr(error, "body", None) or {}
        detail = body.get("error", body) if isinstance(body, dict) else {}
        failed = (detail or {}).get("failed_generation") or ""

        repaired = re.sub(r',\s*""\s*}', "}", failed).strip()

        if repaired:
            try:
                json.loads(repaired)
                return repaired
            except json.JSONDecodeError:
                pass

        retry = {k: v for k, v in kwargs.items() if k != "response_format"}
        completion = self.model.chat(**retry)

        return completion.choices[0].message.content or ""

    @staticmethod
    def _parse(raw, schema):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = JSON_BLOCK.search(raw)

            if not match:
                raise ValueError(
                    f"Judge did not return JSON. Got: {raw[:400]}"
                )

            data = json.loads(match.group(0))

        return schema(**data)
