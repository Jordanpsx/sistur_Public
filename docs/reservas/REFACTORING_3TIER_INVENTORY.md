# Refactoring: 3-Tier Inventory Architecture + Dynamic Configuration

**Date:** 2026-03-09
**Revision:** b7c8d9e0f1a2
**Status:** Complete — Models Updated, Migration File Generated

---

## Executive Summary

The Reservas module has been refactored to implement a **3-tier inventory architecture** that decouples physical stock (resources) from commercial pricing (tariffs). Additionally, a dynamic configuration system (`ReservaConfig`) has been added to support flexible, per-venue settings via JSON.

**Key motivations:**
- Multiple pricing tiers (e.g., "Inteira" and "Meia") often sell the same physical capacity
- Stock management should be independent of pricing changes
- Settings (taxes, pricing rules, deposit percentages) should be configurable without code changes

---

## Architecture Overview

### Before (2-Tier)

```
ReservaSource (Venue)
├── ReservaCategory (Type: "Day Use")
│   └── ReservaItem (Tariff: "Inteira")
│       ├── name: "Inteira"
│       ├── billing_type: FIXED
│       ├── stock_quantity: 50  ← Stock was on tariff
│       └── requires_deposit: False
│   └── ReservaItem (Tariff: "Meia")
│       ├── name: "Meia"
│       ├── billing_type: FIXED
│       ├── stock_quantity: 50  ← Duplicate stock!
│       └── requires_deposit: False
```

**Problem:** Both tariffs have separate `stock_quantity=50`, leading to:
- Inconsistent inventory tracking (total capacity appears as 100 when it's really 50)
- Difficult to edit stock levels (must update both items)
- Unclear which item "owns" the inventory

### After (3-Tier)

```
ReservaSource (Venue)
├── ReservaCategory (Type: "Day Use")
│   └── ReservaItem (Tariff: "Inteira", resource_id=1, category_id=1)
│   └── ReservaItem (Tariff: "Meia", resource_id=1, category_id=1)  ← Same resource!
├── ReservaResource (Physical Pool: "Capacidade Day Use")
│   ├── name: "Capacidade Day Use"
│   ├── stock_quantity: 50  ← Single source of truth
│   └── deleted_at: NULL
└── ReservaConfig (Settings)
    ├── taxa_servico: 10.0
    ├── precificacao_dinamica: true
    └── margem_lucro_minima: 25.0
```

**Benefits:**
- ✅ Single inventory pool shared by multiple tariffs
- ✅ Consistent stock updates (change once, affects all tariffs)
- ✅ Clean separation: Resource = inventory, Item = pricing
- ✅ Flexible configuration without code changes

---

## Model Changes

### New Model: `ReservaResource`

**Table:** `sistur_reserva_resources`

```python
class ReservaResource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey(...), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    stock_quantity = db.Column(db.Integer, nullable=True)  # NULL = infinite
    deleted_at = db.Column(db.DateTime, nullable=True)  # soft-delete
    criado_em = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('source_id', 'name', name='uq_reserva_resource_source_name'),
    )
```

**Semantics:**
- `source_id`: FK to parent venue; CASCADE on delete
- `name`: Unique within source (e.g., "Capacidade Day Use", "Churrasqueira Grande")
- `stock_quantity`: `NULL` = infinite; `0+` = limited stock
- `deleted_at`: Soft-delete timestamp; `NULL` = active

**Examples:**
```python
# Venue "Cachoeira": Day Use capacity
ReservaResource(source_id=1, name="Capacidade Day Use", stock_quantity=50)

# Venue "Cachoeira": Grills available
ReservaResource(source_id=1, name="Churrasqueira Grande", stock_quantity=2)

# Venue "Cachoeira": Bar (unlimited)
ReservaResource(source_id=1, name="Bar Drinks", stock_quantity=None)
```

### New Model: `ReservaConfig`

**Table:** `sistur_reserva_config`

```python
class ReservaConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey(...), nullable=True, index=True)
    config_key = db.Column(db.String(100), nullable=False)
    config_value = db.Column(db.JSON, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False)
    atualizado_em = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('source_id', 'config_key', name='uq_reserva_config_source_key'),
    )
```

**Semantics:**
- `source_id`: FK to parent venue; `NULL` = global config (applies to all venues)
- `config_key`: String identifier (e.g., "taxa_servico"); unique per source
- `config_value`: JSON — can be scalar, object, or array
- `description`: Optional documentation

**Standard Keys:**

| Key | Type | Example | Purpose |
|-----|------|---------|---------|
| `taxa_servico` | float | `10.0` | Service tax percentage |
| `precificacao_dinamica` | bool | `true` | Enable dynamic pricing |
| `margem_lucro_minima` | float | `25.0` | Minimum profit margin % |
| `limite_antecedencia_dias` | int | `90` | Max advance booking days |
| `email_confirmacao` | bool | `true` | Send confirmation email |
| `percentual_deposito` | float | `30.0` | Deposit percentage of total |
| `regras_grupo` | object | `{"minimo_pessoas": 10, "desconto_percentual": 15}` | Group booking rules |

**Examples:**
```python
# Global config: service tax
ReservaConfig(source_id=None, config_key="taxa_servico", config_value=10.0)

# Venue-specific: dynamic pricing enabled for "Cachoeira"
ReservaConfig(source_id=1, config_key="precificacao_dinamica", config_value=True)

# Complex: group rules for "Vinhedo"
ReservaConfig(
    source_id=2,
    config_key="regras_grupo",
    config_value={
        "minimo_pessoas": 10,
        "desconto_percentual": 15,
        "limite_grupos_por_dia": 2
    }
)
```

### Refactored Model: `ReservaItem`

**Changes:**
1. **Added:** `category_id` (FK to ReservaCategory)
2. **Added:** `resource_id` (FK to ReservaResource)
3. **Removed:** `stock_quantity` (now managed by ReservaResource)
4. **New unique constraint:** `(category_id, name)` — prevents duplicate tariff names within a category

**Before:**
```python
class ReservaItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    billing_type = db.Column(db.Enum(BillingType), nullable=False)
    stock_quantity = db.Column(db.Integer, nullable=True)  ← REMOVED
    requires_deposit = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(db.DateTime, nullable=False)
```

**After:**
```python
class ReservaItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey(...), nullable=False, index=True)  ← NEW
    resource_id = db.Column(db.Integer, db.ForeignKey(...), nullable=True, index=True)  ← NEW
    name = db.Column(db.String(150), nullable=False)
    billing_type = db.Column(db.Enum(BillingType), nullable=False)
    requires_deposit = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('category_id', 'name', name='uq_reserva_item_category_name'),
    )

    # Relationships (NEW)
    category = db.relationship("ReservaCategory", back_populates="items")
    resource = db.relationship("ReservaResource", back_populates="items")
```

**Semantics:**
- `category_id`: FK to parent category; CASCADE on delete
- `resource_id`: FK to physical resource; SET NULL on delete (tariff can exist without resource reference)
- `name`: Unique within category (e.g., "Inteira", "Meia" within "Day Use")

**Relationships Updated:**
- `ReservaCategory.items` now returns all tariffs in that category
- `ReservaResource.items` returns all tariffs that decrement that resource

### Model: `ReservaCategory` (Enhanced)

**New relationship:**
```python
items = db.relationship(
    "ReservaItem",
    back_populates="category",
    cascade="all, delete-orphan",
    lazy="select",
)
```

Now ReservaCategory owns its items (one-to-many).

### Model: `ReservaSource` (Enhanced)

**New relationships:**
```python
resources = db.relationship(
    "ReservaResource",
    back_populates="source",
    cascade="all, delete-orphan",
    lazy="select",
)
config = db.relationship(
    "ReservaConfig",
    back_populates="source",
    cascade="all, delete-orphan",
    lazy="select",
)
```

Now ReservaSource owns resources and config.

---

## Database Migration

### Migration File

**Revision ID:** `b7c8d9e0f1a2`
**File:** `migrations/versions/b7c8d9e0f1a2_refactor_reservas_3tier_inventory_config.py`

### Upgrade Steps

1. **Create `sistur_reserva_resources` table**
   - Schema: id (PK), source_id (FK), name (String 150), stock_quantity (Integer, nullable), deleted_at, criado_em
   - Unique constraint: `(source_id, name)`
   - Index: source_id

2. **Create `sistur_reserva_config` table**
   - Schema: id (PK), source_id (FK, nullable), config_key (String 100), config_value (JSON), description, criado_em, atualizado_em
   - Unique constraint: `(source_id, config_key)`
   - Index: source_id

3. **Refactor `sistur_reserva_items` table**
   - Add column: `category_id` (Integer, FK to sistur_reserva_categories)
   - Add column: `resource_id` (Integer, FK to sistur_reserva_resources, nullable)
   - Create indexes: category_id, resource_id
   - Add unique constraint: `(category_id, name)`
   - **Drop column:** `stock_quantity`

### Running the Migration

**Local Development:**
```bash
# Auto-generate from models (if database is available):
flask db migrate -m "Refactor reservas: add 3-tier inventory..."
flask db upgrade

# OR manually run:
flask db upgrade
```

**VPS Deployment:**
```bash
# Automatic via GitHub Actions deploy.yml:
# 1. ssh into VPS
# 2. cd sistur && docker compose down
# 3. docker compose up -d --build
# 4. docker exec -it sistur-flask-app flask db upgrade

# OR manual trigger:
docker exec -it sistur-flask-app flask db upgrade
```

### Downgrade (Rollback)

```bash
flask db downgrade
```

This will:
1. Restore `stock_quantity` column to sistur_reserva_items
2. Drop `category_id`, `resource_id` columns
3. Drop sistur_reserva_config table
4. Drop sistur_reserva_resources table

---

## Data Migration Guide

### Scenario 1: New Installation

No data transformation needed. Schema is created cleanly from models.

### Scenario 2: Existing Data (Upgrade from Old Schema)

**Situation:**
- Old: Each `ReservaItem` has its own `stock_quantity`
- New: Stock is in `ReservaResource`; items reference it

**Strategy:**

1. **Identify categories and resources** (manual mapping):
   ```
   Old:
   - ReservaItem id=1, name="Inteira", category=?, stock_quantity=50
   - ReservaItem id=2, name="Meia", category=?, stock_quantity=50

   New:
   - ReservaCategory id=1, name="Day Use"
   - ReservaResource id=1, name="Capacidade Day Use", stock_quantity=50
   - ReservaItem id=1, category_id=1, resource_id=1, name="Inteira"
   - ReservaItem id=2, category_id=1, resource_id=1, name="Meia"
   ```

2. **Create ReservaResource entries:**
   - For each unique `(venue, stock_pool)` pair, create one ReservaResource
   - Use a business rule: "If multiple items have the same stock_quantity and belong to the same venue, they likely share the same resource"

3. **Create ReservaCategory entries:**
   - If not already present, infer from item types or create a default category per venue

4. **Update ReservaItem:**
   - Set `category_id` based on mapping
   - Set `resource_id` based on which ReservaResource it should decrement from
   - `stock_quantity` will be dropped by migration

**Example SQL (post-migration):**
```sql
-- After migration, ReservaCategory and ReservaResource already exist (via script)

-- Map items to categories and resources
UPDATE sistur_reserva_items
SET category_id = (SELECT id FROM sistur_reserva_categories WHERE name = 'Day Use' LIMIT 1),
    resource_id = (SELECT id FROM sistur_reserva_resources WHERE name = 'Capacidade Day Use' LIMIT 1)
WHERE name IN ('Inteira', 'Meia');
```

---

## Backward Compatibility

### API Changes

**Endpoints:** No changes to existing routes (still return 501 Not Implemented).

**Response schemas** (when implemented):

**Old:** Item included stock in payload
```json
{
  "item": {
    "id": 1,
    "name": "Inteira",
    "stock_quantity": 50
  }
}
```

**New:** Stock is fetched from resource
```json
{
  "item": {
    "id": 1,
    "name": "Inteira",
    "category_id": 1,
    "resource_id": 1
  },
  "resource": {
    "id": 1,
    "name": "Capacidade Day Use",
    "stock_quantity": 50
  }
}
```

### Code Changes in Services

When implementing `ReservaService` methods, use pattern:

```python
# Old way (DEPRECATED):
item = ReservaItem.query.get(item_id)
available = item.stock_quantity - reserved

# New way:
item = ReservaItem.query.get(item_id)
resource = item.resource  # load relationship
available = resource.stock_quantity - reserved if resource else float('inf')
```

---

## Testing

### Unit Tests (to be added)

```python
# tests/models/test_reservas.py

def test_reserva_resource_creation():
    """ReservaResource can be created with stock_quantity."""
    assert ReservaResource(source_id=1, name="Capacidade", stock_quantity=50)

def test_reserva_item_references_resource():
    """ReservaItem can reference a ReservaResource."""
    item = ReservaItem(category_id=1, resource_id=1, name="Inteira")
    assert item.resource_id == 1

def test_reserva_config_json_value():
    """ReservaConfig.config_value can store JSON."""
    config = ReservaConfig(
        source_id=1,
        config_key="regras_grupo",
        config_value={"minimo_pessoas": 10}
    )
    assert config.config_value["minimo_pessoas"] == 10

def test_unique_constraint_item_category_name():
    """Cannot create duplicate items within same category."""
    # Attempt to create two "Inteira" items in same category
    # Should raise IntegrityError
```

### Integration Tests

```python
def test_multiple_items_share_resource():
    """Multiple items can decrement the same resource."""
    resource = ReservaResource(source_id=1, name="Capacity", stock_quantity=50)
    item1 = ReservaItem(category_id=1, resource_id=resource.id, name="Inteira")
    item2 = ReservaItem(category_id=1, resource_id=resource.id, name="Meia")

    # Both items point to same resource
    assert item1.resource.id == item2.resource.id
```

---

## Documentation Updates

### Files Updated

1. **`app/models/reservas.py`**
   - Updated module docstring with 3-tier explanation
   - Added ReservaResource class
   - Added ReservaConfig class
   - Refactored ReservaItem with category_id, resource_id

2. **`migrations/versions/b7c8d9e0f1a2_*.py`**
   - Migration file with upgrade/downgrade logic

4. **This file:** `docs/reservas/REFACTORING_3TIER_INVENTORY.md`
   - Complete refactoring guide

### To Be Created

- `docs/reservas/MIGRATION_GUIDE.md` — detailed data migration steps for existing deployments
- `docs/reservas/API_SCHEMAS_3TIER.md` — updated response schemas (when API is implemented)

---

## Next Steps

### Immediate

1. ✅ **Models:** Updated (ReservaResource, ReservaConfig added; ReservaItem refactored)
2. ✅ **Migration:** File created (`b7c8d9e0f1a2_*.py`)
3. ✅ **Documentation:** this guide created
4. ⬜ **Test:** Run migrations locally to verify schema changes
5. ⬜ **API Implementation:** Update/implement ReservaService to use new architecture

### Before Production Deployment

- [ ] Run full migration suite on staging database
- [ ] Verify relationships in ORM (lazy loading, cascade behavior)
- [ ] Update any existing ReservaService methods to reference resource_id
- [ ] Create ReservaConfigService for managing dynamic settings
- [ ] Add unit tests for new models
- [ ] Document API schemas with new fields (category_id, resource_id)

### Long-term

- [ ] Implement ReservaService methods (criar, atualizar, deletar, listar)
- [ ] Add ReservaConfigService (get, set, delete config values)
- [ ] Build UI for managing resources and config (admin panel)
- [ ] Implement inventory depletion logic (consume stock when booking confirmed)
- [ ] Add audit logging for config changes

---

## Reference

- **Auditoria obrigatória:** All mutations to ReservaConfig must call `AuditService.log_update()`
- **Testes obrigatórios:** All new service methods must have unit tests
- **API Contracts:** All new/updated endpoints must be documented in `docs/api_catalog.md`
