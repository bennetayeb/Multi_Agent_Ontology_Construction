import json

from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from config import DEEPSEEK_API_KEY, MODEL_NAME, get_agent_dir
from datasets.prompts.system_prompts_structuré import SYSTEM_AGENT_VALIDATOR_PREDICATES
from run_context import get_stats
from state import PipelineState, base_state
from utils.llm_response_utils import extract_reasoning_text, merge_token_usage, normalize_message_content
from utils.ontology_diff import compute_diff, print_diff, save_diff
from utils.ontology_utils import merge_correction_patch
from utils.parsers import (
    candidates_block_present,
    extract_candidates_block,
    extract_corrections_patch_block,
    extract_rejected_block,
    infer_rejected_from_candidates,
    rejected_block_present,
    resolve_triplets_by_id,
    strip_list_for_llm,
)
from utils.triplet_layers import (
    apply_predicate_decisions,
    normalize_layers,
    partition_by_layer,
)


def _load_enrich_rejected(iteration):
    path = get_agent_dir(iteration, "enrich_predicates") / "rejected.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def validate_semantics_predicates(state):
    print(f"\n(agent_validator_semant_predicates.py) Validation sémantique prédicats")
    iteration = state.get("iteration_count", 0)
    agent_dir = get_agent_dir(iteration, "validator_semant_predicates")
    stats = get_stats()

    layers = normalize_layers(state.get("triplet_layers"))

    # TTL de référence avant enrichissement prédicats
    ttl_before = (
        state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("enriched_ttl_subjects")# PAS VALIDE 
        or state["ontology_ttl"]
    )

    # x1 : sujets non validés -> skip
    if not state.get("validation_subject_ok", False):
        print("(agent_validator_semant_predicates.py) validation sujets échouée -> skip")
        if stats:
            stats.record_semantics_predicates_check(
                0, 0, skipped=True, skip_reason="validation_subject_ok_false"
            )
        return {
            **base_state(state),
            "messages": [
                AIMessage(content="[ValidatorPredSem] skip: validation_subject_ok=False")
            ],
        }

    # A) probeleme parsing : l'enricher n'a pas pu parser
    # need_subject --> send to pool for next iterations via layers (subject=False conservé) & already_ok passent
    if state.get("enrich_predicates_parse_failed"):
        already_ok = list(state.get("valid_assertions", []) or [])
        print(
            f"(agent_validator_semant_predicates.py) enrich_predicates_parse_failed -> "
            f"skip LLM | TTL=ttl_before | already_ok={len(already_ok)} passent"
        )
        _save_outputs(agent_dir, ttl_before, already_ok, [], "")

        if stats:
            stats.record_semantics_predicates_check(
                candidates_confirmed=len(already_ok), rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in already_ok if c.get("id")],
                skipped=True, skip_reason="enrich_predicates_parse_failed",
            )
        return {
            **base_state(state),
            "ttl_after_predicate_semantic": ttl_before,
            "enriched_ttl_predicates": ttl_before,
            "valid_assertions": already_ok,
            "validation_predicate_ok": len(already_ok) > 0,
            "predicate_toolchain_needed": False,
            "triplet_layers": layers,
            "messages": [
                AIMessage(content=f"[ValidatorPredSem] skip parse_failure already_ok={len(already_ok)}")
            ],
        }

    valid_assertions = state.get("valid_assertions", [])
    need_pred, already_ok = partition_by_layer(valid_assertions, layers, "predicate")
    enrich_rejected = _load_enrich_rejected(iteration)

    ttl_enriched = state["enriched_ttl_predicates"]

    print(
        f"(agent_validator_semant_predicates.py) need_pred={len(need_pred)} | "
        f"enrich_rejected={len(enrich_rejected)} | already_ok={len(already_ok)}"
    )

    # skip -> tous déjà validés, aucun rejet enrichisseur à confirmer
    if not need_pred and not enrich_rejected:
        candidates = already_ok
        _save_outputs(agent_dir, ttl_enriched, candidates, [], "")
        if stats:
            stats.record_semantics_predicates_check(
                candidates_confirmed=len(candidates), rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in candidates if c.get("id")],
                skipped=True, skip_reason="all_predicate_already_ok",
            )
        st = _build_state(state, ttl_enriched, candidates, [], layers, update_layers=False)
        st["predicate_toolchain_needed"] = False
        return st

    # diff
    diff = compute_diff(ttl_before, ttl_enriched, label=f"ValidatorPredSem_iter{iteration}")
    print_diff(diff, verbose=True)
    save_diff(diff, agent_dir / "diff.json")

    patch_path = get_agent_dir(iteration, "enrich_predicates") / "patch.ttl"
    patch_ttl = patch_path.read_text(encoding="utf-8").strip() if patch_path.exists() else ""

    # AUTO VALIDATION : stratégie A pur (diff vide + aucun rejet à confirmer)
    if diff.is_empty and not enrich_rejected:
        print("(agent_validator_semant_predicates.py) stratégie conservative pur -> validation auto")
        new_ok = [{**c, "reason_predicat": c.get("reason_predicat", "?")} for c in need_pred]
        candidates = new_ok + already_ok
        _save_outputs(agent_dir, ttl_enriched, candidates, [], "")
        if stats:
            stats.record_semantics_predicates_check(
                candidates_confirmed=len(candidates), rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in candidates if c.get("id")],
            )
        st = _build_state(state, ttl_enriched, candidates, [], layers, newly_accepted=new_ok)
        st["predicate_toolchain_needed"] = False
        return st

    candidates_by_id = {
        c["id"]: c
        for c in (*need_pred, *enrich_rejected)
        if isinstance(c, dict) and "id" in c
    }

    llm = ChatDeepSeek(
        model=MODEL_NAME,
        api_key=DEEPSEEK_API_KEY,
        max_tokens=16000,
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
    )

    assertions_for_llm = strip_list_for_llm(need_pred, extra_keep={"reason", "reason_predicat"})
    patch_display = patch_ttl if patch_ttl else "(aucun patch prédicat)"

    user_prompt = f"""Ontologie enrichie par l'agent précédent (référence de lecture, ne pas la réécrire) :
{ttl_enriched}

Nouveaux axiomes Turtle ajoutés par l'agent précédent (Patch) :
{patch_display}

Triplets intégrés avec leur stratégie déclarée :
{json.dumps(assertions_for_llm, ensure_ascii=False, indent=2)}

Triplets rejetés par l'agent enrichissement prédicats (itération courante) :
{json.dumps(strip_list_for_llm(enrich_rejected), ensure_ascii=False, indent=2)}

<output_format>
Réponds avec exactement trois blocs XML, dans cet ordre, sans autre texte :

<corrections_semantiques>
(Turtle ciblé ou vide si patch correct)
</corrections_semantiques>

<triplets_candidats>
[{{"id": "...", "reason_predicat": "P-A|P-B|P-C1|P-C2|P-D"}}]
</triplets_candidats>

<triplets_rejetes>
[{{"id": "..."}}]
</triplets_rejetes>
</output_format>
"""

    (agent_dir / "prompt.txt").write_text(
        f"[SYSTEM]\n{SYSTEM_AGENT_VALIDATOR_PREDICATES}\n\n[USER]\n{user_prompt}", encoding="utf-8"
    )

    def _invoke():
        return llm.invoke(
            [SystemMessage(content=SYSTEM_AGENT_VALIDATOR_PREDICATES), HumanMessage(content=user_prompt)]
        )

    response = _invoke()
    usage = response.response_metadata.get("token_usage", {})
    stop_reason = response.response_metadata.get("finish_reason", "unknown")

    output = normalize_message_content(response.content)
    (agent_dir / "raw_call1.txt").write_text(output, encoding="utf-8")
    r1 = extract_reasoning_text(response)
    (agent_dir / "call1_raisonnement.txt").write_text(r1 if r1 else "(aucune trace reasoning)\n", encoding="utf-8")

    corrections_patch = extract_corrections_patch_block(output)
    candidates_raw = extract_candidates_block(output)
    rejected_raw = extract_rejected_block(output)

    patch_parse_ok = corrections_patch is not None
    cand_ok = candidates_block_present(output)
    rej_ok = rejected_block_present(output)

    if not patch_parse_ok or not cand_ok or stop_reason == "length":
        print("(agent_validator_semant_predicates.py) parse incomplet -> 2e appel LLM")
        if stats:
            stats.record_llm_second_call("sem_validator_predicates", "semantic_retry_parse")
        response = _invoke()
        usage2 = response.response_metadata.get("token_usage", {})
        usage = merge_token_usage(usage, usage2)
        output = normalize_message_content(response.content)
        (agent_dir / "raw_call2.txt").write_text(output, encoding="utf-8")
        r2 = extract_reasoning_text(response)
        (agent_dir / "call2_raisonnement.txt").write_text(r2 if r2 else "(aucune trace reasoning)\n", encoding="utf-8")

        corrections_patch = extract_corrections_patch_block(output)
        candidates_raw = extract_candidates_block(output)
        rejected_raw = extract_rejected_block(output)
        patch_parse_ok = corrections_patch is not None
        cand_ok = candidates_block_present(output)
        rej_ok = rejected_block_present(output)

    # parse KO after 2 calls : need_pred conservés mais layers NON mis à jour
    # -> predicate=False conservé -> retraitement itération suivante
    if not patch_parse_ok or not cand_ok:
        print("(agent_validator_semant_predicates.py) parse KO after 2 calls")
        candidates = [dict(c) for c in need_pred] + already_ok
        _save_outputs(agent_dir, ttl_enriched, candidates, [], output)
        if stats:
            u = usage if isinstance(usage, dict) else {}
            stats.record_llm_usage(
                agent="sem_validator_predicates",
                input_tokens=u.get("input_tokens", u.get("prompt_tokens", 0)),
                output_tokens=u.get("output_tokens", u.get("completion_tokens", 0)),
                model=MODEL_NAME,
            )
            stats.record_semantics_predicates_check(
                candidates_confirmed=len(already_ok),
                rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in already_ok if c.get("id")],
                skipped=True,
                skip_reason="parse_ko_both_calls",
            )
        st = _build_state(state, ttl_enriched, candidates, [], layers, newly_accepted=[], newly_rejected=[])
        st["validation_predicate_ok"] = False
        st["predicate_toolchain_needed"] = False
        return st

    # IDS
    new_ok = resolve_triplets_by_id(candidates_raw, candidates_by_id, "validator_semant_predicates")
    if rej_ok:
        rejected = resolve_triplets_by_id(rejected_raw, candidates_by_id, "validator_semant_predicates")
    else:
        print("(agent_validator_semant_predicates.py) <triple_rejected> absent -> rejetés = need_pred − candidats")
        rejected = infer_rejected_from_candidates(need_pred, new_ok)

    candidates = new_ok + already_ok

    #
    enriched_ttl = ttl_enriched
    corrections_applied = False
    if corrections_patch and corrections_patch.strip():
        print("(agent_validator_semant_predicates.py) corrections sémantiques -> application patch")
        candidate_ttl, corrections_applied = merge_correction_patch(ttl_enriched, corrections_patch)
        if corrections_applied:
            enriched_ttl = candidate_ttl
            corr_diff = compute_diff(ttl_enriched, enriched_ttl, label=f"ValidatorPred_corr_iter{iteration}")
            print_diff(corr_diff)
            save_diff(corr_diff, agent_dir / "corrections_diff.json")
        else:
            print("(agent_validator_semant_predicates.py) patch correctif non appliqué -> TTL inchangé")
    elif corrections_patch == "":
        print("(agent_validator_semant_predicates.py) aucune correction nécessaire")

    print(
        f"(agent_validator_semant_predicates.py) new_ok={len(new_ok)} | "
        f"already_ok={len(already_ok)} | rejetés={len(rejected)}"
    )
    _save_outputs(agent_dir, enriched_ttl, candidates, rejected, output)

    if stats:
        u = usage if isinstance(usage, dict) else {}
        stats.record_semantics_predicates_check(
            candidates_confirmed=len(candidates),
            rejected=len(rejected),
            corrections_applied=corrections_applied,
            confirmed_ids=[str(c["id"]) for c in candidates if c.get("id")])

        stats.record_llm_usage(
            agent="sem_validator_predicates",
            input_tokens=u.get("input_tokens", u.get("prompt_tokens", 0)),
            output_tokens=u.get("output_tokens", u.get("completion_tokens", 0)),
            model=MODEL_NAME,
            usage_dict=u)

    return _build_state(
        state, enriched_ttl, candidates, rejected, layers,
        newly_accepted=new_ok, newly_rejected=rejected)


def _save_outputs(agent_dir, enriched_ttl, candidates, rejected, raw_output):
    (agent_dir / "output.ttl").write_text(enriched_ttl, encoding="utf-8")
    (agent_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (agent_dir / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    if raw_output:
        (agent_dir / "raw_last.txt").write_text(raw_output, encoding="utf-8")


def _build_state(state, enriched_ttl, candidates, rejected, layers,
    newly_accepted=None, newly_rejected=None, update_layers=True):
    if update_layers:
        layers = apply_predicate_decisions(
            layers,
            newly_accepted if newly_accepted is not None else candidates,
            newly_rejected if newly_rejected is not None else rejected)

    ttl_before = (
        state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("enriched_ttl_subjects")
        or state["ontology_ttl"])
    return {
        **base_state(state),
        "ttl_after_predicate_semantic": enriched_ttl,
        "enriched_ttl_predicates": enriched_ttl,
        "valid_assertions": candidates,
        "rejected_triplets": state.get("rejected_triplets", []) + rejected,
        "predicate_toolchain_needed": bool(enriched_ttl != ttl_before),
        "validation_predicate_ok": len(candidates) > 0,
        "triplet_layers": layers,
        
        "messages": [
            AIMessage(content=f"[ValidatorPredSem] candidats={len(candidates)} rejetés_confirmés={len(rejected)}")
        ],
    }
