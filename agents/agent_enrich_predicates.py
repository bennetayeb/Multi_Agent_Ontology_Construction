import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from config import DEEPSEEK_API_KEY, MODEL_NAME, get_agent_dir
from datasets.prompts.system_prompts_structuré import SYSTEM_AGENT_ENRICH_PREDICATES

from state import base_state
from utils.llm_response_utils import extract_reasoning_text, merge_token_usage, normalize_message_content
from utils.ontology_diff import compute_diff, print_diff, save_diff
from utils.ontology_utils import merge_patch
from utils.triplet_layers import normalize_layers, partition_by_layer
from utils.parsers import (
    strip_list_for_llm,
    # parse XML <.....> </...> in LLM output
    extract_patch_block,
    extract_candidates_block,
    extract_rejected_block,
    # check if LLM output is present ? --> boolean
    candidates_block_present,
    rejected_block_present,
    # infer rejected from candidates
    resolve_triplets_by_id,
    infer_rejected_from_candidates,
    ensure_batch_coverage,
    reject_all_batch,
)
from run_context import get_stats


def run_enrich_predicates(state):
    print("\n(agent_enrich_predicates.py) enrichissement des prédicats")

    iteration = state.get("iteration_count", 0)
    adir = get_agent_dir(iteration, "enrich_predicates")
    stats = get_stats()

    top_candidates = state.get("top_candidates", [])
    base_ttl = (
        state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("enriched_ttl_subjects") # pas validé 
        or state["ontology_ttl"]
    )

    layers = normalize_layers(state.get("triplet_layers"))

    # Aucun sujet valide --> rien à enrichir pour les prédicats
    if not state.get("validation_subject_ok", False):
        print("(agent_enrich_predicates.py) validation sujets échouée --> skip total prédicats")
        if stats:
            stats.record_agent_predicates(integrated=0, rejected=0, reasons={"skip_subject_failed": 1})
        (adir / "patch.ttl").write_text("", encoding="utf-8")
        (adir / "output.ttl").write_text(base_ttl, encoding="utf-8")
        (adir / "candidates.json").write_text("[]", encoding="utf-8")
        (adir / "rejected.json").write_text("[]", encoding="utf-8")
        return {
            **base_state(state),
            "enriched_ttl_predicates": base_ttl,
            "valid_assertions": state.get("valid_assertions", []),
            "enrich_predicates_parse_failed": False,
            "predicate_toolchain_needed": False,
            "triplet_layers": layers,
            "messages": [AIMessage(content="[EnrichPred] skip: validation_subject_ok=False")],
        }

    # batch = valid_assertions = triples validé par semant subjects (already_ok subjects + nouveaux candidat)
    valid_assertions = state.get("valid_assertions", [])

    candidates_by_id = {
        c["id"]: c
        for c in top_candidates
        if isinstance(c, dict) and "id" in c
    }

    need_pred, already_ok = partition_by_layer(valid_assertions, layers, "predicate")
    print(
        f"(agent_enrich_predicates.py) iter={iteration} | batch={len(valid_assertions)} | "
        f"need_pred={len(need_pred)} | already_ok={len(already_ok)}")

    if not need_pred:
        (adir / "patch.ttl").write_text("", encoding="utf-8")
        (adir / "output.ttl").write_text(base_ttl, encoding="utf-8")
        (adir / "candidates.json").write_text( json.dumps(already_ok, ensure_ascii=False, indent=2), encoding="utf-8")
        (adir / "rejected.json").write_text("[]", encoding="utf-8")
        if stats:
            stats.record_agent_predicates(integrated=len(already_ok), rejected=0, reasons={"skip_already_ok": len(already_ok)})
        return {
            **base_state(state),
            "enriched_ttl_predicates": base_ttl,
            "valid_assertions": already_ok,
            "enrich_predicates_parse_failed": False,
            "predicate_toolchain_needed": False, 
            "triplet_layers": layers,
            "messages": [
                AIMessage(content=f"[EnrichPred] skip already_ok={len(already_ok)}")
            ],
        }

    llm = ChatDeepSeek(
        model=MODEL_NAME,
        api_key=DEEPSEEK_API_KEY,
        max_tokens=16000,
        extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "low",
        })

    candidates_for_llm = strip_list_for_llm(need_pred)
    #candidates_str = json.dumps(candidates_for_llm, ensure_ascii=False, indent=2)


    user_prompt = f"""Ontologie après enrichissement des sujets (contexte de lecture uniquement, ne pas la réécrire) :
{base_ttl}

Triplets candidats (traiter les PRÉDICATS uniquement) :
{json.dumps(candidates_for_llm, ensure_ascii=False, indent=2)}

<output_instructions>
Réponds avec exactement trois blocs XML, dans cet ordre, sans autre texte :

<ontologie_patch>
(Nouveaux axiomes Turtle. Peut être vide.)
</ontologie_patch>

<triplets_candidats>
[{{"id": "...", "reason_predicat": "P-A|P-B|P-C1|P-C2|P-D|"}}]
</triplets_candidats>

<triplets_rejetes>
[{{"id":"...", "reason":"blocage_semantique"}}]
</triplets_rejetes>
</output_instructions>
"""

    (adir / "prompt.txt").write_text(f"[SYSTEM]\n{SYSTEM_AGENT_ENRICH_PREDICATES}\n\n[USER]\n{user_prompt}", 
        encoding="utf-8")       

    print("(agent_enrich_predicates.py) LLM pending...", flush=True)

    def _invoke():
        return llm.invoke([SystemMessage(content=SYSTEM_AGENT_ENRICH_PREDICATES), HumanMessage(content=user_prompt)])

    response = _invoke()
    usage = response.response_metadata.get("token_usage", {})
    stop_reason = response.response_metadata.get("finish_reason", "unknown")
    #print(f"(agent_enrich_predicates.py) stop_reason={stop_reason}", flush=True)

    ################# extract outputs ###################
    output = normalize_message_content(response.content)
    (adir / "raw_call1.txt").write_text(output, encoding="utf-8")
    r1 = extract_reasoning_text(response)
    (adir / "call1_raisonnement.txt").write_text(r1 if r1 else "(aucune trace reasoning)\n", encoding="utf-8")

    patch_ttl = extract_patch_block(output)
    candidates_raw = extract_candidates_block(output)
    rejected_raw = extract_rejected_block(output)

    patch_parse_ok = patch_ttl is not None
    cand_ok = candidates_block_present(output)

    ############################ probleme parsing or truncation --> 2eme appel LLM
    if not patch_parse_ok or not cand_ok or stop_reason == "length":
        print("(agent_enrich_predicates.py) parse incomplet --> 2e appel LLM")
        if stats:
            stats.record_llm_second_call("agent_enrich_predicates", "enrich_retry_parse")

        response = _invoke()
        usage2 = response.response_metadata.get("token_usage", {})
        # cumul tokens call1 + call2
        usage = merge_token_usage(usage, usage2)
        output = normalize_message_content(response.content)
        (adir / "raw_call2.txt").write_text(output, encoding="utf-8")
        r2 = extract_reasoning_text(response)
        (adir / "call2_raisonnement.txt").write_text(r2 if r2 else "(aucune trace reasoning)\n", encoding="utf-8")

        patch_ttl = extract_patch_block(output)
        candidates_raw = extract_candidates_block(output)
        rejected_raw = extract_rejected_block(output)
        patch_parse_ok = patch_ttl is not None
    ##############################

    ############################## parsing <triple_candidate> and <triple_rejected>
    cand_ok = candidates_block_present(output)
    rej_ok = rejected_block_present(output)
    parse_failed = False

    if not cand_ok:
        print(
            f"(agent_enrich_predicates.py) PARSE FAIL after 2calls"
            f"need_pred={len(need_pred)} send to pool for next iterations | "
            f"semant+toolchain : skip LLM automatique (enrich_predicates_parse_failed=True)"
        )
        enriched_ttl = base_ttl
        patch_ttl = ""
        candidates = []
        rejected = reject_all_batch(need_pred, reason="parse_failure")
        parse_failed = True

    else:
        candidates = resolve_triplets_by_id(candidates_raw, candidates_by_id, "agent_enrich_predicates")
        if rej_ok:
            rejected = resolve_triplets_by_id(rejected_raw, candidates_by_id, "agent_enrich_predicates")
        else:
            print("(agent_enrich_predicates.py) rejeté non parsable -> rejected = need_pred − candidats")
            rejected = infer_rejected_from_candidates(need_pred, candidates)

        rejected = ensure_batch_coverage(need_pred, candidates, rejected, agent_name="agent_enrich_predicates")

        #################### patch parsing ############################
        if not patch_parse_ok:
            print("(agent_enrich_predicates.py) <ontology_patch> absent -> TTL = base")
            enriched_ttl = base_ttl
            patch_ttl = ""
        elif patch_ttl == "":
            print("(agent_enrich_predicates.py) Patch vide (stratégie A) -> TTL = base")
            enriched_ttl = base_ttl
        else:
            enriched_ttl = merge_patch(base_ttl, patch_ttl)
            diff = compute_diff(base_ttl, enriched_ttl, label=f"agent_enrich_predicates_{iteration}")
            print_diff(diff)
            save_diff(diff, adir / "diff.json")

    valid_assertions = candidates + already_ok

    print(
        f"(agent_enrich_predicates.py) proposés_ok={len(candidates)} | "
        f"already_ok={len(already_ok)} | proposés_rejet={len(rejected)} | parse_failed={parse_failed}"
    )

    (adir / "patch.ttl").write_text(patch_ttl or "", encoding="utf-8")
    (adir / "output.ttl").write_text(enriched_ttl, encoding="utf-8")
    (adir / "candidates.json").write_text(json.dumps(valid_assertions, ensure_ascii=False, indent=2), encoding="utf-8")
    (adir / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")

    ################# stats ###################
    if stats:
        reasons = {}
        if parse_failed:
            reasons["parse_failure"] = len(rejected)
        else:
            for candidate in candidates:
                r = candidate.get("reason_predicat", "?")
                #if r and r != "?":
                    #reasons[str(r)] = reasons.get(str(r), 0) + 1
                reasons[str(r)] = reasons.get(str(r), 0) + 1
        if already_ok:
            reasons["skip_already_ok"] = len(already_ok)
        stats.record_agent_predicates(
            integrated=len(candidates),  
            rejected=len(rejected),
            reasons=reasons)
        strat_ev = [
            (str(c["id"]), "predicate", str(c.get("reason_predicat", "?")))
            for c in candidates
            if isinstance(c, dict) and c.get("id")
        ]
        stats.record_enrich_strategy_events(iteration, strat_ev)
        usage_data = usage if isinstance(usage, dict) else {}
        stats.record_llm_usage(
            agent="agent_enrich_predicates",
            input_tokens=usage_data.get("input_tokens", usage_data.get("prompt_tokens", 0)),
            output_tokens=usage_data.get("output_tokens", usage_data.get("completion_tokens", 0)),
            model=MODEL_NAME,
            usage_dict=usage_data,
        )

    return {
        **base_state(state),
        "enriched_ttl_predicates": enriched_ttl,
        "valid_assertions": valid_assertions,
        "enrich_predicates_parse_failed": parse_failed,
        "predicate_toolchain_needed": bool(enriched_ttl != base_ttl),
        "triplet_layers": layers,
        "messages": [
            AIMessage(
                content=(
                    f"[EnrichPred] new={len(candidates)} already_ok={len(already_ok)} "
                    f"proposés_rejet={len(rejected)} parse_failed={parse_failed}"
                )
            )
        ],
    }
