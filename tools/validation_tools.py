
import os, shutil, tempfile, subprocess, re
import xml.etree.ElementTree as ET
from pathlib import Path

from langchain_core.tools import tool

from owlready2 import (
    World,
    Nothing, Thing, ThingClass,
    TransitiveProperty, FunctionalProperty, AsymmetricProperty,
    InverseFunctionalProperty, IrreflexiveProperty,
)
from rdflib import Graph, URIRef, BNode
from rdflib.namespace import RDF, RDFS, OWL
import requests


_DEFAULT_JAVA_EXE   = os.environ.get("OWL_JAVA_EXE",   "")
_DEFAULT_HERMIT_JAR = os.environ.get("OWL_HERMIT_JAR", "HermiT.jar")


OWL_NOTHING = URIRef("http://www.w3.org/2002/07/owl#Nothing")
OWL_CLASS = URIRef("http://www.w3.org/2002/07/owl#Class")
RDFS_SUBCLOF = URIRef("http://www.w3.org/2000/01/rdf-schema#subClassOf")


def _ttl_to_rdfxml(ttl_content) :
    g = Graph()
    g.parse(data=ttl_content, format="turtle")
    xml = g.serialize(format="xml")
    return xml.decode("utf-8") if isinstance(xml, bytes) else xml


def _short_name(uri):
    return uri.split("#")[-1].split("/")[-1]


def _name_owlready(obj):
    if hasattr(obj, "name") and obj.name:
        return obj.name
    return str(obj)


def _load_onto_owlready2(owl_path):
    world = World()
    onto = world.get_ontology("http://example.org/validator.owl")
    with open(owl_path, "rb") as f:
        onto.load(fileobj=f)
    return onto


# HermiT

def _run_hermit_jar(owl_input, owl_output, hermit_jar, java_exe):
    cmd = [
        java_exe, "-jar", hermit_jar,
        "-c", "-k",
        "-o", str(owl_output),
        owl_input.as_uri()]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        stdout_b = proc.stdout
        stderr_txt = proc.stderr.decode("utf-8", errors="replace").strip()

        if owl_output.exists() and owl_output.stat().st_size > 0:
            return True, ""

        if stdout_b and b"<rdf:RDF" in stdout_b:
            owl_output.write_bytes(stdout_b)
            return True, ""

        if "InconsistentOntologyException" in stderr_txt or "Inconsistent ontology" in stderr_txt:
            owl_output.write_text("INCONSISTENT", encoding="utf-8")
            return True, ""

        return False, f"HermiT sans output. rc={proc.returncode} stderr={stderr_txt[:400]}"

    except FileNotFoundError:
        return False, f"java introuvable : {java_exe}"
    except subprocess.TimeoutExpired:
        return False, "HermiT timeout"
    except Exception as exc:
        return False, f"Erreur : {exc}"



#####################################################################################################
def _localiser_incoherences(owl_original):

    msgs = []
    if not owl_original.exists():
        return ["[OW1-LOC] Ontologie originale introuvable pour diagnostic"]

    try:
        g = Graph()
        g.parse(data=owl_original.read_bytes(), format="xml")
    except Exception as exc:
        return [f"[OW1-LOC] Lecture impossible : {exc}"]

    OWL_DW = URIRef("http://www.w3.org/2002/07/owl#disjointWith")
    OWL_ADC = URIRef("http://www.w3.org/2002/07/owl#AllDisjointClasses")
    OWL_MEM = URIRef("http://www.w3.org/2002/07/owl#members")
    OWL_EQ = URIRef("http://www.w3.org/2002/07/owl#equivalentClass")
    OWL_ON = URIRef("http://www.w3.org/2002/07/owl#onProperty")
    OWL_AVF = URIRef("http://www.w3.org/2002/07/owl#allValuesFrom")
    OWL_RST = URIRef("http://www.w3.org/2002/07/owl#Restriction")
    OWL_NI = URIRef("http://www.w3.org/2002/07/owl#NamedIndividual")
    RDF_TYPE = RDF.type
    RDF_FIRST = RDF.first
    RDF_REST = RDF.rest
    RDF_NIL = RDF.nil

    ### tout sauf individu 


    ######

    def rdf_list(node):
        items = []
        while node and node != RDF_NIL:
            f = g.value(node, RDF_FIRST)
            if f: items.append(f)
            node = g.value(node, RDF_REST)
        return items

    def parents_of(cls):
        return set(g.objects(cls, RDFS_SUBCLOF))

    disjoint_pairs = set()
    for a, _, b in g.triples((None, OWL_DW, None)):
        pair = tuple(sorted([str(a), str(b)]))
        disjoint_pairs.add((URIRef(pair[0]), URIRef(pair[1])))

    found_l1 = set()
    for cls in g.subjects(RDF_TYPE, OWL_CLASS):
        if not isinstance(cls, URIRef): continue
        parents = parents_of(cls)
        for (a, b) in disjoint_pairs:
            if a in parents and b in parents:
                key = (_short_name(str(cls)), _short_name(str(a)), _short_name(str(b)))
                if key not in found_l1:
                    found_l1.add(key)
                    msgs.append(
                        f"[OW1-L1] Classe insatisfaisable : '{_short_name(str(cls))}'"
                        f" hérite de '{_short_name(str(a))}' ET '{_short_name(str(b))}'"
                        f" (owl:disjointWith)"
                    )

    # L2 
    for adc_node in g.subjects(RDF_TYPE, OWL_ADC):
        members_node = g.value(adc_node, OWL_MEM)
        if not members_node: continue
        members = rdf_list(members_node)
        for cls in g.subjects(RDF_TYPE, OWL_CLASS):
            if not isinstance(cls, URIRef): continue
            parents = parents_of(cls)
            matches = [m for m in members if m in parents]
            if len(matches) >= 2:
                names = [_short_name(str(m)) for m in matches]
                msgs.append(
                    f"[OW1-L2] Classe insatisfaisable : '{_short_name(str(cls))}'"
                    f" hérite de {names} (owl:AllDisjointClasses)"
                )

    # L3 
    for ind in g.subjects(RDF_TYPE, OWL_NI):
        ind_types = set(g.objects(ind, RDF_TYPE)) - {OWL_NI}
        for (a, b) in disjoint_pairs:
            if a in ind_types and b in ind_types:
                msgs.append(
                    f"[OW1-L3] Individu ABox incohérent : '{_short_name(str(ind))}'"
                    f" est à la fois '{_short_name(str(a))}' et '{_short_name(str(b))}'"
                    f" (owl:disjointWith)"
                )

    # L4 
    avf_restrictions = {}
    for cls, _, eq_node in g.triples((None, OWL_EQ, None)):
        if isinstance(eq_node, BNode) and (eq_node, RDF_TYPE, OWL_RST) in g:
            prop = g.value(eq_node, OWL_ON)
            avf  = g.value(eq_node, OWL_AVF)
            if prop and avf:
                avf_restrictions.setdefault(cls, []).append((prop, avf))

    for restricted_cls, constraints in avf_restrictions.items():
        sub_classes = set(g.subjects(RDFS_SUBCLOF, restricted_cls)) | {restricted_cls}
        for sub_cls in sub_classes:
            for ind in g.subjects(RDF_TYPE, sub_cls):
                for (prop, expected_type) in constraints:
                    for val in g.objects(ind, prop):
                        val_types = set(g.objects(val, RDF_TYPE)) - {OWL_NI}
                        for vt in val_types:
                            is_disj = (
                                (vt, OWL_DW, expected_type) in g or
                                (expected_type, OWL_DW, vt) in g
                            )
                            if is_disj:
                                msgs.append(
                                    f"[OW1-L4] Violation allValuesFrom :"
                                    f" '{_short_name(str(ind))}' (type '{_short_name(str(sub_cls))}')"
                                    f" -> '{_short_name(str(prop))}' → '{_short_name(str(val))}'"
                                    f" de type '{_short_name(str(vt))}'"
                                    f" disjoint de '{_short_name(str(expected_type))}'"
                                )

    if not msgs:
        msgs.append(
            "[OW1-LOC] Incohérence HermiT non localisée automatiquement"
            " — vérifiez owl:disjointWith, AllDisjointClasses et allValuesFrom."
        )
    return msgs


# OW1
def _check_ow1_unsatisfiable(inferred_owl) :

    errors = []
    if not inferred_owl.exists():
        errors.append("[OW1] dichier inféré absent")
        return errors

    try:
        txt = inferred_owl.read_text(encoding="utf-8").strip()
        if txt == "INCONSISTENT":
            errors.append(
                "[OW1] ONTOLOGIE GLOBALEMENT INCOHÉRENTE"
                " (HermiT : InconsistentOntologyException)"
            )
            owl_original = inferred_owl.parent / "input.owl"
            errors.extend(_localiser_incoherences(owl_original))
            return errors
    except Exception:
        pass


    try:
        content_str = inferred_owl.read_text(encoding="utf-8", errors="replace")
        found = set()
        for nothing_token in ["owl:Nothing", "<http://www.w3.org/2002/07/owl#Nothing>"]:
            pattern = rf'SubClassOf\s*\(\s*(<[^>]+>|[^\s()]+)\s*{re.escape(nothing_token)}\s*\)'
            for m in re.finditer(pattern, content_str):
                raw = m.group(1).strip("<>")
                n = _short_name(raw)
                if n and n.lower() not in ("nothing", "") and n not in found:
                    found.add(n)
                    errors.append(
                        f"[OW1] Classe INSATISFAISABLE : '{n}'"
                        f" — SubClassOf owl:Nothing inféré par HermiT"
                    )


        if not found and content_str.strip().startswith("<"):
            g = Graph()
            g.parse(data=inferred_owl.read_bytes(), format="xml")
            for subj, pred, obj in g:
                if pred == RDFS_SUBCLOF and obj == OWL_NOTHING:
                    n = _short_name(str(subj))
                    if n and n.lower() not in ("nothing", "") and n not in found:
                        found.add(n)
                        errors.append(f"[OW1] Classe INSATISFAISABLE : '{n}'")
    except Exception as exc:
        errors.append(f"[OW1] Erreur lecture inféré : {exc}")

    return errors




def _check_ow2_untyped_individuals(onto):
    warnings = []
    try:
        for ind in onto.individuals():
            types_concrets = [
                t for t in ind.is_a
                if isinstance(t, ThingClass)
                and t is not Thing
                and _name_owlready(t) not in ("Thing", "NamedIndividual")
            ]
            if not types_concrets:
                warnings.append(f"[OW2] Individu sans type concret : '{_name_owlready(ind)}'")
    except Exception as exc:
        warnings.append(f"[OW2] Erreur : {exc}")
    return warnings


def _check_ow3_subclass_cycles(onto) :
    errors = []
    try:

        children: dict = {}
        for cls in onto.classes():
            if cls is Thing:
                continue
            name = _name_owlready(cls)
            parents = [
                p for p in cls.is_a
                if isinstance(p, ThingClass) and p is not Thing
            ]
            for parent in parents:
                pname = _name_owlready(parent)
                children.setdefault(pname, set()).add(name)


        seen_cycles: set[frozenset] = set()

        def dfs(start, current, path, visited):
            for child in children.get(current, []):
                if child == start:

                    cycle_key = frozenset(path + [child])
                    if cycle_key not in seen_cycles:
                        seen_cycles.add(cycle_key)
                        cycle_str = " ⊆ ".join(path + [child])
                        if len(path) == 1:

                            errors.append(
                                f"[OW3] Cycle subClassOf DIRECT : {cycle_str}"
                                f" (équivalence implicite non déclarée)"
                            )
                        else:

                            errors.append(
                                f"[OW3] Cycle subClassOf TRANSITIF : {cycle_str}"
                            )
                elif child not in visited:
                    visited.add(child)
                    dfs(start, child, path + [child], visited)
                    visited.discard(child)

        for cls in onto.classes():
            if cls is Thing:
                continue
            start_name = _name_owlready(cls)
            dfs(start_name, start_name, [start_name], {start_name})

    except Exception as exc:
        errors.append(f"[OW3] Erreur détection cycles : {exc}")
    return errors


def _check_ow4_missing_domain_range(onto):
    return []


def _check_ow5_isolated_root_classes(onto) :
    warnings = []
    try:
        for cls in onto.classes():
            if cls is Thing: continue
            parents = [p for p in cls.is_a if isinstance(p, ThingClass) and p is not Thing]
            if not parents:
                warnings.append(f"[OW5] Classe racine isolée : '{_name_owlready(cls)}'")
    except Exception as exc:
        warnings.append(f"[OW5] Erreur : {exc}")
    return warnings


def _check_ow6_transitive_functional(onto):
    errors = []
    try:
        for prop in onto.object_properties():
            if TransitiveProperty in prop.is_a and FunctionalProperty in prop.is_a:
                errors.append(
                    f"[OW6] Combinaison interdite OWL-DL : '{_name_owlready(prop)}'"
                    f" est TransitiveProperty ET FunctionalProperty"
                )
    except Exception as exc:
        errors.append(f"[OW6] Erreur : {exc}")
    return errors


def _check_ow7_asymmetric_violation(onto):
    errors = []
    try:
        for prop in onto.object_properties():
            if AsymmetricProperty not in prop.is_a: continue
            for subj in onto.individuals():
                values = list(getattr(subj, prop.python_name, None) or [])
                for obj in values:
                    reverse = list(getattr(obj, prop.python_name, None) or [])
                    if subj in reverse:
                        errors.append(
                            f"[OW7] Violation AsymmetricProperty '{_name_owlready(prop)}' :"
                            f" '{_name_owlready(subj)}' ↔ '{_name_owlready(obj)}'"
                        )
    except Exception as exc:
        errors.append(f"[OW7] Erreur : {exc}")
    return errors


@tool
def validate_syntax_rdf(ttl_content) :

    g = Graph()
    try:
        g.parse(data=ttl_content, format="turtle")
        return {"valid": True, "errors": [], "triple_count": len(g)}
    except Exception as exc:
        return {"valid": False, "errors": [str(exc)], "triple_count": 0}


@tool
def validate_with_reasoner(ttl_content,java_exe: str = _DEFAULT_JAVA_EXE, hermit_jar: str = _DEFAULT_HERMIT_JAR):
    errors = []
    warnings= []

    java_path = Path(java_exe)
    if java_path.is_dir():
        candidate = java_path / "java.exe"
        if not candidate.exists():
            candidate = java_path / "java"
        java_exe = str(candidate)

    if not Path(java_exe).exists():
        return {"valid": False, "errors": [f"pas de java : {java_exe}"], "warnings": []}
    if not Path(hermit_jar).exists():
        return {"valid": False, "errors": [f" pas HermiT.jar  : {hermit_jar}"], "warnings": []}

    try:
        rdfxml = _ttl_to_rdfxml(ttl_content)
    except Exception as exc:
        return {"valid": False, "errors": [f"[PARSE] Turtle invalide : {exc}"], "warnings": []}

    tmp_dir = Path(tempfile.mkdtemp(prefix="hermit_"))
    owl_input = tmp_dir / "input.owl"
    owl_inferred = tmp_dir / "inferred.owl"

    try:
        owl_input.write_text(rdfxml, encoding="utf-8")

        ok, err_msg = _run_hermit_jar(owl_input, owl_inferred, hermit_jar, java_exe)
        if not ok:
            errors.append(f"[OW0] HermiT JAR échec : {err_msg}")
        else:
            errors += _check_ow1_unsatisfiable(owl_inferred)

        try:
            onto = _load_onto_owlready2(owl_input)
            errors  += _check_ow6_transitive_functional(onto)
            errors  += _check_ow7_asymmetric_violation(onto)
            warnings += _check_ow2_untyped_individuals(onto)
            errors += _check_ow3_subclass_cycles(onto)  


            warnings += _check_ow4_missing_domain_range(onto)
        except Exception as exc:
            warnings.append(f"[LOAD-OW] owlready2 erreur : {exc}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


    #"P13",  # "P38",  
    # "P39",  #
    #"P04",  "P10",

OOPS_CRITICAL_CODES = {
    "P03", 
    "P05",  
    "P06",  
    "P10", 
    "P15", 
    "P18", 
    "P19",  
    "P25",  
    "P26",
    "P27", 
    "P28", 
    "P29",  
    "P31",  
    "P34",
    "P35", 
    "P40", 
}

OOPS_URL = "https://oops.linkeddata.es/rest"
OOPS_NS = "http://www.oeg-upm.net/oops"

_OOPS_DOWN = False
_OOPS_FAIL_COUNT = 0
_OOPS_FAILURES_BEFORE_DOWN = 2


def _is_oops_transport_failure(error):
    e = (error or "").lower()
    return any(
        m in e
        for m in (
            "timeout",
            "502",
            "503",
            "504",
            "proxy error",
            "server error",
            "connexion impossible",
            "connection",
        )
    )




def _build_oops_request(rdfxml_content) :
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <OOPSRequest>
            <OntologyUrl></OntologyUrl>
            <OntologyContent><![CDATA[
        {rdfxml_content}
            ]]></OntologyContent>
            <Pitfalls></Pitfalls>
            <OutputFormat>XML</OutputFormat>
        </OOPSRequest>"""


def _parse_oops_response(xml_text) :
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return [{"code": "PARSE_ERROR", "name": str(exc), "importance": "Unknown",
                 "affected_count": 0, "affected": []}]

    pitfalls = []
    for pitfall in root.findall(f"{{{OOPS_NS}}}Pitfall"):
        def txt(tag):
            el = pitfall.find(f"{{{OOPS_NS}}}{tag}")
            return el.text.strip() if el is not None and el.text else ""

        affected = [
            ae.text.strip()
            for ae in pitfall.findall(f".//{{{OOPS_NS}}}AffectedElement")
            if ae.text
        ]
        pitfalls.append({
            "code" : txt("Code"),
            "name" : txt("Name"),
            "importance" : txt("Importance"),
            "affected_count": int(txt("NumberAffectedElements") or 0),
            "affected": affected, 
        })
    return pitfalls


@tool
def validate_oops(ttl_content: str) :
    import time

    global _OOPS_DOWN, _OOPS_FAIL_COUNT

    if _OOPS_DOWN:
        print("(validate_oops) API marquée down — appel ignoré")
        return {"critical": [], "ignored_count": 0, "error": None}

    MAX_RETRIES = 3
    TIMEOUTS  = [60, 90, 120]   
    BACKOFF = [10, 30]  

    try:
        rdfxml = _ttl_to_rdfxml(ttl_content)
        body = _build_oops_request(rdfxml)
    except Exception as exc:
        return {"critical": [], "ignored_count": 0, "error": f"Conversion TTL->RDF/XML : {exc}"}

    last_error = ""
    for attempt in range(MAX_RETRIES):
        timeout = TIMEOUTS[attempt]
        try:
            print(f"(validate_oops) Tentative {attempt + 1}/{MAX_RETRIES} (timeout={timeout}s)…")
            resp = requests.post(
                OOPS_URL,
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                timeout=timeout,
            )
            resp.raise_for_status()
            all_pitfalls = _parse_oops_response(resp.text)
            #for p in all_pitfalls:
            #    print(f"  OOPS pitfall: {p['code']} | {p['name']} | importance={p['importance']} | critique={p['code'] in OOPS_CRITICAL_CODES}")

            critical = [p for p in all_pitfalls if p["code"] in OOPS_CRITICAL_CODES]
            ignored_count = sum(1 for p in all_pitfalls if p["code"] not in OOPS_CRITICAL_CODES)
            print(f"(validate_oops) ✓ Réponse reçue — {len(all_pitfalls)} pitfalls ({len(critical)} critiques)")
            _OOPS_FAIL_COUNT = 0
            return {"critical": critical, "ignored_count": ignored_count, "error": None}

        except requests.exceptions.Timeout:
            last_error = f"Timeout tentative {attempt + 1} (>{timeout}s)"
            print(f"(validate_oops) temps {last_error}")
        except requests.exceptions.ConnectionError as exc:
            last_error = f"Connexion impossible : {exc}"
            print(f"(validate_oops) x {last_error}")
            break  # inutile de réessayer si pas de réseau
        except Exception as exc:
            last_error = str(exc)
            print(f"(validate_oops) x Erreur = : {last_error}")
            break

        # Attente avant prochain essai (sauf dernier)
        if attempt < MAX_RETRIES - 1:
            wait = BACKOFF[attempt]
            print(f"(validate_oops) reesyer dans :  {wait} ")
            time.sleep(wait)

    print(f"(validate_oops)echeccc après {MAX_RETRIES} x tentatives {last_error}")

    if _is_oops_transport_failure(last_error):
        _OOPS_FAIL_COUNT += 1
        print(
            f"(validate_oops) ehec transport {_OOPS_FAIL_COUNT}"
            f"{_OOPS_FAILURES_BEFORE_DOWN}"
        )
        if _OOPS_FAIL_COUNT >= _OOPS_FAILURES_BEFORE_DOWN:
            _OOPS_DOWN = True
            print("(validate_oops) API down")
        return {
            "critical": [],
            "ignored_count": 0,
            "error": None,
            "oops_unavailable": True,
            "oops_unavailable_reason": last_error,
        }

    return {"critical": [], "ignored_count": 0, "error": last_error}