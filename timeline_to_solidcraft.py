#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Translate a Fusion timeline (from ExtractTimeline.py) into a SolidCraft command script.

This is the part that makes a migrated part *editable* rather than a frozen solid. A STEP import
gives you geometry; this gives you the recipe -- parameters, sketches, and features that still
rebuild when you change a dimension.

    python timeline_to_solidcraft.py part.timeline.json -o part.json
    python timeline_to_solidcraft.py ~/cad/fusion-timeline --all      translate a whole tree
    python timeline_to_solidcraft.py part.timeline.json --verify      build it and report measures

Every step is either translated or recorded as untranslated with a reason. Nothing is dropped
quietly: a half-migrated part that claims to be whole is worse than one that admits the gap.
"""

import argparse
import json
import re
import os
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")


def find_cli():
    """Locate solidcraft-cli: $SOLIDCRAFT_CLI, PATH, a local cargo build, then a portable unzip."""
    env = os.environ.get("SOLIDCRAFT_CLI")
    if env and os.path.exists(env):
        return env
    exe = "solidcraft-cli.exe" if os.name == "nt" else "solidcraft-cli"
    found = shutil.which(exe) or shutil.which("solidcraft-cli")
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("../../target/release", "../../target/debug"):
        p = os.path.join(here, rel, exe)
        if os.path.exists(p):
            return os.path.abspath(p)
    import glob
    for p in sorted(glob.glob(os.path.join(
            os.path.expanduser("~"), "tools", "solidcraft", "*", exe)), reverse=True):
        return p
    return exe  # let the OS report it, with a name the user recognises


DEFAULT_CLI = find_cli()


# Fusion's FeatureOperations enum arrives as a number; these are its documented values.
OPERATION = {
    "0": "join", "1": "cut", "2": "intersect", "3": "new", "4": "new",
    "JoinFeatureOperation": "join", "CutFeatureOperation": "cut",
    "IntersectFeatureOperation": "intersect", "NewBodyFeatureOperation": "new",
    "NewComponentFeatureOperation": "new",
}

# FeatureExtentTypes.
EXTENT = {"0": "one_side", "1": "two_sides", "2": "symmetric"}

ORIGIN_PLANES = {"XY", "XZ", "YZ"}

# The origin planes expressed as frames, so a sketch's plane can be reasoned about uniformly.
PLANE_FRAMES = {
    "XY": {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "y_dir": [0, 1, 0]},
    "XZ": {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "y_dir": [0, 0, 1]},
    "YZ": {"origin": [0, 0, 0], "x_dir": [0, 1, 0], "y_dir": [0, 0, 1]},
}

# Fusion stores lengths in centimetres; everything written here is millimetres.
CM_UNITS = {"cm"}
UNITS = {"mm", "cm", "m", "in", "ft", "deg", "rad"}


def order_parameters(params):
    """Define parameters before whatever uses them.

    Fusion lists user parameters in creation order, which is not dependency order -- a design can
    define "length = cutOut * 2" before "cutOut" exists. Emitting that order verbatim fails on the
    first forward reference, so sort dependencies first and leave genuine cycles in their original
    place for the engine to reject with its own message.
    """
    by_name = {p["name"]: p for p in params if p.get("name")}
    ordered, state = [], {}

    def visit(name):
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            return  # a cycle: leave it, the expression engine will report it properly
        state[name] = "visiting"
        p = by_name[name]
        for tok in re.findall(r"\b[A-Za-z_]\w*\b", p.get("expression") or ""):
            if tok in by_name and tok != name:
                visit(tok)
        state[name] = "done"
        ordered.append(p)

    for p in params:
        if p.get("name"):
            visit(p["name"])
    return ordered



class Skip(Exception):
    """This step cannot be rebuilt faithfully; say why rather than emit something wrong."""


def expr(v, default=None):
    """Prefer Fusion's expression over its number: the expression is what stays parametric."""
    if isinstance(v, dict):
        e = v.get("expression")
        if e:
            return e
        mm = v.get("value_mm")
        if mm is not None:
            return "{} mm".format(round(mm, 9))
    return default


def op_of(step, default="auto"):
    o = step.get("operation")
    return OPERATION.get(str(o), default) if o is not None else default


# ---------------------------------------------------------------------------
# sketches
# ---------------------------------------------------------------------------

def plane_frame(sk):
    """Rebuild a sketch's plane from its own coordinate frame.

    Most sketches in real designs sit on construction planes or faces rather than on XY/XZ/YZ,
    and their identity cannot be carried across. Their *frame* can: Fusion's sketch transform is
    sketch space to model space, so its axes and origin define the plane directly, and the target
    accepts a plane given that way. This is what rescues the majority of real sketches.

    Fusion's matrix is row-major with the translation in the last column, and its lengths are in
    centimetres.
    """
    t = sk.get("transform")
    if not t or len(t) < 12:
        return None
    x_dir = [t[0], t[4], t[8]]
    y_dir = [t[1], t[5], t[9]]
    origin = [t[3] * 10.0, t[7] * 10.0, t[11] * 10.0]
    # A degenerate frame would silently place the sketch somewhere arbitrary.
    for v in (x_dir, y_dir):
        if sum(c * c for c in v) < 0.5:
            return None
    return {"origin": [round(c, 9) for c in origin],
            "x_dir": [round(c, 9) for c in x_dir],
            "y_dir": [round(c, 9) for c in y_dir]}


def emit_sketch(step, out, ctx):
    sk = step.get("sketch") or {}
    plane = (sk.get("plane") or {}).get("name")
    if plane in ORIGIN_PLANES:
        spec = plane
    else:
        spec = plane_frame(sk)
        if spec is None:
            raise Skip(
                "sketch is on {} and carries no usable coordinate frame".format(
                    plane or "an unnamed plane"))

    out.append({"command": "sketch.create", "params": {"plane": spec}})

    curves = sk.get("curves") or []
    if not curves:
        raise Skip("sketch has no readable curves")

    made = 0
    unsupported = {}
    # SolidCraft names sketch curves predictably and per type -- l1, l2 for lines, c1 for
    # circles, a1 for arcs -- and addresses their points as "l1.start", "c1.center". Counting
    # as we emit therefore tells us the id of every curve without having to read anything back,
    # which is what lets a constraint be written in the same script that creates its operands.
    seq = {"line": 0, "circle": 0, "arc": 0}
    prefix = {"line": "l", "circle": "c", "arc": "a"}
    ids = {}  # Fusion entity token -> SolidCraft curve id

    for c in curves:
        kind = c.get("type")
        cons = bool(c.get("construction"))
        try:
            if kind == "line" and c.get("start") and c.get("end"):
                out.append({"command": "sketch.line",
                            "params": {"points": [c["start"], c["end"]], "construction": cons}})
            elif kind == "circle" and c.get("center") and c.get("radius_mm") is not None:
                out.append({"command": "sketch.circle.center",
                            "params": {"center": c["center"], "radius": c["radius_mm"],
                                       "construction": cons}})
            elif kind == "arc" and c.get("center") and c.get("start") and c.get("end"):
                out.append({"command": "sketch.arc.center_point",
                            "params": {"center": c["center"], "start": c["start"],
                                       "end": c["end"], "construction": cons}})
            elif kind in ("fitted_spline", "fixed_spline", "control_spline") and c.get("points"):
                pts = [p for p in c["points"] if p]
                if len(pts) < 2:
                    raise ValueError("too few points")
                out.append({"command": "sketch.spline.fit_point",
                            "params": {"points": pts, "construction": cons}})
            elif kind == "ellipse" and c.get("center"):
                raise ValueError("ellipse needs major/minor axes that were not captured")
            else:
                raise ValueError("no mapping")
            made += 1
            if kind in seq:
                seq[kind] += 1
                tok = c.get("token")
                if tok:
                    ids[tok] = "{}{}".format(prefix[kind], seq[kind])
        except Exception as e:
            unsupported[kind] = unsupported.get(kind, 0) + 1
            del e

    if not made:
        raise Skip("no curve in this sketch could be translated ({})".format(
            ", ".join("{}x{}".format(v, k) for k, v in unsupported.items()) or "empty"))

    ctx["sketches"].add(sk.get("name"))
    ctx.setdefault("frames", {})[sk.get("name")] = (
        spec if isinstance(spec, dict) else PLANE_FRAMES.get(spec))

    extra = {}
    if ctx.get("constraints"):
        extra = emit_sketch_relations(sk, ids, out)
    return dict({"curves_made": made}, **extra) if not unsupported else \
        dict({"curves_made": made, "curves_skipped": unsupported}, **extra)

    return {"curves_made": made, "curves_skipped": unsupported} if unsupported else {"curves_made": made}



# Fusion constraint name -> SolidCraft command, for the ones that take two operands the same way.
CONSTRAINT_CMDS = {
    "parallel": "sketch.constraint.parallel",
    "perpendicular": "sketch.constraint.perpendicular",
    "collinear": "sketch.constraint.collinear",
    "concentric": "sketch.constraint.concentric",
    "equal": "sketch.constraint.equal",
    "tangent": "sketch.constraint.tangent",
    "symmetry": "sketch.constraint.symmetry",
    "midpoint": "sketch.constraint.midpoint",
}

# Dimension class name -> the kind of dimension to place.
DIMENSION_KINDS = {
    "linear": "distance", "offset": "distance", "lineardiameter": "diameter",
    "radial": "radius", "diameter": "diameter", "angular": "angle",
    "distancebetweenlines": "distance", "distancebetweenpointandline": "distance",
}


def emit_sketch_relations(sk, ids, out):
    """Re-apply Fusion's constraints and dimensions to the curves just created.

    Without these the geometry lands in the right place but holds no relationships, so changing a
    parameter moves nothing in the sketch. With them, a dimension driven by "width" still drives
    the part. Coincidence is left to the curve commands, which already join points that meet;
    re-stating it would only risk an over-constrained sketch for no gain.
    """
    made = {"constraints": 0, "dimensions": 0, "skipped": 0}

    for k in sk.get("constraints") or []:
        cmd = CONSTRAINT_CMDS.get((k.get("type") or "").lower())
        refs = [ids.get(t) for t in (k.get("refs") or [])]
        refs = [r for r in refs if r]
        if not cmd or len(refs) < 2:
            made["skipped"] += 1
            continue
        out.append({"command": cmd, "params": {"a": refs[0], "b": refs[1]}})
        made["constraints"] += 1

    for d in sk.get("dimensions") or []:
        kind = DIMENSION_KINDS.get((d.get("type") or "").lower())
        refs = [ids.get(t) for t in (d.get("refs") or [])]
        refs = [r for r in refs if r]
        value = d.get("expression")
        if not kind or not refs or not value:
            made["skipped"] += 1
            continue
        params = {"entities": refs[:2], "type": kind, "value": value}
        out.append({"command": "sketch.dimension", "params": params})
        made["dimensions"] += 1

    return {"relations": made} if (made["constraints"] or made["dimensions"]) else {}


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------

def profiles_param(step, ctx):
    """Re-find each profile by a point inside it, which survives parameter changes.

    If the profile's own sketch was not translated, the feature cannot be built either: emitting
    it anyway produces a script that fails halfway, which is worse than one that stops short and
    says why.
    """
    profs = step.get("profiles")
    if not profs:
        return "all"
    wanted = {p.get("sketch") for p in profs if p.get("sketch")}
    missing = {w for w in wanted if w and w not in ctx["sketches"]}
    if missing:
        raise Skip("needs sketch {}, which was not translated".format(
            ", ".join(sorted(missing))))
    pts = [{"point": p["point"]} for p in profs if p.get("point")]
    return pts or "all"


def emit_extrude(step, out, ctx):
    d = expr(step.get("distance"))
    to = step.get("to_point")
    if not d and not to:
        raise Skip("no distance expression and no target ({})".format(
            step.get("extent_definition") or "unknown extent"))

    if d:
        params = {"distance": d}
    else:
        # "To object": the extrude runs until it meets something. Saying where that something is
        # keeps the intent -- it still lands on the face rather than near where the face was.
        params = {"to": to}
        off = expr(step.get("to_offset"))
        if off and not str(off).startswith("0"):
            params["to_offset"] = off
    params.update({"profiles": profiles_param(step, ctx), "operation": op_of(step)})
    t = expr(step.get("taper"))
    if t and not str(t).startswith("0"):
        params["taper"] = t
    if str(step.get("extent_type")) == "2" or step.get("is_symmetric") in (True, "True"):
        params["direction"] = "symmetric"
    d2 = expr(step.get("distance_two"))
    if d2:
        params["distance2"] = d2
    out.append({"command": "solid.extrude", "params": params})
    return {}


def _sets_of(step, size_keys):
    """One record per edge set. A single Fusion fillet can round groups to different radii, so
    each set becomes its own command rather than being flattened to one size."""
    sets = step.get("edge_sets")
    if sets:
        return [(s["edge_points"], next((expr(s.get(k)) for k in size_keys if s.get(k)), None))
                for s in sets if s.get("edge_points")]
    pts = step.get("edge_points")
    if pts:
        return [(pts, next((expr(step.get(k)) for k in size_keys if step.get(k)), None))]
    return []


def emit_fillet(step, out, ctx):
    sets = _sets_of(step, ("radius",))
    if not sets:
        raise Skip("no edge geometry captured ({} edges referenced)".format(
            step.get("edge_count", "?")))
    made = 0
    for pts, size in sets:
        size = size or expr(step.get("radius"))
        if not size:
            continue
        out.append({"command": "solid.fillet", "params": {"edges": pts, "radius": size}})
        made += 1
    if not made:
        raise Skip("no radius expression on any edge set")
    return {"edge_sets": made, "edges": sum(len(p) for p, _ in sets)}


def emit_chamfer(step, out, ctx):
    sets = _sets_of(step, ("distance", "radius"))
    if not sets:
        raise Skip("no edge geometry captured ({} edges referenced)".format(
            step.get("edge_count", "?")))
    made = 0
    for pts, size in sets:
        size = size or expr(step.get("distance")) or expr(step.get("radius"))
        if not size:
            continue
        out.append({"command": "solid.chamfer", "params": {"edges": pts, "distance": size}})
        made += 1
    if not made:
        raise Skip("no chamfer distance on any edge set")
    return {"edge_sets": made, "edges": sum(len(p) for p, _ in sets)}


def emit_shell(step, out, ctx):
    pts = step.get("face_points") or []
    t = expr(step.get("thickness"))
    if not t:
        raise Skip("no thickness expression")
    out.append({"command": "solid.shell", "params": {"faces": pts, "thickness": t,
                                                     "direction": "inside"}})
    return {"faces": len(pts)}


def emit_draft(step, out, ctx):
    pts = step.get("face_points")
    if not pts:
        raise Skip("no face geometry captured")
    a = expr(step.get("angle"))
    if not a:
        raise Skip("no draft angle expression")
    out.append({"command": "solid.draft",
                "params": {"faces": pts, "angle": a, "neutral": "XY"}})
    return {"faces": len(pts)}


def world_axis(axis):
    """Name the world axis a revolve turns about, or None if it is not one of them.

    Only X, Y and Z through the origin can be named. A parallel axis offset from the origin is
    a different axis entirely, so it has to be refused rather than rounded to the nearest name --
    the earlier version guessed "x" unconditionally, which silently produced the wrong solid.
    """
    if not axis or not axis.get("start") or not axis.get("end"):
        return None
    s, e = axis["start"], axis["end"]
    d = [e[i] - s[i] for i in range(3)]
    length = sum(c * c for c in d) ** 0.5
    if length < 1e-9:
        return None
    d = [c / length for c in d]
    for i, name in enumerate("XYZ"):
        if abs(abs(d[i]) - 1.0) > 1e-6:
            continue
        # Aligned with this axis; it must also pass through the origin to *be* that axis.
        off = [s[j] for j in range(3) if j != i]
        if max(abs(v) for v in off) <= 1e-6:
            return name
        return None
    return None


def axis_lies_in(frame, name):
    """Is world axis `name` contained in this sketch plane?

    A revolve turns a profile about an axis in its own plane; naming an axis that merely exists
    in the world gets the right-sounding answer and the wrong solid, which the engine then
    rejects with "the Y axis does not lie in the sketch plane".
    """
    if not frame:
        return True  # unknown plane: let the engine judge it rather than refuse wrongly
    i = "XYZ".index(name)
    d = [1.0 if j == i else 0.0 for j in range(3)]
    x, y, o = frame["x_dir"], frame["y_dir"], frame["origin"]
    # Plane normal, then: the direction must be perpendicular to it and the origin must be on it.
    n = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
    if abs(sum(n[k] * d[k] for k in range(3))) > 1e-6:
        return False
    return abs(sum(n[k] * o[k] for k in range(3))) <= 1e-6


def emit_revolve(step, out, ctx):
    name = world_axis(step.get("axis"))
    if not name:
        raise Skip("axis is not a world axis through the origin ({})".format(
            "no axis captured" if not step.get("axis") else "offset or oblique"))
    for prof in step.get("profiles") or []:
        frame = (ctx.get("frames") or {}).get(prof.get("sketch"))
        if not axis_lies_in(frame, name):
            raise Skip("the {} axis does not lie in sketch {}'s plane".format(
                name, prof.get("sketch")))
    params = {"axis": name, "angle": expr(step.get("angle"), "360 deg"),
              "profiles": profiles_param(step, ctx), "operation": op_of(step)}
    a2 = expr(step.get("angle2"))
    if a2:
        params["angle2"] = a2
    if str(step.get("extent_type")) == "2" or step.get("is_symmetric") in (True, "True"):
        params["direction"] = "symmetric"
    out.append({"command": "solid.revolve", "params": params})
    return {"axis": name}


def emit_offset_faces(step, out, ctx):
    pts = step.get("face_points")
    if not pts:
        raise Skip("no face geometry captured ({} faces referenced)".format(
            step.get("face_count", "?")))
    d = expr(step.get("distance"))
    if not d:
        raise Skip("no offset distance expression")
    out.append({"command": "solid.offset_face", "params": {"faces": pts, "distance": d}})
    return {"faces": len(pts)}


HANDLERS = {
    "Sketch": emit_sketch,
    "ExtrudeFeature": emit_extrude,
    "FilletFeature": emit_fillet,
    "ChamferFeature": emit_chamfer,
    "ShellFeature": emit_shell,
    "DraftFeature": emit_draft,
    "RevolveFeature": emit_revolve,
    "OffsetFacesFeature": emit_offset_faces,
}


def translate(doc, with_constraints=False):
    """Timeline document -> (command script, coverage report)."""
    cmds = []
    steps = []
    ctx = {"sketches": set(), "constraints": with_constraints}

    # Fusion's auto-named model parameters ("d4") are driven by features, not set by hand, so
    # they cannot be re-created as inputs. Where a user parameter leans on one, substitute its
    # measured value: the number survives even though the link to the feature does not.
    model = {m["name"]: m for m in doc.get("model_parameters", []) if m.get("name")}
    user_names = {p["name"] for p in doc.get("parameters", [])}
    substituted = {}

    def resolve(expression):
        if not expression:
            return expression, []
        used = []

        def sub(m):
            tok = m.group(0)
            if tok in user_names or tok in UNITS or tok not in model:
                return tok
            val = model[tok].get("value")
            if val is None:
                return tok
            used.append(tok)
            # Fusion reports lengths in centimetres; parameters here are written in mm.
            unit = (model[tok].get("unit") or "").strip()
            return "({} {})".format(round(val * (10.0 if unit in CM_UNITS else 1.0), 9),
                                    "mm" if unit in CM_UNITS else unit).rstrip() \
                if unit else "({})".format(round(val, 9))

        return re.sub(r"\b[A-Za-z_]\w*\b", sub, expression), used

    for p in order_parameters(doc.get("parameters", [])):
        expression, used = resolve(p["expression"])
        prm = {"name": p["name"], "expression": expression}
        if p.get("unit"):
            prm["unit"] = p["unit"]
        if p.get("comment"):
            prm["comment"] = p["comment"]
        if used:
            substituted[p["name"]] = used
        cmds.append({"command": "parameters.add", "params": prm})

    for step in doc.get("timeline", []):
        kind = step.get("entity") or "unknown"
        rec = {"index": step.get("index"), "name": step.get("name"), "entity": kind}

        if step.get("suppressed"):
            rec.update(status="skipped", reason="suppressed in Fusion")
            steps.append(rec)
            continue

        handler = HANDLERS.get(kind)
        if handler is None:
            rec.update(status="untranslated", reason="no mapping for {} yet".format(kind))
            steps.append(rec)
            continue

        mark = len(cmds)
        try:
            detail = handler(step, cmds, ctx) or {}
            rec.update(status="translated", commands=len(cmds) - mark, **detail)
        except Skip as e:
            del cmds[mark:]  # never leave half a feature behind
            rec.update(status="untranslated", reason=str(e))
        except Exception as e:
            del cmds[mark:]
            rec.update(status="error", reason="{}: {}".format(type(e).__name__, e))
        steps.append(rec)

    done = sum(1 for s in steps if s["status"] == "translated")
    report = {
        "design": doc.get("name"),
        "path": doc.get("path"),
        "units": doc.get("units"),
        "parameters": len(doc.get("parameters", [])),
        "steps_total": len(steps),
        "steps_translated": done,
        "coverage": round(done / len(steps), 4) if steps else 0.0,
        "commands": len(cmds),
        "substituted_model_params": substituted,
        "steps": steps,
    }
    return {"commands": cmds}, report


def verify(cli, script_path):
    """Build the translated script and report what SolidCraft actually made of it."""
    # The CLI runs from its own directory, so the script must be addressed absolutely.
    script_path = os.path.abspath(script_path)
    try:
        p = subprocess.run([cli, "eval", script_path], capture_output=True, text=True,
                           timeout=300, cwd=os.path.dirname(cli) or None)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    txt = (p.stdout or "").strip()
    if not txt.startswith("{"):
        err = (p.stderr or p.stdout or "no output").strip().splitlines()
        return {"ok": False, "error": (err[0] if err else "no output")[:300]}
    d = json.loads(txt)
    t = d.get("total") or {}
    # Getting measurements back at all means every command ran: a failing step makes the CLI
    # report the error instead. Zero bodies is a legitimate result -- plenty of real designs are
    # sketch-only references with no solid in them -- so it is reported, not treated as failure.
    return {"ok": True, "bodies": d.get("body_count") or 0,
            "solids": bool(d.get("body_count")),
            "volume_mm3": t.get("volume_mm3"), "area_mm2": t.get("area_mm2"),
            "faces": t.get("faces")}


def fusion_volumes(export_dir):
    """Fusion's own volume per design, from the export manifest.

    Building without error only proves a script ran; it says nothing about whether the result is
    the part you started with. A rebuilt timeline and the STEP export of the same design should
    agree on how much material there is, and that is the only check here that can actually catch
    a translation that is quietly wrong -- a missed cut, a fillet on the wrong edge, an extrude
    that went the other way.
    """
    manifest = os.path.join(export_dir, "manifest.json")
    if not os.path.exists(manifest):
        return {}
    out = {}
    try:
        with open(manifest, encoding="utf-8") as fh:
            for r in json.load(fh).get("designs", []):
                m = r.get("measure") or {}
                if m.get("volume_mm3") and r.get("step"):
                    stem = os.path.splitext(r["step"].replace("\\", "/"))[0]
                    out[stem.lower()] = m["volume_mm3"]
    except Exception:
        return {}
    return out


# Volume on curved faces is measured from a tessellation; a plain cylinder reads 1.1e-4 under
# analytic, so this sits above that floor and far below anything a real mistranslation costs.
REBUILD_TOL = 1e-3


def compare_to_fusion(report, rel_stem, volumes):
    """Judge a rebuilt design against Fusion's measurement of the original."""
    want = volumes.get(rel_stem.lower())
    v = report.get("verify") or {}
    if want is None or not v.get("ok") or not v.get("volume_mm3"):
        return
    got = v["volume_mm3"]
    drift = abs(got - want) / want if want else None
    report["fusion_volume_mm3"] = want
    report["volume_drift"] = drift
    if drift is not None and drift <= REBUILD_TOL:
        # Only a design whose every step translated can be called a faithful rebuild; a partial
        # one matching by luck is still partial.
        report["fidelity"] = ("exact" if report["coverage"] >= 1.0 else "partial-but-matching")
    else:
        report["fidelity"] = "volume-mismatch"


def one(path, out_path, cli, do_verify, with_constraints=False):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    script, report = translate(doc, with_constraints)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(script, fh, indent=2)
    with open(os.path.splitext(out_path)[0] + ".coverage.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if do_verify:
        report["verify"] = verify(cli, out_path)
        with open(os.path.splitext(out_path)[0] + ".coverage.json", "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help=".timeline.json file, or a folder with --all")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--all", action="store_true", help="translate every timeline under a folder")
    ap.add_argument("--outdir", default=os.path.join(HOME, "cad", "rebuilt"))
    ap.add_argument("--verify", action="store_true", help="build each script and measure it")
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--constraints", action="store_true",
                    help="also re-apply sketch constraints and dimensions "
                         "(parametric sketches, but an over-constrained one fails "
                         "the whole script)")
    ap.add_argument("--against", default=os.path.join(HOME, "cad", "fusion-export"),
                    help="export tree whose manifest holds Fusion's own volumes")
    args = ap.parse_args()

    if args.verify and not os.path.exists(args.cli):
        sys.exit("solidcraft-cli not found at {}".format(args.cli))

    if args.all:
        root = os.path.abspath(os.path.expanduser(args.input))
        files = []
        for dp, _d, names in os.walk(root):
            for n in names:
                if n.endswith(".timeline.json"):
                    files.append(os.path.join(dp, n))
        if not files:
            sys.exit(
                "no .timeline.json files under {}\n\n"
                "{}\n"
                "Run the ExtractTimeline script inside Fusion first:\n"
                "  Utilities > ADD-INS > Scripts and Add-Ins > ExtractTimeline > Run\n"
                "It writes one .timeline.json per design to ~/cad/fusion-timeline.".format(
                    root,
                    "That folder does not exist yet." if not os.path.isdir(root)
                    else "That folder exists but is empty.",
                )
            )
        files.sort()

        volumes = fusion_volumes(args.against) if args.verify else {}
        if args.verify:
            print("{} designs have a Fusion volume to check against\n".format(len(volumes))
                  if volumes else
                  "no export manifest at {} -- builds will be checked for success only,\n"
                  "not for correctness. Run ExportAllDesigns in Fusion to enable that.\n"
                  .format(args.against))

        reports = []
        for f in files:
            rel = os.path.relpath(f, root).replace("\\", "/")
            stem = rel[: -len(".timeline.json")]
            out_path = os.path.join(args.outdir, stem + ".json")
            try:
                r = one(f, out_path, args.cli, args.verify, args.constraints)
                # The document's own "path" is its folder, so two designs in one folder share it.
                # Carry the full stem as well, which is what identifies a design everywhere else
                # (the export manifest, the rebuilt scripts, this report).
                r["stem"] = stem
            except Exception as e:
                print("FAIL {} - {}".format(rel, e))
                continue
            if args.verify and volumes:
                compare_to_fusion(r, stem, volumes)
            reports.append(r)
            v = r.get("verify") or {}
            vs = ""
            if args.verify:
                if not v.get("ok"):
                    vs = " | BUILD FAILED"
                elif r.get("fidelity") == "exact":
                    vs = " | MATCHES FUSION"
                elif r.get("fidelity") == "partial-but-matching":
                    vs = " | volume matches (partial)"
                elif r.get("fidelity") == "volume-mismatch":
                    vs = " | VOLUME OFF {:.2%}".format(r.get("volume_drift") or 0)
                else:
                    vs = " | built {} bodies".format(v.get("bodies"))
            print("{:>5.0%}  {:>3}/{:<3} steps  {}{}".format(
                r["coverage"], r["steps_translated"], r["steps_total"], stem[:48], vs))

        os.makedirs(args.outdir, exist_ok=True)
        with open(os.path.join(args.outdir, "rebuild-report.json"), "w", encoding="utf-8") as fh:
            json.dump({"designs": reports}, fh, indent=2)

        tot = sum(r["steps_total"] for r in reports)
        got = sum(r["steps_translated"] for r in reports)
        print("\n{} designs, {}/{} timeline steps translated ({:.0%})".format(
            len(reports), got, tot, (got / tot) if tot else 0))

        checked = [r for r in reports if r.get("fidelity")]
        if checked:
            exact = sum(1 for r in checked if r["fidelity"] == "exact")
            part = sum(1 for r in checked if r["fidelity"] == "partial-but-matching")
            off = sum(1 for r in checked if r["fidelity"] == "volume-mismatch")
            print("\nchecked against Fusion's own volumes: {} faithful, {} matching but "
                  "partial, {} wrong".format(exact, part, off))

        missing = {}
        for r in reports:
            for s in r["steps"]:
                if s["status"] != "translated":
                    missing[s["entity"]] = missing.get(s["entity"], 0) + 1
        if missing:
            print("\nmost common untranslated steps:")
            for k, v in sorted(missing.items(), key=lambda kv: -kv[1])[:12]:
                print("  {:<34} {}".format(k, v))
        print("\nreport: {}".format(os.path.join(args.outdir, "rebuild-report.json")))
        return 0

    out_path = args.out or os.path.splitext(os.path.splitext(args.input)[0])[0] + ".json"
    r = one(args.input, out_path, args.cli, args.verify, args.constraints)
    print("{}: {}/{} steps translated ({:.0%}), {} commands".format(
        r["design"], r["steps_translated"], r["steps_total"], r["coverage"], r["commands"]))
    for s in r["steps"]:
        if s["status"] != "translated":
            print("  {} #{} {} - {}".format(s["status"], s["index"], s["entity"],
                                            s.get("reason", "")[:90]))
    if args.verify:
        print("verify:", json.dumps(r.get("verify")))
    print("script: {}".format(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
