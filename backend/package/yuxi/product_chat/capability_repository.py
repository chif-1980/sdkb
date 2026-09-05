"""Permission-aware access to the governed enterprise capability map."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_product import CapabilityCatalog, CapabilityEvidence


class CapabilityCatalogRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_visible(
        self,
        *,
        query: str = "",
        tenant_key: str | None = None,
        limit: int = 30,
    ) -> list[dict]:
        stmt = select(CapabilityCatalog).where(
            or_(CapabilityCatalog.tenant_key.is_(None), CapabilityCatalog.tenant_key == tenant_key),
        )
        normalized = query.strip()
        if normalized:
            pattern = f"%{normalized}%"
            stmt = stmt.where(
                or_(
                    CapabilityCatalog.name.ilike(pattern),
                    CapabilityCatalog.description.ilike(pattern),
                    CapabilityCatalog.category.ilike(pattern),
                )
            )
        stmt = stmt.order_by(CapabilityCatalog.name.asc(), CapabilityCatalog.id.asc()).limit(max(1, min(limit, 100)))
        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())
        now = datetime.utcnow()
        output: list[dict] = []
        for row in rows:
            if row.valid_until and row.valid_until < now:
                continue
            evidence_result = await self.db.execute(
                select(CapabilityEvidence).where(
                    CapabilityEvidence.capability_id == row.id,
                    CapabilityEvidence.status == "ACTIVE",
                ).order_by(CapabilityEvidence.created_at.desc())
            )
            evidence = list(evidence_result.scalars().all())
            output.append({
                "id": row.id,
                "name": row.name,
                "category": row.category or "",
                "delivery_status": row.delivery_status or "UNKNOWN",
                "description": row.description or "",
                "supported_scopes": row.supported_scopes or [],
                "limitations": row.limitations or [],
                "owner": row.owner,
                "valid_until": row.valid_until.isoformat() if row.valid_until else None,
                "evidence": [
                    {
                        "citation_id": item.citation_id,
                        "evidence_type": item.evidence_type,
                        "valid_at": item.valid_at.isoformat() if item.valid_at else None,
                    }
                    for item in evidence
                ],
            })
        return output
