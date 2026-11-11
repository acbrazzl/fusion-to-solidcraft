# fusion-to-solidcraft

Scripts that move designs from Autodesk Fusion 360 to [SolidCraft](https://github.com/tangency-labs/solidcraft),
an open-source parametric CAD application. Two scripts run inside Fusion and write each design's
geometry (STEP), Fusion's own volume and area for it, and its parametric timeline (parameters,
sketches, constraints and features) as JSON. Scripts run outside Fusion then translate those
timelines into SolidCraft designs and check every result against Fusion's measurements.

Method, verification and results are described in
[Rebuilding a Fusion 360 parametric timeline in SolidCraft with an LLM agent](https://totaloverwatchproductions.com/page/fusion-to-solidcraft-timeline/).

**Status: early, changes often.** Expect the JSON shape (`SCHEMA` in `ExtractTimeline.py`) and
the translator's coverage to move between commits.

## Requirements

- A licensed Fusion 360 installation, signed in to the account that owns the designs. Extraction
  runs inside Fusion.
- Your own designs.
- Python 3.9+ for the scripts that run outside Fusion.
- `solidcraft-cli` for translation and verification: on `PATH`, or named by `$SOLIDCRAFT_CLI`.
- Optional, for `f3d_inventory.py`: `pip install zstandard`.

The scripts use only Fusion's public Python scripting API. They do not read, parse or
reverse-engineer the `.f3d` format. (`f3d_inventory.py` reads only the preview images that an
`.f3d` archive carries as ordinary zip entries.)

## Install the Fusion scripts

Copy the two folders under `fusion/` into Fusion's scripts folder:

- Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Scripts\`
- macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/Scripts/`

so that you have `Scripts/ExportAllDesigns/ExportAllDesigns.py` and
`Scripts/ExtractTimeline/ExtractTimeline.py`. In Fusion, open
**Utilities → Add-Ins → Scripts and Add-Ins**; both appear under *My Scripts*.

Settings are constants at the top of each script:

| Script | Setting | Meaning |
|---|---|---|
| `ExportAllDesigns` | `OUT_DIR` | Output folder (default `~/cad/fusion-export`) |
| | `EXPORT_STEP`, `EXPORT_F3D` | Which files to write per design |
| | `ALL_HUBS` | Walk every hub you belong to, not only the active one |
| `ExtractTimeline` | `OUT_DIR` | Output folder (default `~/cad/fusion-timeline`) |
| | `SELECT` | `"all"`, a group name from `GROUPS`, `"list"` (paths in `SELECT_LIST`), or a substring of the design name or folder |
| | `LIMIT` | Stop after this many designs; `0` = no limit. Use a small number for a first run |
| | `ROLLBACK_FOR_EDGES`, `MEASURE_EACH_STEP`, `PROBE_UNMAPPED` | Roll back to read consumed inputs; record measurements after every step; record what unmapped feature types expose |

## Run

1. **ExportAllDesigns** (in Fusion). Writes one STEP and one F3D per design under
   `OUT_DIR/<Hub>/<Project>/<folders>/`, and `manifest.json` with Fusion's own volume, area and
   body count per design. Only visible bodies are measured, because only visible bodies are
   exported. Resumable: designs already exported are skipped.
2. **ExtractTimeline** (in Fusion). Writes one `<Design>.timeline.json` per design: user
   parameters with their expressions, sketch curves, constraints and dimensions, and each feature
   with its inputs. Resumable: designs already at the current `SCHEMA` are skipped.

Both scripts are meant to be started and left alone. A large hub takes a long time, and Fusion's
window can stay unresponsive for minutes while designs are enumerated.

Then, outside Fusion:

| Script | Reads | Produces |
|---|---|---|
| `migrate.py` | the export tree and `manifest.json` | imports each STEP with `solidcraft-cli`, compares volume and area with Fusion's, writes `migration-report.json`; `--convert` also saves a `.solidcraft` per design, `--thumbs` a PNG |
| `timeline_to_solidcraft.py` | a `.timeline.json` or a tree (`--all`) | a SolidCraft command script per design plus a `.coverage.json` naming every step that was or was not translated, and why; `--verify` builds it and compares with the manifest |
| `build_library.py` | the timelines and the export | one `.solidcraft` per design by the best route available — the rebuilt timeline (editable) or the STEP import (exact, not editable) — and `library.json` saying which route each took |
| `f3d_inventory.py` | a folder of `.f3d` files | an HTML contact sheet of every design's preview, cross-referenced with the migration report |

Each script prints `--help`.

## Verification

A rebuild is checked against Fusion's numbers, not against "it built":

- **Whole design:** the rebuilt volume is compared with the volume Fusion reported at export.
  The tolerance (1e-3 relative) was set from a control part: curved-face volumes are measured from
  a tessellation, so a plain cylinder reads about 1e-4 under its analytic value.
- **Per step:** with `MEASURE_EACH_STEP` on, `ExtractTimeline` rolls the timeline marker to each step
  and records volume, area, body count and bounding box there. A rebuild is replayed one step at a
  time and compared at each one, so a difference is attributed to the step that introduced it.
  Compare what each step adds, not only the running total, and compare the bounding box as well
  as the volume: a feature built at the wrong height keeps the volume and moves the box.
- **Consumed inputs:** a fillet, chamfer or draft destroys the edges or faces it was given, so
  those are read with the timeline rolled back to just before the feature. They are recorded as
  points on the geometry (and, for faces, area and centroid), which is how SolidCraft selects.
- **Nothing is dropped silently:** an untranslated step is listed in the coverage report with a
  reason; an unreadable selection is recorded as such in the timeline.

`golden/run_golden.py` is the regression suite for the translator. It writes timelines for
invented shapes whose volumes are known in closed form (a rounded plate, a chamfered block, a
shelled box, a parametric plate rebuilt at a second thickness, a constrained sketch) plus negative
controls (an unmapped feature type and a suppressed feature, which must be reported, not
translated), translates and builds them, and compares. It needs `solidcraft-cli`; it does not
need Fusion.

## What translates

As of the mid-October 2026 survey:

- **Translated:** sketches (with constraints and dimensions), extrude, revolve, fillet, chamfer,
  shell, draft, offset faces.
- **Not yet translated:** loft, sweep, hole, pattern, mirror, combine, thread, sheet metal,
  assemblies.

Designs with no modelling history (imported bodies, assemblies) have no timeline to translate;
for those the STEP route in `migrate.py` / `build_library.py` is the correct result.
Many build failures are in the SolidCraft kernel rather than in translation, and they change as
SolidCraft changes.

## Design data stays out of this repository

`.gitignore` excludes geometry (`*.f3d`, `*.step`, `*.stl`, …), every JSON/HTML/CSV output of the
pipeline, renders, and the default output folders. Keep it that way in forks: an extracted timeline
contains the full design.

## License

Apache License 2.0 — see `LICENSE` and `NOTICE`. Copyright 2026 Total Overwatch Productions LLC.

Not affiliated with or endorsed by Autodesk, Inc. Fusion 360 is a trademark of Autodesk, Inc.
