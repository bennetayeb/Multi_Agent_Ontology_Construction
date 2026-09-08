import argparse
import ctypes
import json
import platform
import sys
import time
from pathlib import Path

from config import (
    LANGCHAIN_PROJECT,
    LANGCHAIN_TRACING_V2,
    PIPELINE_STATS_ENABLED,
    PLU_ONTOLOGY_SEED_TTL,
    TRIPLETS_JSON,
    init_run,
    langsmith_tracing_enabled,
    resolve_seed_ttl,
    resolve_triplets_json,
)
import config
from graph import build_graph
from state import PipelineState

import run_context
from utils.checkpoint_io import load_checkpoint, last_saved_ttl, save_checkpoint, is_post_manager_chunk
from utils.ontology_count import count_ontology_entities


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _keep_awake():
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
        print("(main.py) anti veille on")
    except Exception as exc:
        print(f"(main.py) anti veille: {exc}")


def _release_awake():
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass


def _load_triplets_and_ontology(path_json, path_ttl):
    data = json.loads(path_json.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        triplets = data.get("triplets") or data.get("triples") or data.get("candidates")
        if not isinstance(triplets, list):
            raise ValueError(
                f"JSON triplets invalide dans {path_json}: "
                "attendu clé 'triplets'/'triples' (liste) "
                f"(clés={list(data.keys())[:8]})"
            )
    elif isinstance(data, list):
        triplets = data
    else:
        raise ValueError(f"JSON triplets invalide: type={type(data)}")
    return triplets, path_ttl.read_text(encoding="utf-8")



def run_pipeline(
    triplets_json,
    ontology_ttl,
    thread_id="pipeline-001",
    resume=False,
    doc_id=None,
    stats_enabled=None):

    _keep_awake()
    t0 = time.perf_counter()
    try:
        run_id = init_run(resume=resume, doc_id=doc_id)
        pj, po = Path(triplets_json), Path(ontology_ttl)
        triplets_all, ontology_str = _load_triplets_and_ontology(pj, po)
        n_total = len(triplets_all)
        triplets, ontology = triplets_all, ontology_str
        ckpt = load_checkpoint(thread_id) if resume else None
        if ckpt:
            print(
                f"(main.py) reprise run={run_id} | "
                f"pool_restant={len(ckpt.get('triplets_json') or [])} | "
                f"iter_count={ckpt.get('iteration_count')} "
                f"(prochains artefacts -> iter_{ckpt.get('iteration_count', 0):03d})"
            )
            triplets = ckpt["triplets_json"]
            ontology = ckpt["ontology_ttl"] or last_saved_ttl(ckpt.get("iteration_count", 0)) or ontology_str

        RUN_DIR = config.ONTOLOGY_DIR
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        use_stats = PIPELINE_STATS_ENABLED if stats_enabled is None else stats_enabled
        stats_path = RUN_DIR / "pipeline_stats.json"
        stats = run_context.init_stats(
            enabled=use_stats,
            output_dir=RUN_DIR,
            total_triplets=n_total,
            resume_path=stats_path if (use_stats and resume and ckpt) else None,
        )
        run_context.set_stats(stats)
        if use_stats:
            print("(main.py) pipeline stats ON")
        else:
            print("(main.py) pipeline stats OFF (--no-stats ou PIPELINE_STATS=false)")

        base = count_ontology_entities(ontology)
        if stats and (not resume or not ckpt):
            stats.record_ontology_base(**base)

        cp = ckpt or {}

        # initialisation state
        state: PipelineState = {
            "triplets_json": triplets,
            "max_iterations": n_total,
            "rejected_triplets": cp.get("rejected_triplets", []),
            "ontology_ttl": ontology,
            "ontology_version": cp.get("ontology_version", 1),
            "iteration_count": cp.get("iteration_count", 0),
            "stable": cp.get("stable", False),
            "top_candidates": [],

            "ttl_after_syntax": "",
            "enriched_ttl_subjects": "",
            "enriched_ttl_predicates": "",
            "enriched_ttl_objects": "",

            "ttl_after_subject_semantic": "",
            "ttl_after_subject_validation": "",
            "ttl_after_predicate_semantic": "",
            "ttl_after_predicate_validation": "",
            "ttl_after_object_semantic": "",
            "ttl_after_object_validation": "",
            # toolchain syntax validation
            "subject_syntax_ok_after_semantic": False,
            "predicate_syntax_ok_after_semantic": False,
            "object_syntax_ok_after_semantic": False,

            "valid_assertions": [],
            "subject_assertions_semantic_ok": [],
            "enrich_subjects_parse_failed": False,
            "subject_toolchain_needed": True,
            "predicate_toolchain_needed": True,
            "object_toolchain_needed": True,

            "validation_subject_ok": False,
            "validation_predicate_ok": False,
            "validation_full_ok": False,
            "syntax_errors": [],
            "syntax_warnings": [],
            "semantic_errors": [],
            "messages": [],
            "ontology_history": [],
            "triplet_penalty_expiry": cp.get("triplet_penalty_expiry", {}),
            # vide au démarrage -> toutes couches False ; restauré si --resume
            "triplet_layers": cp.get("triplet_layers", {}),
        }

        graph = build_graph(with_memory=True)
        run_name = f"pipeline-{run_id}"
        cfg = {
            "configurable": {"thread_id": thread_id},
            "run_name": run_name,
            "tags": ["multi-agents", "plu", f"thread:{thread_id}"],
            "metadata": {
                "run_id": run_id,
                "thread_id": thread_id,
                "n_triplets": n_total,
                "triplets_json": pj.name,
                "ontology_seed": po.name,
                "resume": resume,
            },
        }
        if langsmith_tracing_enabled():
            print(f"(main.py) LangSmith ON -> project={LANGCHAIN_PROJECT} run={run_name}")
            print(
                "(main.py) trace sur https://smith.langchain.com ",
                flush=True,
            )
        elif LANGCHAIN_TRACING_V2:
            print("(main.py) LangSmith: pas LANGCHAIN_API_KEY ")
        prev_it, saved_it = state["iteration_count"], state["iteration_count"]
        final = state
        for chunk in graph.stream(state, config=cfg, stream_mode="values"):
            final = chunk
            cit = chunk.get("iteration_count", 0)
            if is_post_manager_chunk(chunk, prev_it) and cit > saved_it:
                save_checkpoint(chunk, thread_id)
                saved_it = cit
                prev_it = cit

        out = RUN_DIR / "ontology_FINAL.ttl"
        ttl = (
            final.get("ttl_after_object_validation")
            or final.get("ttl_after_object_semantic")
            #or final.get("enriched_ttl_objects")
            or final.get("ttl_after_predicate_validation")
            or final.get("ttl_after_predicate_semantic")
            #or final.get("enriched_ttl_predicates")
            or final.get("ttl_after_subject_validation")
            or final.get("ttl_after_subject_semantic")
            #or final.get("enriched_ttl_subjects")
            or final["ontology_ttl"]
        )
        out.write_text(ttl, encoding="utf-8")
        wall_s = round(time.perf_counter() - t0, 2)

        print("###########################")
        if stats:
            stats.record_ontology_final(**count_ontology_entities(ttl))
            stats.set_wall_time_seconds(wall_s)
            stats._save()

            summ = stats._compute_summary()
            print(f"Iterations: {final['iteration_count']} | stop: {stats.stop_reason}")
            print(
                f"IR définitif (sujet∩prédicat∩objet): {summ.get('N_fully_integrated')}/{n_total} "
                f"({summ.get('integration_rate_pct')}%) | RR: {summ.get('rejection_rate_pct')}%"
            )
            print(
                f"IR par couche %: sujet={summ.get('IR_subject_pct')} | "
                f"prédicat={summ.get('IR_predicate_pct')} | objet={summ.get('IR_object_pct')}"
            )
            print(
                f"Corrections sémantiques: itérations sujet={summ.get('sem_corrections_subjects')} | "
                f"P&O={summ.get('semantic_correction_iterations_po')} | "
                f"triplets corrigés (SCR)={summ.get('SCR_triplet_pct')}%"
            )
            wall = summ.get("wall_time_seconds")
            print(
                f"Coût USD: {summ.get('total_cost_usd')} | temps: {wall}s"
                + (f" ({round(wall / 60, 2)} min)" if wall else "")
            )
            print(f"tentatives enrich sujet (cumul): {summ['total_integrated']}")
            print(f"pool restant final: {summ.get('final_remaining_pool')}")
            print(f"stratégies (%): {summ.get('strategy_distribution_pct', {})}")
            pt = stats.compute_paper_table()
            print(
                f"paper_table -> IR={pt.get('IR_pct')}% | coût={pt.get('total_cost_usd')} USD | "
                f"temps={pt.get('wall_time_minutes')} min"
            )
            print(f"Ontologie: {out}")
            print(f"Stats: {stats_path}")
            print("Audit itérations:")
            for it in stats.iterations:
                it_id = it.get("iteration")
                manager = it.get("manager_decision", {})
                m_continue = manager.get("continue")
                m_reason = manager.get("reason", "")
                rejected_it = manager.get("rejected_this_iteration")
                remaining_pool = manager.get("remaining_pool")
                print(
                    f"iter={it_id} | continue={m_continue} | rejected={rejected_it} | "
                    f"remaining={remaining_pool} | reason={m_reason}"
                )
        else:
            final_counts = count_ontology_entities(ttl)
            print(f"Iterations: {final['iteration_count']} | temps: {wall_s}s ({round(wall_s / 60, 2)} min)")
            print(
                f"Ontologie finale: {final_counts.get('classes', 0)} classes, "
                f"{final_counts.get('individuals', 0)} individus"
            )
            print(f"Ontologie: {out}")
        print("###########################")
        return final
    finally:
        _release_awake()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--triplets",
        default=None,
        help=f"JSON des triplets (défaut: {TRIPLETS_JSON} ou datasets/runs/<doc-id>/triples.json)",
    )
    ap.add_argument(
        "--ontology",
        default=None,
        help=f"TTL seed (défaut: {PLU_ONTOLOGY_SEED_TTL} ou datasets/runs/<doc-id>/seed.ttl)",
    )
    ap.add_argument(
        "--doc-id",
        default=None,
        help="Identifiant d'expérience : triplets/seed dans datasets/runs/<id>/, artefacts dans ontology_versions/<id>/",
    )
    ap.add_argument("--thread", default="pipeline-001")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument(
        "--no-stats",
        action="store_true",
        help="Désactive pipeline_stats.json et le bilan métriques (équivalent PIPELINE_STATS=false)",
    )
    a = ap.parse_args()
    triplets = a.triplets or str(resolve_triplets_json(a.doc_id))
    ontology = a.ontology or str(resolve_seed_ttl(a.doc_id))
    print(f"(main.py) triplets={triplets}")
    print(f"(main.py) ontology={ontology}")
    stats_on = PIPELINE_STATS_ENABLED and not a.no_stats
    run_pipeline(triplets, ontology, a.thread, a.resume, doc_id=a.doc_id, stats_enabled=stats_on)


if __name__ == "__main__":
    main()