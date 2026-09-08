from copy import deepcopy
from typing import Any, Literal

LayerName = Literal["subject", "predicate", "object"]

_DEFAULT = {"subject": False, "predicate": False, "object": False}


def default_layers():
    return dict(_DEFAULT)


def normalize_layers(raw: dict | None):
    out: dict[str, dict[str, bool]] = {}
    for k, v in (raw or {}).items():
        sk = str(k)
        if not isinstance(v, dict):
            out[sk] = default_layers()
            continue
        out[sk] = {
            "subject": bool(v.get("subject", False)),
            "predicate": bool(v.get("predicate", False)),
            "object": bool(v.get("object", False)),
        }
    return out


def get_layers(layers, tid):
    return layers.get(str(tid), default_layers())


def is_layer_ok(layers, tid, layer) :
    return bool(get_layers(layers, tid).get(layer, False))


def is_fully_done(layers, tid) :
    L = get_layers(layers, tid)
    return bool(L["subject"] and L["predicate"] and L["object"])


def partition_by_layer(triplets, layers, layer):
    need = [] 
    already_ok = [] 

    for t in triplets:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        if is_layer_ok(layers, str(t["id"]), layer):
            already_ok.append(t)
        else:
            need.append(t)
    return need, already_ok


##################################### mark triples as processed by layer ####################

def mark_subject_ok(layers, tid) :
    tid = str(tid)
    L = layers.setdefault(tid, default_layers())
    L["subject"] = True
    L["object"] = False


def mark_subject_rejected(layers, tid):
    tid = str(tid)
    L = layers.setdefault(tid, default_layers())
    L["subject"] = False
    L["object"] = False


def mark_predicate_ok(layers, tid) :
    tid = str(tid)
    L = layers.setdefault(tid, default_layers())
    L["predicate"] = True
    L["object"] = False


def mark_predicate_rejected(layers, tid) :
    tid = str(tid)
    L = layers.setdefault(tid, default_layers())
    L["predicate"] = False
    L["object"] = False


def mark_object_ok(layers, tid) :
    tid = str(tid)
    L = layers.setdefault(tid, default_layers())
    L["object"] = True


def mark_object_rejected(layers, tid) :
    tid = str(tid)
    L = layers.setdefault(tid, default_layers())
    L["object"] = False



################################## apply decisions to layers ####################
def apply_subject_decisions(layers, accepted, rejected) :
    layers = normalize_layers(layers)
    for c in accepted:
        if isinstance(c, dict) and c.get("id"):
            mark_subject_ok(layers, str(c["id"]))
    for r in rejected:
        if isinstance(r, dict) and r.get("id"):
            mark_subject_rejected(layers, str(r["id"]))
    return layers


def apply_predicate_decisions(layers, accepted, rejected):
    layers = normalize_layers(layers)
    for c in accepted:
        if isinstance(c, dict) and c.get("id"):
            mark_predicate_ok(layers, str(c["id"]))
    for r in rejected:
        if isinstance(r, dict) and r.get("id"):
            mark_predicate_rejected(layers, str(r["id"]))
    return layers


def apply_object_decisions(layers, accepted, rejected):
    layers = normalize_layers(layers)
    for c in accepted:
        if isinstance(c, dict) and c.get("id"):
            mark_object_ok(layers, str(c["id"]))
    for r in rejected:
        if isinstance(r, dict) and r.get("id"):
            mark_object_rejected(layers, str(r["id"]))
    return layers


def prune_layers_to_pool(layers, pool) :
    layers = normalize_layers(layers)
    pool_ids = {str(t["id"]) for t in pool if isinstance(t, dict) and t.get("id")}
    return {k: v for k, v in layers.items() if k in pool_ids}


def copy_layers(layers: dict | None):
    return deepcopy(normalize_layers(layers))
