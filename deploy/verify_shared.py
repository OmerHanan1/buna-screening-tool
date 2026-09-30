"""Synthetic shared-source persistence checks. Never uses a user manuscript."""
import argparse
import hashlib
import json
import os
import time
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import pymupdf


PHRASE = "amber birds gather beside quiet rivers during winter mornings"


def pdf(text):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(45, 45, 550, 750), text, fontsize=10)
        return document.tobytes()


def wait(client, path, headers):
    for _ in range(180):
        response = client.get(path, headers=headers)
        response.raise_for_status()
        state = response.json()
        if state["status"] != "running" and not any(s["state"] in {"pending", "saving"} for s in state["library_saves"]):
            return state
        time.sleep(1)
    raise RuntimeError("Synthetic shared-save verification exceeded its deadline.")


def verify(base, digest=None):
    with httpx.Client(base_url=base, timeout=60) as client:
        response = client.post("/api/public/session", json={"email": os.environ["BUNA_TEST_EMAIL"]})
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        library = client.get("/api/public/library", headers=headers).json()
        assert library["shared_saving_available"], library.get("shared_library_warning")
        before = {p["sha256"] for p in library["papers"]}
        target = pdf("Abstract\nIndependent original synthetic observations describe " + PHRASE + ".")
        if digest is None:
            name = "Synthetic shared persistence probe " + str(uuid4())
            source = pdf(name + "\nIntroduction\nSource observations show " + PHRASE
                         + ".\nMethods\nThis complete synthetic fixture is used only to validate storage and is not an actual research paper."
                         + "\nDiscussion\nThe original source concludes with independent text about controlled observations.")
            digest = hashlib.sha256(source).hexdigest()
            fields = {"selected": "[]", "save_sources": "[0]", "share_authorized": "true"}
            files = [("target", ("synthetic-target.pdf", target, "application/pdf")),
                     ("sources", (name + ".pdf", source, "application/pdf"))]
        else:
            assert digest in before, "Saved source was not visible to the new visitor after restart."
            name = next(p["title"] for p in library["papers"] if p["sha256"] == digest)
            assert name.startswith("Synthetic shared persistence probe ")
            fields = {"selected": json.dumps([digest])}
            files = [("target", ("synthetic-new-visitor.pdf", target, "application/pdf"))]
        response = client.post("/api/public/jobs", headers=headers, data=fields, files=files)
        response.raise_for_status()
        path = "/api/public/jobs/" + response.json()["id"]
        state = wait(client, path, headers)
        assert state["status"] == "complete" and state["checked"] == 1 and state["overlap_percent"] > 0, state
        if state["library_saves"]:
            assert state["library_saves"][0]["state"] in {"saved", "already-present"}, state["library_saves"]
        after = client.get("/api/public/library", headers=headers).json()["papers"]
        assert before <= {p["sha256"] for p in after} and digest in {p["sha256"] for p in after}
        report = client.get(path + "/report.pdf", headers=headers)
        report.raise_for_status()
        assert report.content.startswith(b"%PDF-")
        assert client.get("/api/public/sources/" + digest, headers=headers).status_code == 404
        client.delete(path, headers=headers).raise_for_status()
        return {"sha256": digest, "title": name, "ready_sources": len(after), "pdf_bytes": len(report.content),
                "fully_checked": 1, "private_job_deleted": True, "shared_source_retained": True}


def verify_immediate(base, digest=None):
    """Save a 45-page original fixture without a manuscript or comparison POST."""
    from benchmark_hosted import manuscript
    with httpx.Client(base_url=base, timeout=90) as client:
        response = client.post("/api/public/session", json={"email": os.environ["BUNA_TEST_EMAIL"]})
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        library = client.get("/api/public/library", headers=headers).json()
        assert library["immediate_shared_saving"] and library["shared_saving_available"]
        receipt = None
        if digest is None:
            title = "Synthetic immediate persistence probe " + str(uuid4()) + ".pdf"
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "source.pdf"
                manuscript(path, pages=45)
                source = path.read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            response = client.post("/api/public/source-saves",
                                   headers={**headers, "Idempotency-Key": str(uuid4())},
                                   data={"share_authorized": "true"},
                                   files={"source": (title, source, "application/pdf")})
            response.raise_for_status()
            receipt = response.json()["id"]
            for _ in range(180):
                response = client.get("/api/public/source-saves/" + receipt, headers=headers)
                response.raise_for_status()
                state = response.json()
                if state["state"] in {"saved", "already-present", "failed", "cancelled"}:
                    break
                time.sleep(1)
            assert state["state"] in {"saved", "already-present"}, state
        papers = client.get("/api/public/library", headers=headers).json()["papers"]
        paper = next(p for p in papers if p["sha256"] == digest)
        assert paper["title"].startswith("Synthetic immediate persistence probe ")
        assert "45 PDF pages" in paper["version"]
        target = pdf("Abstract\nWe examined whether the effects of psychological distance on emotion depended on the task context and the order of presentation.")
        response = client.post("/api/public/jobs", headers=headers,
                               data={"selected": json.dumps([digest])},
                               files={"target": ("original-synthetic-target.pdf", target, "application/pdf")})
        response.raise_for_status()
        path = "/api/public/jobs/" + response.json()["id"]
        state = wait(client, path, headers)
        assert state["status"] == "complete" and state["checked"] == 1 and state["overlap_percent"] > 0, state
        report = client.get(path + "/report.pdf", headers=headers)
        report.raise_for_status()
        assert report.content.startswith(b"%PDF-")
        client.delete(path, headers=headers).raise_for_status()
        return {"sha256": digest, "title": paper["title"], "source_pages": 45, "save_receipt": receipt,
                "source_saved_without_manuscript_or_comparison": receipt is not None,
                "library_visible": True, "ready_sources": len(papers), "pdf_bytes": len(report.content),
                "saved_source_only_fully_checked": 1, "private_job_deleted": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--existing-sha")
    parser.add_argument("--immediate", action="store_true")
    args = parser.parse_args()
    print(json.dumps((verify_immediate if args.immediate else verify)(args.base, args.existing_sha)))
