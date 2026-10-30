import logging

from backend.app.ports import BlobStore, VectorStore
from backend.app.repositories.video import AssetCleanup
from backend.app.services.media import MediaService

logger = logging.getLogger(__name__)


class AssetCleanupError(RuntimeError):
    pass


def delete_asset_artifacts(
    asset: AssetCleanup,
    blob_store: BlobStore,
    vector_store: VectorStore,
) -> None:
    try:
        blob_store.delete(asset.blob_key)
        if asset.blob_key == f"videos/{asset.asset_id}":
            for extension in MediaService.supported_extensions:
                blob_store.delete(f"{asset.blob_key}{extension}")
        for key in (
            f"audio/extracted/{asset.asset_id}.wav",
            f"transcripts/{asset.asset_id}.json",
            f"transcripts/{asset.asset_id}.chunks.json",
            f"transcripts/{asset.asset_id}.embeddings.json",
            f"summaries/{asset.asset_id}.json",
            f"audio/summaries/{asset.asset_id}.wav",
        ):
            blob_store.delete(key)
        blob_store.delete_prefix(f"audio/answers/{asset.asset_id}")
        vector_store.delete_video(asset.asset_id)
    except Exception as exc:
        logger.exception("Failed to remove artifacts for media asset %s", asset.asset_id)
        raise AssetCleanupError(f"Unable to fully delete media asset {asset.asset_id}") from exc
