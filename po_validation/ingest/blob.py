"""Azure Blob source for f5psalesstorlanding / purchaseorders.

The prototype used a bare DefaultAzureCredential() and it failed on the
cluster: no environment credential, no IMDS response, no CLI, no cached
token. That is not a bug in the code, it is that the Databricks cluster
has no Azure identity attached. There are four ways out, in descending
order of how much Rishi will like them:

  1. managed_identity - attach a managed identity to the cluster and
     grant it Storage Blob Data Reader on the storage account. Nothing
     secret ever lands in the notebook. Ask for this first.

  2. service_principal - client id / secret / tenant in the existing
     SalesKeyVaultScope. Works on any cluster, rotates like any secret.

  3. sas_token - a scoped, expiring SAS in Key Vault. Fine for a pilot,
     annoying to rotate.

  4. account_key - full account access. Avoid. Included only because it
     is sometimes the only thing an access ticket comes back with.

Set POV_BLOB_AUTH to pick one. Everything else in the pipeline is
unaffected by the choice.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterator, Optional

from .sources import Document, DocumentSource

log = logging.getLogger(__name__)

ACCOUNT_URL = "https://f5psalesstorlanding.blob.core.windows.net"
CONTAINER = "purchaseorders"


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
                       (("tenant", tenant), ("client id", client), ("secret", secret))
                       if not v]
            if missing:
                raise RuntimeError(
                    f"service_principal auth is missing {', '.join(missing)}. "
                    f"Add them to the '{self.secret_scope}' scope or pass them "
                    f"explicitly.")
            return ClientSecretCredential(tenant, client, secret)

        if self.auth == "sas_token" or os.environ.get("AZURE_STORAGE_SAS_TOKEN"):
            sas = self._secret("po-blob-sas-token") or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
            if not sas:
                raise RuntimeError("sas_token auth selected but no SAS token found.")
            return sas.lstrip("?")

        if self.auth == "account_key" or os.environ.get("AZURE_STORAGE_KEY"):
            key = self._secret("po-blob-account-key") or os.environ.get("AZURE_STORAGE_KEY")
            if not key:
                raise RuntimeError("account_key auth selected but no key found.")
            return key

        if self.auth == "default":
            return DefaultAzureCredential()

        raise ValueError(f"Unknown auth mode {self.auth!r}")

    @property
    def client(self):
        if self._client is None:
            from azure.storage.blob import BlobServiceClient
            conn_str = self._secret("connection_string") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
            if conn_str and ("AccountName=..." in conn_str or "..." in conn_str):
                conn_str = None
            if conn_str:
                svc = BlobServiceClient.from_connection_string(conn_str)
            else:
                svc = BlobServiceClient(account_url=self.account_url,
                                        credential=self._credential())
            self._client = svc.get_container_client(self.container)
        return self._client

    # ------------------------------------------------------------ preflight
    def check_access(self) -> dict:
        """Run this before anything else. Turns the wall of
        DefaultAzureCredential noise into one actionable sentence."""
        report = {"account": self.account_url, "container": self.container,
                  "auth": self.auth, "ok": False, "detail": ""}
        try:
            props = self.client.get_container_properties()
            n = sum(1 for _ in self.client.list_blobs())
            report.update(ok=True,
                          detail=f"Connected. {n} blob(s) in container "
                                 f"'{props.name}'.")
        except Exception as exc:
            report["detail"] = (
                f"{type(exc).__name__}: {exc}\n"
                f"Most likely: no valid credentials in environment or cluster identity has no role assignment."
            )
        return report

    # ------------------------------------------------------------- listing
    def list_documents(self) -> Iterator[Document]:
        for blob in self.client.list_blobs(name_starts_with=self.prefix or None):
            if self.suffix and not blob.name.lower().endswith(self.suffix):
                continue
            try:
                data = self.client.download_blob(blob.name).readall()
            except Exception as exc:
                log.error("could not download %s: %s", blob.name, exc)
                continue
            yield Document(
                source_id=blob.name,
                data=data,
                modified=getattr(blob, "last_modified", None) or datetime.now(timezone.utc),
                metadata={"size": getattr(blob, "size", None),
                          "container": self.container},
            )

    def archive(self, source_id: str, destination_container: str) -> None:
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
        pipeline,
        poll_interval: float = 4.0,
        on_result=None,
        on_event=None,
    ):
        self.blob_source = blob_source
        self.pipeline = pipeline
        self.poll_interval = poll_interval
        self.on_result = on_result
        self.on_event = on_event
        self._running = False

    def scan_once(self) -> list:
        results = []
        try:
            for doc in self.blob_source.list_documents():
                if self.pipeline.ledger.seen(doc.content_hash):
                    continue
                if self.on_event:
                    self.on_event({
                        "event": "inbound_received",
                        "source": "azure_blob",
                        "filename": doc.source_id,
                        "size": len(doc.data),
                    })
                log.info("📥 Ingesting PO from Azure Blob: %s (%d bytes)", doc.source_id, len(doc.data))
                res = self.pipeline.process_document(doc)
                results.append(res)
                if self.on_result:
                    try:
                        self.on_result(res, doc)
                    except Exception as e:
                        log.error("on_result error for %s: %s", doc.source_id, e)
        except Exception as exc:
            log.warning("BlobWatcher scan error: %s", exc)
        return results

    def start(self):
        import time
        self._running = True
        log.info("🚀 Azure Blob Watcher started (polling every %.1fs)...", self.poll_interval)
        while self._running:
            self.scan_once()
            time.sleep(self.poll_interval)

    def stop(self):
        self._running = False
