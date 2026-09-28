"""Synthetic public deployment verification; never uses a user manuscript."""
import argparse
import json
import time
import httpx
import pymupdf


def smoke(base: str):
    sentence = "The ceramic sensor records a stable sequence of local measurements during every carefully controlled laboratory cycle."
    with httpx.Client(base_url=base, timeout=90) as client:
        client.get("/health").raise_for_status()
        first = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        second = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        library = client.get("/api/public/library").json()["papers"]
        response = client.post("/api/public/jobs", headers=first,
            data={"selected": json.dumps([p["sha256"] for p in library])},
            files=[("target", ("synthetic.txt", ("Abstract\n" + sentence + "\nOriginal target ending.").encode())),
                   ("sources", ("synthetic-source.txt", ("Synthetic source\n" + sentence + "\nDifferent ending.").encode()))])
        response.raise_for_status()
        path = "/api/public/jobs/" + response.json()["id"]
        started = time.monotonic()
        while time.monotonic() - started < 270:
            status_response = client.get(path, headers=first)
            status_response.raise_for_status()
            result = status_response.json()
            if result["status"] != "running":
                break
            time.sleep(4)
        assert result["status"] == "complete", result
        assert result["checked"] == len(library) + 1 and result["overlap_percent"] > 0, result
        assert client.get(path, headers=second).status_code == 404
        assert client.get(path + "/report.pdf", headers=second).status_code == 404
        assert client.get(path + "/report.pdf").status_code == 401
        response = client.get(path + "/report.pdf", headers=first)
        assert response.content.startswith(b"%PDF-")
        with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
            text = " ".join(" ".join(page.get_text() for page in pdf).split())
            assert "Public source attribution" in text
            assert "Journal of Medical Internet Research" in text
            assert "Editorial: Emotion regulation" in text
        assert client.get("/api/jobs").status_code == 404
        assert client.post("/api/library/imports", json={}).status_code == 404
        client.delete(path, headers=first).raise_for_status()
        assert client.get(path, headers=first).status_code == 404
        return {"curated_sources": len(library), "fully_checked": result["checked"],
                "pdf_bytes": len(response.content), "cross_visitor_denied": True,
                "deleted": True, "seconds": round(time.monotonic() - started, 2)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    print(json.dumps(smoke(args.base)))
