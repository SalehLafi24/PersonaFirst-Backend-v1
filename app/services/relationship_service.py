from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.attribute_value_relationship import AttributeValueRelationship


def list_relationships(
    db: Session, workspace_id: int, status: str | None = None
) -> list[AttributeValueRelationship]:
    q = db.query(AttributeValueRelationship).filter(
        AttributeValueRelationship.workspace_id == workspace_id
    )
    if status:
        q = q.filter(AttributeValueRelationship.status == status)
    return q.all()


def _transition(
    db: Session, workspace_id: int, relationship_id: int, new_status: str
) -> AttributeValueRelationship:
    rel = (
        db.query(AttributeValueRelationship)
        .filter(
            AttributeValueRelationship.id == relationship_id,
            AttributeValueRelationship.workspace_id == workspace_id,
        )
        .first()
    )
    if not rel:
        raise HTTPException(status_code=404, detail="Relationship not found")
    rel.status = new_status
    db.commit()
    db.refresh(rel)
    return rel


def approve_relationship(db: Session, workspace_id: int, relationship_id: int):
    return _transition(db, workspace_id, relationship_id, "approved")


def reject_relationship(db: Session, workspace_id: int, relationship_id: int):
    return _transition(db, workspace_id, relationship_id, "rejected")


def archive_relationship(db: Session, workspace_id: int, relationship_id: int):
    return _transition(db, workspace_id, relationship_id, "archived")
