import operator
from typing import Annotated, NotRequired, TypedDict
from langchain_core.messages import BaseMessage



class PipelineState(TypedDict):

    ######### pipeline triples management 
    triplets_json: list[dict] # liste triples to treat
    max_iterations: int # = len(triples)
    top_candidates: list[dict] 

    valid_assertions: list[dict] # integrated triples
    rejected_triplets: list[dict] # rejets de l'itération, le pool  via triplet_layers

    # suivi indépendant des S/P/O : id -> {subject, predicate, object: bool}
    triplet_layers: NotRequired[dict[str, dict[str, bool]]]

    ####### versionning ontology 
    ontology_ttl: str  # current version TTL
    ontology_version: int # = itération 

    ######## versions TTL pipeline in one iteration 
    enriched_ttl_subjects: str  # TTL after enrichment subject --> by: agent_enrich_subjects.py
    ttl_after_subject_semantic: str # TTL after subject semantic validation --> agent_validator_semant_subjects.py
    ttl_after_subject_validation: str  # TTL after subject toolchain : rdflib + HermiT + OOPS --> agent_validateur_syntax_phases.py --> validate_subject_validation_chain

    ttl_after_predicate_semantic: str  
    ttl_after_predicate_validation: str  

    ttl_after_object_semantic: str 
    ttl_after_object_validation: str

    # TTL après enrichissement prédicat / objet (étapes séparées, comme enriched_ttl_subjects)
    enriched_ttl_predicates: str
    enriched_ttl_objects: str
    ttl_after_syntax: str # Last TTL validated (miroir dernier toolchain)

    # triplets validés sémantiquement côté sujets (référence en cas de rollback parse)
    subject_assertions_semantic_ok: list[dict]
    # True si enrich_subjects n'a pas pu parser les candidats -> skip semant
    enrich_subjects_parse_failed: NotRequired[bool]
    enrich_predicates_parse_failed: NotRequired[bool]
    enrich_objects_parse_failed: NotRequired[bool]

    # Toolchain S/P/O : False = skip (couche déjà validée / TTL inchangé)
    subject_toolchain_needed: NotRequired[bool]
    predicate_toolchain_needed: NotRequired[bool]
    object_toolchain_needed: NotRequired[bool]



    ########### toolchain syntax validation
    subject_syntax_ok_after_semantic: bool 
    predicate_syntax_ok_after_semantic: bool  
    object_syntax_ok_after_semantic: bool

    ################# Résultats des validations sémantiques
    validation_subject_ok: bool # avant enrichissement des prédicats
    validation_predicate_ok : bool # avant enrichissement des objet
    validation_full_ok: bool # true triplet est validé apres s prédicat objet 

    ################################# erreur rdflib hermit oops
    syntax_errors: list[str] # erreurs de syntaxe détectées
    syntax_warnings: list[str] # warnings syntaxiques non bloquants
    semantic_errors: list[str] # erreurs de cohérence sémantique

    ################# arret pipline 
    iteration_count: int 
    stable: bool  # false 
    triplet_penalty_expiry: NotRequired[dict[str, int]]  # vide (pénalité non)

    ### logs 
    messages: Annotated[list[BaseMessage], operator.add] # messages LangChain
    ontology_history: Annotated[list[dict], operator.add]  # version ontology


REDUCER_KEYS = ("messages", "ontology_history")


def base_state(state):
    return {k: v for k, v in state.items() if k not in REDUCER_KEYS}