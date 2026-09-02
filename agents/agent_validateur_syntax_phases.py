import json
import os
import re
from pathlib import Path

from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from config import MODEL_NAME, DEEPSEEK_API_KEY, get_agent_dir, get_iteration_dir, JAVA_EXE, HERMIT_JAR
from datasets.prompts.system_prompts_structuré import (
    SYSTEM_AGENT_RDFLIB_SYNT,
    SYSTEM_AGENT_VALIDATOR_HERMIT_OOPS,
)
from tools.validation_tools import validate_syntax_rdf, validate_with_reasoner, validate_oops
from utils.llm_response_utils import extract_reasoning_text
from utils.parsers import extract_turtle_block, extract_corrections_patch_block
from utils.ontology_utils import merge_patch
from run_context import get_stats
from state import base_state


def _build_llm_syntax():
    return ChatDeepSeek(
        model=MODEL_NAME,
        api_key=DEEPSEEK_API_KEY,
        max_tokens=20000,
        extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        })


def _build_llm_oops_hermit():
    return ChatDeepSeek(
        model=MODEL_NAME,
        api_key=DEEPSEEK_API_KEY,
        max_tokens=16000,
        extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
    )


def _safe_slug(text):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip())

def _json_dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _json_load(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _format_oops_issue(issue) :
    code = issue.get("code", "P??")
    name = issue.get("name", "").strip()
    affected = issue.get("affected", [])
    affected_txt = f" | affected={affected}" if affected else ""
    return f"[OOPS] {code} {name}{affected_txt}"


def _collect_error_signatures(rdflib_errors, hermit_errors, oops_critical, oops_error=None):
    signatures = list(rdflib_errors)
    signatures.extend(hermit_errors)
    signatures.extend([_format_oops_issue(p) for p in oops_critical])
    if oops_error:
        signatures.append(f"[OOPS-API] {oops_error}")
    seen = set()
    uniq = []
    for s in signatures:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

####### pour les stats 

def _compare_error_lists(previous_errors, current_errors):
    prev = set(previous_errors or [])
    curr = set(current_errors or [])
    return {
        #"previous_count": len(prev),
        "current_count" : len(curr),
        #"new_errors" : sorted(curr - prev),
        "resolved_errors": sorted(prev - curr),
    }



############################   validation tools 

def _run_rdf_syntax_check(ttl_content):
    syn_result = validate_syntax_rdf.invoke({"ttl_content": ttl_content})
    rdflib_errors = [f"[SYN] {e}" for e in syn_result.get("errors", [])]
    return {
        "valid" : syn_result["valid"],
        "errors" : rdflib_errors,
        "triple_count": syn_result.get("triple_count", 0),
    }


def _run_hermit_oops_checks(ttl_content) :
    hermit_result = validate_with_reasoner.invoke({
        "ttl_content": ttl_content,
        "java_exe" : JAVA_EXE,
        "hermit_jar": HERMIT_JAR,
    })
    oops_result = validate_oops.invoke({"ttl_content": ttl_content})

    hermit_errors = [f"[REA] {e}" for e in hermit_result.get("errors", [])]
    hermit_warnings = [f"[WARN-REA] {w}" for w in hermit_result.get("warnings", [])]

    oops_error = oops_result.get("error")
    oops_unavailable = bool(oops_result.get("oops_unavailable"))
    oops_unavailable_reason = oops_result.get("oops_unavailable_reason") or ""
    oops_critical = []
    oops_warnings = []
    if not oops_error:
        oops_critical = oops_result.get("critical", [])
        oops_warnings = oops_result.get("warnings", [])


    warnings = list(hermit_warnings)
    if oops_unavailable and oops_unavailable_reason:
        warnings.append(f"[WARN-OOPS] {oops_unavailable_reason}")
        print(
            f"(agent_validateur_syntax_phases.py) OOPS injoignable (non bloquant) : "
            f"{oops_unavailable_reason}"
        )
    elif oops_error:
        warnings.append(f"[WARN-OOPS] {oops_error}")

    for problem in oops_warnings:
        formatted_warning = _format_oops_issue(problem)
        formatted_warning = formatted_warning.replace(
            "[OOPS]",
            "[WARN-OOPS]"
        )
        warnings.append(formatted_warning)


    current_errors = _collect_error_signatures(
        rdflib_errors=[],
        hermit_errors=hermit_errors,
        oops_critical=oops_critical,
        oops_error=None if oops_unavailable else oops_error,
    )

    oops_checked = oops_error is None
    valid = len(hermit_errors) == 0 and len(oops_critical) == 0 and oops_checked

    if oops_error and not oops_unavailable:
        print(f"(agent_validateur_syntax_phases.py) OOPS indisponible : {oops_error}")

    return {
        "valid" : valid,
        "hermit_errors": hermit_errors,
        "hermit_warnings": hermit_warnings,
        "oops_critical": oops_critical,
        "oops_warnings": oops_warnings,
        "oops_error": oops_error,
        "oops_unavailable": oops_unavailable,
        "warnings" : warnings,
        "current_errors": current_errors}

def _repair_with_llm(system_prompt, ttl, validation_report, phase, suffix, artifact_dir: Path):

    llm = _build_llm_oops_hermit() if suffix == "semantic" else _build_llm_syntax()
    #### 1)
    if suffix == "semantic":
        user_prompt = f"""Ontologie Turtle complète :
    {ttl}

    Rapport de validation HermiT/OOPS :
    {json.dumps(validation_report, ensure_ascii=False, indent=2)}

    Renvoyer exactement ce bloc XML, sans aucun autre texte :
    <corrections_patch>
    (Corrections ciblées en Turtle.
    Exemple d'ajout : ex:NewClass rdfs:subClassOf ex:Parent .
    Exemple de suppression : # DELETE: ex:ClassA rdfs:subClassOf ex:ClassB .
    Laisser vide si aucune correction n'est nécessaire.)
    </corrections_patch>
    """
    else:
        user_prompt = f"""Ontologie Turtle à corriger :
    {ttl}

    Rapport de validation RDFLib :
    {json.dumps(validation_report, ensure_ascii=False, indent=2)}

    <output_instructions>
    Renvoyer exactement ce bloc XML, sans aucun autre texte :
    <enriched_ontology>
    (Turtle complet et syntaxiquement valide)
    </enriched_ontology>

    </output_instructions>
    """

    phase_slug = _safe_slug(phase)
    artifact_dir = Path(artifact_dir)
    os.makedirs(os.fspath(artifact_dir), exist_ok=True)
    prompt_file = artifact_dir / f"repair_{suffix}_{phase_slug}_prompt.txt"
    os.makedirs(os.fspath(prompt_file.parent), exist_ok=True)
    prompt_file.write_text(
        f"system prompt : \n{system_prompt}\n\nuser prompt : \n{user_prompt}",
        encoding="utf-8",
    )

    response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])

    ### checks 
    stop_reason = response.response_metadata.get("finish_reason", "unknown")
    usage = response.response_metadata.get("token_usage", {})

    ps = get_stats()
    if ps:
        ck = "syntax_repair_rdflib" if suffix == "syntax" else "syntax_repair_hermit_oops"
        ps.record_llm_usage(
            agent=f"syntax_repair_{suffix}",
            input_tokens=0,
            output_tokens=0,
            model=MODEL_NAME,
            usage_dict=usage if isinstance(usage, dict) else {},
            call_kind=ck,
        )

    print(f"(agent_validateur_syntax_phases.py) stop_reason={stop_reason}")
    print(f"(agent_validateur_syntax_phases.py) usage={usage}")

    if stop_reason == "length":
        print("--> TRUNCATION PATCH ")
    elif stop_reason == "stop":
        print("generation complète")


    raw = response.content
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        raw_output = " ".join(parts)
    else:
        raw_output = str(raw)

    output_file = artifact_dir / f"repair_{suffix}_{phase_slug}_raw.txt"
    os.makedirs(os.fspath(output_file.parent), exist_ok=True)
    output_file.write_text(raw_output, encoding="utf-8")
    reasoning = extract_reasoning_text(response)
    reasoning_file = artifact_dir / f"repair_{suffix}_{phase_slug}_reasoning.txt"
    reasoning_file.write_text(
        reasoning if reasoning else "(aucune trace reasoning)\n", encoding="utf-8"
    )
    print(f"(agent_validateur_syntax_phases.py) : raw output LLM save : {artifact_dir}")

    repaired_ttl = extract_turtle_block(raw_output) or ttl

    return raw_output, repaired_ttl



def _write_toolchain_diagnostic_files(out_dir, pre_rdf, post_rdf, hermit_oops_pre, hermit_oops_post):
    rdflib_d = out_dir / "rdflib"
    hermit_d = out_dir / "hermit"
    oops_d = out_dir / "oops"
    for d in (rdflib_d, hermit_d, oops_d):
        d.mkdir(parents=True, exist_ok=True)
    (rdflib_d / "errors_pre.txt").write_text("\n".join(pre_rdf.get("errors", [])), encoding="utf-8")
    (rdflib_d / "errors_post.txt").write_text("\n".join(post_rdf.get("errors", [])), encoding="utf-8")
    (rdflib_d / "valid_pre.txt").write_text(str(pre_rdf.get("valid")), encoding="utf-8")
    (rdflib_d / "valid_post.txt").write_text(str(post_rdf.get("valid")), encoding="utf-8")
    (hermit_d / "errors_pre.txt").write_text("\n".join(hermit_oops_pre.get("hermit_errors", [])), encoding="utf-8")
    (hermit_d / "errors_post.txt").write_text("\n".join(hermit_oops_post.get("hermit_errors", [])), encoding="utf-8")
    (hermit_d / "warnings_pre.txt").write_text("\n".join(hermit_oops_pre.get("hermit_warnings", [])), encoding="utf-8")
    (hermit_d / "warnings_post.txt").write_text("\n".join(hermit_oops_post.get("hermit_warnings", [])), encoding="utf-8")
    (oops_d / "critical_pre.json").write_text(json.dumps(hermit_oops_pre.get("oops_critical", []), ensure_ascii=False, indent=2), encoding="utf-8")
    (oops_d / "critical_post.json").write_text(json.dumps(hermit_oops_post.get("oops_critical", []), ensure_ascii=False, indent=2), encoding="utf-8")
    (oops_d / "warnings_pre.json").write_text(json.dumps(hermit_oops_pre.get("oops_warnings", []), ensure_ascii=False, indent=2), encoding="utf-8")
    (oops_d / "warnings_post.json").write_text(json.dumps(hermit_oops_post.get("oops_warnings", []), ensure_ascii=False, indent=2), encoding="utf-8")
    pre_err = hermit_oops_pre.get("oops_error")
    post_err = hermit_oops_post.get("oops_error")
    (oops_d / "api_status.txt").write_text(f"oops_error_pre={pre_err!r}\noops_error_post={post_err!r}\n", encoding="utf-8")

##########################################################################################################################

def validate_syntax(
    state,
    ttl_key,
    phase,
    status_key,
    stats_phase_key=None,
    output_syntax_ttl_key: str | None = None,
    artifact_dir_slug: str | None = None,
    compare_artifact_dir_slug: str | None = None):

    iteration = state.get("iteration_count", 0)
    iter_dir = get_iteration_dir(iteration)
    out_dir = get_agent_dir(iteration, artifact_dir_slug) if artifact_dir_slug else iter_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    phase_slug = _safe_slug(phase)
    use_named_reports = artifact_dir_slug is None
    ttl_fname = f"ontology_after_syntax_{phase_slug}.ttl" if use_named_reports else "ontology_validated.ttl"
    report_fname = f"syntax_report_{phase_slug}.json" if use_named_reports else "syntax_validation_report.json"

    ttl_initial = state.get(ttl_key)
    if not ttl_initial:
        print(f"(agent_validateur_syntax_phases.py) clé '{ttl_key}' absente dans le state --> fallback ontology_ttl.")
        ttl_initial = state["ontology_ttl"]

    print(f"\n(agent_validateur_syntax_phases.py) phase={phase} | iter={iteration}")

    ttl_current = ttl_initial
    syntax_fix_applied = False
    semantic_fix_applied = False
    syntax_raw_output = None
    semantic_raw_output = None
    ps = get_stats()







    # ########################### 1) rdflib syntax check ###############################
    pre_rdf = _run_rdf_syntax_check(ttl_current)
    print(f"rdflib avant: valid={pre_rdf['valid']} | erreurs={len(pre_rdf['errors'])}")
    for e in pre_rdf["errors"]:
        print(f" {e}")

    # repair RDFLIB syntax errors
    if not pre_rdf["valid"]:
        syntax_fix_applied = True
        print("(agent_validateur_syntax_phases.py) Erreur rdflib -->  correction via LLM")

        syntax_raw_output, ttl_current = _repair_with_llm(
            system_prompt=SYSTEM_AGENT_RDFLIB_SYNT,
            ttl=ttl_current,
            validation_report={"rdflib_errors": pre_rdf["errors"]},
            phase=phase,
            suffix="syntax",
            artifact_dir=out_dir,
        )

        post_rdf = _run_rdf_syntax_check(ttl_current)
        print(f" rdflib apres réparation : valid={post_rdf['valid']} | erreurs={len(post_rdf['errors'])}")

        if not post_rdf["valid"]:
            print("(agent_validateur_syntax_phases.py) syntaxe non réparée → fallback ttl_initial (ttl_after_semantic)")
            ttl_current = ttl_initial
            post_rdf = _run_rdf_syntax_check(ttl_current)
            print(f"rdflib apres fallback ttl_initial : valid={post_rdf['valid']} | erreurs={len(post_rdf['errors'])}")
            if ps:
                ps.record_ttl_syntax_rollback(
                    iteration=iteration,
                    phase=phase,
                    reason="rdflib_repair_failed_restore_ttl_initial",
                )
    else:
        post_rdf = pre_rdf

    # 3 HermiT + OOPS
    hermit_oops_pre = _run_hermit_oops_checks(ttl_current) if post_rdf["valid"] else {
        "valid" : False,
        "hermit_errors": [],
        "hermit_warnings": [],
        "oops_critical": [],
        "oops_warnings": [],
        "oops_error" : None,
        "warnings": ["HermiT et OOPS non lancés car syntax rdf non valide"]}

    print(
        f" HermiT/OOPS avant : valid={hermit_oops_pre['valid']} | "
        f"hermit={len(hermit_oops_pre['hermit_errors'])} | "
        f"oops={len(hermit_oops_pre['oops_critical'])}"
    )

    # 3 Hermit OOPs error reppair 
    hermit_oops_post = hermit_oops_pre
    oops_transport_only = (
        hermit_oops_pre.get("oops_unavailable")
        and not hermit_oops_pre["hermit_errors"]
        and not hermit_oops_pre["oops_critical"]
    )
    if oops_transport_only:
        print(
            "(agent_validateur_syntax_phases.py) OOPS injoignable seul — "
            "pas de correction LLM HermiT/OOPS"
        )
    elif post_rdf["valid"] and not hermit_oops_pre["valid"]:
        semantic_fix_applied = True
        print("(agent_validateur_syntax_phases.py) erreurs HermiT/OOPS --> correction via LLM")

        semantic_validation_report = {
            "hermit_errors": hermit_oops_pre["hermit_errors"],
            "oops_critical": [{"code": p.get("code"), "name": p.get("name"),"affected": p.get("affected", [])}
                for p in hermit_oops_pre["oops_critical"]],
            "oops_error": hermit_oops_pre["oops_error"],
        }

        semantic_raw_output, _ = _repair_with_llm(
            system_prompt=SYSTEM_AGENT_VALIDATOR_HERMIT_OOPS,
            ttl=ttl_current,
            validation_report=semantic_validation_report,
            phase=phase,
            suffix="semantic",
            artifact_dir=out_dir,
        )

        corrections_patch = extract_corrections_patch_block(semantic_raw_output)

        if corrections_patch and corrections_patch.strip():
            print("(agent_validateur_syntax_phases.py) Patch HermiT/OOPS reçu --> merge_patch()")
            ttl_candidate  = merge_patch(ttl_current, corrections_patch)
            syntax_check   = _run_rdf_syntax_check(ttl_candidate)
            if syntax_check["valid"]:
                ttl_current = ttl_candidate
                print("(agent_validateur_syntax_phases.py) patch appliqué ")
            else:
                print(" Patch invalide syntaxiquement --> rollback ontologie avant patch")
        elif corrections_patch == "":
            print("(agent_validateur_syntax_phases.py) Patch vide --> pas de correction")
        else:
            print("<corrections_patch> absent --> ontologie same")

        hermit_oops_post = _run_hermit_oops_checks(ttl_current)
        print(
            f" HermiT/OOPS apres repair : valid={hermit_oops_post['valid']} | "
            f"hermit={len(hermit_oops_post['hermit_errors'])} | "
            f"oops={len(hermit_oops_post['oops_critical'])}"
        )

    ################################       comparaison pre post #######################
    compare_report = None
    if compare_artifact_dir_slug:
        prev_report_path = get_agent_dir(iteration, compare_artifact_dir_slug) / "syntax_validation_report.json"
        prev_report = _json_load(prev_report_path)
        prev_errors = prev_report.get("post", {}).get("current_errors", []) if prev_report else []
        compare_report = _compare_error_lists(prev_errors, hermit_oops_post["current_errors"])
        print(
            f" Comparaison avec dossier {compare_artifact_dir_slug} : "
            f"résolues={len(compare_report['resolved_errors'])} | "
            f"total={compare_report['current_count']}"
        )

    ############################ saving ######################
    out_path = out_dir / ttl_fname
    out_path.write_text(ttl_current, encoding="utf-8")
    print(f"(agent_validateur_syntax_phases.py) Ontologie sauvegardée : {out_path}")

    _write_toolchain_diagnostic_files(out_dir, pre_rdf, post_rdf, hermit_oops_pre, hermit_oops_post)

    # repport json
    final_valid = post_rdf["valid"] and hermit_oops_post["valid"]
    final_current_errors = _collect_error_signatures(
        rdflib_errors=post_rdf["errors"],
        hermit_errors=hermit_oops_post["hermit_errors"],
        oops_critical=hermit_oops_post["oops_critical"],
        oops_error=hermit_oops_post["oops_error"],
    )

    report = {
        "phase" : phase,
        "ttl_key" : ttl_key,
        "iteration": iteration,
        "syntax_fix_applied" : syntax_fix_applied,
        "semantic_fix_applied": semantic_fix_applied,
        "pre": {
            "rdflib_valid": pre_rdf["valid"],
            "rdflib_errors": pre_rdf["errors"],
            "hermit_valid" : hermit_oops_pre["valid"],
            "hermit_errors": hermit_oops_pre["hermit_errors"],
            "oops_critical": [_format_oops_issue(p) for p in hermit_oops_pre["oops_critical"]],
            "oops_error" : hermit_oops_pre["oops_error"],
        },
        "post": {
            "valid"  : final_valid,
            "rdflib_valid" : post_rdf["valid"],
            "rdflib_errors" : post_rdf["errors"],
            "hermit_errors": hermit_oops_post["hermit_errors"],
            "oops_critical" : [_format_oops_issue(p) for p in hermit_oops_post["oops_critical"]],
            "oops_warnings" : [_format_oops_issue(p) for p in hermit_oops_post.get("oops_warnings", [])],
            "oops_error" : hermit_oops_post["oops_error"],
            "warnings" : hermit_oops_post["warnings"],
            "current_errors": final_current_errors,
        },
        "comparison": compare_report,
        "output_files": {
            "ttl": str(out_path),
            "syntax_validation_report": str(out_dir / report_fname),
            "rdflib_dir": str(out_dir / "rdflib"),
            "hermit_dir": str(out_dir / "hermit"),
            "oops_dir": str(out_dir / "oops"),
            "raw_syntax_llm": str(out_dir / f"repair_syntax_{phase_slug}_raw.txt") if syntax_raw_output else None,
            "raw_semantic_llm": str(out_dir / f"repair_semantic_{phase_slug}_raw.txt") if semantic_raw_output else None,
        },
    }

    report_path = out_dir / report_fname
    _json_dump(report_path, report)
    print(f"(agent_validateur_syntax_phases.py) rapport saving : {report_path}")

    #  Stats 
    if stats_phase_key:
        pipeline_stats = get_stats()
        if pipeline_stats:
            pipeline_stats.record_syntax_check(
                phase=stats_phase_key,
                rdflib_errors_pre=pre_rdf["errors"],
                rdflib_errors_post=post_rdf["errors"],
                hermit_errors_pre=hermit_oops_pre["hermit_errors"],
                hermit_errors_post=hermit_oops_post["hermit_errors"],
                oops_critical_pre=hermit_oops_pre["oops_critical"],
                oops_critical_post=hermit_oops_post["oops_critical"],
                warnings=[],
                llm_called=(syntax_fix_applied or semantic_fix_applied),
                llm_called_rdflib=syntax_fix_applied,
                llm_called_hermit=semantic_fix_applied,
            )

    # state
    syn_ttl_key = output_syntax_ttl_key or "ttl_after_syntax"
    out = {
        **base_state(state),
        ttl_key: ttl_current,
        status_key: final_valid,
        "syntax_errors": post_rdf["errors"],
        "semantic_errors": (
            hermit_oops_post["hermit_errors"]
            + [_format_oops_issue(p) for p in hermit_oops_post["oops_critical"]]
        ),
        "syntax_warnings": hermit_oops_post["warnings"],
        "messages": [
            AIMessage(content=f"[{phase}] valid={final_valid} | errors={len(final_current_errors)}")
        ],
    }
    out[syn_ttl_key] = ttl_current
    out["ttl_after_syntax"] = ttl_current
    return out



#####################################################################################################################
def _passthrough_toolchain(
    state,
    ttl_key: str,
    status_key: str,
    output_syntax_ttl_key: str,
    phase: str,
    reason: str,
    fallback_ttl: str = "",
):
    """Skip rdflib/HermiT/OOPS : couche déjà validée / TTL inchangé."""
    ttl = state.get(ttl_key) or fallback_ttl or state.get("ontology_ttl") or ""
    print(f"(agent_validateur_syntax_phases.py) SKIP {phase} — {reason}")
    out = {
        **base_state(state),
        status_key: True,
        "messages": [AIMessage(content=f"[{phase}] skipped: {reason}")],
    }
    out[output_syntax_ttl_key] = ttl
    out["ttl_after_syntax"] = ttl
    # conserver ttl_key tel quel
    if ttl_key not in out or not out.get(ttl_key):
        out[ttl_key] = ttl
    return out



# sauter le noeud sujet 
def validate_subject_validation_chain(state):
    ttl = state.get("ttl_after_subject_semantic") or state["ontology_ttl"]
    base = state["ontology_ttl"]

    if state.get("subject_toolchain_needed") is False or ttl == base:
        return _passthrough_toolchain(
            state,
            ttl_key="ttl_after_subject_semantic",
            status_key="subject_syntax_ok_after_semantic",
            output_syntax_ttl_key="ttl_after_subject_validation",
            phase="subject_chain_rdf_hermit_oops",
            reason="sujet déjà validé / TTL inchangé",
            fallback_ttl=base,
        )
    return validate_syntax(
        state=state,
        ttl_key="ttl_after_subject_semantic",
        phase="subject_chain_rdf_hermit_oops",
        status_key="subject_syntax_ok_after_semantic",
        stats_phase_key="syntax_check_subject_chain",
        output_syntax_ttl_key="ttl_after_subject_validation",
        artifact_dir_slug="subject_toolchain",
    )


def validate_predicate_validation_chain(state) :
    ttl = state.get("ttl_after_predicate_semantic") or state.get("ontology_ttl") or ""
    base = (
        state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("enriched_ttl_subjects")
        or state["ontology_ttl"]
    )
    if state.get("predicate_toolchain_needed") is False or ttl == base:
        return _passthrough_toolchain(
            state,
            ttl_key="ttl_after_predicate_semantic",
            status_key="predicate_syntax_ok_after_semantic",
            output_syntax_ttl_key="ttl_after_predicate_validation",
            phase="predicate_chain_rdf_hermit_oops",
            reason="prédicat déjà validé / TTL inchangé",
            fallback_ttl=base,
        )
    return validate_syntax(
        state=state,
        ttl_key="ttl_after_predicate_semantic",
        phase="predicate_chain_rdf_hermit_oops",
        status_key="predicate_syntax_ok_after_semantic",
        stats_phase_key="syntax_check_predicate_chain",
        output_syntax_ttl_key="ttl_after_predicate_validation",
        artifact_dir_slug="predicate_toolchain",
        compare_artifact_dir_slug="subject_toolchain",
    )


def validate_object_validation_chain(state):
    ttl = state.get("ttl_after_object_semantic") or state.get("ontology_ttl") or ""
    base = (
        state.get("ttl_after_predicate_validation")
        or state.get("ttl_after_predicate_semantic")
        or state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("enriched_ttl_subjects")
        or state["ontology_ttl"]
    )
    if state.get("object_toolchain_needed") is False or ttl == base:
        return _passthrough_toolchain(
            state,
            ttl_key="ttl_after_object_semantic",
            status_key="object_syntax_ok_after_semantic",
            output_syntax_ttl_key="ttl_after_object_validation",
            phase="object_chain_rdf_hermit_oops",
            reason="objet déjà validé / TTL inchangé",
            fallback_ttl=base,
        )
    updated = validate_syntax(
        state=state,
        ttl_key="ttl_after_object_semantic",
        phase="object_chain_rdf_hermit_oops",
        status_key="object_syntax_ok_after_semantic",
        stats_phase_key="syntax_check_object_chain",
        output_syntax_ttl_key="ttl_after_object_validation",
        artifact_dir_slug="object_toolchain",
        compare_artifact_dir_slug="predicate_toolchain",
    )
    pipeline_stats = get_stats()
    if pipeline_stats:
        pipeline_stats.check_syntax_loop()
    return updated



