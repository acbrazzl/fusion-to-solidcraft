#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Import a Fusion export tree into SolidCraft and check every part against Fusion's own numbers.

Pairs with the Fusion script ExportAllDesigns.py. That script writes a STEP per design plus a
manifest recording the volume and area Fusion measured. This one imports each STEP, measures it
with solidcraft-cli, and fails loudly where the two disagree -- so a silent geometry loss shows up
as a number, not as something you notice months later.

    python migrate.py                     check the export, write reports
    python migrate.py --convert           also save a .solidcraft design per part
    python migrate.py --thumbs            also render a PNG per part
    python migrate.py --only Bracket      restrict to paths matching a substring

Exit status is non-zero if any design failed, so it can gate a migration.
"""

import argparse
import concurrent.futures
import json
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

DEFAULT_EXPORT = os.path.join(HOME, "cad", "fusion-export")
DEFAULT_OUT = os.path.join(HOME, "cad", "fusion-import")

# Volume on curved faces is measured from a tessellation, not integrated exactly: a plain
# cylinder reads 1.1e-4 under its analytic volume, and a part that is mostly fillets drifts
# further. Anything genuinely lost in translation shows up percent-level, so this sits well
# above the measurement floor and still well below real geometry loss. Set from a measured
# control (cylinder r10 h20), not guessed.
VOLUME_TOL = 1e-3  # relative


def run_cli(cli, args, timeout=300):
    p = subprocess.run(
        [cli] + args, capture_output=True, text=True, timeout=timeout,
        cwd=os.path.dirname(cli) or None,
    )
    return p.returncode, p.stdout, p.stderr


def measure(cli, path):
    """Measure one file, returning (totals, error)."""
    try:
        code, out, err = run_cli(cli, ["eval", path])
    except subprocess.TimeoutExpired:
        return None, "timeout"
    text = (out or "").strip()
    if not text.startswith("{"):
        msg = (err or out or "no output").strip().splitlines()
        return None, (msg[0] if msg else "no output")[:300]
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        return None, "unparseable output: {}".format(e)
    if code != 0 and not d.get("bodies"):
        return None, (err or "exit {}".format(code)).strip()[:300]
    return d, None


def check(cli, step_path, expected, out_dir, rel, convert, thumbs):
    """Import and verify one design. Returns a result record."""
    rec = {
        "design": rel,
        "step": step_path,
        "status": "ok",
        "notes": [],
        "solidcraft": None,
        "fusion": expected,
    }

    d, err = measure(cli, step_path)
    if err:
        rec["status"] = "import-failed"
        rec["notes"].append(err)
        return rec

    total = d.get("total") or {}
    got = {
        "bodies": d.get("body_count"),
        "volume_mm3": total.get("volume_mm3"),
        "area_mm2": total.get("area_mm2"),
        "faces": total.get("faces"),
        "edges": total.get("edges"),
    }
    rec["solidcraft"] = got

    if not got["bodies"]:
        rec["status"] = "empty"
        rec["notes"].append("imported with no bodies")
        return rec
    if not got["volume_mm3"]:
        rec["status"] = "empty"
        rec["notes"].append("imported with zero volume")
        return rec

    # The real oracle: does SolidCraft agree with Fusion about how much material this is?
    if expected and expected.get("volume_mm3"):
        want = expected["volume_mm3"]
        if want > 0:
            drift = abs(got["volume_mm3"] - want) / want
            rec["volume_drift"] = drift
            if drift > VOLUME_TOL:
                rec["status"] = "volume-mismatch"
                rec["notes"].append(
                    "fusion {:.6f} mm3 vs solidcraft {:.6f} mm3 ({:.4%})".format(
                        want, got["volume_mm3"], drift
                    )
                )
        wb = expected.get("solid_bodies")
        if wb and got["bodies"] != wb:
            rec["notes"].append("body count {} vs fusion {}".format(got["bodies"], wb))

    stem = os.path.join(out_dir, rel)
    os.makedirs(os.path.dirname(stem) or ".", exist_ok=True)

    if convert:
        native = stem + ".solidcraft"
        code, out, err = run_cli(cli, ["run", "--in", step_path, "--save", native, "--quiet"])
        if not os.path.exists(native):
            rec["status"] = "convert-failed" if rec["status"] == "ok" else rec["status"]
            rec["notes"].append("save failed: {}".format((err or out or "").strip()[:200]))
        else:
            rec["native"] = native
            # A conversion that silently changed the solid is worse than one that failed.
            nd, nerr = measure(cli, native)
            if nerr:
                rec["status"] = "convert-failed"
                rec["notes"].append("native unreadable: {}".format(nerr))
            else:
                nv = (nd.get("total") or {}).get("volume_mm3")
                if nv and abs(nv - got["volume_mm3"]) / got["volume_mm3"] > 1e-9:
                    rec["status"] = "roundtrip-drift"
                    rec["notes"].append("native volume {:.6f} vs step {:.6f}".format(nv, got["volume_mm3"]))

    if thumbs:
        png = stem + ".png"
        run_cli(cli, ["snapshot", step_path, "--out", png, "--width", "640", "--height", "480"])
        if os.path.exists(png):
            rec["thumb"] = png

    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--export", default=DEFAULT_EXPORT, help="Fusion export tree")
    ap.add_argument("--out", default=DEFAULT_OUT, help="where converted designs land")
    ap.add_argument("--only", default=None, help="substring filter on design path")
    ap.add_argument("--convert", action="store_true", help="save a .solidcraft per design")
    ap.add_argument("--thumbs", action="store_true", help="render a PNG per design")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    if not os.path.exists(args.cli):
        sys.exit("solidcraft-cli not found at {}".format(args.cli))
    if not os.path.isdir(args.export):
        sys.exit(
            "no Fusion export at {}\nRun the ExportAllDesigns script inside Fusion first.".format(
                args.export
            )
        )

    # Fusion's measurements, keyed by the STEP path it wrote.
    expected = {}
    mpath = os.path.join(args.export, "manifest.json")
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as fh:
            for r in json.load(fh).get("designs", []):
                if r.get("step") and r.get("measure") and not r["measure"].get("error"):
                    expected[r["step"].replace("\\", "/")] = r["measure"]
        print("manifest: {} designs with Fusion measurements".format(len(expected)))
    else:
        print("no manifest.json -- checking that parts import, but not against Fusion's numbers")

    steps = []
    for root, _dirs, files in os.walk(args.export):
        for f in files:
            if f.lower().endswith((".step", ".stp")):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, args.export).replace("\\", "/")
                if args.only and args.only.lower() not in rel.lower():
                    continue
                steps.append((full, rel))
    steps.sort(key=lambda s: s[1])

    if not steps:
        sys.exit("no STEP files found under {}".format(args.export))
    print("checking {} parts\n".format(len(steps)))

    os.makedirs(args.out, exist_ok=True)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(
                check, args.cli, full, expected.get(rel),
                args.out, os.path.splitext(rel)[0], args.convert, args.thumbs,
            ): rel
            for full, rel in steps
        }
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            results.append(rec)
            mark = "ok  " if rec["status"] == "ok" else "FAIL"
            extra = "; ".join(rec["notes"])
            print("{} {:<58} {}".format(mark, rec["design"][:58], extra[:70]))

    results.sort(key=lambda r: r["design"])
    bad = [r for r in results if r["status"] != "ok"]

    report = os.path.join(args.out, "migration-report.json")
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(
            {"checked": len(results), "failed": len(bad), "results": results}, fh, indent=2
        )

    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    print("\n{} checked, {} need attention".format(len(results), len(bad)))
    for k, v in sorted(by_status.items()):
        print("  {:<18} {}".format(k, v))
    print("\nreport: {}".format(report))

    verified = [r for r in results if r["status"] == "ok" and r.get("volume_drift") is not None]
    if verified:
        worst = max(r["volume_drift"] for r in verified)
        print(
            "{} parts verified against Fusion's own volume (worst drift {:.2e})".format(
                len(verified), worst
            )
        )

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
