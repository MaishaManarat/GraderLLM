"""Thin OpenAI-compatible client layer shared by the evaluator models and the
PDF vision step. Every provider in config.PROVIDERS is reached through the
same `openai` SDK by swapping base_url + API key, so adding or replacing a
provider is a config change, not a code change.
"""
import truststore
truststore.inject_into_ssl()

import base64
import threading

import config


import ssl, httpx, truststore
_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

class NonRetryableError(Exception):
    """A failure that retrying cannot fix (retired model, request larger than
    the plan's per-minute token limit, truncated/empty reply, bad key...)."""


_clients: dict = {}
_lock = threading.Lock()


def _client(provider: str):
    with _lock:
        client = _clients.get(provider)
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=config.api_key_for(provider),
                base_url=config.base_url_for(provider),
                http_client=httpx.Client(verify=False),
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                max_retries=0,  # llm_engine owns retry/backoff; SDK retries would multiply
            )
            _clients[provider] = client
        return client


def _extra_body(model_id: str) -> dict:
    # Reasoning-effort only applies to gpt-oss style reasoning models; sending
    # it to a plain model would be rejected as an invalid parameter.
    if config.REASONING_EFFORT and "gpt-oss" in model_id:
        return {"reasoning_effort": config.REASONING_EFFORT}
    return {}


def _complete(model_spec: str, messages: list, max_tokens: int) -> str:
    provider, model_id = config.parse_model_spec(model_spec)
    kwargs = dict(
        model=model_id,
        messages=messages,
        temperature=config.TEMPERATURE,
        max_tokens=max_tokens,
    )
    extra = _extra_body(model_id)
    if extra:
        kwargs["extra_body"] = extra

    response = _client(provider).chat.completions.create(**kwargs)
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        raise NonRetryableError(
            f"empty reply from {model_spec} (finish_reason={choice.finish_reason}). "
            f"A reasoning model likely used all {max_tokens} tokens thinking - "
            f"raise MAX_TOKENS or lower REASONING_EFFORT."
        )
    return content


def chat_text(model_spec: str, request_text: str) -> str:
    return _complete(model_spec, [{"role": "user", "content": request_text}], config.MAX_TOKENS)


def chat_vision(model_spec: str, prompt: str, png_bytes: bytes, max_tokens: int = 300) -> str:
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    return _complete(model_spec, messages, max_tokens)
