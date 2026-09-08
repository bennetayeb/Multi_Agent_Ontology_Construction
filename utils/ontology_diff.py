
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rdflib import Graph, URIRef, Literal, BNode
from rdflib.namespace import OWL, RDF, RDFS, SKOS



@dataclass
class TripleDiff:
    subject : str
    predicate: str
    obj : str
    action : str   # "added" | "removed"
    category : str   # "class"| "property" | "altLabel" | "label"| "subClassOf" | "other"


@dataclass
class OntologyDiff:
    label : str
    triples_before : int
    triples_after : int
    triples_added_count : int
    triples_removed_count : int
    net_change : int

    # changements
    new_classes : list[str]       # URIs des nouvelles owl:Class
    removed_classes : list[str]
    new_properties : list[str]      # URIs des nouvelles owl:ObjectProperty
    removed_properties : list[str]
    new_altlabels : list[tuple[str, str]]   # (uri, label)
    removed_altlabels: list[tuple[str, str]]
    new_labels : list[tuple[str, str]]   # (uri, label)
    removed_labels : list[tuple[str, str]]
    new_subclassof : list[tuple[str, str]]   # (child, parent)
    removed_subclassof: list[tuple[str, str]]
    new_inverseOf: list[tuple[str, str]]
    new_disjointWith : list[tuple[str, str]]
    other_added  : list[TripleDiff]
    other_removed : list[TripleDiff]
    is_empty: bool = False    # aucun changement


def _local(uri) :
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.split("/")[-1]


def _str_term(term):
    if isinstance(term, URIRef):
        return str(term)
    if isinstance(term, Literal):
        lang = f"@{term.language}" if term.language else ""
        return f'"{term}"{lang}'
    if isinstance(term, BNode):
        return f"_:{term}"
    return str(term)


def _parse(ttl) :
    g = Graph()
    try:
        g.parse(data=ttl, format="turtle")
    except Exception as e:
        print(f"(ontology_diff.py) Parse error : {e}")
    return g


def _categorize_triple(s, p, o):
    p_str = str(p)
    o_str = str(o)

    if p_str == str(RDF.type):
        if o_str == str(OWL.Class):
            return "class_declaration"
        if "Property" in o_str:
            return "property_declaration"
        if o_str == str(OWL.NamedIndividual):
            return "individual_declaration"

    if p_str == str(RDFS.subClassOf):
        return "subClassOf"
    if p_str == str(SKOS.altLabel):
        return "altLabel"
    if p_str in (str(RDFS.label), str(SKOS.prefLabel)):
        return "label"
    if p_str == str(OWL.inverseOf):
        return "inverseOf"
    if p_str == str(OWL.disjointWith):
        return "disjointWith"
    if p_str in (str(RDFS.domain), str(RDFS.range)):
        return "domain_range"

    return "other"


def _is_meaningful_triple(s, p, o) :

    # bnodes problem
    if isinstance(s, BNode):
        return False

    p_str = str(p)

    if p_str in (
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#first",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest",
    ):
        return False

    if p_str == str(RDF.type) and isinstance(o, URIRef):
        if str(o) in {
            "http://www.w3.org/2002/07/owl#Restriction",
            "http://www.w3.org/2002/07/owl#AllDisjointClasses",
            "http://www.w3.org/2002/07/owl#AllDisjointProperties",
            "http://www.w3.org/2002/07/owl#Axiom",
            "http://www.w3.org/2002/07/owl#NegativePropertyAssertion",
        }:
            return False

    if isinstance(o, BNode):
        return False

    return True


def compute_diff(before_ttl,after_ttl,label="unknown"):
    g_before = _parse(before_ttl)
    g_after = _parse(after_ttl)

    set_before = {(s, p, o) for s, p, o in g_before if _is_meaningful_triple(s, p, o)}
    set_after = {(s, p, o) for s, p, o in g_after  if _is_meaningful_triple(s, p, o)}

    added = set_after - set_before
    removed = set_before - set_after

    new_classes = []
    new_properties = []
    new_altlabels  = []
    new_labels  = []
    new_subclassof = []
    new_inverseOf = []
    new_disjointWith = []
    other_added  = []

    for s, p, o in added:
        cat = _categorize_triple(s, p, o)
        s_str, p_str, o_str = _str_term(s), _str_term(p), _str_term(o)

        if cat == "class_declaration":
            new_classes.append(s_str)
        elif cat == "property_declaration":
            new_properties.append(s_str)
        elif cat == "altLabel":
            new_altlabels.append((s_str, _str_term(o)))
        elif cat == "label":
            new_labels.append((s_str, _str_term(o)))
        elif cat == "subClassOf":
            new_subclassof.append((s_str, o_str))
        elif cat == "inverseOf":
            new_inverseOf.append((s_str, o_str))
        elif cat == "disjointWith":
            new_disjointWith.append((s_str, o_str))
        else:
            other_added.append(TripleDiff(s_str, p_str, o_str, "added", cat))

    removed_classes= []
    removed_properties = []
    removed_altlabels = []
    removed_labels = []
    removed_subclassof = []
    other_removed  = []

    for s, p, o in removed:
        cat = _categorize_triple(s, p, o)
        s_str, p_str, o_str = _str_term(s), _str_term(p), _str_term(o)

        if cat == "class_declaration":
            removed_classes.append(s_str)
        elif cat == "property_declaration":
            removed_properties.append(s_str)
        elif cat == "altLabel":
            removed_altlabels.append((s_str, _str_term(o)))
        elif cat == "label":
            removed_labels.append((s_str, _str_term(o)))
        elif cat == "subClassOf":
            removed_subclassof.append((s_str, o_str))
        else:
            other_removed.append(TripleDiff(s_str, p_str, o_str, "removed", cat))

    is_empty = len(added) == 0 and len(removed) == 0

    meaningful_added   = (len(new_classes) + len(new_properties) + len(new_altlabels) +
                          len(new_labels) + len(new_subclassof) + len(new_inverseOf) +
                          len(new_disjointWith) + len(other_added))
    meaningful_removed = (len(removed_classes) + len(removed_properties) +
                          len(removed_altlabels) + len(removed_labels) +
                          len(removed_subclassof) + len(other_removed))
    if meaningful_added > 50 or meaningful_removed > 50:
        print(
            f"(ontology_diff.py)  [{label}] Diff bcp de BNodes : "
            f"+{meaningful_added} ajouts / -{meaningful_removed} suppressions")

    return OntologyDiff(
        label  = label,
        triples_before= len(g_before),   # compte total + BNodes pour  rdflib
        triples_after  = len(g_after),

        triples_added_count  = len(added),
        triples_removed_count = len(removed),
        net_change  = len(set_after) - len(set_before),
        new_classes = new_classes,
        removed_classes= removed_classes,
        new_properties  = new_properties,
        removed_properties = removed_properties,
        new_altlabels = new_altlabels,
        removed_altlabels = removed_altlabels,
        new_labels = new_labels,
        removed_labels = removed_labels,
        new_subclassof = new_subclassof,
        removed_subclassof = removed_subclassof,
        new_inverseOf = new_inverseOf,
        new_disjointWith = new_disjointWith,
        other_added  = other_added,
        other_removed = other_removed,
        is_empty = is_empty,
    )


def print_diff(diff: OntologyDiff, verbose: bool = True):
    sep = "#" * 60
    print(f"\n{sep}")
    print(f" DIFF [{diff.label}]")
    print(f"Triplets : {diff.triples_before} → {diff.triples_after} "
          f"(+{diff.triples_added_count} / -{diff.triples_removed_count}, net={diff.net_change:+d})")
    print(sep)

    if diff.is_empty:
        print("  (aucun changement)")
        print(sep)
        return

    if diff.new_classes:
        print(f"  ✚ Nouvelles classes ({len(diff.new_classes)}) :")
        for c in diff.new_classes:
            print(f"      {_local(c)}")

    if diff.new_properties:
        print(f" + Nouvelles propriétés ({len(diff.new_properties)}) :")
        for p in diff.new_properties:
            print(f"      {_local(p)}")

    if diff.new_altlabels:
        print(f" + Nouveaux altLabels ({len(diff.new_altlabels)}) :")
        for uri, lbl in diff.new_altlabels:
            print(f"      {_local(uri)} ← {lbl}")

    if diff.new_subclassof:
        print(f"  + Nouveaux subClassOf ({len(diff.new_subclassof)}) :")
        for child, parent in diff.new_subclassof:
            print(f"      {_local(child)} ⊆ {_local(parent)}")

    if diff.new_inverseOf:
        print(f"  + Nouvelles inverseOf ({len(diff.new_inverseOf)}) :")
        for a, b in diff.new_inverseOf:
            print(f"      {_local(a)} ↔ {_local(b)}")

    if diff.new_disjointWith:
        print(f" + Nouvelles disjonctions ({len(diff.new_disjointWith)}) :")
        for a, b in diff.new_disjointWith:
            print(f"      {_local(a)} ⊥ {_local(b)}")

    if diff.removed_subclassof:
        print(f" x subClassOf supprimés ({len(diff.removed_subclassof)}) :")
        for child, parent in diff.removed_subclassof:
            print(f"      {_local(child)} ⊆ {_local(parent)}  [DELETE]")

    if verbose and diff.other_added:
        print(f" + Autres triplets ajoutés ({len(diff.other_added)}) :")
        for t in diff.other_added[:10]:
            print(f"  {_local(t.subject)} — {_local(t.predicate)} — {t.obj[:40]}")
        if len(diff.other_added) > 10:
            print(f"      ... et {len(diff.other_added) - 10} de plus")

    if verbose and diff.other_removed:
        print(f" autres triplets supprimés ({len(diff.other_removed)}) :")
        for t in diff.other_removed[:5]:
            print(f" {_local(t.subject)} — {_local(t.predicate)} — {t.obj[:40]}")

    print(sep)


def diff_to_dict(diff: OntologyDiff) -> dict:
    return {
        "label"  : diff.label,
        "triples_before" : diff.triples_before,
        "triples_after"  : diff.triples_after,
        "triples_added" : diff.triples_added_count,
        "triples_removed"  : diff.triples_removed_count,
        "net_change" : diff.net_change,
        "new_classes" : [_local(c) for c in diff.new_classes],
        "new_classes_count" : len(diff.new_classes),
        "removed_classes" : [_local(c) for c in diff.removed_classes],
        "new_properties" : [_local(p) for p in diff.new_properties],
        "new_properties_count" : len(diff.new_properties),
        "new_altlabels" : [{"uri": _local(u), "label": l} for u, l in diff.new_altlabels],
        "new_altlabels_count": len(diff.new_altlabels),
        "new_labels"  : [{"uri": _local(u), "label": l} for u, l in diff.new_labels],
        "new_labels_count"  : len(diff.new_labels),
        "new_subclassof"  : [{"child": _local(c), "parent": _local(p)} for c, p in diff.new_subclassof],
        "new_subclassof_count" : len(diff.new_subclassof),
        "removed_subclassof": [{"child": _local(c), "parent": _local(p)} for c, p in diff.removed_subclassof],
        "removed_subclassof_count": len(diff.removed_subclassof),
        "new_inverseOf_count" : len(diff.new_inverseOf),
        "new_disjointWith_count": len(diff.new_disjointWith),
        "is_empty" : diff.is_empty,
    }


def save_diff(diff: OntologyDiff, output_path: Path) -> None:
    d = diff_to_dict(diff)
    output_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"(ontology_diff.py) diff sauvegardé : {output_path}")