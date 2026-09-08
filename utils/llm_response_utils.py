from typing import Any


def merge_token_usage(a, b):
    left = a if isinstance(a, dict) else {}
    right = b if isinstance(b, dict) else {}
    out = {}
    for k in set(left) | set(right):
        va, vb = left.get(k), right.get(k)
        if isinstance(va, dict) or isinstance(vb, dict):
            out[k] = merge_token_usage(
                va if isinstance(va, dict) else {},
                vb if isinstance(vb, dict) else {},
            )
        elif isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            out[k] = va + vb
        elif isinstance(vb, (int, float)):
            out[k] = vb
        elif isinstance(va, (int, float)):
            out[k] = va
        else:
            out[k] = vb if vb is not None else va
    return out


def normalize_message_content(raw: Any):
    if isinstance(raw, list):
        parts = []
        for b in raw:
            if isinstance(b, dict):
                t = (b.get("type") or "").lower()
                if t in ("reasoning", "thinking"): # openAI: redacted_reasoning 
                    continue
                parts.append(b.get("text", "") or "")
            else:
                parts.append(str(b))
        return " ".join(p for p in parts if p).strip()
    return str(raw or "")


def extract_reasoning_text(response: Any):
    chunks = []

    raw = getattr(response, "content", None)

    if isinstance(raw, list):
        for b in raw:
            if not isinstance(b, dict):
                continue
            t = (b.get("type") or "").lower()

            if t in ("reasoning", "thinking"):
                txt = b.get("text") or b.get("reasoning") or ""

                if txt:
                    chunks.append(str(txt))

    md = getattr(response, "response_metadata", None) or {}

    if isinstance(md, dict):
        for k in ("reasoning_content", "thinking", "reasoning"):
            v = md.get(k)

            if v:
                chunks.append(str(v))

    ak = getattr(response, "additional_kwargs", None) or {}
    if isinstance(ak, dict):
        for k in ("reasoning_content", "thinking", "reasoning"):
            v = ak.get(k)
            if v:
                chunks.append(str(v))

    return "\n\n".join(chunks).strip()
