"""Local-only reproducible benchmark. No fixtures or network retrieval are embedded.

Prepare separates source-family gold files. Development selection never opens
validation gold; evaluate validation only after writing a method freeze receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import TextStringObject

from buna.documents import _structure, extract_document


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def validate_split(manifest: dict) -> None:
    development, validation = set(manifest["development_ids"]), set(manifest["validation_ids"])
    if development & validation:
        raise ValueError("A source cannot be development and validation")
    families = defaultdict(set)
    for source in manifest["sources"]:
        if source["source_id"] in development:
            families[source["family"]].add("development")
        if source["source_id"] in validation:
            families[source["family"]].add("validation")
    if any(len(splits) > 1 for splits in families.values()):
        raise ValueError("A source family crosses the locked split")


def original_page(page) -> dict:
    """Read original-font glyphs and highlight paths, excluding report overlays.

    This is an auditable adapter for this PDF family, NOT a vendor token parser.
    Unsupported fonts and ambiguous source assignments are counted, not guessed.
    """
    fonts = page["/Resources"].get("/Font", {})
    fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
    state = {"color": (0, 0, 0), "font": None, "size": 12, "cs": 0, "ws": 0}
    stack, points, glyphs, rectangles, badges = [], [], [], [], []
    unsupported = set()

    def xy(x, y, matrix):
        return (float(x) * matrix[0] + float(y) * matrix[2] + matrix[4],
                float(x) * matrix[1] + float(y) * matrix[3] + matrix[5])

    def before(op, args, cm, tm):
        nonlocal state, points
        if op == b"q":
            stack.append(state.copy())
        elif op == b"Q" and stack:
            state = stack.pop()
        elif op == b"rg":
            state["color"] = tuple(round(float(x), 6) for x in args)
        elif op == b"g":
            state["color"] = (float(args[0]),) * 3
        elif op == b"Tf":
            state.update(font=str(args[0]), size=float(args[1]))
        elif op == b"Tc":
            state["cs"] = float(args[0])
        elif op == b"Tw":
            state["ws"] = float(args[0])
        elif op in (b"m", b"l"):
            points.append(xy(args[0], args[1], cm))
        elif op == b"c":
            points.extend(xy(args[i], args[i + 1], cm) for i in range(0, 6, 2))
        elif op == b"re":
            x, y, w, h = map(float, args)
            points.extend([xy(x, y, cm), xy(x + w, y + h, cm)])
        elif op in (b"f", b"f*"):
            if points:
                xs, ys = zip(*points)
                box = (min(xs), min(ys), max(xs), max(ys))
                if box[0] > 40 and 4 < box[3] - box[1] < 25 and max(state["color"]) - min(state["color"]) > .02:
                    rectangles.append({"bbox": box, "color": state["color"]})
            points = []
        elif op in (b"n", b"S", b"s"):
            points = []
        if op not in (b"TJ", b"Tj") or not state["font"]:
            return
        font = fonts[state["font"]].get_object()
        if "NotoSans" in str(font.get("/BaseFont", "")):
            return
        widths, first = font.get("/Widths"), font.get("/FirstChar")
        if widths is None or first is None:
            unsupported.add(str(font.get("/BaseFont")))
            return
        widths = widths.get_object()
        x, y, size = float(tm[4]), float(tm[5]), state["size"]
        for part in args[0] if op == b"TJ" else [args[0]]:
            if isinstance(part, (float, int)):
                x -= float(part) * size / 1000
                continue
            text = str(part) if isinstance(part, TextStringObject) else bytes(part).decode("cp1252", errors="replace")
            for char in text:
                try:
                    code = char.encode("cp1252")[0]
                except UnicodeEncodeError:
                    code = 32
                width = float(widths[code - int(first)]) if 0 <= code - int(first) < len(widths) else 500
                advance = width * size / 1000 + state["cs"] + (state["ws"] if char == " " else 0)
                gx, gy = xy(x + advance / 2, y + size * .2, cm)
                if gx > 40 and size >= 9:
                    glyphs.append((gx, gy, char))
                x += advance

    def visitor(text, cm, tm, font, size):
        text = text.strip()
        if text.isdigit() and tm[4] < 40 and size < 9:
            badges.append({"source_id": int(text), "y": float(tm[5])})

    page.extract_text(visitor_operand_before=before, visitor_text=visitor)
    glyphs.sort(key=lambda g: (-round(g[1], 1), g[0]))
    text, positions, last_y = "", [], None
    for glyph in glyphs:
        y = round(glyph[1], 1)
        if last_y is not None and abs(last_y - y) > 1:
            text += "\n"
            positions.append(None)
        text += glyph[2]
        positions.append(glyph)
        last_y = y
    for rectangle in rectangles:
        x0, y0, x1, y1 = rectangle["bbox"]
        indices = [i for i, glyph in enumerate(positions) if glyph and
                   x0 - .2 <= glyph[0] <= x1 + .2 and y0 - .2 <= glyph[1] <= y1 + .2]
        rectangle.update(text="".join(text[i] for i in indices), positions=indices)
    # In this report family highlights are painted in source-rank order, then
    # badges in the same order. Locate each group's first rectangle by badge Y.
    cursor, starts, ambiguities = 0, [], []
    for badge in badges:
        candidates = [i for i in range(cursor, len(rectangles))
                      if abs(rectangles[i]["bbox"][1] + 2.8 - badge["y"]) < 2]
        if not candidates:
            ambiguities.append({"badge": badge, "reason": "No unambiguous following paint path at badge height."})
            continue
        start = candidates[0]
        starts.append((start, badge))
        cursor = start + 1
    for index, (start, badge) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(rectangles)
        color = rectangles[start]["color"]
        last_y = float("inf")
        for rectangle in rectangles[start:stop]:
            if rectangle["color"] != color or rectangle["bbox"][1] > last_y + 1:
                ambiguities.append({"badge": badge, "reason": "Paint order/color reset; remaining rectangles not assigned."})
                break
            rectangle.update(source_id=badge["source_id"], group_y=badge["y"])
            last_y = rectangle["bbox"][1]
    return {"text": text, "rectangles": rectangles, "badges": badges,
            "ambiguities": ambiguities, "unsupported_fonts": sorted(unsupported)}


def prepare(root: Path) -> None:
    manifest = json.loads((root / "manifest.lock.json").read_text())
    validate_split(manifest)
    ref = Path(manifest["inputs"]["reference"]["path"])
    manuscript = Path(manifest["inputs"]["candidate_manuscript"]["path"])
    for key, path in [("reference", ref), ("candidate_manuscript", manuscript)]:
        if digest(path) != manifest["inputs"][key]["sha256"]:
            raise ValueError(f"{key} hash mismatch")
    pages = []
    gold = defaultdict(lambda: {"positions": set(), "observed_pages": set(), "groups": [], "ambiguities": []})
    offset = 0
    reader = PdfReader(ref)
    for number, page in enumerate(reader.pages[8:], 1):
        data = original_page(page)
        pages.append({"number": number, "text": data["text"]})
        group_map = defaultdict(list)
        for rectangle in data["rectangles"]:
            if "source_id" not in rectangle:
                continue
            sid = rectangle["source_id"]
            positions = {offset + i for i in rectangle["positions"] if data["text"][i].isalnum()}
            gold[sid]["positions"].update(positions)
            gold[sid]["observed_pages"].add(number)
            group_map[(sid, rectangle["group_y"])].append({
                "bbox": rectangle["bbox"], "text": rectangle["text"], "positions": sorted(positions),
                "partial_numeric": bool(re.search(r"\d[.,]$", rectangle["text"].strip())),
            })
        for (sid, y), rectangles in group_map.items():
            gold[sid]["groups"].append({"reference_pdf_page": number + 8, "submission_page": number,
                                        "badge_y": y, "rectangles": rectangles})
        for badge in data["badges"]:
            gold[badge["source_id"]]["observed_pages"].add(number)
        for issue in data["ambiguities"]:
            gold[issue["badge"]["source_id"]]["ambiguities"].append({"page": number + 8, **issue})
        offset += len(data["text"]) + 2
    target = _structure(pages, "Reference embedded submission (rendered-text surrogate)", [
        "Reconstructed original PDF glyphs exclude report badges/overlays, retain original headers.",
        "Not the original uploaded bytes or vendor token stream. Full 45-page submission, including wrappers.",
    ])
    write_new(root / "target.json", target)
    candidate = extract_document(manuscript)
    same_pages = []
    for i, page in enumerate(candidate["pages"]):
        expected = pages[i + 2]["text"] if i + 2 < len(pages) else ""
        ratio = SequenceMatcher(None, re.sub(r"\s+", "", page["text"]).casefold(),
                                re.sub(r"\s+", "", expected).casefold(), autojunk=False).ratio()
        same_pages.append({"candidate_page": i + 1, "reference_pdf_page": i + 11, "normalized_character_ratio": ratio})
    write_new(root / "input-comparison.json", {"candidate_pages": len(candidate["pages"]),
               "target_submission_pages": len(pages), "page_alignment": same_pages,
               "vendor_word_count": 12312, "candidate_tokens": len(re.findall(r"\w+", candidate["text"])),
               "target_tokens": len(re.findall(r"\w+", target["text"])), "same_submission_bytes": False})
    # Prepare labels by split into distinct files; selection reads only dev.
    dev_positions = set().union(*(gold[sid]["positions"] for sid in manifest["development_ids"]))
    for split, ids in [("development", manifest["development_ids"]), ("validation", manifest["validation_ids"])]:
        labels = {}
        for sid in ids:
            value = gold[sid]
            labels[str(sid)] = {**value, "positions": sorted(value["positions"]),
                                "observed_pages": sorted(value["observed_pages"]),
                                "development_overlap_positions": sorted(value["positions"] & dev_positions) if split == "validation" else []}
        write_new(root / f"gold-{split}.json", {"scope": "Visible source-assigned PDF glyph positions only; hidden matches unadjudicated", "sources": labels})
    sources = []
    for row in manifest["sources"]:
        if row["status"] not in ("downloaded", "user-supplied"):
            continue
        path = Path(row["path"])
        if digest(path) != row["sha256"]:
            raise ValueError("Source hash mismatch")
        document = extract_document(path)
        out = root / f"source-{row['source_id']}.json"
        write_new(out, document)
        sources.append({"id": str(row["source_id"]), "title": row.get("title", document["title"]),
                        "family": row["family"], "split": row["split"], "document_file": str(out),
                        "sha256": row["sha256"], "doi": row.get("doi"),
                        "pages": len(document["pages"]), "warnings": document["warnings"]})
    write_new(root / "prepared-sources.json", {"sources": sources, "target_hash": digest(root / "target.json"),
                                            "manifest_hash": digest(root / "manifest.lock.json")})


def position_metrics(predicted: set[int], gold: set[int], observed: set[int], exclude: set[int] | None = None) -> dict:
    exclude = exclude or set()
    predicted, gold, observed = predicted - exclude, gold - exclude, observed - exclude
    in_scope = predicted & observed
    true = len(predicted & gold)
    return {"matched_reference_characters": true, "reference_characters": len(gold),
            "predicted_characters_in_observed_pages": len(in_scope),
            "additional_unadjudicated_characters": len(in_scope - gold),
            "predicted_characters_outside_observed_pages": len(predicted - observed),
            "reference_recall": true / len(gold) if gold else None,
            "strict_visible_precision": true / len(in_scope) if in_scope else None,
            "adjudicated_precision": None, "adjudicated_f1": None,
            "excluded_leakage_characters": len(exclude)}


def evaluate(root: Path, split: str, method: str, config: dict | None = None) -> dict:
    if split == "validation" and not (root / "method.freeze.json").exists():
        raise ValueError("Freeze the method before reading validation annotations")
    if split == "validation":
        freeze = json.loads((root / "method.freeze.json").read_text())
        for filename, expected in [("manifest.lock.json", freeze["manifest_hash"]),
                                   ("gold-validation.json", freeze["validation_gold_hash"])]:
            if digest(root / filename) != expected:
                raise ValueError("Frozen manifest or validation annotations changed")
        for filename, expected in freeze["code_hashes"].items():
            if digest(Path(__file__).parent / filename) != expected:
                raise ValueError("Frozen comparison code changed; this validation is no longer held out")
        if method != "baseline" and config != freeze["selected_policy"]:
            raise ValueError("Only the frozen selected policy can access validation gold")
    target = json.loads((root / "target.json").read_text())
    labels = json.loads((root / f"gold-{split}.json").read_text())["sources"]
    sources = [row for row in json.loads((root / "prepared-sources.json").read_text())["sources"] if row["split"] == split]
    if method.startswith("baseline"):
        spec = importlib.util.spec_from_file_location("buna._frozen_baseline", root / "baseline_2_3.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        compare = module.compare_documents
    else:
        from buna.comparison import compare_documents
        compare = compare_documents
    results = []
    for source in sources:
        started = time.monotonic()
        document = json.loads(Path(source["document_file"]).read_text())
        kwargs = {} if method.startswith("baseline") else {"match_policy": config}
        report = compare(target, [{**source, "document": document}], **kwargs)
        path = root / f"result-{split}-{method}-{source['id']}.json"
        write_new(path, report)
        predicted = set()
        for match in report["matches"]:
            for a, b in match["manuscript"].get("scored_highlights", match["manuscript"]["highlights"]):
                if match.get("included_words"):
                    start = match["manuscript"]["start"]
                    predicted.update(i for i in range(start + a, start + b) if target["text"][i].isalnum())
        label = labels[source["id"]]
        observed = set()
        offset = 0
        for page in target["pages"]:
            if page["number"] in label["observed_pages"]:
                observed.update(i for i in range(offset, offset + len(page["text"])) if target["text"][i].isalnum())
            offset += len(page["text"]) + 2
        metric = position_metrics(predicted, set(label["positions"]), observed, set(label["development_overlap_positions"]))
        metric.update(source_id=source["id"], family=source["family"], seconds=time.monotonic() - started,
                      score=report["metrics"], outcomes=report["source_coverage"],
                      ambiguous_gold_groups=len(label["ambiguities"]),
                      fully_covered_groups=sum(
                          set(pos for rect in group["rectangles"] for pos in rect["positions"] if target["text"][pos].isalnum()) <= predicted
                          for group in label["groups"]
                      ), visible_groups=len(label["groups"]),
                      all_positions=position_metrics(predicted, set(label["positions"]), observed))
        results.append(metric)
    summed = {key: sum(row[key] for row in results) for key in
              ["matched_reference_characters", "reference_characters", "predicted_characters_in_observed_pages",
               "additional_unadjudicated_characters", "predicted_characters_outside_observed_pages"]}
    recall = [row["reference_recall"] for row in results if row["reference_recall"] is not None]
    precision = [row["strict_visible_precision"] for row in results if row["strict_visible_precision"] is not None]
    return {"method": method, "split": split, "config": config, "sources": results,
            "micro": {**summed, "reference_recall": summed["matched_reference_characters"] / summed["reference_characters"] if summed["reference_characters"] else None,
                      "strict_visible_precision": summed["matched_reference_characters"] / summed["predicted_characters_in_observed_pages"] if summed["predicted_characters_in_observed_pages"] else None},
            "macro": {"reference_recall": sum(recall) / len(recall) if recall else None,
                      "strict_visible_precision": sum(precision) / len(precision) if precision else None,
                      "recall_source_denominator": len(recall), "precision_source_denominator": len(precision)},
            "adjudicated_precision": None, "adjudicated_f1": None, "ninety_percent_claim": False,
            "limitations": ["Visible top-source gold is incomplete; additional predictions are unadjudicated, not automatically false positives.",
                           "Validation is same-manuscript source-family validation, not an independent unseen-document holdout."]}


def freeze_method(root: Path) -> None:
    manifest = json.loads((root / "manifest.lock.json").read_text())
    scores = []
    for policy in manifest["candidate_families"]:
        confirmed = root / f"metrics-development-{policy['id']}-confirmed.json"
        metrics = json.loads((confirmed if confirmed.exists() else root / f"metrics-development-{policy['id']}.json").read_text())
        precision, recall = metrics["micro"]["strict_visible_precision"] or 0, metrics["micro"]["reference_recall"] or 0
        harmonic = 2 * precision * recall / (precision + recall) if precision + recall else 0
        scores.append((harmonic, policy))
    selected = max(scores, key=lambda item: (item[0], -item[1]["maximum_gap"]))[1]
    write_new(root / "method.freeze.json", {
        "selected_policy": selected,
        "selection_metrics": [{"id": policy["id"], "strict_visible_harmonic": score} for score, policy in scores],
        "code_hashes": {filename: digest(Path(__file__).parent / filename) for filename in ["comparison.py", "local_alignment.py"]},
        "manifest_hash": digest(root / "manifest.lock.json"),
        "development_gold_hash": digest(root / "gold-development.json"),
        "validation_gold_hash": digest(root / "gold-validation.json"),
        "production_default_change": False,
        "reason": "Strict-visible harmonic selects an experimental candidate, not proof of adjudicated accuracy. Production default remains prior policy.",
    })


def synthetic_metrics(root: Path) -> dict:
    """Fully labeled structural controls, separate from incomplete reference gold."""
    freeze = json.loads((root / "method.freeze.json").read_text())
    from buna.comparison import compare_documents
    phrase = "amber birds gather beside quiet rivers during winter mornings"
    methods = "all participants provided informed consent before completing the experimental task"
    cases = [
        ("exact", phrase, phrase, phrase),
        ("insertion", phrase, phrase.replace("beside", "calmly beside"), phrase),
        ("methods", methods, methods, methods),
        ("case-linewrap", phrase.upper().replace(" ", "\n"), phrase, phrase.upper().replace(" ", "\n")),
        ("short-eight", " ".join(phrase.split()[:8]), " ".join(phrase.split()[:8]), ""),
        ("reordered", " ".join(reversed(phrase.split())), phrase, ""),
        ("numeric-template", "M = 0.12 SD = 0.23 M = 0.34 SD = 0.45", "M = 0.78 SD = 0.89 M = 0.91 SD = 0.92", ""),
        ("concept-only", "warm rainfall causes trees to grow along the river", "water promotes forest development beside flowing streams", ""),
        ("long-gaps", phrase, (" unrelated description " * 20).join(phrase.split()), ""),
        ("quoted", '"' + phrase + '"', phrase, ""),
        ("bibliography", "Original text.\n\nReferences\n" + phrase, phrase, ""),
    ]
    values = []
    for name, left, right, gold_phrase in cases:
        target = _structure([{"number": 1, "text": "Target.\n" + left}], "Synthetic target", [])
        source = _structure([{"number": 1, "text": "Source.\n" + right}], "Synthetic source", [])
        report = compare_documents(target, [{"id": "synthetic", "document": source}], match_policy=freeze["selected_policy"])
        expected = set()
        if gold_phrase:
            start = target["text"].index(gold_phrase)
            expected = {start + i for i, char in enumerate(gold_phrase) if char.isalnum()}
        predicted = set()
        for match in report["matches"]:
            if match["included_words"]:
                for a, b in match["manuscript"]["highlights"]:
                    start = match["manuscript"]["start"]
                    predicted.update(i for i in range(start + a, start + b) if target["text"][i].isalnum())
        values.append({"case": name, "true_positive": len(predicted & expected),
                       "false_positive": len(predicted - expected), "false_negative": len(expected - predicted)})
    tp, fp, fn = (sum(row[key] for row in values) for key in ("true_positive", "false_positive", "false_negative"))
    return {"scope": "11 fully labeled original synthetic structural controls; NOT published-source adjudicated precision.",
            "cases": values, "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None}


def summary(root: Path) -> dict:
    files = {
        "development_baseline": "metrics-development-baseline-confirmed.json",
        "development_candidate": "metrics-development-ordered-chain-strict-confirmed.json",
        "validation_baseline": "metrics-validation-baseline.json",
        "validation_candidate": "metrics-validation-ordered-chain-strict.json",
    }
    reports = {key: json.loads((root / path).read_text()) for key, path in files.items()}
    result = {
        "manifest": json.loads((root / "manifest.lock.json").read_text()),
        "freeze": json.loads((root / "method.freeze.json").read_text()),
        "measurements": reports, "synthetic_controls": synthetic_metrics(root),
        "ninety_percent_goal_achieved": False,
        "production_default": "Prior local lexical method retained. Candidate is explicitly experimental and opt-in.",
        "conclusion": "Reference recall improved, but public-source adjudicated precision is unknown and strict-visible agreement is below 90%. "
                      "Two validation works on one previously inspected manuscript do not establish generalization.",
        "limitations": [
            "The original manuscript attachment matches 33/42 body pages after whitespace normalization, not the complete 45-page submitted package.",
            "Benchmark uses rendered embedded submission text with original-font glyphs; vendor-normalized token stream and exact uploaded bytes unavailable.",
            "Top-source highlights omit alternatives. Additional candidate positions remain unadjudicated, not automatically false positives.",
            "Source-family validation was locked before tuning but the reference report had been viewed in prior research; this is provisional same-manuscript validation.",
            "Partial decimal/highlight glyph units are characters, not vendor words. Public-source precision/F1 cannot be certified from this gold.",
            "Two attempted additional sources were unavailable; retrieval and license/version provenance remain in the acquisition manifest.",
        ],
    }
    write_new(root / "benchmark-summary.json", result)
    from buna.reports import _THEME, _escape
    rows = []
    for key, metrics in reports.items():
        micro = metrics["micro"]
        rows.append(f"<tr><td>{_escape(key)}</td>"
                    f"<td>{micro['matched_reference_characters']} / {micro['reference_characters']}</td>"
                    f"<td>{micro['matched_reference_characters']} / {micro['predicted_characters_in_observed_pages']}</td>"
                    f"<td>{micro['additional_unadjudicated_characters']}</td></tr>")
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script>(()=>{const p=new URLSearchParams(location.search).get("clawpilotTheme");document.documentElement.dataset.theme=
(p==="light"||p==="dark")?p:(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");})();</script>
<title>Paper Overlap Detector core benchmark</title><style>""" + _THEME + """
body{background:var(--cp-bg);color:var(--cp-text);font:16px/1.5 "Segoe UI",Aptos,Calibri,sans-serif;margin:32px auto;padding:24px;max-width:1100px}
section{background:var(--cp-surface);border:1px solid var(--cp-border);border-radius:16px;padding:24px;margin:20px 0}
h1{color:var(--cp-accent)}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid var(--cp-border)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px Consolas,"Courier New",monospace}summary{cursor:pointer}
@media print{body{margin:0;padding:0;font-size:10pt}details:not([open]){display:none}tr{break-inside:avoid}}
</style></head><body><h1>Core benchmark: 90% accuracy gate not established</h1>
<p>Five available works; three development families and two provisional validation families. Same embedded submission surrogate.</p>
<section><h2>Decision</h2><p>""" + _escape(result["production_default"]) + "</p><p>" + _escape(result["conclusion"]) + """</p></section>
<section><h2>Exact micro counts</h2><p>Strict-visible agreement is NOT adjudicated precision. Unshown alternative-source matches remain unknown.</p>
<table><thead><tr><th>Run</th><th>Reference recall TP/gold</th><th>Strict-visible TP/predicted on observed pages</th><th>Unadjudicated extras</th></tr></thead>
<tbody>""" + "".join(rows) + """</tbody></table></section><section><h2>Limitations</h2><ul>""" + "".join(
        "<li>" + _escape(item) + "</li>" for item in result["limitations"]) + """</ul></section>
<details><summary>Complete protocol, provenance, per-source metrics and synthetic controls</summary><pre>""" + _escape(
        json.dumps(result, ensure_ascii=False, indent=2)) + "</pre></details></body></html>"
    (root / "benchmark-summary.html").write_text(html, encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "evaluate", "freeze", "summary"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--split", choices=["development", "validation"], default="development")
    parser.add_argument("--method", default="baseline")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.root)
    elif args.operation == "freeze":
        freeze_method(args.root)
    elif args.operation == "summary":
        result = summary(args.root)
        print(json.dumps({"ninety_percent_goal_achieved": result["ninety_percent_goal_achieved"], "synthetic_controls": result["synthetic_controls"]}))
    else:
        result = evaluate(args.root, args.split, args.method, json.loads(args.config.read_text()) if args.config else None)
        write_new(args.root / f"metrics-{args.split}-{args.method}.json", result)
        print(json.dumps({key: result[key] for key in ["method", "split", "micro", "macro", "ninety_percent_claim"]}))


if __name__ == "__main__":
    main()
