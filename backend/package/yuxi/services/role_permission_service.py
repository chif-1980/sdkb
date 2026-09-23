from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import RolePermission, User


async def feedback_scope(db: AsyncSession, user: User) -> str:
    if user.role == "superadmin":
        return "all"
    if user.role != "admin":
        return "none"
    record = await db.get(RolePermission, (user.role, "feedback.view"), populate_existing=True)
    return record.scope if record and record.scope in {"department", "all"} else "none"
