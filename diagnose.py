"""Check whether the network cuts requests above a certain SIZE, regardless of content.

Usage (from your Trial4 folder):
    python diagnose_size.py
    python diagnose_size.py "submission\\SomeOther.pdf"

1. Shows the upload size (in KB) of Test C (went through) and Test D (blocked).
2. Sends harmless filler text at increasing sizes to find the cut-off point.
Results are saved to diagnose_size_result.txt.
"""

import json
import sys
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

import openai

import config
import llm_engine
import providers

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else r"submission\RobiSample_ConceptPaper.pdf"
MODEL = config.LLM_MODELS[0]
PREFIX = "Reply with only the word OK.\n\n"
FILLER = "The quick brown fox jumps over the lazy dog. "
SIZES_KB = [40, 50, 56, 60, 62, 63, 64, 65, 66, 68, 72, 80, 100, 128]

lines_out: list[str] = []


def out(msg: str = "") -> None:
    print(msg)
    lines_out.append(msg)


def body_kb(text: str) -> float:
    """Approximate size of the HTTP request body the SDK uploads."""
    body = {
        "model": config.parse_model_spec(MODEL)[1],
        "messages": [{"role": "user", "content": PREFIX + text}],
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
    }
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8")) / 1024


def extract_text(path: str) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except ImportError:
        pass
    import fitz  # PyMuPDF
    with fitz.open(path) as doc:
        return "\n".join(p.get_text() for p in doc)


def load_prompt() -> str:
    path = Path(getattr(config, "PROMPT_PATH", "prompt.md"))
    return path.read_text(encoding="utf-8").strip()


def _send(text: str) -> tuple[bool, str]:
    time.sleep(0.5)
    try:
        providers.chat_text(MODEL, PREFIX + text)
        return False, ""
    except openai.APIConnectionError as exc:
        return True, repr(exc.__cause__)
    except Exception as exc:  # HTTP error = the request reached Groq
        return False, f"(reached Groq, reply error: {type(exc).__name__})"


def is_blocked(text: str) -> tuple[bool, str]:
    blocked, note = _send(text)
    if blocked:  # retry once so a random glitch isn't counted as a block
        blocked, note = _send(text)
    return blocked, note


def filler_of_kb(kb: float) -> str:
    """Filler text whose full request body is about `kb` kilobytes."""
    overhead = body_kb("")
    n_chars = max(0, int((kb - overhead) * 1024))
    return (FILLER * (n_chars // len(FILLER) + 1))[:n_chars]


def main() -> None:
    out(f"Model: {MODEL}")
    prompt = load_prompt()
    submission = extract_text(PDF_PATH).strip()
    req_c = llm_engine.build_request(prompt, "This is a short test submission.")
    req_d = llm_engine.build_request(prompt, submission)
    out(f"Test C upload size: {body_kb(req_c):.1f} KB  (went through before)")
    out(f"Test D upload size: {body_kb(req_d):.1f} KB  (blocked before)")

    out("\n--- Filler text at increasing sizes ---")
    last_ok, first_blocked = None, None
    for kb in SIZES_KB:
        blocked, note = is_blocked(filler_of_kb(kb))
        out(f"  {kb:>4} KB -> {'BLOCKED ' + note if blocked else 'went through ' + note}")
        if blocked:
            first_blocked = kb
            break
        last_ok = kb

    if first_blocked is None:
        out("\nNo size limit found up to 128 KB - size is NOT the problem;"
            " the block depends on the content combination.")
    else:
        lo, hi = float(last_ok or 0), float(first_blocked)
        while hi - lo > 0.5:  # narrow the limit down to ~0.5 KB
            mid = (lo + hi) / 2
            blocked, _ = is_blocked(filler_of_kb(mid))
            out(f"  {mid:6.1f} KB -> {'BLOCKED' if blocked else 'went through'}")
            if blocked:
                hi = mid
            else:
                lo = mid
        out(f"\nSize limit found: requests above ~{lo:.1f} KB are cut, whatever their content.")
        out(f"Your full request is {body_kb(req_d):.1f} KB, so it needs to shrink by "
            f"~{max(0.0, body_kb(req_d) - lo):.1f} KB (or more, to leave room for bigger PDFs).")

    with open("diagnose_size_result.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out))
    print("\nSaved to diagnose_size_result.txt")


if __name__ == "__main__":
    main()