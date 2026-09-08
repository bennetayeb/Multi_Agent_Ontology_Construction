
import re
from datetime import datetime
from langchain_core.tools import tool
from rdflib import Graph, OWL, RDF, RDFS, URIRef, Literal
from rdflib.namespace import SKOS, Namespace

from config import get_ontology_dir


EX = Namespace("http://example.org/plui#")


def _camel_to_words(text):
    if not text:
        return ""
    text = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)
    text = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)
    return text.strip()


def _local_name(uri):
    uri = str(uri)
    #extraction local nameURI aprss # ou dernier /
    if "#" in uri:
        return uri.split("#")[-1]

    return uri.split("/")[-1]


@tool
def get_ontology_concepts(ttl_content):
    g = Graph()

    try:
        g.parse(data=ttl_content, format="turtle")
    except Exception as exc:
        print(f"rdf_tools.py: get_ontology_concepts: erreur de parsing : {exc}")
        return []

    entity_types = {
        OWL.Class: "Class",
        OWL.NamedIndividual: "NamedIndividual"}

    label_props = [RDFS.label, SKOS.altLabel, SKOS.prefLabel, SKOS.hiddenLabel]

    code_props = [
        EX.codeZone,
        EX.code,
        URIRef("http://example.org/plui#codeZone"),
        URIRef("http://www.w3.org/2004/02/skos/core#notation")]

    seen_uris = set()
    concepts = []

    for owl_type, type_label in entity_types.items():
        for subj in g.subjects(RDF.type, owl_type):
            uri_str = str(subj)
            if uri_str in seen_uris:
                continue
            seen_uris.add(uri_str)

            all_labels = []
            for prop in label_props:
                for obj in g.objects(subj, prop):
                    val = str(obj).strip()
                    if val and val not in all_labels:
                        all_labels.append(val)

            #################
            codes = []
            for prop in code_props:
                for obj in g.objects(subj, prop):
                    val = str(obj).strip()
                    if val and val not in codes:
                        codes.append(val)
                        if val not in all_labels:
                            all_labels.append(val)

            #################
            local = _local_name(uri_str)
            camel_label = _camel_to_words(local)
            if camel_label and camel_label not in all_labels:
                all_labels.append(camel_label)
            if local and local not in all_labels:
                all_labels.append(local)


            primary_label = all_labels[0] if all_labels else local


            concepts.append({
                "uri" : uri_str,
                "label" : primary_label,
                "altLabels": all_labels[1:], 
                "code" : codes[0] if codes else None,
                "type" : type_label})

    print(
        f"[get_ontology_concepts] {len(concepts)} entités extraites"
        f"({len([c for c in concepts if c['type'] == 'Class'])} classes,"
        f"{len([c for c in concepts if c['type'] == 'NamedIndividual'])} individus)"
    )

    return concepts


@tool
def save_ontology_version(ttl_content, version, stage) :
    filename = get_ontology_dir() / f"ontology_v{version:03d}_{stage}_{datetime.now():%Y%m%d_%H%M%S}.ttl"
    filename.write_text(ttl_content, encoding="utf-8")
    return str(filename)



