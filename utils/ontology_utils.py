
import re
from typing import Optional
from rdflib import Graph


def merge_patch(base_ttl: str, patch_ttl: str) -> str:

    if not patch_ttl or not patch_ttl.strip():
        print("(ontology_utils.merge_patch) Patch vide -> retour base_ttl inchangée.")
        return base_ttl

    g = Graph()
    try:
        g.parse(data=base_ttl, format="turtle")
    except Exception as e:
        print(f"(ontology_utils.merge_patch) ERREUR parsing base_ttl : {e} -> retour base_ttl brute.")
        return base_ttl

    #extraire les lignes DELETE avant de parser le patch
    delete_lines = re.findall(r"#\s*DELETE\s*:\s*(.+?)(?:\n|$)", patch_ttl)
    if delete_lines:
        print(f"(ontology_utils.merge_patch) {len(delete_lines)} ligne(s) DELETE trouvée(s).")

    # comment
    clean_patch = re.sub(r"#\s*DELETE\s*:.+?(?:\n|$)", "\n", patch_ttl)

    # ajout  nouveaux triplets du patch 
    prefixes_block = _build_prefixes_block(g)

    # test1 
    patch_parsed = False
    try:
        g_patch = Graph()
        g_patch.parse(data=clean_patch, format="turtle")
        for triple in g_patch:
            g.add(triple)
        for prefix, ns in g_patch.namespaces():
            g.bind(prefix, ns)
        patch_parsed = True
    except Exception as e1:
        #print(f"(ontology_utils.merge_patch) probleme patch sans prefixes: ({e1}).")

    # test2  ajout prefixe 
    if not patch_parsed:
        enriched_patch = prefixes_block + "\n" + clean_patch
        try:
            g_patch = Graph()
            g_patch.parse(data=enriched_patch, format="turtle")
            for triple in g_patch:
                g.add(triple)
            for prefix, ns in g_patch.namespaces():
                g.bind(prefix, ns)
            patch_parsed = True
        except Exception as e2:
            print(f"(ontology_utils.merge_patch) ERREUR parsing patch enrichi : {e2} -> fallback base_ttl.")
            return base_ttl

    if delete_lines:
        for triple_str in delete_lines:
            triple_str = triple_str.strip()
            if not triple_str:
                continue

            if not triple_str.endswith("."):
                triple_str += " ."
            try:
                g_del = Graph()
                full_stmt = prefixes_block + "\n" + triple_str
                g_del.parse(data=full_stmt, format="turtle")
                for triple in g_del:
                    if triple in g:
                        g.remove(triple)
                        print(f"(ontology_utils.merge_patch) suppimer ttiplet: {triple}")
                    else:
                        print(f"(ontology_utils.merge_patch) Triple DELETE introuvable (ignoré) : {triple_str}")
            except Exception as e:
                print(f"(ontology_utils.merge_patch) Impossible de parser DELETE '{triple_str}' : {e}")

    result = g.serialize(format="turtle")
    print(
        f"(ontology_utils.merge_patch) Fusion oui "
        f"{len(g)} triplets dans l'ontologie finale."
    )
    return result


def validate_patch_syntax(patch_ttl, base_ttl):

    if not patch_ttl or not patch_ttl.strip():
        return False, "Patch vide"

    clean = re.sub(r"#\s*DELETE\s*:.+?(?:\n|$)", "\n", patch_ttl)

    #  patch seul
    try:
        Graph().parse(data=clean, format="turtle")
        return True, ""
    except Exception as e1:
        pass

    # avec préfixes de la base
    try:
        g_base = Graph()
        g_base.parse(data=base_ttl, format="turtle")
        prefixes = _build_prefixes_block(g_base)
        Graph().parse(data=prefixes + "\n" + clean, format="turtle")
        return True, ""
    except Exception as e2:
        return False, str(e2)


def _build_prefixes_block(g) :
    lines = []
    for prefix, ns in g.namespaces():
        if prefix:
            lines.append(f"@prefix {prefix}: <{ns}> .")
        else:
            lines.append(f"@base <{ns}> .")
    return "\n".join(lines)


def merge_correction_patch(base_ttl, patch_ttl):

    if not patch_ttl or not patch_ttl.strip():
        return base_ttl, False

    patch_ok, err = validate_patch_syntax(patch_ttl, base_ttl)
    if not patch_ok:
        print(f"(ontology_utils.merge_correction_patch) patch ignoré : {err}")
        return base_ttl, False

    try:
        merged = merge_patch(base_ttl, patch_ttl)
        Graph().parse(data=merged, format="turtle")
        n_delta = count_new_triples(base_ttl, merged)
        has_delete = bool(re.search(r"#\s*DELETE\s*:", patch_ttl, re.I))
        if n_delta == 0 and not has_delete:
            print("(ontology_utils.merge_correction_patch) patch sans effet sur le graphe -> ignoré")
            return base_ttl, False
        return merged, True
    except Exception as exc:
        print(f"(ontology_utils.merge_correction_patch) échec : {exc}")
        return base_ttl, False


def count_new_triples(base_ttl, merged_ttl):
    try:
        g_base = Graph()
        g_base.parse(data=base_ttl, format="turtle")
        g_merged = Graph()
        g_merged.parse(data=merged_ttl, format="turtle")
        return len(g_merged) - len(g_base)
    except Exception:
        return -1
