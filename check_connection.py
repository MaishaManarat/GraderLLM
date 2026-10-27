"""Pre-flight check: run this before a real batch (and whenever calls fail).

For every provider your .env uses it checks, separately:
  1. DNS resolution         2. TCP connection        3. an authenticated API call
and then confirms that every model in LLM_MODELS (and VISION_MODEL) is
actually offered to your key - which catches retired/renamed model IDs
(`model_not_found`) before they fail a whole batch.

Usage:
    python check_connection.py
"""

import socket
import sys
from urllib.parse import urlparse

import config
import llm_engine
import providers


def check_reference_files() -> bool:
    """Confirm the rubric, PDPA text and case study all exist and are
    non-empty, and report their approximate token size - this is what
    determines whether a model's tokens-per-minute limit can work at all,
    and a missing/empty file here would otherwise only surface mid-batch."""
    print("=== reference files ===")
    ok = True

    print(f"1. Rubric ({config.PROMPT_FILE}) ...", end=" ")
    try:
        with open(config.PROMPT_FILE, "r", encoding="utf-8") as f:
            prompt_text = f.read()
        if not prompt_text.strip():
            raise ValueError("file is empty")
        print(f"OK -> ~{llm_engine.estimate_tokens(prompt_text):,} tokens")
    except Exception as exc:
        ok = False
        print(f"FAILED ({exc})")

    print("2. PDPA + case study ...", end=" ")
    try:
        tokens = llm_engine.preload_references()
        print("OK")
        for label, count in tokens.items():
            print(f"   {label}: ~{count:,} tokens ({_path_for(label)})")
    except Exception as exc:
        ok = False
        print(f"FAILED ({exc})")

    print()
    return ok


def _path_for(label: str) -> str:
    return config.PDPA_PATH if label == "PDPA" else config.CASE_STUDY_PATH


def _hosts_in_use() -> dict:
    specs = list(config.LLM_MODELS)
    if config.PDF_IMAGE_HANDLING == "describe" and config.VISION_MODEL:
        specs.append(config.VISION_MODEL)
    used: dict = {}
    for spec in specs:
        provider, model_id = config.parse_model_spec(spec)
        used.setdefault(provider, []).append(model_id)
    return used


def check_provider(provider: str, wanted_models: list) -> bool:
    url = config.base_url_for(provider)
    host = urlparse(url).hostname
    port = urlparse(url).port or 443
    print(f"=== {provider} ({host}) ===")

    print("1. DNS resolution ...", end=" ")
    try:
        print(f"OK -> {socket.gethostbyname(host)}")
    except socket.gaierror as exc:
        print(f"FAILED ({exc})\n   -> no internet, broken DNS, or this domain is blocked on your network.")
        return False

    print("2. TCP connection ...", end=" ")
    try:
        with socket.create_connection((host, port), timeout=10):
            print("OK")
    except OSError as exc:
        print(f"FAILED ({exc})\n   -> a firewall/proxy/antivirus/VPN is blocking outbound HTTPS to this host.")
        return False

    print("3. Authenticated API call (list models) ...", end=" ")
    try:
        listing = providers._client(provider).models.list()
        available = {m.id.split("models/", 1)[-1] for m in listing.data}
        print(f"OK -> {len(available)} model(s) visible to this key")
    except Exception as exc:
        print(f"FAILED ({type(exc).__name__}: {exc})")
        status = getattr(exc, "status_code", None)
        if status == 401:
            print(f"   -> network is fine; {config.PROVIDERS[provider]['key_env']} is invalid or missing.")
        elif status == 429:
            print("   -> connection and key are fine; you are rate-limited right now.")
        return False

    ok = True
    print("4. Configured models on this provider:")
    for model_id in wanted_models:
        if model_id in available:
            print(f"   OK      {model_id}")
        else:
            ok = False
            print(f"   MISSING {model_id}  <- not offered to your key (retired, renamed, or no access)")
    if not ok:
        print("   Models your key CAN use:")
        for m in sorted(available):
            print(f"     {m}")
    return ok


def main() -> int:
    all_ok = check_reference_files()

    for provider, models in _hosts_in_use().items():
        all_ok &= check_provider(provider, models)
        print()

    print("Make sure each model's tokens-per-minute limit on your plan is comfortably above the")
    print("reference-file token counts printed above (plus the submission itself); the API error")
    print("'Request too large ... TPM' means it is not.")
    print()
    print("ALL CHECKS PASSED" if all_ok else "PROBLEMS FOUND - see above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
