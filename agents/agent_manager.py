import json

from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from config import (
    MODEL_NAME,
    DEEPSEEK_API_KEY,
    MAX_CANDIDATES_PER_ITERATION,
    get_agent_dir)

from datasets.prompts.system_prompts_structuré import SYSTEM_AGENT_MANAGER
from utils.parsers import parse_json_response
from utils.embedding import select_top_candidates
from state import PipelineState, base_state
from run_context import get_stats
from utils.ontology_count import count_ontology_entities
from utils.llm_response_utils import extract_reasoning_text, normalize_message_content
from utils.triplet_layers import (
    is_fully_done,
    normalize_layers,
    prune_layers_to_pool)



def _build_candidates_summary(top_candidates):
    summary = []
    for c in top_candidates:
        best_concept = None
        top_concepts = c.get("top_concepts", [])

        if top_concepts:
            best = max(top_concepts, key=lambda x: x.get("similarity", 0))
            best_concept = {"label" : best.get("label")}
        summary.append({
            "subject" : c.get("subject"),
            "predicate" : c.get("predicate"),
            "object" : c.get("object"),
            "best_ontology_match": best_concept})
    return summary


def run_manager(state) :
    print("\n(agent_manager.py)")

    old_ic = state.get("iteration_count", 0)
    iteration = old_ic + 1
    version  = state.get("ontology_version", 1) + 1
    cur_iter = old_ic
    mgr_dir = get_agent_dir(cur_iter, "manager")

    top_candidates = state.get("top_candidates", [])
    ids_traites = {
        str(t["id"]) for t in top_candidates
        if isinstance(t, dict) and t.get("id") is not None and str(t["id"]) != ""
    }
    pool = [
        t for t in (state.get("triplets_json") or [])
        if isinstance(t, dict) and t.get("id") is not None and str(t["id"]) != ""
    ]
    non_traites = [t for t in pool if str(t["id"]) not in ids_traites]

    layers = normalize_layers(state.get("triplet_layers"))

    # Trace uniquement (ne décide pas le membership du pool)
    rejected_ids_seen = set()
    rejected_clean = []
    for t in state.get("rejected_triplets") or []:
        if not isinstance(t, dict) or t.get("id") is None:
            continue
        tid = str(t["id"])
        if tid and tid not in rejected_ids_seen:
            rejected_ids_seen.add(tid)
            rejected_clean.append(t)

    # Pool suivant = tout triplets_json sauf fully done (S∧P∧O)
    by_id = {}
    for t in pool:
        tid = str(t["id"])
        if is_fully_done(layers, tid):
            print(f"(agent_manager.py) triplet {tid} fully done (S,P,O) -> sort du pool")
            continue
        by_id[tid] = t
    new_triplets = list(by_id.values())
    layers = prune_layers_to_pool(layers, new_triplets)

    new_ontology = (
        state.get("ttl_after_object_validation")
        or state.get("ttl_after_object_semantic")
        #or state.get("enriched_ttl_objects")
        or state.get("ttl_after_predicate_validation")
        or state.get("ttl_after_predicate_semantic")
        #or state.get("enriched_ttl_predicates")
        or state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("ttl_after_syntax")
        #or state.get("enriched_ttl_subjects")
        or state["ontology_ttl"]
    )

    stats = get_stats()
    if stats and new_ontology:
        growth = count_ontology_entities(new_ontology)
        stats.record_ontology_growth(**growth)

    pen= {}

    if new_triplets:
        top_next = select_top_candidates(
            triplets=new_triplets,
            ontology_ttl=new_ontology,
            k=MAX_CANDIDATES_PER_ITERATION,
            embedding_iteration=iteration,
            triplet_penalty_expiry=pen,
        )
    else:
        top_next = []

    candidates_summary = _build_candidates_summary(top_next)

    valid_assertions = state.get("valid_assertions", [])

    print(
        f"(agent_manager.py) iter={iteration} | "
        f"ids_traités={len(ids_traites)} | non_traités={len(non_traites)} | "
        f"rejetés={len(rejected_clean)} | total_suivant={len(new_triplets)} | "
        f"top_sélectionnés={len(top_next)} "
    )

    llm = ChatDeepSeek(
        model=MODEL_NAME,
        api_key=DEEPSEEK_API_KEY,
        max_tokens=6000,
        extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "low",
        },
    )

    prompt_content = f"""Results of iteration {iteration}/{state.get("max_iterations")} :
    {{
    "triplets_traites": {len(ids_traites)},
    "triplets_integres": {len(valid_assertions)},
    "triplets_rejetes": {len(rejected_clean)},
    "triplets_restants_total": {len(new_triplets)},
    }}
    
    Les {len(top_next)} candidats les plus proches de l'ontologie pour la prochaine itération :
    {json.dumps(candidates_summary, ensure_ascii=False, indent=2)}
    "Ces triplets peuvent-ils enrichir davantage l'ontologie ?"""

    (mgr_dir / "prompt.txt").write_text(
        f"system user : \n{SYSTEM_AGENT_MANAGER}\n\n user\n{prompt_content}",
        encoding="utf-8",
    )

    response = llm.invoke([SystemMessage(content=SYSTEM_AGENT_MANAGER), HumanMessage(content=prompt_content)])

    output = normalize_message_content(response.content)
    (mgr_dir / "raw.txt").write_text(output, encoding="utf-8")
    (mgr_dir / "raisonnement.txt").write_text(
        extract_reasoning_text(response) or "(aucune trace reasoning)\n", encoding="utf-8"
    )
    decision = parse_json_response(output)

    usage = response.response_metadata.get("token_usage", {})
    if stats:
        u = usage if isinstance(usage, dict) else {}
        stats.record_llm_usage(
            agent="manager",
            input_tokens=u.get("input_tokens", u.get("prompt_tokens", 0)),
            output_tokens=u.get("output_tokens", u.get("completion_tokens", 0)),
            model=MODEL_NAME,
            usage_dict=u,
        )

    (mgr_dir / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    stable = ( len(new_triplets) == 0  or not decision.get("continue", True))

    stop_reason = None
    if stable and stats:
        if len(new_triplets) == 0:
            stop_reason = "no_remaining_triplets"
        elif not decision.get("continue", True):
            stop_reason = "manager_decision"
        if stop_reason:
            stats.record_stop_reason(
                reason=stop_reason,
                iteration=iteration,
                details=decision.get("summary", "")[:200],
            )
    if stats:
        stats.record_manager_decision(
            continue_pipeline=decision.get("continue", True),
            reason=decision.get("reason", ""),
            summary=decision.get("summary", ""),
            rejected_this_iteration=len(rejected_clean),  
            remaining_pool=len(new_triplets),
        )
    if stats and getattr(stats, "_current", None) and "iteration" in stats._current:
        stats.end_iteration()

    print(f"(agent_manager.py) itération {iteration}/{state.get('max_iterations')} | stable={stable} | {decision.get('summary', '')[:100]}")

    return {
    **base_state(state),
    "ontology_ttl": new_ontology,
    "triplets_json": new_triplets,
    "top_candidates": top_next,
    "rejected_triplets": [],
    "valid_assertions": [],
    "iteration_count": iteration,
    "ontology_version": version,
    "stable": stable,

    "enriched_ttl_subjects": "",
    "enriched_ttl_predicates": "",
    "enriched_ttl_objects": "",

    "ttl_after_syntax": "",
    "ttl_after_subject_semantic": "",
    "ttl_after_subject_validation": "",
    "ttl_after_predicate_semantic": "",
    "ttl_after_predicate_validation": "",
    "ttl_after_object_semantic": "",
    "ttl_after_object_validation": "",

    "subject_syntax_ok_after_semantic": False,
    "predicate_syntax_ok_after_semantic": False,
    "object_syntax_ok_after_semantic": False,

    "subject_assertions_semantic_ok": state.get("subject_assertions_semantic_ok", []),
    "enrich_subjects_parse_failed": False,
    "enrich_predicates_parse_failed": False,
    "enrich_objects_parse_failed": False,
    "subject_toolchain_needed": True,
    "predicate_toolchain_needed": True,
    "object_toolchain_needed": True,
    "triplet_layers": layers,
    "triplet_penalty_expiry": pen,

    "validation_subject_ok": False,
    "validation_predicate_ok": False,
    "validation_full_ok": False,
    "syntax_errors": [],
    "syntax_warnings": [],
    "semantic_errors": [],
    "messages": [ AIMessage(content=f"[Manager] iter={iteration} stable={stable} | {decision.get('summary', '')[:200]}")],
    "ontology_history": [{
        "iteration": iteration - 1,
        "version": version - 1,
        "stable": stable,
        "integrated": len(valid_assertions),
        "rejected_remis": len(rejected_clean),
        "remaining": len(new_triplets),
        "stop_reason": stop_reason,
    }],
}