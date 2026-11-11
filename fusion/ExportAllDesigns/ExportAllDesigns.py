# SPDX-License-Identifier: Apache-2.0
# ExportAllDesigns - batch-export every Fusion design for migration to another CAD tool.
#
# Walks every project and folder in your hub(s), opens each design and writes:
#   <OUT>/<Hub>/<Project>/<folder path>/<Design>.step   geometry, portable B-rep (what SolidCraft imports)
#   <OUT>/<Hub>/<Project>/<folder path>/<Design>.f3d    Fusion archive, insurance against losing cloud access
#   <OUT>/manifest.json                                 one record per design, for reconciling the migration
#
# Resumable: a design whose outputs already exist is skipped, so you can stop it and run it again.
# Run from Fusion: Utilities > ADD-INS > Scripts and Add-Ins > ExportAllDesigns > Run.

import adsk.core
import adsk.fusion
import traceback
import json
import os
import re
import time

# ---- settings -------------------------------------------------------------
OUT_DIR = os.path.expanduser("~/cad/fusion-export")
EXPORT_STEP = True
EXPORT_F3D = True
ALL_HUBS = False  # True walks every hub you belong to, not just the active one
# ---------------------------------------------------------------------------

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name):
    """Make a Fusion name usable as one path segment on Windows and mac."""
    cleaned = _INVALID.sub("_", (name or "untitled")).strip().rstrip(".")
    # Reserved device names on Windows would make an unopenable file.
    if cleaned.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:120] or "untitled"


def measure(design):
    """Fusion's own volume/area/body count, so the import can be checked against it later.

    Fusion's API works in centimetres; the rest of the migration is in millimetres.
    Counts occurrence bodies too, because that is what a STEP export actually writes.
    """
    bodies = []
    root = design.rootComponent
    for i in range(root.bRepBodies.count):
        bodies.append(root.bRepBodies.item(i))
    for occ in root.allOccurrences:
        for i in range(occ.bRepBodies.count):
            bodies.append(occ.bRepBodies.item(i))

    volume_cm3 = 0.0
    area_cm2 = 0.0
    counted = 0
    hidden = 0
    for b in bodies:
        try:
            if not b.isSolid:
                continue
            # A STEP export writes the visible bodies, so measuring the hidden ones too makes
            # Fusion look like it holds more material than it exported and turns a correct
            # import into a reported mismatch. Count what will actually be written.
            if get(b, "isVisible") is False or get(b, "isLightBulbOn") is False:
                hidden += 1
                continue
            props = b.physicalProperties
            volume_cm3 += props.volume
            area_cm2 += props.area
            counted += 1
        except Exception:
            pass

    return {
        "bodies": len(bodies),
        "hidden_bodies": hidden,
        "solid_bodies": counted,
        "volume_mm3": volume_cm3 * 1000.0,
        "area_mm2": area_cm2 * 100.0,
    }


def collect(folder, rel_parts, found):
    """Depth-first walk of a DataFolder, collecting every Fusion design."""
    for i in range(folder.dataFiles.count):
        f = folder.dataFiles.item(i)
        try:
            if f.fileExtension == "f3d":
                found.append((f, list(rel_parts)))
        except Exception:
            pass
    for i in range(folder.dataFolders.count):
        sub = folder.dataFolders.item(i)
        collect(sub, rel_parts + [safe(sub.name)], found)


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        # Fusion versions disagree about how (and whether) they expose a sign-in flag, so ask only
        # if this build has one, and otherwise let the hub enumeration below be the real test.
        signed_in = getattr(app.data, "isSignedIn", None)
        if signed_in is False:
            ui.messageBox("Not signed in to Autodesk. Sign in, then run this again.")
            return

        # ---- enumerate every design first, so we can show real progress ----
        try:
            hubs = []
            if ALL_HUBS:
                for i in range(app.data.dataHubs.count):
                    hubs.append(app.data.dataHubs.item(i))
            else:
                hubs.append(app.data.activeHub)
            hubs = [h for h in hubs if h is not None]
        except Exception as e:
            ui.messageBox(
                "Could not reach your Autodesk hub ({}).\n\n"
                "Sign in to Fusion and make sure you are online, then run this again.".format(e)
            )
            return

        if not hubs:
            ui.messageBox(
                "No Autodesk hub available. Sign in to Fusion and make sure you are online, "
                "then run this again."
            )
            return

        designs = []  # (dataFile, [path parts])
        for hub in hubs:
            if len(hubs) > 1:
                # Only switch when there is something to switch to; the setter is not writable
                # on every build, and the single-hub case is already active.
                try:
                    app.data.activeHub = hub
                except Exception:
                    pass
            adsk.doEvents()
            hub_name = safe(hub.name)
            projects = app.data.dataProjects
            for p in range(projects.count):
                proj = projects.item(p)
                try:
                    root = proj.rootFolder
                except Exception:
                    continue
                collect(root, [hub_name, safe(proj.name)], designs)

        if not designs:
            ui.messageBox("No Fusion designs found in your hub.")
            return

        # Two designs can share a name inside one folder, and then they share an output path.
        # The resumable skip then sees the first one's files already present and passes over
        # the second without writing a record, so it vanishes silently: N found, fewer
        # exported, and nothing saying which were lost.
        #
        # Names are made unique up front instead, in enumeration order, so a second
        # "Bracket" becomes "Bracket (2)" and is exported rather than skipped.
        basenames = {}
        for i, (data_file, parts) in enumerate(designs):
            key = (tuple(parts), safe(data_file.name).lower())
            seen = basenames.setdefault(key, [])
            seen.append(i)
        unique = {}
        for key, idxs in basenames.items():
            for rank, i in enumerate(idxs):
                base = safe(designs[i][0].name)
                unique[i] = base if rank == 0 else "%s (%d)" % (base, rank + 1)
        collisions = sum(1 for idxs in basenames.values() if len(idxs) > 1)

        os.makedirs(OUT_DIR, exist_ok=True)
        manifest_path = os.path.join(OUT_DIR, "manifest.json")

        # Merge with any previous run so a resumed run keeps its earlier records.
        records = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    for r in json.load(fh).get("designs", []):
                        records[r["id"]] = r
            except Exception:
                records = {}

        total = len(designs)
        progress = ui.createProgressDialog()
        progress.isCancelButtonShown = True
        progress.show("Exporting Fusion designs", "%v / %m   %p%", 0, total, 0)

        started = time.time()
        ok = skipped = failed = 0

        for idx, (data_file, parts) in enumerate(designs):
            if progress.wasCancelled:
                break
            progress.progressValue = idx
            progress.message = "%v / %m   %p%\n" + data_file.name[:60]
            adsk.doEvents()

            out_dir = os.path.join(OUT_DIR, *parts)
            os.makedirs(out_dir, exist_ok=True)
            base = os.path.join(out_dir, unique.get(idx, safe(data_file.name)))
            step_path = base + ".step"
            f3d_path = base + ".f3d"

            want = []
            if EXPORT_STEP and not os.path.exists(step_path):
                want.append("step")
            if EXPORT_F3D and not os.path.exists(f3d_path):
                want.append("f3d")
            if not want:
                # Record it. Returning here without a record is what made designs
                # untraceable: "found N, exported fewer" with no way to name the difference.
                records.setdefault(data_file.id, {
                    "id": data_file.id, "name": data_file.name, "path": "/".join(parts),
                    "version": None, "step": os.path.relpath(step_path, OUT_DIR).replace("\\", "/"),
                    "f3d": os.path.relpath(f3d_path, OUT_DIR).replace("\\", "/"),
                    "measure": None, "error": None, "skipped": True,
                })
                skipped += 1
                continue

            rec = {
                "id": data_file.id,
                "name": data_file.name,
                "path": "/".join(parts),
                "version": None,
                "step": None,
                "f3d": None,
                "measure": None,
                "error": None,
            }

            doc = None
            try:
                # Opening invisibly is much faster, but some builds will not export from a
                # hidden document -- fall back to a visible one rather than lose the design.
                design = None
                for visible in (False, True):
                    try:
                        doc = app.documents.open(data_file, visible)
                    except Exception:
                        doc = None
                    if doc is None:
                        continue
                    adsk.doEvents()
                    design = adsk.fusion.Design.cast(
                        doc.products.itemByProductType("DesignProductType")
                    )
                    if design is not None:
                        break
                    try:
                        doc.close(False)
                    except Exception:
                        pass
                    doc = None

                if doc is None:
                    raise RuntimeError("document would not open")
                if design is None:
                    raise RuntimeError("not a Design document")

                rec["version"] = data_file.versionNumber
                try:
                    rec["measure"] = measure(design)
                except Exception as e:
                    rec["measure"] = {"error": str(e)}

                em = design.exportManager
                if "step" in want:
                    em.execute(em.createSTEPExportOptions(step_path))
                    rec["step"] = os.path.relpath(step_path, OUT_DIR).replace("\\", "/")
                elif EXPORT_STEP:
                    rec["step"] = os.path.relpath(step_path, OUT_DIR).replace("\\", "/")

                if "f3d" in want:
                    em.execute(em.createFusionArchiveExportOptions(f3d_path))
                    rec["f3d"] = os.path.relpath(f3d_path, OUT_DIR).replace("\\", "/")
                elif EXPORT_F3D:
                    rec["f3d"] = os.path.relpath(f3d_path, OUT_DIR).replace("\\", "/")

                ok += 1
            except Exception as e:
                rec["error"] = "{}: {}".format(type(e).__name__, e)
                failed += 1
            finally:
                if doc is not None:
                    try:
                        doc.close(False)
                    except Exception:
                        pass
                adsk.doEvents()

            records[rec["id"]] = rec

            # Write as we go, so a crash or a cancel still leaves a usable manifest.
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "out_dir": OUT_DIR,
                        "total_found": total,
                        "designs": list(records.values()),
                    },
                    fh,
                    indent=2,
                )

        progress.hide()
        elapsed = int(time.time() - started)
        ui.messageBox(
            "Export finished.\n\n"
            "exported: {}\nskipped (already present): {}\nfailed: {}\n"
            "found: {}\nelapsed: {}m {}s\n\n{}".format(
                ok, skipped, failed, total, elapsed // 60, elapsed % 60, OUT_DIR
            )
        )

    except Exception:
        if ui:
            ui.messageBox("Script failed:\n{}".format(traceback.format_exc()))
