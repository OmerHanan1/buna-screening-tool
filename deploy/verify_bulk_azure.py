"""Exercise real isolated workers and MSI storage in a disposable Blob namespace."""
import json
import os
import re
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from buna.public_app import create_public_app
from buna.shared_library import AzureBlobStore
from verify_bulk_local import original_pdf


class NamespaceStore:
    def __init__(self, store, prefix):
        if not re.fullmatch(r"validation/bulk-[0-9a-f]{32}/", prefix):
            raise ValueError("A disposable synthetic namespace is required.")
        self.store, self.prefix = store, prefix
    def read(self, name, limit):
        return self.store.read(self.prefix + name, limit)
    def write(self, name, data, expected):
        return self.store.write(self.prefix + name, data, expected)
    def immutable(self, name, data):
        return self.store.immutable(self.prefix + name, data)
    def delete(self, name):
        return self.store.delete(self.prefix + name)


def verify(namespace=None, cleanup=False):
    store = AzureBlobStore(*[os.environ[key] for key in (
        "BUNA_SHARED_ACCOUNT_URL", "BUNA_SHARED_CONTAINER", "BUNA_SHARED_IDENTITY_CLIENT_ID")])
    prefix = namespace or "validation/bulk-" + uuid4().hex + "/"
    isolated = NamespaceStore(store, prefix)
    print("BULK_VALIDATION_NAMESPACE " + prefix, flush=True)
    started = time.monotonic()
    # TestClient uses actual app admission, capability ownership and subprocess
    # isolation; only its catalog/object namespace differs from production.
    with TestClient(create_public_app(shared_store=isolated), base_url="http://127.0.0.1") as client:
        response = client.post("/api/public/session", json={"email": os.environ["BUNA_ALLOWED_EMAIL"].split(",")[-1]})
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        def send(method, path, **kwargs):
            request_headers = kwargs.pop("headers", headers)
            for _ in range(5):
                response = client.request(method, path, headers=request_headers, **kwargs)
                if response.status_code != 429:
                    response.raise_for_status()
                    return response
                time.sleep(min(60, int(response.headers.get("Retry-After", "60"))))
            raise RuntimeError("Synthetic verification exceeded its bounded rate-limit retries.")
        if namespace is None:
            for number in range(50):
                source = original_pdf(2000 + number, pages=8)
                accepted = send("POST", "/api/public/source-saves",
                                headers={**headers, "Idempotency-Key": str(uuid4())},
                                data={"share_authorized": "true"},
                                files={"source": (f"Original synthetic bulk save {number}.pdf", source, "application/pdf")}).json()
                for _ in range(90):
                    state = send("GET", "/api/public/source-saves/" + accepted["id"]).json()
                    if state["state"] in {"saved", "already-present", "failed", "cancelled"}:
                        break
                    time.sleep(1)
                assert state["state"] == "saved", state
        papers = send("GET", "/api/public/library").json()["papers"]
        assert len(papers) == 94 and sum(p["storage_kind"] == "shared" for p in papers) == 50
        result = {"namespace": prefix, "ready_sources": 94, "shared_sources": 50,
                  "actual_curated_sources": 44, "source_pages_each": 8,
                  "actual_isolated_parsers_and_msi_saves": namespace is None,
                  "second_email_after_restart_visibility": namespace is not None}
        if namespace is None:
            accepted = send("POST", "/api/public/jobs", data={"selected": json.dumps([p["sha256"] for p in papers])},
                            files={"target": ("Original synthetic 45-page target.pdf", original_pdf(3000, pages=45), "application/pdf")}).json()
            path = "/api/public/jobs/" + accepted["id"]
            for _ in range(280):
                state = send("GET", path).json()
                if state["status"] != "running":
                    break
                time.sleep(3)
            assert state["status"] == "complete" and state["checked"] == state["total"] == 94 and not state["partial"], state
            pdf = send("GET", path + "/report.pdf").content
            assert pdf.startswith(b"%PDF-")
            evidence = send("GET", path + "/report.json").json()
            assert len(evidence["source_coverage"]) == 94
            send("DELETE", path)
            result.update(manuscript_pages=45, checked_sources=94, partial=False, pdf_bytes=len(pdf), private_job_deleted=True)
        result["wall_seconds"] = round(time.monotonic() - started, 2)
    if cleanup:
        names = [blob.name for blob in store.client.list_blobs(name_starts_with=prefix)]
        assert all(name.startswith(prefix) for name in names)
        for name in names:
            store.delete(name)
        result["only_validation_namespace_cleaned"] = True
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.namespace, args.cleanup)))
