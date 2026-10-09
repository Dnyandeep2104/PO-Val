"""Azure Blob source for f5psalesstorlanding / purchaseorders.

The prototype used a bare DefaultAzureCredential() and it failed on the
cluster: no environment credential, no IMDS response, no CLI, no cached
token. There are four ways out:

  1. managed_identity - attach a managed identity to the cluster and
     grant it Storage Blob Data Reader on the storage account.

  2. service_principal - client id / secret / tenant in Key Vault.

  3. sas_token - a scoped, expiring SAS.

  4. connection_string - full account connection string.

Set POV_BLOB_AUTH or environment variables to pick one.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from .sources import Document, DocumentSource

log = logging.getLogger(__name__)

ACCOUNT_URL = os.environ.get("AZURE_STORAGE_ACCOUNT_URL", "https://f5psalesstorlanding.blob.core.windows.net")
CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER", "purchaseorders")


def decode_pdf_payload(data: bytes) -> bytes:
    """Power Automate's Create blob action often writes the attachment's
    'contentBytes' directly, which is base64-encoded text, not binary bytes.
    A PDF base64 string starts with 'JVBERi0' (%PDF-). If we see that,
    decode it back to binary so the PDF parser does not reject it."""
    if not data:
        return data
    trimmed = data.strip()
    if trimmed.startswith(b"{") and b"$content" in trimmed:
        try:
            import json, base64
            parsed = json.loads(trimmed.decode("utf-8", errors="ignore"))
            content = parsed.get("$content") or parsed.get("contentBytes")
            if content:
                return base64.b64decode(content, validate=False)
        except Exception:
            pass
    head = trimmed[:16]
    if head.startswith(b"JVBERi0"):
        import base64
        try:
            return base64.b64decode(trimmed, validate=False)
        except Exception as exc:
            log.warning("Payload looked like base64 PDF but decode failed: %s", exc)
    return data


class BlobSource(DocumentSource):
    name = "blob"

    def __init__(
        self,
        account_url: str = ACCOUNT_URL,
        container: str = CONTAINER,
        auth: str = "managed_identity",
        prefix: str = "",
        suffix: str = ".pdf",
        secrets=None,                # dbutils.secrets, or None
        secret_scope: str = "SalesKeyVaultScope",
        **auth_kwargs,
    ):
        self.account_url = account_url
        self.container = container
        self.auth = auth
        self.prefix = prefix
        self.suffix = suffix
        self.secrets = secrets
        self.secret_scope = secret_scope
        self.auth_kwargs = auth_kwargs
        self._client = None

    # ----------------------------------------------------------------- auth
    def _secret(self, key: str, fallback: Optional[str] = None) -> Optional[str]:
        if self.auth_kwargs.get(key):
            return self.auth_kwargs[key]
        if self.secrets is not None:
            try:
                return self.secrets.get(scope=self.secret_scope, key=key)
            except Exception as exc:
                log.warning("secret %s not readable from %s: %s",
                            key, self.secret_scope, exc)
        return fallback

    def _credential(self):
        from azure.identity import (DefaultAzureCredential,
                                    ManagedIdentityCredential,
                                    ClientSecretCredential)

        # 0. Check connection string
        conn_str = self._secret("connection_string") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str and ("AccountName=..." in conn_str or "..." in conn_str):
            conn_str = None
        if conn_str or self.auth == "connection_string":
            if not conn_str:
                raise RuntimeError("connection_string auth selected but no valid connection string found.")
            return conn_str

        if self.auth == "managed_identity":
            client_id = self.auth_kwargs.get("managed_identity_client_id")
            return (ManagedIdentityCredential(client_id=client_id)
                    if client_id else ManagedIdentityCredential())

        if self.auth == "service_principal":
            tenant = self._secret("po-blob-tenant-id") or os.environ.get("AZURE_TENANT_ID")
            client = self._secret("po-blob-client-id") or os.environ.get("AZURE_CLIENT_ID")
            secret = self._secret("po-blob-client-secret") or os.environ.get("AZURE_CLIENT_SECRET")
            missing = [n for n, v in
                       [("tenant", tenant), ("client", client), ("secret", secret)]
                       if not v]
            if missing:
                raise RuntimeError(
                    f"service_principal auth requires {missing}; "
                    f"checked {self.secret_scope} and env vars.")
            return ClientSecretCredential(tenant_id=tenant,
                                          client_id=client,
                                          client_secret=secret)

        if self.auth == "sas_token":
            sas = self._secret("po-blob-sas-token") or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
            if not sas or "..." in sas:
                raise RuntimeError(f"sas_token auth requires SAS token; "
                                   f"checked {self.secret_scope}/po-blob-sas-token "
                                   f"and env AZURE_STORAGE_SAS_TOKEN.")
            return sas.lstrip("?")

        if self.auth == "account_key":
            key = self._secret("po-blob-account-key") or os.environ.get("AZURE_STORAGE_KEY")
            if not key:
                raise RuntimeError("account_key auth selected but no key supplied.")
            return key

        if self.auth == "default":
            return DefaultAzureCredential(exclude_shared_token_cache_credential=True)

        raise ValueError(f"Unknown auth method {self.auth!r}")

    @property
    def client(self):
        if self._client is None:
            from azure.storage.blob import ContainerClient, BlobServiceClient
            conn_str = self._secret("connection_string") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
            if conn_str and ("AccountName=..." in conn_str or "..." in conn_str):
                conn_str = None
            if conn_str:
                svc = BlobServiceClient.from_connection_string(conn_str)
                self._client = svc.get_container_client(self.container)
            else:
                cred = self._credential()
                if isinstance(cred, str) and not self.auth == "sas_token":
                    # account key or conn string
                    self._client = ContainerClient(
                        account_url=self.account_url,
                        container_name=self.container,
                        credential=cred,
                    )
                elif self.auth == "sas_token" or (isinstance(cred, str) and ("sig=" in cred or "se=" in cred)):
                    sas = cred if isinstance(cred, str) else ""
                    self._client = ContainerClient(
                        account_url=self.account_url,
                        container_name=self.container,
                        credential=sas,
                    )
                else:
                    self._client = ContainerClient(
                        account_url=self.account_url,
                        container_name=self.container,
                        credential=cred,
                    )
        return self._client

    # ----------------------------------------------------------- operations
    def check_access(self) -> dict:
        """Lightweight preflight: checks list permissions on the container."""
        try:
            pager = self.client.list_blobs(name_starts_with=self.prefix, results_per_page=1)
            next(iter(pager), None)
            return {"ok": True, "detail": f"Container '{self.container}' (prefix '{self.prefix or '/'}') accessible."}
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    def list_entries(self) -> list[dict]:
        """Names, etags, sizes and timestamps without downloading the bytes."""
        entries = []
        try:
            for b in self.client.list_blobs(name_starts_with=self.prefix):
                name = getattr(b, "name", "")
                if not name.lower().endswith(self.suffix.lower()):
                    continue
                etag = (getattr(b, "etag", None) or "").strip('"')
                size = getattr(b, "size", 0)
                modified = getattr(b, "last_modified", None)
                entries.append({
                    "name": name,
                    "etag": etag,
                    "size": size,
                    "last_modified": modified.isoformat() if modified else None,
                })
        except Exception as exc:
            log.warning("list_entries failed: %s", exc)
            raise
        return entries

    def download(self, name: str) -> bytes:
        raw = self.client.get_blob_client(name).download_blob().readall()
        return decode_pdf_payload(raw)

    def list_documents(self) -> Iterator[Document]:
        for blob in self.client.list_blobs(name_starts_with=self.prefix):
            if not blob.name.endswith(self.suffix):
                continue
            data = self.download(blob.name)
            yield Document(
                source_id=blob.name,
                source_type=self.name,
                data=data,
                received_at=blob.last_modified or datetime.now(timezone.utc),
                metadata={
                    "container": self.container,
                    "etag": blob.etag,
                    "size": blob.size,
                    "content_type": blob.content_settings.content_type if blob.content_settings else None,
                },
            )

    def archive(self, source_id: str, destination_container: str = "archive") -> None:
        """Move a processed PO out of the landing container."""
        src = f"{self.account_url}/{self.container}/{source_id}"
        from azure.storage.blob import BlobServiceClient
        conn_str = self._secret("connection_string") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            svc = BlobServiceClient.from_connection_string(conn_str)
        else:
            svc = BlobServiceClient(account_url=self.account_url,
                                    credential=self._credential())
        target = svc.get_container_client(destination_container)
        target.get_blob_client(source_id).start_copy_from_url(src)
        self.client.delete_blob(source_id)


class BlobWatcher:
    """Continuously monitors an Azure Blob container for new incoming PO PDFs."""

    def __init__(
        self,
        blob_source: BlobSource,
        pipeline=None,
        on_pdf=None,
        poll_interval: float = 4.0,
        on_result=None,
        on_event=None,
        state_file: Optional[Path] = None,
        backfill: bool = False,
    ):
        self.blob_source = blob_source
        self.pipeline = pipeline
        self.on_pdf = on_pdf
        self.poll_interval = poll_interval
        self.on_result = on_result
        self.on_event = on_event
        self.state_file = Path(state_file) if state_file else Path("out/portal/blob_state.json")
        self.backfill = backfill
        self._running = False
        self._seen: set[str] = set()
        self._load_state()

    def _load_state(self):
        try:
            if self.state_file.is_file():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self._seen = set(data.get("seen", []))
        except Exception as exc:
            log.warning("Failed to load blob state: %s", exc)
            self._seen = set()

    def _save_state(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({
                    "seen": sorted(self._seen),
                    "saved_at": datetime.now(timezone.utc).isoformat()
                }, indent=2),
                encoding="utf-8"
            )
        except Exception as exc:
            log.warning("Failed to save blob state: %s", exc)

    def baseline(self) -> int:
        """Mark all existing blobs in the container as seen so only newly
        arriving blobs trigger processing."""
        if self.backfill:
            return 0
        try:
            entries = self.blob_source.list_entries()
            count = 0
            for e in entries:
                key = f"{e['name']}:{e.get('etag') or ''}"
                if key not in self._seen:
                    self._seen.add(key)
                    count += 1
            if count:
                self._save_state()
            log.info("Azure Blob baseline: %d existing PDFs recorded.", len(self._seen))
            return count
        except Exception as exc:
            log.warning("Azure Blob baseline failed: %s", exc)
            return 0

    def _read_sidecar(self, pdf_blob_name: str) -> dict:
        """If Power Automate wrote an email.json sidecar next to the PDF, read it."""
        parent = PurePosixPath(pdf_blob_name).parent
        candidates = [
            f"{parent}/email.json" if str(parent) != "." else "email.json",
            f"{pdf_blob_name}.json",
        ]
        for c in candidates:
            try:
                b_client = self.blob_source.client.get_blob_client(c)
                if b_client.exists():
                    raw = b_client.download_blob().readall()
                    return json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
        return {}

    def poll_once(self) -> list:
        processed = []
        try:
            entries = self.blob_source.list_entries()
        except Exception as exc:
            log.warning("Azure Blob poll failed: %s", exc)
            return []

        for e in entries:
            name = e["name"]
            key = f"{name}:{e.get('etag') or ''}"
            if key in self._seen:
                continue
            # Mark seen early
            self._seen.add(key)
            self._save_state()
            try:
                log.info("📥 Ingesting new PO from Azure Blob: %s (%d bytes)", name, e.get("size", 0))
                meta = self._read_sidecar(name)
                meta["blob_name"] = name
                meta["blob_etag"] = e.get("etag")
                meta["blob_size"] = e.get("size")
                if self.on_event:
                    self.on_event({
                        "event": "inbound_received",
                        "source": "azure_blob",
                        "filename": name,
                        "size": e.get("size", 0),
                        "email_meta": meta,
                    })
                data = self.blob_source.download(name)
                
                # If on_pdf callback provided
                if self.on_pdf:
                    self.on_pdf(name, data, meta)
                elif self.pipeline:
                    doc = Document(
                        source_id=name,
                        source_type="blob",
                        data=data,
                        received_at=datetime.now(timezone.utc),
                        metadata=meta,
                    )
                    res = self.pipeline.process_document(doc)
                    if self.on_result:
                        self.on_result(res, doc)
                processed.append(name)
            except Exception as exc:
                log.error("Failed to process blob %s: %s", name, exc, exc_info=True)
        return processed

    def scan_once(self) -> list:
        return self.poll_once()

    def start(self):
        import time
        self._running = True
        log.info("🚀 Azure Blob Watcher started (polling every %.1fs)...", self.poll_interval)
        self.baseline()
        while self._running:
            self.poll_once()
            time.sleep(self.poll_interval)

    def stop(self):
        self._running = False
