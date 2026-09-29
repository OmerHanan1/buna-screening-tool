"""Synthetic shared-source persistence checks. Never uses a user manuscript."""
import argparse
import hashlib
import json
import os
import time
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--existing-sha")
    args = parser.parse_args()
    print(json.dumps(verify(args.base, args.existing_sha)))
