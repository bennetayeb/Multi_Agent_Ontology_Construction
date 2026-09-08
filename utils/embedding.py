
import hashlib
import pickle
import re
import unicodedata
from pathlib import Path
from typing import Optional, Tuple
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from config import (
    EMBED_MODEL_NAME,
    MAX_CANDIDATES_PER_ITERATION,
    PLU_ABBREV_DOCX,
    TOP_K_CANDIDATES,
)
from run_context import get_stats
from state import base_state
from tools.rdf_tools import get_ontology_concepts

_MATCH_PRIORITY = {
    "string_exact": 0,
    "camel_exact": 0,
    "stripped_embedding": 1,
    "full_embedding": 2,
}


class PluHybridEmbeddingSelector:

    PREFIXES_TO_STRIP = frozenset({"zone", "secteur", "indice"})

    def __init__(self, embedding_model = EMBED_MODEL_NAME, output_dir= "embedding_cache", abbrev_docx = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings_cache = self.output_dir / "embeddings_cache.pkl"

        print(f"(embedding.py) embedding model : {embedding_model}")
        self.model = SentenceTransformer(embedding_model)

        self.abbrev_set = set()
        self.abbrev_docx = Path(abbrev_docx) if abbrev_docx else None
        if self.abbrev_docx and self.abbrev_docx.exists():
            try:
                self.abbrev_set = self._load_abbrev_from_docx(self.abbrev_docx)
                print(f"(embedding.py) {len(self.abbrev_set)} abéviation PLUi ")
            except Exception as exc:
                print(f"(embedding.py) pas dabéviation : {exc}")
        elif self.abbrev_docx:
            print(f"(embedding.py) fichier absent dabéviation: {self.abbrev_docx}")

    @staticmethod
    def _strip_accents(s):
        s = unicodedata.normalize("NFKD", s)
        return "".join(c for c in s if not unicodedata.combining(c))

    def normalize_code(self, s: str | None) -> str:
        if not s:
            return ""
        s = str(s).lower().strip()
        s = s.replace("_", "").replace("-", "").replace(" ", "")
        s = self._strip_accents(s)
        return re.sub(r"[^a-z0-9]+", "", s)

    def normalize_text(self, text):
        if not text or (isinstance(text, float) and pd.isna(text)):
            return ""
        s = str(text).lower().replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", s).strip()

    def extract_prefix_and_remainder(self, text):
        if not text:
            return None, ""
        t = self.normalize_text(text)
        m = re.match(r"^(zone|secteur|indice)[_\s-]+(.+)$", t)
        if m:
            return m.group(1), m.group(2).strip()
        return None, t

    def extract_zone_root(self, zone):
        if not zone:
            return None
        n = self.normalize_code(zone)
        if not n:
            return None
        m = re.match(r"^([a-z]+)\d", n)
        if m:
            return m.group(1)
        m = re.match(r"^\d+([a-z]{2,3})$", n)
        if m:
            return m.group(1)
        if re.match(r"^[a-z]+$", n) and len(n) <= 3:
            return n
        return None

    def _load_abbrev_from_docx(self, docx_path):
        try:
            from docx import Document
        except ImportError as exc:
            raise ImportError(
                "(embedding.py) python-docx requis pour les abréviations PLUi "
                "(pip install python-docx)"
            ) from exc

        doc = Document(str(docx_path))
        chosen_table = None
        for table in doc.tables:
            if not table.rows:
                continue
            headers = [cell.text.strip().lower() for cell in table.rows[0].cells]
            if any("zone" in h for h in headers) and any("secteur" in h for h in headers):
                chosen_table = table
                break
        if chosen_table is None and doc.tables:
            chosen_table = doc.tables[0]
        if chosen_table is None:
            raise ValueError("pas de tableau zone/secteur dans le DOCX")

        tokens: set[str] = set()
        for row in chosen_table.rows[1:]:
            for cell_text in [c.text.strip() for c in row.cells[:2]]:
                if not cell_text or cell_text == "-":
                    continue
                for part in re.split(r"[,;/]\s*", cell_text):
                    part = part.strip()
                    if not part:
                        continue
                    norm = self.normalize_code(part)
                    if norm:
                        tokens.add(norm)
                    root = self.extract_zone_root(part)
                    if root:
                        tokens.add(root)
        return tokens

    @staticmethod
    def _local_name_from_uri(uri: str) -> str:
        if not uri:
            return ""
        if "#" in uri:
            return uri.split("#", 1)[-1]
        return uri.rstrip("/").split("/")[-1]

    def _camel_exact_match(self, subject: str, ent_uri: str, ent_label: str) -> bool:
        trip_n = self.normalize_code(subject)
        if not trip_n:
            return False
        local = self._local_name_from_uri(ent_uri)
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", local or "")
        candidates = {
            self.normalize_code(local),
            self.normalize_code(ent_label),
            self.normalize_code(spaced),
        }
        return trip_n in {c for c in candidates if c}

    def _abbrev_info(self, prefix, remainder, label) -> Tuple[bool, str]:
        rem_norm = self.normalize_code(remainder or "")
        if isinstance(prefix, str) and prefix in self.PREFIXES_TO_STRIP:
            if rem_norm in self.abbrev_set:
                return True, rem_norm
            root = self.extract_zone_root(remainder)
            if root and root in self.abbrev_set:
                return True, rem_norm
        else:
            lbl_norm = self.normalize_code(label or "")
            if lbl_norm in self.abbrev_set:
                return True, lbl_norm
            root = self.extract_zone_root(label)
            if root and root in self.abbrev_set:
                return True, lbl_norm
        return False, ""

    @staticmethod
    def _content_hash(texts) :
        h = hashlib.md5()
        for t in texts:
            h.update(t.encode("utf-8"))
        return h.hexdigest()[:16]

    def _compute_embeddings(self, texts, cache_key_prefix):
        content_hash = self._content_hash(texts)
        cache_key = f"{cache_key_prefix}_{content_hash}"

        cache: dict = {}
        if self.embeddings_cache.exists():
            try:
                with self.embeddings_cache.open("rb") as f:
                    cache = pickle.load(f)
            except Exception:
                cache = {}

        if cache_key in cache:
            print(f"(embedding.py) cache hit : {cache_key}")
            return cache[cache_key]

        print(f"(embedding.py) calcul embeddings {cache_key} ({len(texts)} items)")
        embeddings = self.model.encode(
            texts,
            show_progress_bar=True,
            normalize_embeddings=True,
            batch_size=16,
        )
        cache[cache_key] = embeddings
        with self.embeddings_cache.open("wb") as f:
            pickle.dump(cache, f)
        return embeddings

    def _extract_entities(self, ontology_ttl: str) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["id", "uri", "label", "type"])
        concepts = get_ontology_concepts.invoke({"ttl_content": ontology_ttl})
        if concepts:
            print(f"(embedding.py) premier concept : {concepts[0]}")
        rows = []
        for idx, c in enumerate(concepts or []):
            uri = c.get("uri", "")
            if not uri:
                continue
            entity_id = f"E{idx:04d}"
            etype = c.get("type", "Unknown")
            if c.get("label"):
                rows.append({"id": entity_id, "uri": uri, "label": c["label"], "type": etype})
            for alt in c.get("altLabels", []):
                if alt and alt != c.get("label"):
                    rows.append({"id": entity_id, "uri": uri, "label": alt, "type": etype})

        if not rows:
            print("(embedding.py) 0 labels (ontologie vide ou sans rdfs:label)")
            return empty

        df = pd.DataFrame(rows).drop_duplicates(subset=["uri", "label"]).reset_index(drop=True)
        print(f"(embedding.py) {len(df)} labels pour {df['uri'].nunique()} entités")
        return df

    def _score_pair(self, trip_row, ent_row, sim_full, sim_str, i, j):
        subject_raw = str(trip_row.get("subject", "") or "")
        t_pref = trip_row.get("prefix")
        t_rem = trip_row.get("remainder", "") or ""
        e_pref = ent_row.get("prefix")
        e_rem = ent_row.get("remainder", "") or ""

        t_has_abbrev, t_cmp = self._abbrev_info(t_pref, t_rem, subject_raw)
        e_has_abbrev, e_cmp = self._abbrev_info(e_pref, e_rem, ent_row["label"])

        if t_has_abbrev and e_has_abbrev:
            if self.normalize_code(t_cmp) == self.normalize_code(e_cmp):
                return 1.0, "string_exact", _MATCH_PRIORITY["string_exact"]
            if (
                isinstance(t_pref, str)
                and t_pref in self.PREFIXES_TO_STRIP
                and isinstance(e_pref, str)
                and e_pref in self.PREFIXES_TO_STRIP
            ):
                return float(sim_str[i, j]), "stripped_embedding", _MATCH_PRIORITY["stripped_embedding"]
            return float(sim_full[i, j]), "full_embedding", _MATCH_PRIORITY["full_embedding"]

        if isinstance(t_pref, str) and t_pref in self.PREFIXES_TO_STRIP:
            if isinstance(e_pref, str) and e_pref in self.PREFIXES_TO_STRIP:
                return float(sim_str[i, j]), "stripped_embedding", _MATCH_PRIORITY["stripped_embedding"]
            return float(sim_full[i, j]), "full_embedding", _MATCH_PRIORITY["full_embedding"]

        if self._camel_exact_match(subject_raw, ent_row["uri"], ent_row["label"]):
            return 1.0, "camel_exact", _MATCH_PRIORITY["camel_exact"]
        return float(sim_full[i, j]), "full_embedding", _MATCH_PRIORITY["full_embedding"]

    def _compute_similarity_matrix( self, triplets_df, entities_df):
        triplets_df = triplets_df.copy().reset_index(drop=True)
        entities_df = entities_df.copy().reset_index(drop=True)

        triplets_df["subject_norm"] = triplets_df["subject"].apply(self.normalize_text)
        triplets_df[["prefix", "remainder"]] = triplets_df["subject"].apply(
            lambda s: pd.Series(self.extract_prefix_and_remainder(s)))

        entities_df["label_norm"] = entities_df["label"].apply(self.normalize_text)
        entities_df[["prefix", "remainder"]] = entities_df["label"].apply(
            lambda s: pd.Series(self.extract_prefix_and_remainder(s)))

        entities_df["remainder"] = entities_df.apply(
            lambda r: r["label_norm"]
            if (pd.isna(r["prefix"]) or r["prefix"] is None)
            else r["remainder"],
            axis=1,
        )

        t_full = triplets_df["subject_norm"].fillna("").tolist()
        t_stripped = triplets_df.apply(
            lambda r: r["remainder"]
            if isinstance(r["prefix"], str) and r["prefix"] in self.PREFIXES_TO_STRIP
            else r["subject_norm"],
            axis=1,
        ).tolist()

        e_full = entities_df["label_norm"].fillna("").tolist()
        e_stripped = entities_df.apply(
            lambda r: r["remainder"]
            if isinstance(r["prefix"], str) and r["prefix"] in self.PREFIXES_TO_STRIP
            else r["label_norm"],
            axis=1,
        ).tolist()

        trip_emb_full = self._compute_embeddings(t_full, "triplets_full")
        trip_emb_str = self._compute_embeddings(t_stripped, "triplets_stripped")
        ent_emb_full = self._compute_embeddings(e_full, "entities_full")
        ent_emb_str = self._compute_embeddings(e_stripped, "entities_stripped")

        sim_full = cosine_similarity(trip_emb_full, ent_emb_full)
        sim_str = cosine_similarity(trip_emb_str, ent_emb_str)

        results = []
        for i, trip_row in triplets_df.iterrows():
            for j, ent_row in entities_df.iterrows():
                score, mtype, priority = self._score_pair(
                    trip_row, ent_row, sim_full, sim_str, int(i), int(j)
                )
                results.append({
                    "triplet_id": trip_row["id"],
                    "entity_uri": ent_row["uri"],
                    "entity_label": ent_row["label"],
                    "entity_type": ent_row.get("type", ""),
                    "similarity_score": score,
                    "match_type": mtype,
                    "match_priority": priority,
                })

        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        df = df.sort_values(
            ["triplet_id", "match_priority", "similarity_score"],
            ascending=[True, True, False],
        ).reset_index(drop=True)
        print(f"(embedding.py) {len(df)} paires classée")
        return df

    def select_top_k( self,triplets, ontology_ttl, k=TOP_K_CANDIDATES):
        if not triplets:
            return []

        normalized = []
        for i, t in enumerate(triplets):
            if isinstance(t, dict):
                row = dict(t)
            else:
                row = {"subject": str(t), "predicate": "", "object": ""}
            if "id" not in row:
                row["id"] = f"T{i:06d}"
            normalized.append(row)

        triplets_df = pd.DataFrame(normalized)
        entities_df = self._extract_entities(ontology_ttl)
        if entities_df.empty:
            print("(embedding.py) pas de parsing onto ??? ")
            return [{**t, "top_concepts": []} for t in normalized]

        matches_df = self._compute_similarity_matrix(triplets_df, entities_df)
        top_k_by_triplet: dict[str, list[dict]] = {t["id"]: [] for t in normalized}

        if not matches_df.empty:
            for tid, group in matches_df.groupby("triplet_id", sort=False):
                top_k_by_triplet[str(tid)] = [
                    {
                        "uri": row["entity_uri"],
                        "label": row["entity_label"],
                        "type": row.get("entity_type", ""),
                        "similarity": round(row["similarity_score"], 4),
                        "match_type": row["match_type"],
                        "match_priority": int(row["match_priority"]),
                    }
                    for _, row in group.head(k).iterrows()
                ]

        enriched = [{**t, "top_concepts": top_k_by_triplet.get(t["id"], [])} for t in normalized]
        matched = sum(1 for e in enriched if e["top_concepts"])
        print(f"(embedding.py) {matched}/{len(normalized)} triplets avec top-{k} concepts")
        return enriched


def _resolve_abbrev_docx():
    path = PLU_ABBREV_DOCX
    if path is None:
        return None
    p = Path(path)
    return p if p.exists() else None


_selector = PluHybridEmbeddingSelector(abbrev_docx=_resolve_abbrev_docx())


def resolve_ontology_ttl_for_embedding(state):
    ttl = (
        state.get("ttl_after_object_validation")
        or state.get("ttl_after_object_semantic")
        #or state.get("enriched_ttl_objects")
        or state.get("ttl_after_predicate_validation")
        or state.get("ttl_after_predicate_semantic")
        #or state.get("enriched_ttl_predicates")
        or state.get("ttl_after_subject_validation")
        or state.get("ttl_after_subject_semantic")
        #or state.get("enriched_ttl_subjects")
        or state.get("ontology_ttl")
        or ""
    )
    if not str(ttl).strip():
        raise ValueError("(embedding.py) ontology_ttl vide")
    return str(ttl)


def _best_match(t) :
    concepts = t.get("top_concepts") or []
    return concepts[0] if concepts else {}


def _best_similarity(t) :
    return float(_best_match(t).get("similarity") or 0.0)


def _triplet_rank_key(t):
    c0 = _best_match(t)
    mtype = c0.get("match_type") or "full_embedding"
    pri = c0.get("match_priority")
    if pri is None:
        pri = _MATCH_PRIORITY.get(mtype, 99)
    return (int(pri), -_best_similarity(t))


def rank_triplets_embedding_option_b(enriched,embedding_iteration,triplet_penalty_expiry):
    return sorted(enriched, key=_triplet_rank_key)


def node_embedding_selection(state):
    triplets = state["triplets_json"]
    iteration = state.get("iteration_count", 0)
    onto_ttl = resolve_ontology_ttl_for_embedding(state)

    stats = get_stats()
    if stats:
        stats.start_iteration(iteration=iteration, triplets_sent=len(triplets))

    n_abbrev = len(_selector.abbrev_set)
    if n_abbrev == 0:
        print("(embedding.py) abbrev vide ")
    else:
        sample = sorted(_selector.abbrev_set)[:10]
        print(f"(embedding.py) abbrev_set : {n_abbrev} tokens, exemple {sample}")

    print(
        f"(embedding.py) iter={iteration} | pool={len(triplets)} | "
        f"rank only (hybride) | ontologie={len(onto_ttl)} car."
    )

    all_enriched = _selector.select_top_k(triplets=triplets, ontology_ttl=onto_ttl)
    ranked = rank_triplets_embedding_option_b(
        all_enriched,
        embedding_iteration=iteration,
        triplet_penalty_expiry=state.get("triplet_penalty_expiry"),
    )
    top_candidates = ranked[:MAX_CANDIDATES_PER_ITERATION]

    best_sim = _best_similarity(top_candidates[0]) if top_candidates else 0.0
    worst_sim_top = (
        min(_best_similarity(t) for t in top_candidates) if top_candidates else 0.0
    )
    if stats:
        stats.record_embedding(
            pool_size=len(triplets),
            selected=len(top_candidates),
            best_similarity=best_sim,
            worst_similarity_in_top=worst_sim_top,
        )

    if not top_candidates and triplets:
        bare = [{**t, "top_concepts": []} for t in triplets]
        top_candidates = rank_triplets_embedding_option_b(
            bare,
            embedding_iteration=iteration,
            triplet_penalty_expiry=state.get("triplet_penalty_expiry"),
        )[:MAX_CANDIDATES_PER_ITERATION]

    print(
        f"(embedding.py) sélectionnés={len(top_candidates)}/{len(triplets)} | "
        f"best_sim={round(best_sim, 3)} | "
        f"worst_in_top={round(worst_sim_top, 3)}"
    )
    for rank, cand in enumerate(top_candidates, 1):
        best = (cand.get("top_concepts") or [{}])[0]
        print(
            f"  [{rank}] sujet='{cand.get('subject', '—')}' ---> "
            f"'{best.get('label', '—')}' sim={best.get('similarity', 0):.3f} "
            f"[{best.get('match_type', '—')}]"
        )

    return {**base_state(state), "top_candidates": top_candidates}


def select_top_candidates(triplets,ontology_ttl,k=TOP_K_CANDIDATES,embedding_iteration=0,triplet_penalty_expiry=None):
    if not triplets:
        return []
    if not str(ontology_ttl or "").strip():
        raise ValueError("(embedding.py) ontology_ttl requis")

    enriched = _selector.select_top_k(triplets=triplets, ontology_ttl=ontology_ttl, k=k)
    ranked = rank_triplets_embedding_option_b(
        enriched,
        embedding_iteration=embedding_iteration,
        triplet_penalty_expiry=triplet_penalty_expiry,
    )
    return ranked[:k]
