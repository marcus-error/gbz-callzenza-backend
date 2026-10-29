import os
from pathlib import Path
import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import PRIMARY_BUSINESS_ID, get_current_db_user
from app.core.security import get_current_user_optional
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.file import TenantFile
from app.schemas.file import TenantFileOut

router = APIRouter(prefix="/api/files", tags=["files"])

BASE_UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "uploads"
BASE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("", response_model=TenantFileOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    token_payload: Optional[dict] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> TenantFileOut:
    """Upload a file isolated into the tenant's secure storage directory."""
    user = None
    tenant_id = None
    if token_payload and token_payload.get("sub"):
        user = await db.get(User, token_payload["sub"])
        if user:
            tenant_id = token_payload.get("tenant_id") or token_payload.get("business_id") or user.business_id

    if not user:
        stmt = select(User).where(User.is_active == True).order_by(User.created_at)
        user = (await db.execute(stmt)).scalars().first()
        if user and not tenant_id:
            tenant_id = user.business_id

    tenant_id = str(tenant_id or PRIMARY_BUSINESS_ID)
    uploader_id = user.id if user else None

    tenant_dir = BASE_UPLOAD_DIR / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)

    file_uuid = str(uuid.uuid4())
    safe_filename = f"{file_uuid}_{file.filename}"
    target_path = tenant_dir / safe_filename

    contents = await file.read()
    file_size = len(contents)

    with open(target_path, "wb") as f:
        f.write(contents)

    record = TenantFile(
        tenant_id=tenant_id,
        filename=safe_filename,
        original_filename=file.filename or "unknown",
        file_size_bytes=file_size,
        mime_type=file.content_type or "application/octet-stream",
        file_path=str(target_path).replace("\\", "/"),
        storage_type="LOCAL",
        uploader_id=user.id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@router.get("", response_model=List[TenantFileOut])
async def list_files(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> List[TenantFileOut]:
    """List all files uploaded by the current tenant."""
    stmt = select(TenantFile).where(TenantFile.tenant_id == tenant_id).order_by(TenantFile.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.get("/download-by-name")
async def download_file_by_name(
    filename: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download a file by its original filename preserving exact format and extension."""
    import mimetypes
    clean_name = filename.strip()
    stmt = select(TenantFile).where(
        TenantFile.original_filename == clean_name
    ).order_by(TenantFile.created_at.desc())
    file_record = await db.scalar(stmt)
    if not file_record or not os.path.exists(file_record.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File '{filename}' not found")

    media_type = file_record.mime_type or mimetypes.guess_type(file_record.original_filename)[0] or "application/octet-stream"

    return FileResponse(
        path=file_record.file_path,
        media_type=media_type,
        filename=file_record.original_filename,
        headers={
            "Content-Disposition": f'attachment; filename="{file_record.original_filename}"'
        },
    )


@router.get("/download/{filename:path}")
async def download_by_filename_path(
    filename: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download file by filename in URL path ensuring exact format and filename."""
    import mimetypes
    clean_name = filename.strip()
    stmt = select(TenantFile).where(
        TenantFile.original_filename == clean_name
    ).order_by(TenantFile.created_at.desc())
    file_record = await db.scalar(stmt)
    if not file_record or not os.path.exists(file_record.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File '{filename}' not found")

    media_type = file_record.mime_type or mimetypes.guess_type(file_record.original_filename)[0] or "application/octet-stream"

    return FileResponse(
        path=file_record.file_path,
        media_type=media_type,
        filename=file_record.original_filename,
        headers={
            "Content-Disposition": f'attachment; filename="{file_record.original_filename}"'
        },
    )


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Download a file ensuring correct filename and original format."""
    import mimetypes
    stmt = select(TenantFile).where(TenantFile.id == file_id)
    res = await db.execute(stmt)
    file_record = res.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if not os.path.exists(file_record.file_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Physical file missing on server")

    media_type = file_record.mime_type or mimetypes.guess_type(file_record.original_filename)[0] or "application/octet-stream"

    return FileResponse(
        path=file_record.file_path,
        media_type=media_type,
        filename=file_record.original_filename,
        headers={
            "Content-Disposition": f'attachment; filename="{file_record.original_filename}"'
        },
    )


@router.delete("/{file_id}", status_code=status.HTTP_200_OK)
async def delete_file(
    file_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> dict:
    """Delete a file from tenant storage and database."""
    stmt = select(TenantFile).where(TenantFile.id == file_id, TenantFile.tenant_id == tenant_id)
    res = await db.execute(stmt)
    file_record = res.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    try:
        if os.path.exists(file_record.file_path):
            os.remove(file_record.file_path)
    except Exception:
        pass

    await db.delete(file_record)
    await db.commit()
    return {"message": "File deleted successfully", "id": file_id}
