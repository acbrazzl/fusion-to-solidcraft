#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a visual inventory of a Fusion archive without opening Fusion.

Every .f3d carries Fusion's own rendered previews. Modern ones store zip entries with Zstandard
(method 93), which Python's zipfile refuses, so this reads the local headers directly and
decompresses the frames itself.

The output is a contact sheet: every design you own, with its picture, cross-referenced against
the STEP migration so you can see at a glance what came across and what still needs attention.

    python f3d_inventory.py                       inventory ~/cad/fusion-export
    python f3d_inventory.py --root ~/Downloads    somewhere else (e.g. an Autodesk take-out)
    python f3d_inventory.py --open                open the contact sheet when done

Needs: pip install --user zstandard
"""

import argparse
import concurrent.futures
import html
import io
import json
import os
import struct
import sys
import webbrowser
import zipfile
import zlib

try:
    import zstandard
except ImportError:
    zstandard = None

STORED, DEFLATE, ZSTD = 0, 8, 93


def read_entry(path, info):
    """Read one zip entry, including the Zstandard entries zipfile cannot open."""
    with open(path, "rb") as fh:
        fh.seek(info.header_offset)
        if fh.read(4) != b"PK\x03\x04":
            raise ValueError("bad local header")
        hdr = fh.read(26)
        nlen, elen = struct.unpack("<HH", hdr[22:26])
        fh.seek(nlen + elen, 1)
        raw = fh.read(info.compress_size)

    if info.compress_type == STORED:
        return raw
    if info.compress_type == DEFLATE:
        return zlib.decompress(raw, -15)
    if info.compress_type == ZSTD:
        if zstandard is None:
            raise RuntimeError("needs zstandard: pip install --user zstandard")
        return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    raise NotImplementedError("compression method {}".format(info.compress_type))


def png_size(data):
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None


def extract(f3d_path, out_dir, rel):
    """Pull the best preview out of one .f3d."""
    rec = {
        "design": os.path.splitext(os.path.basename(f3d_path))[0],
        "path": rel,
        "bytes": os.path.getsize(f3d_path),
        "preview": None,
        "preview_px": None,
        "error": None,
    }
    try:
        z = zipfile.ZipFile(f3d_path)
        infos = {i.filename: i for i in z.infolist()}
    except Exception as e:
        rec["error"] = "not readable as a container: {}".format(e)
        return rec

    # Prefer the large preview; fall back to the thumbnail.
    cands = [n for n in infos if n.lower().endswith("previews/big.png")]
    cands += [n for n in infos if n.lower().endswith("previews/small.png")]
    if not cands:
        rec["error"] = "no preview stored in this design"
        return rec

    for name in cands:
        try:
            data = read_entry(f3d_path, infos[name])
        except Exception as e:
            rec["error"] = str(e)
            continue
        size = png_size(data)
        if not size:
            rec["error"] = "preview was not a PNG"
            continue
        dest = os.path.join(out_dir, "previews", rel.replace("/", "__") + ".png")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        rec["preview"] = os.path.relpath(dest, out_dir).replace("\\", "/")
        rec["preview_px"] = list(size)
        rec["error"] = None
        break

    return rec


def migration_status(root, rel, report):
    """Did this design's geometry make it across?"""
    stem = os.path.splitext(rel)[0]
    if report:
        r = report.get(stem)
        if r:
            return r["status"], r
    if os.path.exists(os.path.join(root, stem + ".step")):
        return "step-present", None
    return "no-step", None


PAGE = """<!doctype html><meta charset=utf-8><title>Fusion archive — {n} designs</title>
<style>
:root{{color-scheme:light dark;--bg:#fbfbfd;--fg:#16161a;--mut:#6b6b76;--card:#fff;--line:#e3e3ea;
--ok:#1a7f4b;--okbg:#e8f6ee;--warn:#8a5a00;--warnbg:#fdf3e0;--bad:#a3282b;--badbg:#fbeaea}}
@media(prefers-color-scheme:dark){{:root{{--bg:#131316;--fg:#ececf0;--mut:#9a9aa6;--card:#1c1c21;
--line:#2c2c34;--ok:#5fd39a;--okbg:#13301f;--warn:#e0ad5c;--warnbg:#33260f;--bad:#f08a8a;--badbg:#331618}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}}
header{{padding:28px 32px 18px;border-bottom:1px solid var(--line)}}
h1{{margin:0 0 6px;font-size:21px;letter-spacing:-.01em}}
.sub{{color:var(--mut)}}
.bar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;padding:14px 32px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}}
input{{flex:1;min-width:200px;padding:8px 11px;border:1px solid var(--line);border-radius:7px;
background:var(--card);color:var(--fg);font:inherit}}
button{{padding:7px 13px;border:1px solid var(--line);border-radius:7px;background:var(--card);
color:var(--fg);font:inherit;cursor:pointer}}
button[aria-pressed=true]{{background:var(--fg);color:var(--bg);border-color:var(--fg)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px;padding:22px 32px 56px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:11px;overflow:hidden;
display:flex;flex-direction:column}}
.shot{{aspect-ratio:1;background:#fff;display:grid;place-items:center;overflow:hidden}}
@media(prefers-color-scheme:dark){{.shot{{background:#f4f4f6}}}}
.shot img{{width:100%;height:100%;object-fit:contain}}
.none{{color:#9a9aa6;font-size:12px}}
.meta{{padding:10px 12px 12px;border-top:1px solid var(--line)}}
.name{{font-weight:600;word-break:break-word;margin-bottom:3px}}
.loc{{color:var(--mut);font-size:11.5px;word-break:break-all;margin-bottom:8px}}
.tag{{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;font-weight:600}}
.ok{{background:var(--okbg);color:var(--ok)}}.warn{{background:var(--warnbg);color:var(--warn)}}
.bad{{background:var(--badbg);color:var(--bad)}}
.sz{{float:right;color:var(--mut);font-size:11px;font-weight:400}}
.empty{{padding:48px 32px;color:var(--mut)}}
</style>
<header><h1>Fusion archive</h1>
<div class=sub>{n} designs · {withprev} with previews · {migrated} with verified geometry · {root}</div></header>
<div class=bar><input id=q placeholder="Filter by name or folder…">
<button data-f=all aria-pressed=true>All</button>
<button data-f=ok>Migrated</button>
<button data-f=todo>Needs attention</button></div>
<div class=grid id=g>{cards}</div>
<div class=empty id=none hidden>Nothing matches.</div>
<script>
const g=document.getElementById('g'),q=document.getElementById('q'),none=document.getElementById('none');
let f='all';
function apply(){{
  const t=q.value.toLowerCase();let shown=0;
  for(const c of g.children){{
    const hay=c.dataset.k, st=c.dataset.s;
    const okf = f==='all' || (f==='ok'? st==='ok' : st!=='ok');
    const vis = okf && (!t || hay.includes(t));
    c.hidden=!vis; if(vis)shown++;
  }}
  none.hidden=shown>0;
}}
q.addEventListener('input',apply);
for(const b of document.querySelectorAll('button[data-f]'))
  b.addEventListener('click',()=>{{f=b.dataset.f;
    document.querySelectorAll('button[data-f]').forEach(x=>x.setAttribute('aria-pressed',x===b));apply();}});
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/cad/fusion-export"))
    ap.add_argument("--out", default=os.path.expanduser("~/cad/inventory"))
    ap.add_argument("--report", default=os.path.expanduser("~/cad/fusion-import/migration-report.json"))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--open", action="store_true", help="open the contact sheet when done")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.root))
    out = os.path.abspath(os.path.expanduser(args.out))
    if not os.path.isdir(root):
        sys.exit("no such folder: {}".format(root))
    if zstandard is None:
        print("warning: zstandard not installed, modern .f3d previews will not extract\n"
              "         pip install --user zstandard\n", file=sys.stderr)

    files = []
    for dirpath, _d, names in os.walk(root):
        for n in names:
            if n.lower().endswith(".f3d"):
                full = os.path.join(dirpath, n)
                files.append((full, os.path.relpath(full, root).replace("\\", "/")))
    files.sort(key=lambda x: x[1])
    if not files:
        sys.exit("no .f3d files under {}".format(root))

    report = {}
    if os.path.exists(args.report):
        try:
            with open(args.report, encoding="utf-8") as fh:
                for r in json.load(fh).get("results", []):
                    report[r["design"]] = r
        except Exception:
            pass

    os.makedirs(out, exist_ok=True)
    print("reading {} designs...".format(len(files)))

    recs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(extract, f, out, rel): rel for f, rel in files}
        for fut in concurrent.futures.as_completed(futs):
            recs.append(fut.result())
    recs.sort(key=lambda r: r["path"])

    cards = []
    migrated = 0
    for r in recs:
        status, det = migration_status(root, r["path"], report)
        if status == "ok":
            migrated += 1
            tag, cls, label = "ok", "ok", "geometry verified"
        elif status == "step-present":
            tag, cls, label = "warn", "warn", "STEP not yet checked"
        elif status == "no-step":
            tag, cls, label = "warn", "warn", "no STEP yet"
        else:
            tag, cls, label = "bad", "bad", status.replace("-", " ")

        folder = os.path.dirname(r["path"]) or "."
        if r["preview"]:
            shot = '<img loading=lazy src="{}" alt="{}">'.format(
                html.escape(r["preview"]), html.escape(r["design"])
            )
        else:
            shot = '<span class=none>{}</span>'.format(html.escape(r["error"] or "no preview"))

        cards.append(
            '<div class=card data-k="{k}" data-s="{s}"><div class=shot>{shot}</div>'
            '<div class=meta><div class=name>{name}<span class=sz>{mb}</span></div>'
            '<div class=loc>{folder}</div><span class="tag {cls}">{label}</span></div></div>'.format(
                k=html.escape((r["design"] + " " + folder).lower()),
                s="ok" if status == "ok" else "todo",
                shot=shot,
                name=html.escape(r["design"]),
                mb="{:.1f} MB".format(r["bytes"] / 1e6),
                folder=html.escape(folder),
                cls=cls,
                label=html.escape(label),
            )
        )

    with_prev = sum(1 for r in recs if r["preview"])
    page = PAGE.format(
        n=len(recs), withprev=with_prev, migrated=migrated,
        root=html.escape(root), cards="\n".join(cards),
    )
    sheet = os.path.join(out, "index.html")
    with open(sheet, "w", encoding="utf-8") as fh:
        fh.write(page)
    with open(os.path.join(out, "inventory.json"), "w", encoding="utf-8") as fh:
        json.dump({"root": root, "count": len(recs), "designs": recs}, fh, indent=2)

    failed = [r for r in recs if not r["preview"]]
    print("\n{} designs, {} previews extracted, {} with verified geometry".format(
        len(recs), with_prev, migrated))
    for r in failed[:10]:
        print("  no preview: {} - {}".format(r["path"], r["error"]))
    if len(failed) > 10:
        print("  ... and {} more".format(len(failed) - 10))
    print("\ncontact sheet: {}".format(sheet))

    if args.open:
        webbrowser.open("file:///" + sheet.replace("\\", "/"))


if __name__ == "__main__":
    sys.exit(main())
