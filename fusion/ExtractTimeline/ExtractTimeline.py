# SPDX-License-Identifier: Apache-2.0
# ExtractTimeline - dump each design's parametric history to JSON, for rebuilding elsewhere.
#
# The geometry in a STEP export is a dead solid: no sketches, no parameters, nothing to edit.
# This asks Fusion for the recipe instead -- the ordered timeline, every sketch's curves,
# constraints and dimensions, and each feature's driving expressions -- so the design can be
# re-authored in another tool rather than merely re-imported.
#
# Writes <OUT>/<Hub>/<Project>/<folders>/<Design>.timeline.json, one file per design.
# Unknown feature types are still recorded, with whatever is readable, so nothing goes missing
# silently: a design is only as migratable as its least understood feature, and we want that
# visible rather than quietly dropped.
#
# Run from Fusion: Utilities > ADD-INS > Scripts and Add-Ins > ExtractTimeline > Run.

import adsk.core
import adsk.fusion
import traceback
import json
import os
import re
import time

# ---- settings -------------------------------------------------------------
OUT_DIR = os.path.expanduser("~/cad/fusion-timeline")
ALL_HUBS = False
SKETCH_GEOMETRY = True   # curve-by-curve sketch contents (the bulk of the output)

# Bumped whenever the captured shape changes, so re-running upgrades older files in place
# instead of skipping them -- a resumable run should not also mean a stale one.
SCHEMA = 16

# A fillet consumes the edges it rounds, so they are absent from the finished body and can only
# be read with the model rolled back to that step. That costs a recompute per such feature, so
# it is a switch: turn it off if a hub is large and you would rather have the rest sooner.
ROLLBACK_FOR_EDGES = True
MEASURE_EACH_STEP = True  # record what the model measures after every step (needs rollback)

# Record what an unmapped feature actually exposes, rather than guessing at it and paying a whole
# run to learn the guess was wrong. Cheap: it reads property names, not geometry.
PROBE_UNMAPPED = True

# ---- what to process ------------------------------------------------------
# ONE selector. ONLY and ONLY_LIST used to be separate filters applied in sequence, so they
# INTERSECTED: a stale list left from a previous part cut the set to zero, and the progress
# dialog then died with "invalid argument minimumValue or maximumValue" rather than saying
# nothing matched. Both are folded in here, and an empty set is now reported, not crashed.
#
#   "all"        every design in the hub
#   "<group>"    a named family from GROUPS below, e.g. "brackets": any design whose NAME
#                contains the family word. Matching on parameters or feature names instead
#                catches far more, including long bodies that merely reference one -- too
#                loose to be a scope.
#   "list"       exactly the design paths in SELECT_LIST, one per line, written as they are
#                under OUT_DIR without the ".timeline.json" suffix:
#                   MyHub/ExampleProject/ExampleFolder/Example Part
#   "<text>"     anything else: a case-insensitive substring of the design name or its
#                folder path, as ONLY used to be
#
# Every mode is resumable and idempotent: a design already at this SCHEMA is skipped, so a
# run that is cancelled or crashes can simply be started again.
SELECT = "all"   # see the modes above; LIMIT below caps a first run
SELECT_LIST = os.path.expanduser("~/cad/work/_reextract.txt")

# Named groups, matched on the design's name and its folder path. Add one rather than editing
# SELECT to a substring that happens to work today.
GROUPS = {
    # an example family; edit or add your own
    "brackets": lambda name, path: "bracket" in name.lower(),
}

# Try a few designs before committing to a whole hub. Each design has to be downloaded and
# loaded to be read, so a smoke test on five beats discovering a problem on five hundred.
LIMIT = 0        # 0 = every design; otherwise stop after this many
# ---------------------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MM = 10.0  # Fusion works in centimetres; everything here is written in millimetres.


def safe(name):
    cleaned = _INVALID.sub("_", (name or "untitled")).strip().rstrip(".")
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:120] or "untitled"


def pt(p):
    """A point in millimetres."""
    if p is None:
        return None
    try:
        return [round(p.x * MM, 9), round(p.y * MM, 9), round(p.z * MM, 9)]
    except Exception:
        return None


def pt2(p):
    if p is None:
        return None
    try:
        return [round(p.x * MM, 9), round(p.y * MM, 9)]
    except Exception:
        return None


def get(obj, *names):
    """First readable attribute of several, or None. Fusion raises on many properties."""
    for n in names:
        try:
            v = getattr(obj, n)
            if v is not None:
                return v
        except Exception:
            continue
    return None


def expr_of(value_input_owner, *names):
    """A feature's driving *expression* ('width/2 + 3 mm'), not just its number.

    The expression is the whole point: it is what makes the rebuilt design parametric.
    """
    for n in names:
        try:
            o = getattr(value_input_owner, n)
        except Exception:
            continue
        if o is None:
            continue
        try:
            return {"expression": o.expression, "value_mm": round(o.value * MM, 9)}
        except Exception:
            try:
                return {"expression": str(o.expression)}
            except Exception:
                continue
    return None


def count_of(coll):
    try:
        return coll.count
    except Exception:
        return 1


def as_items(coll):
    """Every entity in whatever Fusion handed back: a collection, a Vector, or one entity.

    `.count` + `.item(i)` is the ObjectCollection shape and covers most of the API. It does
    NOT cover the `*Vector` types -- `DraftFeature.inputFaces` is a `BRepFaceVector`, and a
    probe of the live object could not read `.count` off it at all. The old code's `except:
    return [coll]` then handed back the vector ITSELF as its one item, and `point_on` found no
    point on a vector, so the feature recorded an empty selection while reporting success.
    That is how every draft in a production design came out with `face_count: 0`.

    So try each access shape in turn and take the first that yields entities. Returning
    `[coll]` is still the last resort, because a single entity is a real case, but it is now
    reached only when nothing enumerable is there.
    """
    try:                                        # ObjectCollection
        n = coll.count
        if n is not None and n >= 0:
            return [coll.item(i) for i in range(n)]
    except Exception:
        pass
    try:                                        # sequence-like Vector
        n = len(coll)
        if n > 0:
            return [coll[i] for i in range(n)]
    except Exception:
        pass
    for attr in ("asArray", "toArray"):         # Vector -> list
        try:
            arr = getattr(coll, attr)
            arr = arr() if callable(arr) else arr
            if arr:
                return list(arr)
        except Exception:
            continue
    try:                                        # plain iteration
        items = [x for x in coll]
        if items:
            return items
    except Exception:
        pass
    return [coll]


def first_nonempty(obj, *names):
    """The first of several attributes that actually HOLDS something, with its name.

    `get()` returns the first attribute that is not None, which is the wrong test here: a
    DraftFeature's `faces` is an empty-but-not-None BRepFaces, so `get(ent, "faces",
    "inputFaces")` always answered `faces` and `inputFaces` -- where the picks really live --
    was never consulted. Returns (value, name, tried) so the caller can record which source
    won and what was looked at, instead of a silent empty list.
    """
    tried = []
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception as e:
            tried.append("%s: raised %s" % (n, str(e)[:60]))
            continue
        if v is None:
            tried.append("%s: None" % n)
            continue
        items = as_items(v)
        # as_items' last resort is [v] itself; that is not a populated collection.
        if items and not (len(items) == 1 and items[0] is v):
            return v, n, tried
        tried.append("%s: %s, empty" % (n, type(v).__name__))
    return None, None, tried


def point_on(item, point_attr):
    """One world-space point on an edge or face, in millimetres.

    `pointOnEdge` is the direct answer but it fails on an edge the feature itself consumed --
    a fillet destroys the very edges it rounds, so by the time the design is read back they are
    gone from the body. The curve's own geometry usually survives that, so fall back to it, and
    finally to the bounding box, before giving up.
    """
    p = get(item, point_attr)
    if callable(p):
        try:
            p = p()
        except Exception:
            p = None
    q = pt(p) if p is not None else None
    if q:
        return q

    g = get(item, "geometry", "worldGeometry")
    if g is not None:
        s, e = pt(get(g, "startPoint")), pt(get(g, "endPoint"))
        if s and e:
            return [round((s[i] + e[i]) / 2.0, 9) for i in range(3)]
        for attr in ("center", "origin"):
            c = pt(get(g, attr))
            if c:
                return c

    bb = get(item, "boundingBox")
    if bb is not None:
        lo, hi = pt(get(bb, "minPoint")), pt(get(bb, "maxPoint"))
        if lo and hi:
            return [round((lo[i] + hi[i]) / 2.0, 9) for i in range(3)]
    return None


def picks(coll, point_attr):
    """A world-space point lying on each edge or face, in millimetres.

    This is the whole trick for re-creating fillets, chamfers, shells and drafts somewhere
    else: the identity of an edge does not survive the move, but a point on it does.
    """
    out = []
    for item in as_items(coll):
        q = point_on(item, point_attr)
        if q:
            out.append(q)
    return out


# Set per document so a feature can be read with the model rolled back to just before it.
_ROLLBACK = {"timeline": None}


def measure(design):
    """Volume, area, body count and bounding box of the model as it stands right now.

    Taken with the timeline marker parked after a given step, this is the reference a rebuild
    checks itself against feature by feature. The bounding box matters as much as the volume:
    a feature built at the wrong height keeps the volume exactly right and moves the box.

    Fusion works in centimetres; everything written here is millimetres.
    """
    try:
        root = design.rootComponent
    except Exception:
        return None
    bodies = []
    try:
        for i in range(root.bRepBodies.count):
            bodies.append(root.bRepBodies.item(i))
        for occ in root.allOccurrences:
            for i in range(occ.bRepBodies.count):
                bodies.append(occ.bRepBodies.item(i))
    except Exception:
        pass

    volume_cm3 = area_cm2 = 0.0
    counted = 0
    lo = [None, None, None]
    hi = [None, None, None]
    for b in bodies:
        try:
            if not b.isSolid:
                continue
            if get(b, "isVisible") is False or get(b, "isLightBulbOn") is False:
                continue
            props = b.physicalProperties
            volume_cm3 += props.volume
            area_cm2 += props.area
            counted += 1
            bb = b.boundingBox
            for k, (mn, mx) in enumerate(((bb.minPoint.x, bb.maxPoint.x),
                                          (bb.minPoint.y, bb.maxPoint.y),
                                          (bb.minPoint.z, bb.maxPoint.z))):
                lo[k] = mn if lo[k] is None else min(lo[k], mn)
                hi[k] = mx if hi[k] is None else max(hi[k], mx)
        except Exception:
            continue
    if not counted:
        return {"solid_bodies": 0, "volume_mm3": 0.0, "area_mm2": 0.0, "bbox_mm": None}
    return {"solid_bodies": counted,
            "volume_mm3": round(volume_cm3 * 1000.0, 6),
            "area_mm2": round(area_cm2 * 100.0, 6),
            "bbox_mm": [[round(v * MM, 6) for v in lo], [round(v * MM, 6) for v in hi]]}


def rolled_back_to(index, fn):  # noqa: C901 - guarded by ROLLBACK_FOR_EDGES below
    """Run fn() with the design rolled back to just before timeline step `index`.

    A fillet consumes the edges it rounds, so those edges are absent from the finished body and
    cannot be located there at all. Rolling the marker back puts the model into the state the
    feature actually saw, where its inputs exist. The marker is always restored, and nothing is
    saved, so the design on disk is untouched either way.
    """
    if not ROLLBACK_FOR_EDGES:
        return None
    tl = _ROLLBACK.get("timeline")
    if tl is None or index is None:
        return None
    saved = None
    try:
        saved = tl.markerPosition
        tl.markerPosition = index
        adsk.doEvents()
        return fn()
    except Exception:
        return None
    finally:
        if saved is not None:
            try:
                tl.markerPosition = saved
            except Exception:
                pass
        adsk.doEvents()


def edge_sets(ent):
    """Edge picks for fillets and chamfers, which do not keep their edges where you'd expect.

    A FilletFeature holds `edgeSets`, and each set carries its own radius -- a single feature can
    round several groups of edges to different radii. Chamfers are the same shape on newer
    builds. Returns one record per set so each can be re-created with its own size, falling back
    to a plain `edges` collection where that is all there is.
    """
    sets = []
    for attr in ("edgeSets", "filletEdgeSets", "chamferEdgeSets"):
        coll = get(ent, attr)
        if coll is None:
            continue
        for s in as_items(coll):
            pts = picks(get(s, "edges") or [], "pointOnEdge")
            if not pts:
                continue
            rec = {"edge_points": pts, "kind": type(s).__name__}
            for key, names in (("radius", ("radius", "radiusOne")),
                               ("radius2", ("radiusTwo",)),
                               ("distance", ("distance", "distanceOne")),
                               ("distance2", ("distanceTwo",)),
                               ("angle", ("angle",))):
                v = expr_of(s, *names)
                if v:
                    rec[key] = v
            sets.append(rec)
        if sets:
            return sets

    # Older shape: the edges hang directly off the feature.
    for attr in ("edges", "inputEntities"):
        coll = get(ent, attr)
        if coll is None:
            continue
        pts = picks(coll, "pointOnEdge")
        if pts:
            return [{"edge_points": pts, "kind": "direct"}]
    return sets


def axis_of(ent):
    """A revolve's axis as two points in model space.

    The axis is usually a sketch line or a construction axis, neither of which keeps its identity
    across a migration. Its geometry does, and two points define the same axis anywhere.
    """
    for attr in ("axis", "axisEntity", "centerAxis"):
        a = get(ent, attr)
        if a is None:
            continue
        g = get(a, "geometry", "worldGeometry")
        if g is None:
            continue
        start, end = pt(get(g, "startPoint")), pt(get(g, "endPoint"))
        if start and end:
            return {"start": start, "end": end, "kind": type(a).__name__}
        origin, direction = get(g, "origin"), get(g, "direction")
        if origin is not None and direction is not None:
            o, d = pt(origin), pt(direction)
            if o and d:
                return {"start": o, "end": [o[i] + d[i] for i in range(3)],
                        "kind": type(a).__name__}
    return None


def path_of(ent):
    """A sweep's path as the points along its curves, so the route survives the move."""
    p = get(ent, "path")
    if p is None:
        return None
    pts = []
    try:
        for i in range(p.count):
            c = p.item(i)
            g = get(c, "geometry", "worldGeometry")
            if g is None:
                continue
            for attr in ("startPoint", "endPoint"):
                q = pt(get(g, attr))
                if q and (not pts or q != pts[-1]):
                    pts.append(q)
    except Exception:
        return None
    return pts or None


def unmapped_pick(p, why):
    """What an unreadable profile actually IS, plus whatever identity can be had from it.

    The bare `continue` this replaces is why several designs each carried one extrude that
    cannot be replayed with nothing saying why: `profile_count` is set, `profiles` is absent,
    and the selection exists nowhere. PROBE_UNMAPPED cannot reach these -- it only fires for
    feature kinds outside MAPPED_KINDS, and ExtrudeFeature is inside it.

    Two parts to the record, kept separate on purpose:

      probe     the SHAPE of the object -- every readable property and its type. This is the
                diagnosis, and it decides how the real recorder gets written.
      the rest  a BEST-EFFORT identity (area, a point on it, centroid, bounds), each in its
                own try. The main path fails at `parentSketch`, which a BRepFace does not
                have, so the geometric properties may well still read. If they do, this is
                already enough to re-find the selection; if they do not, the probe still says
                why.

    The opportunistic fields are NOT a substitute for knowing the type: a consumer must check
    `type` before trusting any of them.
    """
    rec = {"type": type(p).__name__, "why": str(why)[:200]}
    try:
        rec["probe"] = probe(p)
    except Exception:
        pass
    # A face knows its own area and carries a point guaranteed to lie ON it; a sketch profile
    # answers the same questions through areaProperties. Try both spellings.
    try:
        a = get(p, "area")
        if isinstance(a, (int, float)):
            rec["area_mm2"] = round(a * MM * MM, 6)
    except Exception:
        pass
    try:
        rec["point_on_face"] = pt(get(p, "pointOnFace"))
    except Exception:
        pass
    try:
        ap = p.areaProperties()
        rec["centroid_model"] = pt(ap.centroid)
        if "area_mm2" not in rec:
            rec["area_mm2"] = round(ap.area * MM * MM, 6)
    except Exception:
        pass
    try:
        bb = p.boundingBox
        rec["bbox_model"] = [pt(bb.minPoint), pt(bb.maxPoint)]
    except Exception:
        pass
    try:
        b = get(p, "body")
        if b is not None:
            rec["body"] = get(b, "name")
    except Exception:
        pass
    return dict((k, v) for k, v in rec.items() if v is not None)


_PRE_OPEN = set()


def remember_open_documents(app):
    """Names of every document open before we started. These belong to the user."""
    _PRE_OPEN.clear()
    try:
        for k in range(app.documents.count):
            try:
                _PRE_OPEN.add(app.documents.item(k).name)
            except Exception:
                continue
    except Exception:
        pass
    return set(_PRE_OPEN)


def close_if_ours(doc):
    """Close `doc` only if WE opened it and it has nothing unsaved. Returns why not, or None.

    documents.open() on an already-open DataFile hands back the SAME Document, so an
    unguarded close(False) here shuts the owner's session and throws away their unsaved
    work. Nothing this script produces is worth that: a document left open costs one extra
    tab, and the next run skips it anyway.
    """
    try:
        name = doc.name
    except Exception:
        name = None
    if name is not None and name in _PRE_OPEN:
        return "was already open before this run (the user's)"
    try:
        saved = doc.isSaved
    except Exception:
        saved = None
    if saved is False:
        return "has unsaved changes"
    try:
        doc.close(False)
    except Exception as e:
        return "close failed: %s" % e
    return None


def profile_picks(prof):
    """Each profile as a point inside it, in its own sketch's 2D coordinates.

    Tools re-find a profile by a point inside it, which keeps following the right region when
    parameters change -- far more robust than a stored index.

    Returns (picks, unmapped). `unmapped` holds one `unmapped_pick` record per profile that
    could not be read, so a step that loses its selection says what it lost instead of going
    silent. Callers wanting only the picks take [0].
    """
    out, unmapped = [], []
    for p in as_items(prof):
        try:
            sk = p.parentSketch
            centroid = p.areaProperties().centroid
            local = sk.modelToSketchSpace(centroid)
            rec = {"sketch": get(sk, "name"), "point": pt2(local)}
            # The 2D point alone has proved unreliable off the XY plane: every profile of the
            # a sketch built on XZ comes back with y exactly 0, and sibling profiles on one
            # sketch all report an identical point. Whatever the conversion does, the centroid
            # in model space is unambiguous -- it needs no sketch frame to interpret, and a
            # consumer can project it onto the sketch plane itself.
            #
            # Still a centroid, so for an annulus or a crescent it lies outside its own
            # region and is a bearing rather than a pick. Recorded as what it is.
            rec["centroid_model"] = pt(centroid)
            try:
                a = p.areaProperties()
                rec["area_mm2"] = round(a.area * MM * MM, 6)
            except Exception:
                pass
            # The region's own extent, which bounds where a usable interior point can be and
            # distinguishes two regions that happen to share an area.
            try:
                bb = p.boundingBox
                rec["bbox_model"] = [pt(bb.minPoint), pt(bb.maxPoint)]
            except Exception:
                pass
            if rec["point"] or rec.get("centroid_model"):
                out.append(rec)
            else:
                unmapped.append(unmapped_pick(p, "no point and no centroid"))
        except Exception as e:
            unmapped.append(unmapped_pick(p, e))
            continue
    return out, unmapped


# ---------------------------------------------------------------------------
# sketches
# ---------------------------------------------------------------------------

def sketch_curves(sk):
    out = []
    try:
        curves = sk.sketchCurves
    except Exception:
        return out

    for coll, kind in (
        (get(curves, "sketchLines"), "line"),
        (get(curves, "sketchCircles"), "circle"),
        (get(curves, "sketchArcs"), "arc"),
        (get(curves, "sketchEllipses"), "ellipse"),
        (get(curves, "sketchFittedSplines"), "fitted_spline"),
        (get(curves, "sketchFixedSplines"), "fixed_spline"),
        (get(curves, "sketchControlPointSplines"), "control_spline"),
        (get(curves, "sketchConicCurves"), "conic"),
    ):
        if coll is None:
            continue
        for i in range(coll.count):
            c = coll.item(i)
            rec = {"type": kind, "construction": bool(get(c, "isConstruction"))}
            tok = token_of(c)
            if tok:
                rec["token"] = tok
            def pt_tok(rec, c, attr, key):
                """Record a defining point's position AND its token.

                A linear dimension is attached to sketch POINTS, not to the curve. Recording the
                position without the token is what made every size dimension unusable: the
                recipe knew where the point was and could not tell which dimension meant it.
                """
                try:
                    p = getattr(c, attr)
                except Exception:
                    return
                try:
                    rec[key] = pt2(p.geometry)
                except Exception:
                    pass
                t = token_of(p)
                if t:
                    rec.setdefault("point_tokens", {})[key] = t

            try:
                if kind == "line":
                    pt_tok(rec, c, "startSketchPoint", "start")
                    pt_tok(rec, c, "endSketchPoint", "end")
                elif kind == "circle":
                    pt_tok(rec, c, "centerSketchPoint", "center")
                    rec["radius_mm"] = round(c.radius * MM, 9)
                elif kind == "arc":
                    pt_tok(rec, c, "centerSketchPoint", "center")
                    pt_tok(rec, c, "startSketchPoint", "start")
                    pt_tok(rec, c, "endSketchPoint", "end")
                    rec["radius_mm"] = round(c.radius * MM, 9)
                elif kind == "ellipse":
                    # CENTRE ALONE IS NOT AN ELLIPSE. Recording only the centre meant the curve
                    # was never drawn in the rebuild, so the regions its arc bounds never
                    # formed: a profile sketch holding a single ellipse loses every region that
                    # ellipse bounds, so the rebuild finds fewer regions than Fusion reports.
                    # The thin slivers left over resolved to nothing, which looked convincingly
                    # like a region finder that would not divide -- it was a boundary that was
                    # never there. `sketch.ellipse` wants centre, the major-axis end, and a
                    # minor radius, so record exactly those.
                    pt_tok(rec, c, "centerSketchPoint", "center")
                    for key, names in (("major_radius_mm", ("majorAxisRadius",)),
                                       ("minor_radius_mm", ("minorAxisRadius",))):
                        v = get(c, *names)
                        if isinstance(v, float):
                            rec[key] = round(v * MM, 9)
                    # The major axis as a direction, and the axis END as a 2D sketch point --
                    # what the rebuild actually selects with.
                    ax = get(c, "majorAxis")
                    ctr = rec.get("center")
                    if ax is not None and ctr and rec.get("major_radius_mm"):
                        try:
                            n = (ax.x * ax.x + ax.y * ax.y + ax.z * ax.z) ** 0.5
                        except Exception:
                            n = 0.0
                        if n > 0:
                            # majorAxis is a model-space vector; the sketch's own frame is what
                            # `center` is in, so project it the same way a sketch point is.
                            try:
                                sk_ax = sk.modelToSketchSpace(adsk.core.Point3D.create(
                                    ax.x / n * (rec["major_radius_mm"] / MM), ax.y / n *
                                    (rec["major_radius_mm"] / MM), ax.z / n *
                                    (rec["major_radius_mm"] / MM)))
                                org = sk.modelToSketchSpace(adsk.core.Point3D.create(0, 0, 0))
                                rec["major_end"] = [round(ctr[0] + (sk_ax.x - org.x) * MM, 9),
                                                    round(ctr[1] + (sk_ax.y - org.y) * MM, 9)]
                            except Exception:
                                pass
                    if not rec.get("major_radius_mm"):
                        # Never leave a curve recorded as a bare point without saying so.
                        rec["geometry_missing"] = True
                    # SELF-CHECK. A record that contradicts itself must say so in the record;
                    # a consumer cannot be expected to re-derive what the extractor knew.
                    _ma = rec.get("major_radius_mm")
                    _mi = rec.get("minor_radius_mm")
                    if isinstance(_ma, float) and isinstance(_mi, float) and _ma < _mi:
                        # The major semi-axis is by definition the larger one, so the two
                        # magnitudes are right and the LABELS are not: whichever field a
                        # consumer reads as "major" will be the wrong axis.
                        rec["axis_labels_suspect"] = True
                        rec["larger_semi_axis_mm"] = max(_ma, _mi)
                        rec["smaller_semi_axis_mm"] = min(_ma, _mi)
                    _me, _ctr = rec.get("major_end"), rec.get("center")
                    if _me and _ctr and isinstance(_ma, float):
                        _d = ((_me[0] - _ctr[0]) ** 2 + (_me[1] - _ctr[1]) ** 2) ** 0.5
                        if abs(_d - _ma) > 1e-6:
                            # Degenerate when _d is ~0: the axis endpoint sits ON the centre,
                            # so the curve has magnitudes but NO ORIENTATION. Harmless where
                            # the axes are the sketch axes; silently wrong on a rotated one.
                            rec["orientation_unrecorded"] = True
                            rec["major_end_length_mm"] = round(_d, 9)
                else:
                    # Splines and conics: record the defining points so the shape can be
                    # rebuilt even where the tool has no identical curve type.
                    fp = get(c, "fitPoints", "controlPoints")
                    if fp is not None:
                        rec["points"] = [pt2(fp.item(k).geometry) for k in range(fp.count)]
            except Exception as e:
                rec["read_error"] = str(e)
            out.append(rec)
    return out


# Where Fusion's constraints and dimensions keep the things they act on. Each subclass names
# them differently and there is no common accessor, so probe the documented names.
REF_ATTRS = (
    "line", "lineOne", "lineTwo", "curveOne", "curveTwo", "entityOne", "entityTwo",
    "entity", "point", "pointOne", "pointTwo", "midPointCurve", "symmetryLine",
    "circle", "arc", "parentCurve",
)


def token_of(obj):
    """A stable identity for a sketch entity, used to tie a constraint back to its curves."""
    try:
        t = obj.entityToken
        return t or None
    except Exception:
        return None


REF_COLLECTIONS = ("parentCurves", "childCurves", "curves", "entities", "points")


def refs_unmapped(obj):
    """The attribute names an operand-less constraint or dimension actually exposes.

    Recorded only when nothing else resolved, so a run says what to read next instead of leaving
    the next guess to be paid for with another extraction -- the same reason profile picks record
    why they could not be mapped.
    """
    out = []
    for a in dir(obj):
        if a.startswith("_"):
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if callable(v) or v is None:
            continue
        out.append("%s:%s" % (a, type(v).__name__))
    return out[:40]


def refs_of(obj):
    """Tokens of whatever this constraint or dimension is attached to.

    A constraint recorded without its operands is unusable -- it says a relationship existed but
    not between what -- which is why these were captured but could never be rebuilt before.
    """
    out = []
    for attr in REF_ATTRS:
        try:
            v = getattr(obj, attr)
        except Exception:
            continue
        if v is None:
            continue
        t = token_of(v)
        if t and t not in out:
            out.append(t)
    # Operands held in a collection rather than a single-valued attribute.
    for attr in REF_COLLECTIONS:
        try:
            coll = getattr(obj, attr)
            cnt = coll.count
        except Exception:
            continue
        for i in range(cnt):
            try:
                t = token_of(coll.item(i))
            except Exception:
                continue
            if t and t not in out:
                out.append(t)
    return out


def sketch_constraints(sk):
    out = []
    try:
        cons = sk.geometricConstraints
    except Exception:
        return out
    for i in range(cons.count):
        c = cons.item(i)
        name = type(c).__name__
        name = re.sub(r"^.*?([A-Za-z]+)Constraint$", r"\1", name) or name
        rec = {"type": name.lower()}
        refs = refs_of(c)
        if refs:
            rec["refs"] = refs
        out.append(rec)
    return out


def sketch_dimensions(sk):
    out = []
    try:
        dims = sk.sketchDimensions
    except Exception:
        return out
    for i in range(dims.count):
        d = dims.item(i)
        rec = {"type": re.sub(r"^Sketch|Dimension$", "", type(d).__name__).lower()}
        try:
            p = d.parameter
            rec["expression"] = p.expression
            rec["value_mm"] = round(p.value * MM, 9)
            rec["name"] = p.name
        except Exception:
            pass
        refs = refs_of(d)
        if refs:
            rec["refs"] = refs
        else:
            rec["exposes"] = refs_unmapped(d)
        out.append(rec)
    return out


def sketch_points(sk):
    """Every sketch point with its token and position.

    Curve endpoints are covered on the curves themselves; this also catches the sketch origin
    and any free point, which is what a dimension measuring from the origin refers to.
    """
    out = []
    try:
        pts = sk.sketchPoints
    except Exception:
        return out
    for i in range(pts.count):
        p = pts.item(i)
        rec = {}
        t = token_of(p)
        if t:
            rec["token"] = t
        try:
            rec["at"] = pt2(p.geometry)
        except Exception:
            pass
        try:
            rec["construction"] = bool(get(p, "isConstruction"))
        except Exception:
            pass
        if rec:
            out.append(rec)
    return out


def describe_sketch(sk):
    rec = {
        "name": get(sk, "name"),
        "plane": None,
        "is_visible": bool(get(sk, "isVisible")),
    }
    try:
        rec["is_fully_constrained"] = bool(sk.isFullyConstrained)
    except Exception:
        pass
    try:
        ref = sk.referencePlane
        rec["plane"] = {"name": get(ref, "name"), "kind": type(ref).__name__}
    except Exception:
        pass
    try:
        t = sk.transform.asArray()
        rec["transform"] = [round(v, 9) for v in t]
    except Exception:
        pass
    if SKETCH_GEOMETRY:
        rec["curves"] = sketch_curves(sk)
        rec["points"] = sketch_points(sk)
        rec["constraints"] = sketch_constraints(sk)
        rec["dimensions"] = sketch_dimensions(sk)
        try:
            rec["profile_count"] = sk.profiles.count
        except Exception:
            pass
    return rec


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


def describe_occurrence(ent):
    """A placement of another design: which component, and where.

    This is what an assembly is made of, and the largest unmapped group in a real hub. The
    component's name is what ties it to the design that was migrated separately; the transform
    is where it sits.
    """
    rec = {}
    comp = get(ent, "component")
    if comp is not None:
        rec["component"] = get(comp, "name")
    for key, attr in (("full_path", "fullPathName"), ("referenced", "isReferencedComponent"),
                      ("grounded", "isGrounded"), ("visible", "isLightBulbOn")):
        v = get(ent, attr)
        if v is not None and not hasattr(v, "objectType"):
            rec[key] = v if isinstance(v, (bool, str)) else str(v)
    # The live placement first, falling back to the one it was created with.
    for attr in ("transform", "transform2", "initialTransform"):
        m = get(ent, attr)
        if m is None:
            continue
        try:
            rec["transform"] = [round(v, 9) for v in m.asArray()]
            break
        except Exception:
            continue
    try:
        rec["bodies"] = ent.bRepBodies.count
    except Exception:
        pass
    # A document reference names the design this placement came from.
    ref = get(ent, "documentReference")
    if ref is not None:
        for key, attr in (("ref_name", "referencedFileName"), ("ref_id", "id")):
            v = get(ref, attr)
            if isinstance(v, str):
                rec[key] = v
    return rec


def _face_profiles(ent):
    """An extrude's profiles when they are model FACES rather than sketch regions.

    Call inside rolled_back_to(): the faces an extrude is built on are consumed by it, so on
    the finished body they are gone. Point, area and normal -- enough for another tool to pick
    the same face. The API's `area` is cm2; everything written here is mm2, like the rest of
    this file.
    """
    out = []
    for f in as_items(get(ent, "profile")):
        if type(f).__name__ != "BRepFace":
            continue
        q = point_on(f, "pointOnFace")
        if not q:
            continue
        r = {"point": q}
        a = get(f, "area")
        if isinstance(a, float):
            r["area_mm2"] = round(a * MM * MM, 6)
        g = get(f, "geometry", "worldGeometry")
        if g is not None:
            r["surface"] = type(g).__name__
            n = get(g, "normal")
            if n is not None:
                try:
                    r["normal"] = [round(n.x, 9), round(n.y, 9), round(n.z, 9)]
                except Exception:
                    pass
        out.append(r)
    return out


def _draft_faces(ent):
    """The faces a draft or shell was given -- as POINTS and as full SIGNATURES.

    A point is not a face. Fusion's tool selects BRepFace objects; recording one point per face
    forces the consumer to work backwards from a coordinate, and that resolution is lossy -- a
    point can sit on an edge, name two faces at once ("`F3:face:42` names more than one face"),
    or land on a face a later step splits in half.

    Measured across a production part: (area, centroid) proved unique for every face on both
    bodies, so a signature identifies a face exactly where a point only
    hints at one. `solid.draft` takes face NAMES as well as points -- it says so itself,
    "`faces` must be a list of points or face names" -- so a consumer holding a signature can
    resolve it against model.faces and select the face, instead of probing geometry.

    The tangent chain is recorded per face for the same reason: every draft on that part has
    isTangentChain True, Fusion silently extends the selection along it, and solid.draft has no
    equivalent option, so a rebuild can only match the real selection if the chain travels with
    the pick.

    Called inside rolled_back_to(), so `ent`'s inputs exist. Returns the `tried` list even on
    failure: a selection that could not be read must say what was looked at.
    """
    coll, src, tried = first_nonempty(ent, "inputFaces", "faces")
    if coll is None:
        return {"points": [], "sigs": [], "source": None, "tried": tried}

    def _sig(f):
        sig = {}
        q = point_on(f, "pointOnFace")
        if q:
            sig["point"] = q
        a = get(f, "area")
        if isinstance(a, float):
            sig["area_mm2"] = round(a * MM * MM, 6)
        c = get(f, "centroid")
        if c is not None:
            try:
                sig["centroid"] = [round(c.x * MM, 6), round(c.y * MM, 6), round(c.z * MM, 6)]
            except Exception:
                pass
        g = get(f, "geometry", "worldGeometry")
        if g is not None:
            sig["surface"] = type(g).__name__
            n = get(g, "normal")
            if n is not None:
                try:
                    sig["normal"] = [round(n.x, 9), round(n.y, 9), round(n.z, 9)]
                except Exception:
                    pass
        return sig

    sigs = []
    for f in as_items(coll):
        sig = _sig(f)
        tc = get(f, "tangentiallyConnectedFaces")
        if tc is not None:
            chain = [c for c in (_sig(g2) for g2 in as_items(tc)) if c]
            if chain:
                sig["tangent_chain_faces"] = chain
        sigs.append(sig)
    return {"points": [s["point"] for s in sigs if s.get("point")],
            "sigs": sigs, "source": src, "tried": tried}


def describe_draft(ent):
    """A draft keeps its angle in draftDefinition, not on the feature itself.

    Read this rolled back wherever possible. The neutral plane is a BRepFace of the model
    (often a sliver face of a couple of mm2), and on the finished body it does not resolve at
    all -- `plane` comes back None and the record silently loses the one thing a rebuild needs
    to orient the draft.
    """
    rec = {}
    for holder in (get(ent, "draftDefinition"), ent):
        if holder is None:
            continue
        a = expr_of(holder, "angle", "draftAngle", "angleOne")
        if a:
            rec["angle"] = a
            break
    plane = get(ent, "plane")
    if plane is not None:
        q = point_on(plane, "pointOnFace")
        if q:
            rec["neutral_point"] = q
        # The pull is the neutral face's own normal, so record it rather than making the
        # consumer re-derive it from a point.
        g = get(plane, "geometry", "worldGeometry")
        n = get(g, "normal") if g is not None else None
        if n is not None:
            try:
                rec["neutral_normal"] = [round(n.x, 9), round(n.y, 9), round(n.z, 9)]
            except Exception:
                pass
        a = get(plane, "area")
        if isinstance(a, float):
            rec["neutral_area_mm2"] = round(a * MM * MM, 6)
    for key, attr in (("draft_type", "draftType"), ("flipped", "isDirectionFlipped"),
                      ("tangent_chain", "isTangentChain"),
                      ("parting_line_type", "partingLineType")):
        v = get(ent, attr)
        if v is not None and not hasattr(v, "objectType"):
            rec[key] = str(v)
    got = _draft_faces(ent)
    if got["points"]:
        rec["face_points"] = got["points"]
        rec["face_pick_source"] = got["source"]
    # SCHEMA 16: the signatures, which are what actually identify the faces. `face_points` is
    # kept so older consumers keep working, but a rebuild should prefer these -- (area,
    # centroid) is a unique key per body and resolves to a face NAME, which solid.draft
    # accepts directly.
    if got.get("sigs"):
        rec["face_sigs"] = got["sigs"]
    # Which bodies the draft was applied to. On this part `bodies` is populated only when
    # rolled back, and a body draft is a different rebuild from a face draft -- it tilts every
    # qualifying face rather than a named set, so the distinction has to survive.
    bcoll, bsrc, _ = first_nonempty(ent, "bodies", "inputBodies")
    if bcoll is not None:
        items = as_items(bcoll)
        rec["body_count"] = len(items)
        rec["body_source"] = bsrc
        areas = []
        for b in items:
            a = get(b, "area")
            if isinstance(a, float):
                areas.append(round(a * MM * MM, 6))
        if areas:
            rec["body_areas_mm2"] = areas
    return rec


def matrix_of(ent, *names):
    for n in names:
        m = get(ent, n)
        if m is None:
            continue
        try:
            return [round(v, 9) for v in m.asArray()]
        except Exception:
            continue
    return None


def describe_feature(ent, index=None):
    """Pull the parametric intent out of one timeline entity."""
    kind = type(ent).__name__
    rec = {"entity": kind, "name": get(ent, "name")}

    # The driving expressions, wherever this feature keeps them.
    for key, names in (
        ("distance", ("distanceOne", "distance")),
        ("distance_two", ("distanceTwo",)),
        ("taper", ("taperAngleOne", "taperAngle")),
        ("angle", ("angle", "draftAngle", "angleOne")),
        ("radius", ("radius",)),
        ("thickness", ("insideThickness", "thickness")),
    ):
        v = expr_of(ent, *names)
        if v:
            rec[key] = v

    # Extent / operation style, which decides how the feature is re-created.
    for key, names in (
        ("operation", ("operation",)),
        ("extent_type", ("extentType",)),
        ("is_symmetric", ("isSymmetric",)),
        ("direction", ("extentDirection",)),
    ):
        v = get(ent, *names)
        if v is not None and not hasattr(v, "objectType"):
            rec[key] = str(v)

    # The *second* side of a two-sided extrude. Reading only extentOne silently halves the
    # feature: a body 10 mm up and 30 mm back from an offset start, so taking extentOne
    # alone builds it 10 mm tall from the sketch plane instead of 40 mm spanning
    # z 0..40 -- the right expression, the wrong solid, and nothing in the record says so.
    ext2 = get(ent, "extentTwo")
    if ext2 is not None:
        rec["two_sided"] = True
        rec["extent_two_definition"] = type(ext2).__name__
        d2 = expr_of(ext2, "distance")
        if d2:
            rec["distance_two"] = d2
        off2 = expr_of(ext2, "offset")
        if off2:
            rec["to_offset_two"] = off2

    # Extrudes keep their extent in a definition object on newer builds.
    ext = get(ent, "extentOne", "extentDefinition")
    if ext is not None:
        d = expr_of(ext, "distance")
        if d:
            rec.setdefault("distance", d)
        rec["extent_definition"] = type(ext).__name__
        # "To object" extrudes have no distance at all -- they run until they reach something.
        # A point on that target is enough to say the same thing elsewhere, and unlike a measured
        # distance it keeps the intent: the extrude still lands on the face, not near where the
        # face used to be.
        target = get(ext, "entity", "toEntity", "target")
        if target is not None:
            q = point_on(target, "pointOnFace") or point_on(target, "pointOnEdge")
            if q is None:
                g = get(target, "geometry", "worldGeometry")
                q = pt(g) if g is not None else None
            if q:
                rec["to_point"] = q
        off = expr_of(ext, "offset")
        if off:
            rec["to_offset"] = off

        # Read rolled back when the finished body could not answer. A "to object" extrude
        # terminates on a FACE, and later features reshape or remove that face -- on
        # a to-object extrude runs to the top face of an earlier body, which later cuts
        # have since carved into, so `ext.entity` resolves to nothing by the end
        # and the step recorded an extent type with no target at all. The replay then had
        # no distance and no face: that step DID NOT BUILD, and every later step
        # inherited a negative volume. Same failure shape as the drafts above: the question was
        # asked of the wrong state, not of the wrong attribute.
        if "ToEntity" in rec["extent_definition"] and not rec.get("to_point"):
            def _late():
                e2 = get(ent, "extentOne", "extentDefinition")
                if e2 is None:
                    return None
                t2 = get(e2, "entity", "toEntity", "target")
                if t2 is None:
                    return None
                p2 = point_on(t2, "pointOnFace") or point_on(t2, "pointOnEdge")
                if p2 is None:
                    g2 = get(t2, "geometry", "worldGeometry")
                    p2 = pt(g2) if g2 is not None else None
                if not p2:
                    return None
                out = {"point": p2, "target_type": type(t2).__name__}
                g2 = get(t2, "geometry", "worldGeometry")
                if g2 is not None:
                    out["target_surface"] = type(g2).__name__
                    n2 = get(g2, "normal")
                    if n2 is not None:
                        try:
                            out["target_normal"] = [round(n2.x, 9), round(n2.y, 9),
                                                    round(n2.z, 9)]
                        except Exception:
                            pass
                return out
            late = rolled_back_to(index, _late)
            if late:
                rec["to_point"] = late["point"]
                rec["to_target_type"] = late.get("target_type")
                if late.get("target_surface"):
                    rec["to_target_surface"] = late["target_surface"]
                if late.get("target_normal"):
                    rec["to_target_normal"] = late["target_normal"]
                rec["to_point_via_rollback"] = True
            else:
                # An extent type that says "runs to something" with no something recorded is
                # exactly the silent gap this file exists to stop. Say so in the record.
                rec["to_point_missing"] = True

    # Where the extrude *starts*. Without this a feature lands on the sketch plane, which can
    # be the wrong place while still producing exactly the right volume -- a fill feature
    # adds the same volume whether it sits at z=0 or at an offset, so no volume check catches
    # it. What does catch it is the face it should have widened: that top face measures far
    # less when the fill lands low than when it lands where it belongs, and the chamfer that
    # follows then has nothing wide enough to cut into.
    start = get(ent, "startExtent", "startDefinition")
    if start is not None:
        rec["start_definition"] = type(start).__name__
        s_off = expr_of(start, "offset")
        if s_off:
            rec["start_offset"] = s_off
        s_target = get(start, "entity", "face", "target")
        if s_target is not None:
            q = point_on(s_target, "pointOnFace") or point_on(s_target, "pointOnEdge")
            if q is None:
                g = get(s_target, "geometry", "worldGeometry")
                q = pt(g) if g is not None else None
            if q:
                rec["start_point"] = q

    # What the feature was applied to. Fusion's internal edge and face ids mean nothing in
    # another tool, but a *point lying on* each edge or face is geometry, and geometry travels.
    # Most CAD tools (SolidCraft included) select exactly that way, so this is the bridge that
    # lets a fillet or a shell be re-created rather than merely reported as missing.
    sets = edge_sets(ent)
    if not sets and ("Fillet" in kind or "Chamfer" in kind):
        # Its own operation removed these edges, so they cannot be found in the finished body.
        sets = rolled_back_to(index, lambda: edge_sets(ent)) or []
        if sets:
            rec["edges_via_rollback"] = True
    if sets:
        rec["edge_sets"] = sets
        rec["edge_count"] = sum(len(s["edge_points"]) for s in sets)
        # Flattened too, so a consumer that only wants "every edge" need not walk the sets.
        rec["edge_points"] = [p for s in sets for p in s["edge_points"]]
    else:
        edges = get(ent, "edges")
        if edges is not None:
            rec["edge_count"] = count_of(edges)
        if "Fillet" in kind or "Chamfer" in kind:
            rec["edge_points_missing"] = True
            sizes = rolled_back_to(index, lambda: [
                {k: v for k, v in st.items() if k != "edge_points"} for st in edge_sets(ent)])
            if sizes:
                rec["edge_set_sizes"] = sizes

    # `inputFaces` BEFORE `faces`, and by whether it holds anything rather than whether it is
    # not None. A DraftFeature's `faces` is an empty-but-not-None BRepFaces, so the old
    # `get(ent, "faces", "inputFaces")` always answered the empty one and never looked at
    # inputFaces, where the picks are. Measured on a production design: every draft recorded
    # `face_count: 0` while every one of them had a populated `inputFaces` BRepFaceVector.
    faces, src, tried = first_nonempty(ent, "inputFaces", "faces")
    if faces is not None:
        pts = picks(faces, "pointOnFace")
        rec["face_count"] = len(pts) if pts else count_of(faces)
        if pts:
            rec["face_points"] = pts
            rec["face_pick_source"] = src

    if "Draft" in kind or "Shell" in kind:
        # ALWAYS roll these back, even when the finished body did answer. The feature reshaped
        # the faces it was given, so a point read afterwards lies on the tilted face, not the
        # one that was picked -- a fraction of a millimetre off on a shallow draft, enough to select
        # a neighbour. The rolled-back answer is the one the feature actually saw, so it wins.
        back = rolled_back_to(index, lambda: _draft_faces(ent))
        if back and back.get("points"):
            if rec.get("face_points") and back["points"] != rec["face_points"]:
                rec["face_points_final"] = rec["face_points"]   # kept, so the shift is visible
            rec["face_points"] = back["points"]
            rec["face_count"] = len(back["points"])
            rec["faces_via_rollback"] = True
            rec["face_pick_source"] = back.get("source")
            # The signatures must come from the SAME rolled-back read as the points, for the
            # reason above: after the feature runs, the faces have moved.
            if back.get("sigs"):
                rec["face_sigs"] = back["sigs"]
        elif not rec.get("face_points"):
            # Nothing, either way. Say what was looked at -- an empty selection that reports
            # success is the failure mode this whole block exists to stop.
            rec["face_points_missing"] = True
            rec["face_pick_tried"] = (back or {}).get("tried") or tried

    prof = get(ent, "profile")
    if prof is not None:
        # The feature's OWN profile count, always -- never the number that happened to be
        # recorded. profile_picks drops any profile whose point it cannot compute, and taking
        # the count from its result made a partial record indistinguishable from a complete
        # one: a rebuild then selects too few regions and the extrude comes out light with
        # nothing saying why.
        info, unmapped = profile_picks(prof)
        rec["profile_count"] = count_of(prof)
        if info:
            rec["profiles"] = info
            rec["profiles_recorded"] = len(info)
            if rec["profile_count"] and len(info) != rec["profile_count"]:
                rec["profiles_incomplete"] = True
        # Whatever could NOT be read. Written whether or not anything else was: a step with
        # some picks and some unmapped ones is the case most likely to be misread as complete.
        if unmapped:
            rec["profiles_unmapped"] = unmapped
            # A profile that is a FACE, not a sketch region, has no sketch to take a 2D point
            # in, so profile_picks drops it. It still has to be selectable, and a point on the
            # face plus its normal is how another tool finds it. Read rolled back: the faces an
            # extrude was built ON are consumed by it, and `ent.faces` on the finished part is
            # the faces the feature CREATED -- a different set entirely, and reading it here is
            # what made a face-based extrude select nothing while reporting a profile count.
            if any((u.get("type") == "BRepFace") for u in unmapped):
                pf = rolled_back_to(index, lambda: _face_profiles(ent))
                if pf:
                    rec["profile_faces"] = pf
                    rec["profile_faces_recorded"] = len(pf)
                else:
                    rec["profile_faces_missing"] = True

    try:
        rec["suppressed"] = bool(ent.isSuppressed)
    except Exception:
        pass

    # Feature-specific geometry that would otherwise be unrecoverable.
    if "Revolve" in kind:
        a = axis_of(ent)
        if a:
            rec["axis"] = a
    if "Sweep" in kind:
        p = path_of(ent)
        if p:
            rec["path_points"] = p
    if "Thread" in kind:
        info = {}
        for key, names in (("designation", ("threadDesignation",)),
                           ("type", ("threadType",)),
                           ("class", ("threadClass",)),
                           ("is_internal", ("isInternal",)),
                           ("full_length", ("isFullLength",))):
            v = get(ent, *names)
            if v is not None and not hasattr(v, "objectType"):
                info[key] = str(v)
        d = expr_of(ent, "threadLength")
        if d:
            info["length"] = d
        if info:
            rec["thread"] = info

    if "Draft" in kind and not rec.get("angle"):
        a = rolled_back_to(index, lambda: expr_of(ent, "angle", "draftAngle", "angleOne"))
        if a:
            rec["angle"] = a

    # Sketches carry their whole contents.
    if kind == "Sketch":
        rec["sketch"] = describe_sketch(ent)

    if kind == "Occurrence":
        rec.update(describe_occurrence(ent))
    elif "Draft" in kind:
        # ALWAYS merge the rolled-back reading, not only when the angle is missing. On the
        # finished body a draft answers its angle happily while `plane` and `bodies` come back
        # None, so keying the rollback off the angle meant the neutral plane -- which is what
        # orients the whole feature -- was never recorded on any draft of this part.
        d = describe_draft(ent)
        back = rolled_back_to(index, lambda: describe_draft(ent)) or {}
        for k, v in back.items():
            if k not in d:
                d[k] = v
                d["draft_via_rollback"] = True
        rec.update(d)
    elif kind == "MoveFeature":
        m = matrix_of(ent, "transform")
        if m:
            rec["transform"] = m
    elif "Thread" in kind:
        info = get(ent, "threadInfo")
        if info is not None:
            t = rec.setdefault("thread", {})
            for key, attr in (("designation", "threadDesignation"), ("size", "threadSize"),
                              ("type", "threadType"), ("class", "threadClass"),
                              ("pitch", "threadPitch")):
                v = get(info, attr)
                if isinstance(v, (str, int, float)):
                    t[key] = v
        for key, names in (("length", ("threadLength",)), ("offset", ("threadOffset",))):
            v = expr_of(ent, *names)
            if v:
                rec.setdefault("thread", {})[key] = v
        pts = picks(get(ent, "inputCylindricalFaces") or [], "pointOnFace")
        if not pts:
            one = get(ent, "inputCylindricalFace")
            q = point_on(one, "pointOnFace") if one is not None else None
            pts = [q] if q else []
        if pts:
            rec["thread_faces"] = pts
    elif kind == "CombineFeature":
        tools = get(ent, "toolBodies")
        if tools is not None:
            rec["tool_body_count"] = count_of(tools)
        v = get(ent, "isKeepToolBodies")
        if v is not None:
            rec["keep_tools"] = bool(v)
    elif "Emboss" in kind:
        d = expr_of(ent, "depth")
        if d:
            rec["depth"] = d

    if PROBE_UNMAPPED and kind not in MAPPED_KINDS:
        rec["probe"] = probe(ent)

    return rec



# Feature kinds we already translate; anything else gets probed instead.
MAPPED_KINDS = {
    "Sketch", "ExtrudeFeature", "FilletFeature", "ChamferFeature", "ShellFeature",
    "RevolveFeature", "OffsetFacesFeature", "Snapshot", "Canvas",
    "ConstructionPlane", "ConstructionAxis", "ConstructionPoint",
}
# Occurrence is deliberately absent: assemblies are the largest unmapped group here, and an
# occurrence is a placement of *another design*, so what it exposes -- which design, and where --
# is exactly what needs probing.

# Never call these: they recompute, mutate, or walk the whole model.
PROBE_SKIP = ("delete", "save", "compute", "update", "parent", "nativeObject", "createForAssembly",
              "timelineObject", "healthState", "errorOrWarningMessage", "attributes")


def probe(ent):
    """Name every readable property of a feature, and say what kind of thing it holds.

    Values are deliberately not captured -- only shape -- so this stays cheap and cannot drag in
    half the document. It is enough to answer "where does a draft keep its angle", which is the
    question that has cost the most time here.
    """
    out = {}
    for name in dir(ent):
        if name.startswith("_") or any(k in name for k in PROBE_SKIP):
            continue
        try:
            v = getattr(ent, name)
        except Exception:
            continue
        if v is None or callable(v):
            continue
        try:
            tname = type(v).__name__
            if isinstance(v, (int, float, str, bool)):
                out[name] = "{}={}".format(tname, str(v)[:40])
            elif hasattr(v, "count"):
                out[name] = "{}[{}]".format(tname, v.count)
            elif hasattr(v, "expression"):
                out[name] = "{} expr={}".format(tname, str(v.expression)[:30])
            else:
                out[name] = tname
        except Exception:
            continue
        if len(out) >= 40:
            break
    return out


def describe_design(design, data_file, parts):
    doc = {
        "schema": SCHEMA,
        "name": get(data_file, "name"),
        "id": get(data_file, "id"),
        "version": get(data_file, "versionNumber"),
        "path": "/".join(parts),
        "units": None,
        "parameters": [],
        "timeline": [],
        "components": [],
        "stats": {},
    }
    try:
        doc["units"] = design.unitsManager.defaultLengthUnits
    except Exception:
        pass

    # Parameters first: the timeline's expressions refer to them by name.
    try:
        for i in range(design.userParameters.count):
            p = design.userParameters.item(i)
            doc["parameters"].append({
                "name": p.name,
                "expression": p.expression,
                "unit": get(p, "unit"),
                "value": round(p.value, 9),
                "comment": get(p, "comment") or "",
            })
    except Exception as e:
        doc["parameters_error"] = str(e)

    # User parameters routinely refer to *model* parameters -- Fusion's auto-named dimensions
    # like "d4" ("outerDiameter = d4 + 10 mm"). Those are driven by features rather than set
    # by hand, so they cannot be re-created as inputs elsewhere; capture their values so a
    # translator can at least resolve the reference instead of emitting an undefined name.
    try:
        user_names = {p["name"] for p in doc["parameters"]}
        model = []
        allp = design.allParameters
        for i in range(allp.count):
            p = allp.item(i)
            name = get(p, "name")
            if not name or name in user_names:
                continue
            model.append({
                "name": name,
                "expression": get(p, "expression"),
                "unit": get(p, "unit"),
                "value": round(p.value, 9),
            })
        doc["model_parameters"] = model
    except Exception as e:
        doc["model_parameters_error"] = str(e)

    try:
        for i in range(design.allComponents.count):
            c = design.allComponents.item(i)
            doc["components"].append({
                "name": get(c, "name"),
                "bodies": c.bRepBodies.count,
                "sketches": c.sketches.count,
                "features": c.features.count,
            })
    except Exception:
        pass

    unknown = {}
    try:
        tl = design.timeline
        _ROLLBACK["timeline"] = tl
        for i in range(tl.count):
            tobj = tl.item(i)
            rec = {"index": i}
            try:
                rec["name"] = tobj.name
            except Exception:
                pass
            try:
                rec["rolled_back"] = not tobj.isSnapshot and False
            except Exception:
                pass
            ent = None
            try:
                ent = tobj.entity
            except Exception as e:
                rec["entity_error"] = str(e)
            if ent is not None:
                try:
                    rec.update(describe_feature(ent, i))
                except Exception as e:
                    rec["describe_error"] = "{}: {}".format(type(e).__name__, e)
                    rec["entity"] = type(ent).__name__
            # What the model measures with the marker just after this step. This is the other
            # half of a lockstep check: a rebuild can roll its own timeline to the same
            # position and compare, which says *which* feature diverged instead of leaving a
            # wrong final volume with dozens of candidate causes.
            #
            # It is also the only reference that catches a feature built in the wrong place,
            # because the z extent moves even when the volume does not.
            if MEASURE_EACH_STEP:
                m = rolled_back_to(i + 1, lambda: measure(design))
                if m:
                    rec["state"] = m

            k = rec.get("entity", "unknown")
            unknown[k] = unknown.get(k, 0) + 1
            doc["timeline"].append(rec)
    except Exception as e:
        doc["timeline_error"] = str(e)

    doc["stats"] = {
        "timeline_steps": len(doc["timeline"]),
        "parameters": len(doc["parameters"]),
        "feature_types": unknown,
    }
    return doc


# ---------------------------------------------------------------------------

def collect(folder, rel_parts, found, stats=None):
    """Walk a folder for designs, recording what was seen so a miss can be explained."""
    if stats is None:
        stats = {}
    try:
        n = folder.dataFiles.count
    except Exception as e:
        stats.setdefault("folder_errors", []).append("{}: {}".format("/".join(rel_parts), e))
        return
    stats["folders"] = stats.get("folders", 0) + 1
    for i in range(n):
        try:
            f = folder.dataFiles.item(i)
        except Exception:
            continue
        stats["files"] = stats.get("files", 0) + 1
        try:
            ext = f.fileExtension
        except Exception as e:
            stats.setdefault("ext_errors", []).append(str(e))
            continue
        exts = stats.setdefault("extensions", {})
        exts[ext or "(none)"] = exts.get(ext or "(none)", 0) + 1
        if ext == "f3d":
            found.append((f, list(rel_parts)))
    try:
        for i in range(folder.dataFolders.count):
            sub = folder.dataFolders.item(i)
            collect(sub, rel_parts + [safe(sub.name)], found, stats)
    except Exception as e:
        stats.setdefault("folder_errors", []).append("{}: {}".format("/".join(rel_parts), e))


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        signed_in = getattr(app.data, "isSignedIn", None)
        if signed_in is False:
            ui.messageBox("Not signed in to Autodesk. Sign in, then run this again.")
            return

        try:
            hubs = []
            if ALL_HUBS:
                for i in range(app.data.dataHubs.count):
                    hubs.append(app.data.dataHubs.item(i))
            else:
                hubs.append(app.data.activeHub)
            hubs = [h for h in hubs if h is not None]
        except Exception as e:
            ui.messageBox("Could not reach your Autodesk hub ({}).\nSign in and try again.".format(e))
            return
        if not hubs:
            ui.messageBox("No Autodesk hub available. Sign in and try again.")
            return

        designs = []
        stats = {"hubs": [], "projects": 0, "project_errors": []}
        for hub in hubs:
            if len(hubs) > 1:
                try:
                    app.data.activeHub = hub
                except Exception:
                    pass
            adsk.doEvents()
            stats["hubs"].append(get(hub, "name") or "(unnamed)")
            try:
                projects = app.data.dataProjects
                pcount = projects.count
            except Exception as e:
                stats["project_errors"].append("listing projects: {}".format(e))
                continue
            stats["projects"] += pcount
            for p in range(pcount):
                try:
                    proj = projects.item(p)
                    root = proj.rootFolder
                except Exception as e:
                    stats["project_errors"].append("project {}: {}".format(p, e))
                    continue
                collect(root, [safe(hub.name), safe(get(proj, "name") or "project")],
                        designs, stats)

        if not designs:
            # Say what was actually seen. "No designs found" with no evidence is the kind of
            # answer that sends you looking in the wrong place.
            exts = stats.get("extensions") or {}
            lines = [
                "No Fusion designs (.f3d) found.",
                "",
                "hub(s):   {}".format(", ".join(stats["hubs"]) or "none"),
                "projects: {}".format(stats["projects"]),
                "folders:  {}".format(stats.get("folders", 0)),
                "files:    {}".format(stats.get("files", 0)),
                "file types seen: {}".format(
                    ", ".join("{} x{}".format(k, v) for k, v in sorted(exts.items()))
                    or "none"),
            ]
            for key, label in (("project_errors", "project errors"),
                               ("folder_errors", "folder errors"),
                               ("ext_errors", "file errors")):
                errs = stats.get(key) or []
                if errs:
                    lines += ["", "{}:".format(label)] + ["  " + e for e in errs[:5]]
                    if len(errs) > 5:
                        lines.append("  ... and {} more".format(len(errs) - 5))
            if stats["projects"] == 0:
                lines += ["", "No projects at all usually means the hub list has not loaded.",
                          "Open the Data Panel in Fusion once, then run this again."]
            ui.messageBox("\n".join(lines))
            return

        found_all = len(designs)
        sel = (SELECT or "all").strip()
        key = sel.lower()
        if key == "all":
            pass
        elif key == "list":
            wanted = set()
            if SELECT_LIST and os.path.exists(SELECT_LIST):
                try:
                    with open(SELECT_LIST, encoding="utf-8") as fh:
                        wanted = {ln.strip().replace("\\", "/").lower()
                                  for ln in fh if ln.strip()}
                except Exception:
                    wanted = set()
            if not wanted:
                ui.messageBox('SELECT = "list", but {} is missing or empty.'.format(SELECT_LIST))
                return
            designs = [(f, p) for (f, p) in designs
                       if "/".join(p + [safe(get(f, "name") or "")]).lower() in wanted]
        elif key in GROUPS:
            match = GROUPS[key]
            designs = [(f, p) for (f, p) in designs if match(get(f, "name") or "", list(p))]
        else:
            designs = [(f, p) for (f, p) in designs
                       if key in (get(f, "name") or "").lower()
                       or key in "/".join(p).lower()]

        # Any branch above can come back empty, and an empty set used to reach
        # progress.show(..., 0, 0), which dies with "invalid argument minimumValue or
        # maximumValue" -- a crash that reads like a Fusion bug instead of "nothing matched".
        if not designs:
            ui.messageBox('SELECT = "{}" matched none of the {} designs found.'.format(
                SELECT, found_all))
            return

        if LIMIT and LIMIT > 0:
            designs = designs[:LIMIT]

        # Scope is reported in the progress dialog's title, never in a message box. This script
        # exists to be started and left alone, and a modal here blocks the whole run until
        # someone comes back and clicks it -- which is the one thing an unattended batch job
        # must never do. Modal dialogs below are only ever at the very end.
        scope = "{} of {} designs".format(len(designs), found_all) \
            if len(designs) != found_all else "{} designs".format(found_all)

        os.makedirs(OUT_DIR, exist_ok=True)
        # Do this BEFORE opening anything, or our own documents land in the set and never
        # get closed.
        remember_open_documents(app)
        total = len(designs)
        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        progress.show("Extracting timelines - " + scope, "%v / %m   %p%", 0, total, 0)

        started = time.time()
        ok = skipped = failed = 0
        summary = []

        for idx, (data_file, parts) in enumerate(designs):
            if progress.wasCancelled:
                break
            progress.progressValue = idx
            progress.message = "%v / %m   %p%\n" + data_file.name[:60]
            adsk.doEvents()

            out_dir = os.path.join(OUT_DIR, *parts)
            os.makedirs(out_dir, exist_ok=True)
            dest = os.path.join(out_dir, safe(data_file.name) + ".timeline.json")
            if os.path.exists(dest):
                try:
                    with open(dest, encoding="utf-8") as fh:
                        if json.load(fh).get("schema", 0) >= SCHEMA:
                            skipped += 1
                            continue
                except Exception:
                    pass  # unreadable or older: re-extract it

            doc = None
            design = None
            try:
                for visible in (False, True):
                    try:
                        doc = app.documents.open(data_file, visible)
                    except Exception:
                        doc = None
                    if doc is None:
                        continue
                    adsk.doEvents()
                    design = adsk.fusion.Design.cast(
                        doc.products.itemByProductType("DesignProductType"))
                    if design is not None:
                        break
                    close_if_ours(doc)
                    doc = None

                if design is None:
                    raise RuntimeError("no Design product")

                info = describe_design(design, data_file, parts)
                with open(dest, "w", encoding="utf-8") as fh:
                    json.dump(info, fh, indent=2)
                summary.append({
                    "design": info["name"],
                    "path": info["path"],
                    "steps": info["stats"]["timeline_steps"],
                    "parameters": info["stats"]["parameters"],
                    "feature_types": info["stats"]["feature_types"],
                })
                ok += 1
            except Exception as e:
                failed += 1
                summary.append({
                    "design": get(data_file, "name"),
                    "path": "/".join(parts),
                    "error": "{}: {}".format(type(e).__name__, e),
                })
            finally:
                _ROLLBACK["timeline"] = None
                if doc is not None:
                    kept = close_if_ours(doc)
                    if kept:
                        summary.append({"left_open": get(doc, "name"), "because": kept})
                adsk.doEvents()

            with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as fh:
                json.dump({"extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "total_found": total, "designs": summary}, fh, indent=2)

        progress.hide()
        elapsed = int(time.time() - started)
        steps = sum(s.get("steps", 0) for s in summary)
        ui.messageBox(
            "Timeline extraction finished.\n\n"
            "designs: {}\nskipped (already done): {}\nfailed: {}\n"
            "timeline steps captured: {}\nelapsed: {}m {}s\n\n{}".format(
                ok, skipped, failed, steps, elapsed // 60, elapsed % 60, OUT_DIR))

    except Exception:
        if ui:
            ui.messageBox("Script failed:\n{}".format(traceback.format_exc()))
