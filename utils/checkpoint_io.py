
import json
from pathlib import Path

from config import get_iteration_dir, get_ontology_dir

TTL_FALLBACK_FILES = (
    "object_toolchain/ontology_validated.ttl",
    "predicate_toolchain/ontology_validated.ttl",
    "subject_toolchain/ontology_validated.ttl",

    #"ontology_after_syntax_object_chain_rdf_hermit_oops.ttl",
    #"ontology_after_syntax_predicate_chain_rdf_hermit_oops.ttl",
    #"ontology_after_syntax_subject_chain_rdf_hermit_oops.ttl",

    "validator_semant_objects/output.ttl",
    "validator_semant_predicates/output.ttl",
    "validator_semant_subjects/output.ttl",
    
    "enrich_objects/output.ttl",
    "enrich_predicates/output.ttl",
    "enrich_subjects/output.ttl",
)


CHECKPOINT_DIR = Path("checkpoints") 


def checkpoint_path(thread_id: str | None = None):
    return get_ontology_dir() / "checkpoint.json"


def load_checkpoint(thread_id: str) -> dict | None:
    cp = checkpoint_path(thread_id)
    if not cp.exists():
        print(f"(checkpoint_io.py) no checkpoint : {cp}")
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        required_keys = {"iteration_count", "ontology_ttl", "triplets_json"}
        missing = required_keys - set(data.keys())
        if missing:
            print(f"(checkpoint_io.py) checkpoint incomplet manques clé: {sorted(missing)})")
            return None
        print(
            f"(checkpoint_io.py) checkpoint iter= {data.get('iteration_count')} "
            f"| restants={len(data.get('triplets_json', []))} "
            f"| fichier={cp}"
        )
        sample = data.get("triplets_json") or []
        if sample:
            first = sample[0] if isinstance(sample[0], dict) else {}
            print(
                f"(checkpoint_io.py)   1er triplet restant: "
                f"id={first.get('id')} sujet={first.get('subject')!r}"
            )
        return data
    except Exception as exc:
        print(f"(checkpoint_io.py) lecture checkpoint : {exc}")
        return None


def last_saved_ttl(iteration_count):
    for i in range(iteration_count, -1, -1):
        iter_dir = get_iteration_dir(i)
        for fname in TTL_FALLBACK_FILES:
            candidate = iter_dir / fname
            if candidate.exists():
                print(f"(checkpoint_io.py) Fallback TTL : {candidate}")
                return candidate.read_text(encoding="utf-8")
    print("(checkpoint_io.py) aucun TTL fallback sur disque")
    return ""


def save_checkpoint(state, thread_id):
    cp = checkpoint_path(thread_id)
    payload = {
        "iteration_count": state.get("iteration_count", 0),
        "ontology_ttl": state.get("ontology_ttl", ""),
        "triplets_json": state.get("triplets_json", []),
        "rejected_triplets": state.get("rejected_triplets", []),
        "triplet_penalty_expiry": state.get("triplet_penalty_expiry", {}),
        "triplet_layers": state.get("triplet_layers", {}),
        "ontology_version": state.get("ontology_version", 1),
        "stable": state.get("stable", False),
        "ontology_history": state.get("ontology_history", [])[-1:],
    }
    try:
        tmp_cp = cp.with_suffix(cp.suffix + ".tmp")
        tmp_cp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_cp.replace(cp)
        print(f"(checkpoint_io.py) checkpoint sauvé iter={payload['iteration_count']}")
    except MemoryError:
        print("(checkpoint_io.py) memoryerror checkpoint minimal")
        try:
            minimal = {
                **payload,
                "ontology_ttl": last_saved_ttl(state.get("iteration_count", 0)),
                "ontology_history": [],
            }
            tmp_cp = cp.with_suffix(cp.suffix + ".tmp")
            tmp_cp.write_text(json.dumps(minimal, ensure_ascii=False), encoding="utf-8")
            tmp_cp.replace(cp)
            print("(checkpoint_io.py) Checkpoint minimal OK")
        except Exception as exc2:
            print(f"(checkpoint_io.py)  echec minimal : {exc2}")
    except Exception as exc:
        print(f"(checkpoint_io.py) sauvegarde : {exc}")


def is_post_manager_chunk(chunk, prev_iteration):
    if chunk.get("iteration_count", 0) <= prev_iteration:
        return False
    keys = (
        "enriched_ttl_subjects",
        "ttl_after_syntax",
        "ttl_after_subject_semantic",
        "ttl_after_subject_validation",
        "ttl_after_predicate_semantic",
        "ttl_after_predicate_validation",
        "enriched_ttl_predicates",
        "enriched_ttl_objects",
        "ttl_after_object_semantic",
        "ttl_after_object_validation",
    )
    return all(not chunk.get(k) for k in keys)