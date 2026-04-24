# Import all models so Alembic can detect them via Base.metadata
from app.models.attribute_allowed_value import AttributeAllowedValue
from app.models.attribute_value_relationship import AttributeValueRelationship
from app.models.audit_log import AuditLog
from app.models.customer_attribute_affinity import CustomerAttributeAffinity
from app.models.customer_purchase import CustomerPurchase
from app.models.product import Product, ProductAttribute
from app.models.product_behavior_relationship import ProductBehaviorRelationship
from app.models.proposed_attribute import (
    ProposedAttributeAggregate,
    ProposedAttributeEvent,
)
from app.models.proposed_attribute_value import (
    ProposedAttributeValueAggregate,
    ProposedAttributeValueEvent,
)
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_user import WorkspaceUser
