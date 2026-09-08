
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from state import PipelineState
from utils.embedding import node_embedding_selection

from agents.agent_enrich_subjects import run_agent_enrich_subjects
from agents.agent_validator_semant_subjects import validate_semantics_subjects

from agents.agent_enrich_predicates import run_enrich_predicates
from agents.agent_validator_semant_predicates import validate_semantics_predicates

from agents.agent_enrich_objects import run_enrich_objects
from agents.agent_validator_semant_objects import validate_semant_objects

from agents.agent_manager import run_manager

from agents.agent_validateur_syntax_phases import (
    validate_subject_validation_chain, # before and after semantic validation
    validate_predicate_validation_chain, 
    validate_object_validation_chain, 
)

def _should_continue(state):
    if state.get("stable", False):
        print("\n (graph.py) stable ontology --> end of pipeline")
        return "end"
    if not state.get("triplets_json"):
        print("\n(graph.py) all triples treated --> end of pipeline")
        return "end"
    return "continue"


def build_graph(with_memory = True):
    graph = StateGraph(PipelineState)
    # nodes
    graph.add_node("embedding_selection", node_embedding_selection)
    graph.add_node("agent_enrich_subjects", run_agent_enrich_subjects)
    graph.add_node("semant_subjects", validate_semantics_subjects)
    graph.add_node("subject_toolchain", validate_subject_validation_chain)
    graph.add_node("enrich_predicates", run_enrich_predicates)
    graph.add_node("semant_predicates", validate_semantics_predicates)
    graph.add_node("predicate_toolchain", validate_predicate_validation_chain)
    graph.add_node("enrich_objects", run_enrich_objects)
    graph.add_node("semant_objects", validate_semant_objects)
    graph.add_node("object_toolchain", validate_object_validation_chain)
    graph.add_node("manager", run_manager)

    # EDGES
    graph.set_entry_point("embedding_selection")
    graph.add_edge("embedding_selection",  "agent_enrich_subjects")
    graph.add_edge("agent_enrich_subjects", "semant_subjects")
    graph.add_edge("semant_subjects", "subject_toolchain")
    graph.add_edge("subject_toolchain", "enrich_predicates")
    graph.add_edge("enrich_predicates", "semant_predicates")
    graph.add_edge("semant_predicates", "predicate_toolchain")
    graph.add_edge("predicate_toolchain", "enrich_objects")
    graph.add_edge("enrich_objects", "semant_objects")
    graph.add_edge("semant_objects", "object_toolchain")
    graph.add_edge("object_toolchain", "manager")


    graph.add_conditional_edges(
        "manager",
        _should_continue,
        {
            "continue": "embedding_selection",
            "end" : END,
        },
    )
    if with_memory:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
    else:
        checkpointer = None
    return graph.compile(checkpointer=checkpointer)




