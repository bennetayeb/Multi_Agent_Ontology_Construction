import json
from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from config import DEEPSEEK_API_KEY, MODEL_NAME, get_agent_dir
from state import base_state
from datasets.prompts.system_prompts_structuré import SYSTEM_AGENT_VALIDATOR_SUBJECTS
from utils.llm_response_utils import extract_reasoning_text, merge_token_usage, normalize_message_content
from utils.ontology_diff import compute_diff, print_diff, save_diff
from utils.ontology_utils import merge_correction_patch
from utils.parsers import (
    extract_candidates_block,
    extract_corrections_patch_block,
    extract_rejected_block,
    candidates_block_present,
    rejected_block_present,
    resolve_triplets_by_id,
    infer_rejected_from_candidates,
    strip_list_for_llm,
)
from utils.triplet_layers import (
apply_subject_decisions,
normalize_layers,
partition_by_layer,
)
from run_context import get_stats


def _load_enrich_rejected(iteration):
    path = get_agent_dir(iteration, "enrich_subjects") / "rejected.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def validate_semantics_subjects(state):
    print(f"\n(agent_validator_semant_subjects.py) Validation sémantique sujets ")
    iteration = state.get("iteration_count", 0)
    adir = get_agent_dir(iteration, "validator_semant_subjects")
    stats = get_stats()

    layers = normalize_layers(state.get("triplet_layers"))
    
    # A) probeleme parsing : l'enricher n'a pas pu parser
    # need_subject --> send to pool for next iterations via layers (subject=False conservé) & already_ok passent
    if state.get("enrich_subjects_parse_failed"):
        already_ok = list(state.get("valid_assertions", []) or [])
        ttl = state["ontology_ttl"]  # rollback : TTL enrichi non fiable
        print(f"(agent_validator_semant_subjects.py) enrich_subjects_parse_failed -> "
            f"skip LLM | TTL=ontology_ttl | already_ok={len(already_ok)} passent")
        #_save_outputs(agent_dir_name, enriched_ttl, candidates, rejected, output)
        _save_outputs(adir, ttl, already_ok, [], "") 

        if stats:
            stats.record_semantics_check(
                candidates_confirmed=len(already_ok),
                rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in already_ok if c.get("id")],
                skipped=True,
                skip_reason="enrich_subjects_parse_failed",
            )
        return {
            **base_state(state),
            "ttl_after_subject_semantic": ttl,
            "valid_assertions": already_ok,
            "validation_subject_ok": len(already_ok) > 0,
            "subject_toolchain_needed": False,
            "triplet_layers": layers,
            "subject_assertions_semantic_ok": already_ok or state.get("subject_assertions_semantic_ok", []),
            "messages": [
                AIMessage(content=f"[ValidatorSujetSem] skip parse_failure already_ok={len(already_ok)}")
            ],
        }

    valid_assertions = state.get("valid_assertions", [])
    # need_sem to validate --> new_ok / already_ok --> already validated
    need_sem, already_ok = partition_by_layer(valid_assertions, layers, "subject")
    enrich_rejected = _load_enrich_rejected(iteration) # rejected enricher 

    ttl_before = state["ontology_ttl"]
    ttl_enriched = state["enriched_ttl_subjects"] # trigger error, spread into the code 

    print(
        f"(agent_validator_semant_subjects.py) need_sem={len(need_sem)} | "
        f"enrich_rejected={len(enrich_rejected)} | already_ok={len(already_ok)}"
    )

    # skip--> all already validated, no enrich rejected to confirm 
    if not need_sem and not enrich_rejected:
        candidates = already_ok
        _save_outputs(adir, ttl_enriched, candidates, [], "")
        if stats:
            stats.record_semantics_check(
                candidates_confirmed=len(candidates), rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in candidates if c.get("id")],
                skipped=True, skip_reason="all_subject_already_ok",
            )
        st = _build_state(state, ttl_enriched, candidates, [], layers, update_layers=False)
        st["subject_assertions_semantic_ok"] = candidates
        st["subject_toolchain_needed"] = False
        return st

    # calculate diff + patch 
    diff = compute_diff(ttl_before, ttl_enriched, label=f"ValidatorSubjSem_iter{iteration}")
    print_diff(diff, verbose=True)
    save_diff(diff, adir / "diff.json")

    patch_path = get_agent_dir(iteration, "enrich_subjects") / "patch.ttl"
    patch_ttl = patch_path.read_text(encoding="utf-8").strip() if patch_path.exists() else ""


    # AUTO VALIDATION : strategie A pur
    # Aucun changement : diff empty + aucun rejet à confirmer -> no call LLM
    if diff.is_empty and not enrich_rejected:
        print("(agent_validator_semant_subjects.py) stratégie A pur -> validation auto")
        new_ok = [{**c, "reason": c.get("reason", "A")} for c in need_sem]
        candidates = new_ok + already_ok
        _save_outputs(adir, ttl_enriched, candidates, [], "")
        if stats:
            stats.record_semantics_check(
                candidates_confirmed=len(candidates), rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in candidates if c.get("id")],
            )
        st = _build_state(state, ttl_enriched, candidates, [], layers, newly_accepted=new_ok)
        st["subject_assertions_semantic_ok"] = candidates
        st["subject_toolchain_needed"] = False
        return st


    candidates_by_id = {
        c["id"]: c
        for c in (*need_sem, *enrich_rejected)
        if isinstance(c, dict) and "id" in c
    }


    llm = ChatDeepSeek(
        model=MODEL_NAME,
        api_key=DEEPSEEK_API_KEY,
        max_tokens=16000,
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
    )

    assertions_for_llm = strip_list_for_llm(need_sem, extra_keep={"reason"})
    patch_display = patch_ttl if patch_ttl else "(No patch)"

    user_prompt = f"""Ontologie enrichie par l'agent précédent (référence de lecture, ne pas la réécrire) :
{ttl_enriched}

Nouveaux axiomes Turtle ajoutés par l'agent précédent (Patch) :
{patch_display}

Triplets intégrés avec leur stratégie déclarée :
{json.dumps(assertions_for_llm, ensure_ascii=False, indent=2)}

Triplets rejetés par l'agent enrichissement sujets (itération courante) :
{json.dumps(strip_list_for_llm(enrich_rejected), ensure_ascii=False, indent=2)}

<output_format>
Réponds avec exactement trois blocs XML, dans cet ordre, sans autre texte :

<corrections_semantiques>
(Turtle ciblé ou vide si patch correct)
</corrections_semantiques>

<triplets_candidats>
[{{"id": "...", "reason": "A|B|C1|C2|D"}}]
</triplets_candidats>

<triplets_rejetes>
[{{"id": "..."}}]
</triplets_rejetes>
</output_format>
"""

    (adir / "prompt.txt").write_text(
        f"[SYSTEM]\n{SYSTEM_AGENT_VALIDATOR_SUBJECTS}\n\n[USER]\n{user_prompt}", encoding="utf-8"
    )

    def _invoke():
        return llm.invoke(
            [SystemMessage(content=SYSTEM_AGENT_VALIDATOR_SUBJECTS), HumanMessage(content=user_prompt)]
        )

    response = _invoke()
    usage = response.response_metadata.get("token_usage", {})
    stop_reason = response.response_metadata.get("finish_reason", "unknown")
    #print(f"(agent_validator_semant_subjects.py) stop_reason={stop_reason}")

    output = normalize_message_content(response.content)
    (adir / "raw_call1.txt").write_text(output, encoding="utf-8")
    r1 = extract_reasoning_text(response)
    (adir / "call1_raisonnement.txt").write_text(r1 if r1 else "(aucune trace reasoning)\n", encoding="utf-8")

    corrections_patch = extract_corrections_patch_block(output)
    candidates_raw = extract_candidates_block(output)
    rejected_raw = extract_rejected_block(output)

    patch_parse_ok = corrections_patch is not None   # <corrections_patch> bloc présent ?
    cand_ok = candidates_block_present(output)        # <triple_candidate> bloc présent ?
    rej_ok = rejected_block_present(output)          

    if not patch_parse_ok or not cand_ok or stop_reason == "length":
        print("(agent_validator_semant_subjects.py) parse incomplet --> 2e appel LLM")
        if stats:
            stats.record_llm_second_call("sem_validator_subjects", "semantic_retry_parse")
        response = _invoke()
        usage2 = response.response_metadata.get("token_usage", {})
        usage = merge_token_usage(usage, usage2)
        output = normalize_message_content(response.content)
        (adir / "raw_call2.txt").write_text(output, encoding="utf-8")
        r2 = extract_reasoning_text(response)
        (adir / "call2_raisonnement.txt").write_text(r2 if r2 else "(aucune trace reasoning)\n", encoding="utf-8")

        corrections_patch = extract_corrections_patch_block(output)
        candidates_raw = extract_candidates_block(output)
        rejected_raw = extract_rejected_block(output)
        patch_parse_ok = corrections_patch is not None
        cand_ok = candidates_block_present(output)
        rej_ok = rejected_block_present(output)

    # need_sem conservés provisoirement mais layers NON mises à jour
    # -> subject=False conservé -> retraitement itération suivante
    if not patch_parse_ok or not cand_ok:
        print("(agent_validator_semant_subjects.py) parse KO after 2 calls")

        candidates = [dict(c) for c in need_sem] + already_ok
        _save_outputs(adir, ttl_enriched, candidates, [], output)
        if stats:
            u = usage if isinstance(usage, dict) else {}
            stats.record_llm_usage(
                agent="sem_validator_subjects",
                input_tokens=u.get("input_tokens", u.get("prompt_tokens", 0)),
                output_tokens=u.get("output_tokens", u.get("completion_tokens", 0)),
                model=MODEL_NAME,
            )
            stats.record_semantics_check(
                candidates_confirmed=len(already_ok),
                rejected=0,
                corrections_applied=False,
                confirmed_ids=[str(c["id"]) for c in already_ok if c.get("id")],
                skipped=True,
                skip_reason="parse_ko_both_calls",
            )
        st = _build_state(state, ttl_enriched, candidates, [], layers, newly_accepted=[], newly_rejected=[])
        st["subject_assertions_semantic_ok"] = already_ok
        st["validation_subject_ok"] = False
        st["subject_toolchain_needed"] = False
        return st
    # ids 
    new_ok = resolve_triplets_by_id(candidates_raw, candidates_by_id, "validator_semant_subjects")
    if rej_ok:
        rejected = resolve_triplets_by_id(rejected_raw, candidates_by_id, "validator_semant_subjects")
    else:
        print("(agent_validator_semant_subjects.py) <triple_rejected> absent -> rejetés = need_sem − candidats")
        rejected = infer_rejected_from_candidates(need_sem, new_ok)

    candidates = new_ok + already_ok

    #apply patch 
    enriched_ttl = ttl_enriched
    corrections_applied = False
    if corrections_patch and corrections_patch.strip():
        print("(agent_validator_semant_subjects.py) corrections sémantiques --> application patch")
        candidate_ttl, corrections_applied = merge_correction_patch(ttl_enriched, corrections_patch)
        if corrections_applied:
            enriched_ttl = candidate_ttl
            corr_diff = compute_diff(ttl_enriched, enriched_ttl, label=f"ValidatorSubj_corr_iter{iteration}")
            print_diff(corr_diff)
            save_diff(corr_diff, adir / "corrections_diff.json")
        else:
            print("(agent_validator_semant_subjects.py) patch correctif non appliqué --> TTL inchangé")
    elif corrections_patch == "":
        print("(agent_validator_semant_subjects.py) aucune correction nécessaire ")

    print(
        f"(agent_validator_semant_subjects.py) new_ok={len(new_ok)} | "
        f"already_ok={len(already_ok)} | rejetés={len(rejected)}"
    )
    _save_outputs(adir, enriched_ttl, candidates, rejected, output)

    if stats:
        u = usage if isinstance(usage, dict) else {}
        stats.record_semantics_check(
            candidates_confirmed=len(candidates),
            rejected=len(rejected),
            corrections_applied=corrections_applied,
            confirmed_ids=[str(c["id"]) for c in candidates if c.get("id")])

        stats.record_llm_usage(
            agent="sem_validator_subjects",
            input_tokens=u.get("input_tokens", u.get("prompt_tokens", 0)),
            output_tokens=u.get("output_tokens", u.get("completion_tokens", 0)),
            model=MODEL_NAME,
            usage_dict=u)

    st = _build_state(
        state, enriched_ttl, candidates, rejected, layers,
        newly_accepted=new_ok, newly_rejected=rejected,
    )
    st["subject_assertions_semantic_ok"] = candidates
    return st





def _save_outputs(adir, enriched_ttl, candidates, rejected, raw_output):
    (adir / "output.ttl").write_text(enriched_ttl, encoding="utf-8")
    (adir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (adir / "rejected.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    if raw_output:
        (adir / "raw_last.txt").write_text(raw_output, encoding="utf-8")


def _build_state(state, enriched_ttl, candidates, rejected, layers,
    newly_accepted=None, newly_rejected=None, update_layers=True):
    if update_layers:
        layers = apply_subject_decisions(
            layers,
            newly_accepted if newly_accepted is not None else candidates,
            newly_rejected if newly_rejected is not None else rejected)
    return {
        **base_state(state),
        "ttl_after_subject_semantic": enriched_ttl,
        "enriched_ttl_subjects": enriched_ttl,
        "valid_assertions": candidates,
        "rejected_triplets": state.get("rejected_triplets", []) + rejected,
        "subject_toolchain_needed": bool(enriched_ttl != state.get("ontology_ttl")),
        "validation_subject_ok": len(candidates) > 0,
        "triplet_layers": layers,
        
        "messages": [
            AIMessage(content=f"[ValidatorSujetSem] candidats={len(candidates)} rejetés_confirmés={len(rejected)}")
        ],
    }
