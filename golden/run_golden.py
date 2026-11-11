#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Golden tests for the Fusion->SolidCraft timeline translator.

Each case hand-authors a timeline exactly as ExtractTimeline.py emits it, for a part whose volume
can be computed by hand. The translator builds it and we compare against that analytic value, so a
regression shows up as a number rather than as a part that merely looks plausible.

Includes negative controls: a feature type with no mapping, and a suppressed feature. A translator
that silently "handles" those would be worse than one that admits the gap, so the suite fails if
they come back translated.

    python run_golden.py
"""

import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TRANSLATOR = os.path.join(os.path.dirname(HERE), "timeline_to_solidcraft.py")
sys.path.insert(0, os.path.dirname(HERE))
from timeline_to_solidcraft import find_cli  # noqa: E402

CLI = find_cli()

# Measured from a control (cylinder r10 h20 reads 1.13e-4 low): curved faces are measured from a
# tessellation, so anything at or below this is the measurement floor, not a translation error.
TOL = 2e-3


def sketch_rect(w, h, name="Sketch1", x0=0.0, y0=0.0):
    c = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]]
    return {
        "name": name,
        "plane": {"name": "XY", "kind": "ConstructionPlane"},
        "curves": [{"type": "line", "construction": False,
                    "start": c[i], "end": c[(i + 1) % 4]} for i in range(4)],
        "constraints": [], "dimensions": [], "profile_count": 1,
    }


def design(name, params, timeline):
    return {"name": name, "id": name, "version": 1, "path": "Golden/" + name,
            "units": "mm", "parameters": params, "timeline": timeline,
            "components": [], "stats": {}}


def case_rounded_plate():
    W, H, T, R = 60.0, 40.0, 12.0, 5.0
    doc = design("RoundedPlate", [
        {"name": "plate_t", "expression": "12 mm", "unit": "mm", "value": 1.2, "comment": ""},
        {"name": "corner_r", "expression": "5 mm", "unit": "mm", "value": 0.5, "comment": ""}], [
        {"index": 0, "name": "Sketch1", "entity": "Sketch", "suppressed": False,
         "sketch": sketch_rect(W, H)},
        {"index": 1, "name": "Extrude1", "entity": "ExtrudeFeature", "suppressed": False,
         "distance": {"expression": "plate_t", "value_mm": T}, "operation": "3",
         "profiles": [{"sketch": "Sketch1", "point": [W / 2, H / 2], "area_mm2": W * H}]},
        {"index": 2, "name": "Fillet1", "entity": "FilletFeature", "suppressed": False,
         "radius": {"expression": "corner_r", "value_mm": R}, "edge_count": 4,
         "edge_points": [[0, 0, T / 2], [W, 0, T / 2], [W, H, T / 2], [0, H, T / 2]]}])
    vol = W * H * T - 4 * (R * R - math.pi * R * R / 4) * T
    return doc, vol, 3


def case_chamfered_block():
    W, H, T, D = 50.0, 30.0, 20.0, 3.0
    doc = design("ChamferedBlock", [
        {"name": "ch", "expression": "3 mm", "unit": "mm", "value": 0.3, "comment": ""}], [
        {"index": 0, "name": "Sketch1", "entity": "Sketch", "suppressed": False,
         "sketch": sketch_rect(W, H)},
        {"index": 1, "name": "Extrude1", "entity": "ExtrudeFeature", "suppressed": False,
         "distance": {"expression": "20 mm", "value_mm": T}, "operation": "3",
         "profiles": [{"sketch": "Sketch1", "point": [W / 2, H / 2], "area_mm2": W * H}]},
        {"index": 2, "name": "Chamfer1", "entity": "ChamferFeature", "suppressed": False,
         "distance": {"expression": "ch", "value_mm": D}, "edge_count": 4,
         "edge_points": [[0, 0, T / 2], [W, 0, T / 2], [W, H, T / 2], [0, H, T / 2]]}])
    vol = W * H * T - 4 * (D * D / 2) * T
    return doc, vol, 3


def case_shelled_box():
    S, T, TH = 40.0, 30.0, 2.0
    doc = design("ShelledBox", [
        {"name": "wall", "expression": "2 mm", "unit": "mm", "value": 0.2, "comment": ""}], [
        {"index": 0, "name": "Sketch1", "entity": "Sketch", "suppressed": False,
         "sketch": sketch_rect(S, S)},
        {"index": 1, "name": "Extrude1", "entity": "ExtrudeFeature", "suppressed": False,
         "distance": {"expression": "30 mm", "value_mm": T}, "operation": "3",
         "profiles": [{"sketch": "Sketch1", "point": [S / 2, S / 2], "area_mm2": S * S}]},
        {"index": 2, "name": "Shell1", "entity": "ShellFeature", "suppressed": False,
         "thickness": {"expression": "wall", "value_mm": TH},
         "face_count": 1, "face_points": [[S / 2, S / 2, T]]}])
    inner = (S - 2 * TH) ** 2 * (T - TH)
    return doc, S * S * T - inner, 3


def case_parametric_rebuild():
    """The point of the whole exercise: the rebuilt part must still be driven by parameters."""
    W, H = 50.0, 25.0
    doc = design("ParametricPlate", [
        {"name": "thk", "expression": "8 mm", "unit": "mm", "value": 0.8, "comment": ""}], [
        {"index": 0, "name": "Sketch1", "entity": "Sketch", "suppressed": False,
         "sketch": sketch_rect(W, H)},
        {"index": 1, "name": "Extrude1", "entity": "ExtrudeFeature", "suppressed": False,
         "distance": {"expression": "thk", "value_mm": 8.0}, "operation": "3",
         "profiles": [{"sketch": "Sketch1", "point": [W / 2, H / 2], "area_mm2": W * H}]}])
    return doc, W * H * 8.0, 2


def case_negative_controls():
    """A feature with no mapping and a suppressed one must both be reported, not absorbed."""
    W, H = 30.0, 20.0
    doc = design("NegativeControls", [], [
        {"index": 0, "name": "Sketch1", "entity": "Sketch", "suppressed": False,
         "sketch": sketch_rect(W, H)},
        {"index": 1, "name": "Extrude1", "entity": "ExtrudeFeature", "suppressed": False,
         "distance": {"expression": "10 mm", "value_mm": 10.0}, "operation": "3",
         "profiles": [{"sketch": "Sketch1", "point": [W / 2, H / 2], "area_mm2": W * H}]},
        {"index": 2, "name": "Loft1", "entity": "LoftFeature", "suppressed": False},
        {"index": 3, "name": "Fillet_off", "entity": "FilletFeature", "suppressed": True,
         "radius": {"expression": "2 mm", "value_mm": 2.0},
         "edge_points": [[0, 0, 5]], "edge_count": 1}])
    return doc, W * H * 10.0, 2  # only sketch+extrude should translate



def case_constrained_sketch():
    """Re-applying Fusion's constraints must not change the solid, or over-constrain it."""
    W, H, T = 45.0, 25.0, 10.0
    sk = sketch_rect(W, H)
    # Tokens are what tie a constraint back to its curves; Fusion supplies them per entity.
    for i, c in enumerate(sk["curves"]):
        c["token"] = "tok%d" % i
    sk["constraints"] = [
        {"type": "parallel", "refs": ["tok0", "tok2"]},
        {"type": "parallel", "refs": ["tok1", "tok3"]},
        {"type": "equal", "refs": ["tok0", "tok2"]},
    ]
    sk["dimensions"] = [
        {"type": "linear", "expression": "plate_w", "value_mm": W, "refs": ["tok0"]},
    ]
    doc = design("ConstrainedSketch", [
        {"name": "plate_w", "expression": "45 mm", "unit": "mm", "value": 4.5, "comment": ""}], [
        {"index": 0, "name": "Sketch1", "entity": "Sketch", "suppressed": False, "sketch": sk},
        {"index": 1, "name": "Extrude1", "entity": "ExtrudeFeature", "suppressed": False,
         "distance": {"expression": "10 mm", "value_mm": T}, "operation": "3",
         "profiles": [{"sketch": "Sketch1", "point": [W / 2, H / 2], "area_mm2": W * H}]}])
    return doc, W * H * T, 2


CASES = [case_rounded_plate, case_chamfered_block, case_shelled_box,
         case_parametric_rebuild, case_negative_controls,
         case_constrained_sketch]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def measure(path):
    p = run([CLI, "eval", os.path.abspath(path)])
    txt = (p.stdout or "").strip()
    if not txt.startswith("{"):
        return None, (p.stderr or p.stdout or "no output").strip()[:200]
    d = json.loads(txt)
    return d, None


def main():
    if not os.path.exists(CLI):
        sys.exit("solidcraft-cli not found at " + CLI)

    print("{:<20} {:>14} {:>14} {:>10}  {}".format(
        "case", "expected", "built", "drift", "result"))
    print("-" * 78)

    failures = []
    for fn in CASES:
        doc, expected_vol, expected_steps = fn()
        name = doc["name"]
        tj = os.path.join(HERE, name + ".timeline.json")
        sj = os.path.join(HERE, name + ".json")
        with open(tj, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

        argv = [sys.executable, TRANSLATOR, tj, "-o", sj]
        if name == "ConstrainedSketch":
            argv.append("--constraints")
        p = run(argv)
        if p.returncode != 0:
            failures.append((name, "translator failed: " + (p.stderr or "")[:160]))
            print("{:<20} {:>14} {:>14} {:>10}  TRANSLATOR FAILED".format(name, "-", "-", "-"))
            continue

        with open(os.path.splitext(sj)[0] + ".coverage.json", encoding="utf-8") as fh:
            cov = json.load(fh)

        if cov["steps_translated"] != expected_steps:
            failures.append((name, "translated {} steps, expected {}".format(
                cov["steps_translated"], expected_steps)))

        d, err = measure(sj)
        if err:
            failures.append((name, "build failed: " + err))
            print("{:<20} {:>14.4f} {:>14} {:>10}  BUILD FAILED".format(name, expected_vol, "-", "-"))
            continue

        got = d["total"]["volume_mm3"]
        drift = abs(got - expected_vol) / expected_vol
        ok = drift <= TOL
        if not ok:
            failures.append((name, "volume drift {:.3e} over tolerance".format(drift)))
        print("{:<20} {:>14.4f} {:>14.4f} {:>10.2e}  {}".format(
            name, expected_vol, got, drift, "pass" if ok else "FAIL"))

    # The parametric case must actually rebuild when the parameter changes.
    print("\nparametric rebuild check")
    sj = os.path.join(HERE, "ParametricPlate.json")
    bump = os.path.join(HERE, "_bump.json")
    with open(bump, "w", encoding="utf-8") as fh:
        json.dump({"commands": [{"command": "parameters.change",
                                 "params": {"name": "thk", "expression": "20 mm"}}]}, fh)
    out = os.path.join(HERE, "_bumped.json")
    run([CLI, "run", os.path.abspath(sj), "--quiet", "--save", os.path.abspath(out)])
    run([CLI, "run", os.path.abspath(bump), "--in", os.path.abspath(out),
         "--quiet", "--save", os.path.abspath(out)])
    d, err = measure(out)
    want = 50.0 * 25.0 * 20.0
    if err or not d:
        failures.append(("ParametricPlate", "rebuild failed: " + str(err)))
        print("  FAILED: {}".format(err))
    else:
        got = d["total"]["volume_mm3"]
        ok = abs(got - want) / want <= TOL
        print("  thk 8mm -> 20mm: expected {:.1f}, got {:.4f}  {}".format(
            want, got, "pass" if ok else "FAIL"))
        if not ok:
            failures.append(("ParametricPlate", "rebuild gave {} not {}".format(got, want)))

    print("\n" + "=" * 78)
    if failures:
        print("{} FAILURE(S)".format(len(failures)))
        for n, why in failures:
            print("  {}: {}".format(n, why))
        return 1
    print("all golden cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
