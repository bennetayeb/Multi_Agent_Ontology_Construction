import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()
BASE_DIR = Path(__file__).parent

PIPELINE_STATS_ENABLED = os.getenv("PIPELINE_STATS", "true").lower() not in (
    "0",
    "false",
    "no",
    "off",
)


DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
MODEL_NAME = "deepseek-v4-pro"
DEEPSEEK_PRICE_INPUT_HIT_PER_1M = float(os.environ.get("DEEPSEEK_PRICE_INPUT_HIT_PER_1M", "0.003625"))
DEEPSEEK_PRICE_INPUT_MISS_PER_1M = float(os.environ.get("DEEPSEEK_PRICE_INPUT_MISS_PER_1M", "0.435"))
DEEPSEEK_PRICE_OUTPUT_PER_1M = float(os.environ.get("DEEPSEEK_PRICE_OUTPUT_PER_1M", "0.87"))



########## begin : à le simplifier plus tard 

LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"
LANGCHAIN_API_KEY = os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY")
LANGCHAIN_PROJECT = (
    os.environ.get("LANGCHAIN_PROJECT")
    or os.environ.get("LANGSMITH_PROJECT")
    or "multi-agents-plu"
)
if LANGCHAIN_TRACING_V2 and not os.environ.get("LANGCHAIN_PROJECT"):
    os.environ["LANGCHAIN_PROJECT"] = LANGCHAIN_PROJECT


def langsmith_tracing_enabled():
    return LANGCHAIN_TRACING_V2 and bool(LANGCHAIN_API_KEY)
########## end : à le simplifier plus tard 

EMBED_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
TOP_K_CANDIDATES = 7
MAX_CANDIDATES_PER_ITERATION = 7

#######################
# datasets without domain ontology 
TRIPLETS_JSON = BASE_DIR / "datasets" / "triples_last_id.json"
EXTRACTED_TRIPLES_DIR = BASE_DIR.parent / "Outputs" / "extracted_triples"
SEED_MINIMAL_TTL = BASE_DIR / "datasets" / "seed_minimal.ttl"

# domain ontology construction from base ontology 
# table zones/secteurs LUP 
PLU_ONTOLOGY_SEED_TTL = BASE_DIR / "datasets" / "plui_ontologie_v3.ttl"
PLU_ABBREV_DOCX = BASE_DIR / "datasets" / "M2appro_RL.docx"
RUNS_DIR = BASE_DIR / "datasets" / "runs"


# HermiT.jar 
JAVA_EXE = r"C:\Program Files\Java\jdk-26\bin\java.exe" 
HERMIT_JAR = str(BASE_DIR / "HermiT.jar")


##################
_RUN_ID_FILE = BASE_DIR / ".last_run_id"

RUN_ID = None
ONTOLOGY_DIR = None


def peek_last_run_id():
    if not _RUN_ID_FILE.exists():
        return None
    rid = _RUN_ID_FILE.read_text(encoding="utf-8").strip()
    return rid or None


def get_ontology_dir():
    if ONTOLOGY_DIR is None:
        raise RuntimeError("(config.py) pas de RUN_ID")
    return ONTOLOGY_DIR


def resolve_triplets_json(doc_id):
    if doc_id:
        triples = RUNS_DIR / doc_id / "triples.json"
        if triples.exists():
            return triples
        alt = RUNS_DIR / doc_id / "triplets.json"
        if alt.exists():
            return alt
        raise FileNotFoundError(
            f"(config.py) --doc-id={doc_id} : ni {triples} ni {alt}"
        )
    return TRIPLETS_JSON


def resolve_seed_ttl(doc_id):
    if doc_id:
        run_seed = RUNS_DIR / doc_id / "seed.ttl"
        if run_seed.exists():
            return run_seed
        print(
            f"(config.py) --doc-id={doc_id} : pas de seed.ttl -> {SEED_MINIMAL_TTL.name}"
        )
        return SEED_MINIMAL_TTL
    return PLU_ONTOLOGY_SEED_TTL


def init_run(*, resume, doc_id):
    global RUN_ID, ONTOLOGY_DIR
    if doc_id:
        rid = doc_id.strip()
        print(f"(config.py) RUN_ID = doc-id : {rid}")
        _RUN_ID_FILE.write_text(rid, encoding="utf-8")
        RUN_ID = rid
        ONTOLOGY_DIR = BASE_DIR / "ontology_versions" / RUN_ID
        ONTOLOGY_DIR.mkdir(parents=True, exist_ok=True)
        return RUN_ID
    if resume:
        rid = peek_last_run_id()
        if rid:
            print(f"(config.py) mode --resume --> RUN_ID reused : {rid}")
            RUN_ID = rid
            ONTOLOGY_DIR = BASE_DIR / "ontology_versions" / RUN_ID
            ONTOLOGY_DIR.mkdir(parents=True, exist_ok=True)
            return RUN_ID
        print("(config.py) --resume demandé mais .last_run_id absent -> nouveau RUN_ID")
    rid = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RUN_ID_FILE.write_text(rid, encoding="utf-8")
    print(f"(config.py) new RUN_ID : {rid}")
    RUN_ID = rid
    ONTOLOGY_DIR = BASE_DIR / "ontology_versions" / RUN_ID
    ONTOLOGY_DIR.mkdir(parents=True, exist_ok=True)
    return RUN_ID


def get_iteration_dir(iteration):
    d = get_ontology_dir() / f"iter_{iteration:03d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_agent_dir(iteration, agent_slug):
    d = get_iteration_dir(iteration) / agent_slug
    d.mkdir(parents=True, exist_ok=True)
    return d
