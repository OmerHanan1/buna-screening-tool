"""Native Azure capacity gate using only original fixtures and an isolated Blob prefix."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from buna.public_app import create_public_app
from buna.shared_library import AzureBlobStore
from verify_bulk_azure import NamespaceStore
from verify_upload_capacity import image_pdf


def verify(namespace=None, digest=None, cleanup=False):
    store = AzureBlobStore(*[os.environ[key] for key in (
        "BUNA_SHARED_ACCOUNT_URL", "BUNA_SHARED_CONTAINER", "BUNA_SHARED_IDENTITY_CLIENT_ID")])
    namespace = namespace or "validation/bulk-" + uuid4().hex + "/"
    isolated = NamespaceStore(store, namespace)
    print("CAPACITY_TEST_NAMESPACE " + namespace, flush=True)
    started = time.monotonic()
    result = {"namespace": namespace, "per_file_bytes": 40 * 1024 * 1024}
    with tempfile.TemporaryDirectory(prefix="capacity-cloud-") as directory:
        root = Path(directory)
        root.chmod(0o711)
        with TestClient(create_public_app(runtime_root=root, shared_store=isolated), base_url="http://127.0.0.1") as client:
            email = os.environ["BUNA_ALLOWED_EMAIL"].split(",")[-1 if digest else 0]
            response = client.post("/api/public/session", json={"email": email})
            response.raise_for_status()
            headers = {"Authorization": "Bearer " + response.json()["token"]}
            if digest:
                papers = client.get("/api/public/library", headers=headers).json()["papers"]
                assert sum(paper["sha256"] == digest for paper in papers) == 1
                catalog = client.app.state.shared_library._read()[0]
                entry = catalog["papers"][digest]
                assert entry["state"] == "ready" and entry["original_bytes"] == 40 * 1024 * 1024
                name = client.app.state.shared_library._object_paths(entry)[0]
                raw, _ = isolated.read(name, 40 * 1024 * 1024)
                assert hashlib.sha256(raw).hexdigest() == digest
                result.update(digest=digest, restart_second_visitor_visible=True, original_blob_sha_verified=True)
            else:
                target, source = root / "target.pdf", root / "source.pdf"
                image_pdf(target, 40 * 1024 * 1024, variant="Original synthetic capacity target.")
                image_pdf(source, 40 * 1024 * 1024, variant="Original synthetic capacity comparison source.")
                with source.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                with source.open("rb") as stream:
                    response = client.post("/api/public/source-uploads", headers={**headers, "Idempotency-Key": str(uuid4())},
                                           files={"source": ("Original synthetic 40 MiB source.pdf", stream, "application/pdf")})
                response.raise_for_status()
                upload_id = response.json()["id"]
                with target.open("rb") as stream:
                    response = client.post("/api/public/jobs", headers=headers,
                                           data={"selected": "[]", "uploaded_sources": json.dumps([upload_id])},
                                           files={"target": ("Original synthetic 40 MiB target.pdf", stream, "application/pdf")})
                response.raise_for_status()
                path = "/api/public/jobs/" + response.json()["id"]
                for _ in range(130):
                    state = client.get(path, headers=headers).json()
                    if state["status"] != "running":
                        break
                    time.sleep(2)
                assert state["status"] == "complete" and state["checked"] == state["total"] == 1, state
                pdf = client.get(path + "/report.pdf", headers=headers)
                pdf.raise_for_status()
                assert pdf.content.startswith(b"%PDF-")
                result.update(target40_source40_compared=True, compared_pdf_bytes=len(pdf.content))
                client.delete(path, headers=headers).raise_for_status()
                client.delete("/api/public/source-uploads/" + upload_id, headers=headers).raise_for_status()
                with source.open("rb") as stream:
                    response = client.post("/api/public/source-saves", headers={**headers, "Idempotency-Key": str(uuid4())},
                                           data={"share_authorized": "true"},
                                           files={"source": ("Original synthetic 40 MiB durable source.pdf", stream, "application/pdf")})
                response.raise_for_status()
                receipt = response.json()["id"]
                for _ in range(100):
                    state = client.get("/api/public/source-saves/" + receipt, headers=headers).json()
                    if state["state"] in {"saved", "already-present", "failed", "cancelled"}:
                        break
                    time.sleep(2)
                assert state["state"] == "saved" and state["digest"] == digest, state
                visible = client.get("/api/public/library", headers=headers).json()["papers"]
                assert sum(paper["sha256"] == digest for paper in visible) == 1
                with source.open("rb") as stream:
                    response = client.post("/api/public/source-saves", headers={**headers, "Idempotency-Key": str(uuid4())},
                                           data={"share_authorized": "true"},
                                           files={"source": ("Original synthetic duplicate40.pdf", stream, "application/pdf")})
                response.raise_for_status()
                duplicate = response.json()["id"]
                for _ in range(60):
                    state = client.get("/api/public/source-saves/" + duplicate, headers=headers).json()
                    if state["state"] in {"saved", "already-present", "failed"}:
                        break
                    time.sleep(2)
                assert state["state"] == "already-present", state
                with source.open("ab") as stream:
                    stream.write(b"\n")
                for endpoint, field, data in (
                    ("/api/public/source-uploads", "source", {}),
                    ("/api/public/source-saves", "source", {"share_authorized": "true"}),
                    ("/api/public/jobs", "target", {"selected": "[]"}),
                ):
                    with source.open("rb") as stream:
                        response = client.post(endpoint, headers={**headers, "Idempotency-Key": str(uuid4())},
                                               data=data, files={field: ("Synthetic 40 MiB plus one.pdf", stream, "application/pdf")})
                    assert response.status_code == 413, (endpoint, response.status_code)
                result.update(digest=digest, source40_saved_and_visible=True, duplicate_not_added=True,
                              plus_one_byte_all_routes_rejected=True, synthetic_job_and_staging_deleted=True)
    if cleanup:
        names = [blob.name for blob in store.client.list_blobs(name_starts_with=namespace)]
        assert all(name.startswith(namespace) for name in names)
        for name in names:
            store.delete(name)
        result["only_test_namespace_cleaned"] = True
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace")
    parser.add_argument("--digest")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.namespace, args.digest, args.cleanup)))
