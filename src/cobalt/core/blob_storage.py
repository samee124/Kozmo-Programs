"""Azure Blob Storage primitives for Cobalt.

Provides upload, download, append, and existence-check operations for the
cobalt-workspace, cobalt-brain, and cobalt-uploads containers.

Configuration (read from .env):
    AZURE_STORAGE_CONNECTION_STRING  — full Azure connection string
    AZURE_WORKSPACE_CONTAINER        — e.g. cobalt-workspace-dev
    AZURE_UPLOADS_CONTAINER          — e.g. cobalt-uploads-dev
    AZURE_BRAIN_CONTAINER            — cobalt-brain

Blob types:
    Block Blob  — all regular workspace .md and .json files
    Append Blob — ledger.md only (append_md writes use append_block)

When AZURE_STORAGE_CONNECTION_STRING is not set, all functions return
gracefully without raising — the local filesystem remains the active store.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Files that use Append Blob type (append_block) instead of Block Blob (upload_blob)
APPEND_ONLY_BLOBS: frozenset[str] = frozenset({"ledger.md"})

# Files that must not be overwritten after creation at the blob layer
WRITE_ONCE_BLOBS: frozenset[str] = frozenset()  # evidence/* enforced at app layer


def _get_connection_string() -> str | None:
    return os.getenv("AZURE_STORAGE_CONNECTION_STRING")


def _get_workspace_container() -> str:
    return os.getenv("AZURE_WORKSPACE_CONTAINER", "cobalt-workspace-dev")


def _get_uploads_container() -> str:
    return os.getenv("AZURE_UPLOADS_CONTAINER", "cobalt-uploads-dev")


def _get_brain_container() -> str:
    return os.getenv("AZURE_BRAIN_CONTAINER", "cobalt-brain")


def _get_blob_client(container: str, blob_path: str):
    """Return a BlobClient for the given container and blob path.

    Returns None if the connection string is not configured.
    """
    conn_str = _get_connection_string()
    if not conn_str:
        return None

    try:
        from azure.storage.blob import BlobServiceClient
        service = BlobServiceClient.from_connection_string(conn_str)
        return service.get_blob_client(container=container, blob=blob_path)
    except Exception as exc:
        logger.warning("blob_storage: failed to create BlobClient for %s/%s: %s",
                       container, blob_path, exc)
        return None


def is_configured() -> bool:
    """Return True if Azure Blob Storage is configured via environment."""
    return bool(_get_connection_string())


# ─── Workspace container helpers ──────────────────────────────────────────────

def workspace_blob_path(
    user_id: str,
    programme_id: str,
    relative_path: str,
) -> str:
    """Build the full blob path for a workspace file.

    Args:
        user_id:       e.g. 'User001'
        programme_id:  e.g. 'PROG-001'
        relative_path: e.g. 'v-V-ABCD-001/identity/entity.md'

    Returns:
        'User001/PROG-001/v-V-ABCD-001/identity/entity.md'
    """
    return f"{user_id}/{programme_id}/{relative_path}".replace("\\", "/")


# ─── Core blob operations ─────────────────────────────────────────────────────

def upload_blob(
    container: str,
    blob_path: str,
    content: str,
    metadata: dict | None = None,
    overwrite: bool = True,
) -> bool:
    """Upload a string as a Block Blob.

    Args:
        container:  Container name (e.g. 'cobalt-workspace-dev').
        blob_path:  Full blob path within the container.
        content:    UTF-8 string content to upload.
        metadata:   Optional dict of blob metadata tags.
        overwrite:  If False, raises BlobAlreadyExistsError when blob exists.

    Returns:
        True on success, False if not configured or on error.
    """
    client = _get_blob_client(container, blob_path)
    if client is None:
        return False

    try:
        client.upload_blob(
            data=content.encode("utf-8"),
            overwrite=overwrite,
            metadata=metadata or {},
        )
        logger.debug("blob_storage: uploaded %s/%s", container, blob_path)
        return True
    except Exception as exc:
        logger.warning("blob_storage: upload failed for %s/%s: %s",
                       container, blob_path, exc)
        return False


def append_to_blob(container: str, blob_path: str, content: str) -> bool:
    """Append a string chunk to an Append Blob (used for ledger.md).

    Creates the Append Blob on first call. Subsequent calls use append_block.

    Args:
        container: Container name.
        blob_path: Full blob path within the container.
        content:   UTF-8 string to append.

    Returns:
        True on success, False if not configured or on error.
    """
    client = _get_blob_client(container, blob_path)
    if client is None:
        return False

    try:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.storage.blob import BlobType

        # Create the Append Blob if it doesn't exist yet
        try:
            client.get_blob_properties()
        except ResourceNotFoundError:
            client.create_append_blob()

        client.append_block(content.encode("utf-8"))
        logger.debug("blob_storage: appended to %s/%s", container, blob_path)
        return True
    except Exception as exc:
        logger.warning("blob_storage: append failed for %s/%s: %s",
                       container, blob_path, exc)
        return False


def download_blob(container: str, blob_path: str) -> str | None:
    """Download a blob and return its content as a UTF-8 string.

    Returns:
        String content, or None if not found or not configured.
    """
    client = _get_blob_client(container, blob_path)
    if client is None:
        return None

    try:
        data = client.download_blob().readall()
        return data.decode("utf-8")
    except Exception as exc:
        logger.warning("blob_storage: download failed for %s/%s: %s",
                       container, blob_path, exc)
        return None


def blob_exists(container: str, blob_path: str) -> bool:
    """Return True if a blob exists in the given container.

    Returns False if not configured or on error.
    """
    client = _get_blob_client(container, blob_path)
    if client is None:
        return False

    try:
        client.get_blob_properties()
        return True
    except Exception:
        return False


def delete_blob(container: str, blob_path: str) -> bool:
    """Delete a blob. Returns True on success, False otherwise.

    Used for cleanup only — never called during normal workspace writes.
    """
    client = _get_blob_client(container, blob_path)
    if client is None:
        return False

    try:
        client.delete_blob()
        return True
    except Exception as exc:
        logger.warning("blob_storage: delete failed for %s/%s: %s",
                       container, blob_path, exc)
        return False


# ─── Convenience wrappers for workspace container ────────────────────────────

def upload_workspace_file(
    user_id: str,
    programme_id: str,
    relative_path: str,
    content: str,
    file_type: str = "",
    process: str = "SHARED",
    vendor_id: str = "_programme",
    overwrite: bool = True,
) -> bool:
    """Upload a file to the workspace container with standard metadata tags.

    Args:
        user_id:       Account owner (e.g. 'User001').
        programme_id:  Programme identifier.
        relative_path: Path relative to {user_id}/{programme_id}/ in blob.
        content:       UTF-8 string to upload.
        file_type:     Metadata tag value (e.g. 'entity_md', 'coverage_md').
        process:       Metadata tag: 'P1', 'P2', 'P3', 'SHARED', 'RUNTIME'.
        vendor_id:     Metadata tag. Use '_programme' for programme-level files.
        overwrite:     Whether to overwrite existing blob.

    Returns:
        True on success, False if Azure not configured or on error.
    """
    filename = Path(relative_path).name
    is_append_only = filename in APPEND_ONLY_BLOBS

    if is_append_only:
        # Append blobs are created via append_to_blob, not upload_blob
        blob_path = workspace_blob_path(user_id, programme_id, relative_path)
        return append_to_blob(_get_workspace_container(), blob_path, content)

    blob_path = workspace_blob_path(user_id, programme_id, relative_path)
    metadata = {
        "user_id": user_id,
        "programme_id": programme_id,
        "vendor_id": vendor_id,
        "file_type": file_type or filename.replace(".", "_"),
        "process": process,
        "immutable": "false",
        "append_only": "false",
    }
    return upload_blob(
        container=_get_workspace_container(),
        blob_path=blob_path,
        content=content,
        metadata=metadata,
        overwrite=overwrite,
    )


def upload_evidence_file(
    user_id: str,
    programme_id: str,
    vendor_id: str,
    evidence_filename: str,
    content: str,
) -> bool:
    """Upload an evidence file as write-once (overwrite=False).

    Evidence files (ev-*.md) are immutable after creation.
    Raises BlobAlreadyExistsError if the file already exists.
    """
    from azure.core.exceptions import ResourceExistsError
    from cobalt.core.exceptions import FileOwnershipViolation

    relative_path = f"v-{vendor_id}/evidence/{evidence_filename}"
    blob_path = workspace_blob_path(user_id, programme_id, relative_path)
    client = _get_blob_client(_get_workspace_container(), blob_path)
    if client is None:
        return False

    try:
        client.upload_blob(
            data=content.encode("utf-8"),
            overwrite=False,
            metadata={
                "user_id": user_id,
                "programme_id": programme_id,
                "vendor_id": vendor_id,
                "file_type": "evidence_md",
                "process": "P1",
                "immutable": "true",
                "append_only": "false",
            },
        )
        return True
    except ResourceExistsError:
        raise FileOwnershipViolation(
            f"Evidence file {evidence_filename} already exists for vendor {vendor_id} "
            f"and cannot be overwritten."
        )
    except Exception as exc:
        logger.warning("blob_storage: evidence upload failed for %s: %s",
                       blob_path, exc)
        return False


def upload_document(
    user_id: str,
    programme_id: str,
    vendor_id: str,
    file_id: str,
    original_filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    trust_level: str = "USER_SUBMITTED",
) -> bool:
    """Upload a P3 document (CSV, Excel, PDF) to the uploads container.

    Path: {user_id}/{programme_id}/{vendor_id}/{file_id}/{original_filename}

    Args:
        content: Raw bytes of the uploaded file.

    Returns:
        True on success, False otherwise.
    """
    import json
    from datetime import timezone

    blob_path = f"{user_id}/{programme_id}/{vendor_id}/{file_id}/{original_filename}"
    container = _get_uploads_container()

    client = _get_blob_client(container, blob_path)
    if client is None:
        return False

    try:
        client.upload_blob(
            data=content,
            overwrite=False,
            metadata={
                "user_id": user_id,
                "programme_id": programme_id,
                "vendor_id": vendor_id,
                "file_id": file_id,
                "content_type": content_type,
            },
        )
    except Exception as exc:
        logger.warning("blob_storage: document upload failed for %s: %s", blob_path, exc)
        return False

    # Write metadata.json alongside the file
    meta_path = f"{user_id}/{programme_id}/{vendor_id}/{file_id}/metadata.json"
    meta_content = json.dumps(
        {
            "file_id": file_id,
            "original_filename": original_filename,
            "trust_level": trust_level,
            "uploaded_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": user_id,
            "programme_id": programme_id,
            "vendor_id": vendor_id,
            "content_type": content_type,
        },
        indent=2,
    )
    upload_blob(container, meta_path, meta_content, overwrite=True)
    return True
