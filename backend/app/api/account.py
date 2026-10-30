from fastapi import APIRouter, Depends, HTTPException, Response, status

from backend.app.api.dependencies import SESSION_COOKIE_NAME, get_current_user
from backend.app.container import get_blob_store, get_vector_store
from backend.app.core.config import settings
from backend.app.services.asset_cleanup import AssetCleanupError, delete_asset_artifacts
from backend.app.services.auth import AuthenticatedUser
from backend.app.services.video_storage import VideoOwnershipError, video_ownership
from backend.app.ports import BlobStore, VectorStore


router = APIRouter(tags=["account"])


@router.delete("/me/data", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_data(
    user: AuthenticatedUser = Depends(get_current_user),
    blob_store: BlobStore = Depends(get_blob_store),
    vector_store: VectorStore = Depends(get_vector_store),
) -> Response:
    try:
        cleanup_assets = video_ownership.delete_user_data(user.username)
        for asset in cleanup_assets:
            delete_asset_artifacts(asset, blob_store, vector_store)
    except VideoOwnershipError as exc:
        raise HTTPException(status_code=503, detail="Unable to delete user data") from exc
    except AssetCleanupError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response
