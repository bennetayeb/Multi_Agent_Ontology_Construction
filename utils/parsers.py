import json
import re
from typing import Optional


_INTERNAL_FIELDS = {"top_concepts"}


def strip_for_llm(triplet: dict, extra_keep: set[str] | None = None) :
    keep = {"id", "titres", "subject", "predicate", "object"}
    if extra_keep:
        keep |= extra_keep
    result = {k: v for k, v in triplet.items() if k in keep}
    for field in ("subject", "predicate", "object"):
        if field in result and isinstance(result[field], str):
            result[field] = result[field].replace("_", " ")
    return result

def strip_list_for_llm(triplets: list[dict], extra_keep: set[str] | None = None) :
    return [strip_for_llm(t, extra_keep) for t in triplets]



def _extract_xml_block(text, tag) :
    if isinstance(text, list):
        text = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in text
        )
    if not isinstance(text, str):
        print(f"(parsers.py) _extract_xml_block : type inattendu {type(text)} pour <{tag}>")
        return None
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return match.group(1).strip() if match else None


def _repair_json_string(raw):
    raw = raw.replace("\u201c", '"').replace("\u201d", '"')
    raw = raw.replace("\u2018", "'").replace("\u2019", "'")
    raw = re.sub(r",\s*([\]}])", r"\1", raw)
    return raw


def _parse_json_list(text, tag):
    content = _extract_xml_block(text, tag)
    if not content:
        print(f"(parsers.py) bloc <{tag}> absent.")
        return []

    # parsing
    try:
        result = json.loads(content)
        if isinstance(result, list):
            return result
        print(f"(parsers.py) <{tag}> pas json (type={type(result).__name__}).")
        return []
    except json.JSONDecodeError as e:
        print(f"(parsers.py) invalide json<{tag}> ({e})")

    repaired = _repair_json_string(content)
    try:
        result = json.loads(repaired)
        if isinstance(result, list):
            print(f"(parsers.py) <{tag}> réparé")
            return result
    except json.JSONDecodeError:
        pass

    objects = re.findall(r"\{[^{}]+\}", content, re.DOTALL)
    parsed = []
    for obj_str in objects:
        try:
            parsed.append(json.loads(_repair_json_string(obj_str)))
        except json.JSONDecodeError:
            pass
    if parsed:
        print(f"(parsers.py) <{tag}> : {len(parsed)} objet(s) récupérés par extraction individuelle.")
        return parsed

    print(f"(parsers.py) <{tag}> : impossible de parser. Contenu brut (200c) : {content[:200]}")
    return []



def extract_turtle_block(text):
    content = _extract_xml_block(text, "enriched_ontology")
    if content:
        return content
    print("(parsers.py) bloc <enriched_ontology> absent -> ")
    return None


def extract_patch_block(text) :

    content = None
    for tag in ("ontology_patch", "ontologie_patch"):
        content = _extract_xml_block(text, tag)
        if content is not None:
            break
    if content is None:
        print("(parsers.py) bloc <ontology_patch> absent -> le caller doit utiliser le fallback.")
        return None
    stripped = _remove_comments(content).strip()
    if not stripped:
        print("(parsers.py) <ontology_patch> présent mais vide")
        return ""
    return content


def extract_corrections_patch_block(text) :
    for tag in ("corrections_patch", "correction_patch", "corrections_semantiques", "corrections"):
        content = _extract_xml_block(text, tag)
        if content is not None:
            stripped = _remove_comments(content).strip()
            if not stripped or stripped.upper() in ("EMPTY", "VIDE", "NONE", "N/A"):
                print(f"(parsers.py) <{tag}> présent mais vide -> retourne '' (aucune correction).")
                return ""
            print(f"(parsers.py) <{tag}> trouvé — {len(content)} chars.")
            return content

    if "# DELETE:" in text or ("rdf:type" in text and "@prefix" in text and "<enriched_ontology>" not in text):
        lines = text.split("\n")
        turtle_lines = []
        in_turtle = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("@prefix") or stripped.startswith("# DELETE:"):
                in_turtle = True
            if in_turtle and stripped:
                turtle_lines.append(line)
        if turtle_lines:
            extracted = "\n".join(turtle_lines)
            print(f"(parsers.py) Turtle brut, extrait des lignes : {len(turtle_lines)}")
            return extracted

    print("(parsers.py) bloc <corrections_patch> absent -> le caller doit utiliser le fallback.")
    return None


def _remove_comments(ttl):
    lines = []
    for line in ttl.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "DELETE" not in stripped.upper():
            continue
        lines.append(line)
    return "\n".join(lines)


_CANDIDATE_TAGS = ("triple_candidate", "candidate_triples", "triplets_candidats")
_REJECTED_TAGS = ("triple_rejected", "rejected_triples", "triplets_rejetes")


def candidates_block_present(text) :
    return any(_extract_xml_block(text, tag) is not None for tag in _CANDIDATE_TAGS)


def rejected_block_present(text) :
    return any(_extract_xml_block(text, tag) is not None for tag in _REJECTED_TAGS)


def extract_candidates_block(text) :
    items = []
    for tag in _CANDIDATE_TAGS:
        if _extract_xml_block(text, tag) is not None:
            items = _parse_json_list(text, tag)
            break
    valid = [it for it in items if isinstance(it, dict) and it.get("id")]
    if len(valid) < len(items):
        print(f"(parsers.py) triple_candidate : {len(items) - len(valid)} entrée(s) sans 'id' ignorée(s).")
    return valid


def json_triplet_blocks_present(text):
    return candidates_block_present(text) or rejected_block_present(text)


def extract_rejected_block(text):
    items = []
    for tag in _REJECTED_TAGS:
        if _extract_xml_block(text, tag) is not None:
            items = _parse_json_list(text, tag)
            break
    valid = [it for it in items if isinstance(it, dict) and it.get("id")]
    if len(valid) < len(items):
        print(f"(parsers.py) triple_rejected : {len(items) - len(valid)} entrée sans 'id' ignorée")
    return valid


def resolve_triplets_by_id(raw_list, candidates_by_id, agent_name):
    resolved = []
    for item in raw_list:
        tid = item.get("id")
        if not tid:
            print(f"(parsers.py / {agent_name}) item sans 'id' ignoré : {item}")
            continue
        full = candidates_by_id.get(tid, {})
        if not full:
            print(f"(parsers.py / {agent_name}) WARNING: id '{tid}' introuvable, conservé")
        resolved.append({**full, **item})
    return resolved


def reject_all_batch(top_candidates, reason: str = "parse_failure"):
    out = []

    for c in top_candidates:
        if isinstance(c, dict) and c.get("id"):
            out.append({**c, "reason": reason})
    return out


def infer_rejected_from_candidates(top_candidates, candidates):
    kept = {str(c["id"]) for c in candidates if isinstance(c, dict) and c.get("id")}
    return [
        {**c, "reason": c.get("reason", "inferred_reject")}
        for c in top_candidates
        if isinstance(c, dict) and c.get("id") and str(c["id"]) not in kept
    ]


def ensure_batch_coverage(top_candidates, candidates, rejected, agent_name):
    seen = {
        str(c["id"])
        for c in (candidates + rejected)
        if isinstance(c, dict) and c.get("id")}

    missing = [
        {**c, "reason": c.get("reason", "unlisted")}
        for c in top_candidates
        if isinstance(c, dict) and c.get("id") and str(c["id"]) not in seen]
        
    if missing:
        print(f"(parsers.py / {agent_name}) {len(missing)} missing ids added to rejected ")
    return rejected + missing


def parse_json_response(text):
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if start is None:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    try:
                        return json.loads(_repair_json_string(candidate))
                    except json.JSONDecodeError:
                        start = None

    return {"error": "parse_failed", "raw": text[:300]}