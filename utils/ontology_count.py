from rdflib import Graph
from rdflib.namespace import RDF, RDFS, OWL


def count_ontology_entities(ttl_content):
    try:
        g = Graph()
        g.parse(data=ttl_content, format="turtle")

        classes = set(g.subjects(RDF.type, OWL.Class))
        classes.update(g.subjects(RDFS.subClassOf, None))
        
        obj_properties = set(g.subjects(RDF.type, OWL.ObjectProperty))
        data_properties = set(g.subjects(RDF.type, OWL.DatatypeProperty))
        individuals = set(g.subjects(RDF.type, None))

        # retirer les classes/propriétés des individus
        individuals -= classes
        individuals -= obj_properties
        individuals -= data_properties

        return {
            "classes": len(classes),
            "obj_properties": len(obj_properties),
            "data_properties": len(data_properties),
            "individuals": len(individuals),
            "triples": len(g),
        }

    except Exception as exc:
        print(f"erreur compter class, preoriété ontologie : {exc}")

        return {
            "classes": 0,
            "obj_properties": 0,
            "data_properties": 0,
            "individuals": 0,
            "triples": 0,
        }