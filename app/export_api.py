"""Admin export endpoint: stream a portable backup bundle over HTTP.

The bundle carries credentials (users and API key hashes), so it is available to
the admin role only and is never cached. The zip is built in memory and streamed
out, so no temporary file lands on the data volume.
"""

from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from . import archive, auth
from .deps import Conn

router = APIRouter(
    tags=["export"],
    dependencies=[Depends(auth.require("admin"))],
)


@router.get("/api/export")
def download_export(conn: Conn) -> Response:
    buffer = io.BytesIO()
    stamp = archive.utc_stamp()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        archive.write_bundle(zf, conn, assets=True, created_at=stamp)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="shelfmark-{stamp}.zip"',
            "Cache-Control": "no-store",
        },
    )
