# Graph Schema Overview

This document summarizes the current Neo4j graph schema used in this project.

## Nodes

### Recipe
- Required: `id`, `title`, `url`, `instructions`
- Optional: `duration`, `serves`

### Ingredient
- Required: `name`, `canonical_id`
- Optional: `miskg_id`

### FoodOnClass
- Required: `foodon_id`
- Optional: `name`, `label`

### FlavorDBIngredient
- Required: `flavordb_id`, `name`, `is_hub`

### FlavorDBCompound
- Required: `flavordb_id`, `name`, `is_hub`

## Relationships

### HAS_INGREDIENT (Recipe → Ingredient)
- Required: `measurement`, `unit`

### HAS_CLASS (Ingredient → FoodOnClass)
- No properties

### SUBCLASS_OF (FoodOnClass → FoodOnClass)
- No properties

### PAIRS_WITH (FlavorDBIngredient ↔ FlavorDBIngredient)
- Required: `score`

### HAS_FLAVOR_COMPOUND (FlavorDBIngredient → FlavorDBCompound)
- Required: `score`

### HAS_DRUG_COMPOUND (FlavorDBIngredient → FlavorDBCompound)
- Required: `score`

### FLAVORDB_EQUIVALENT (Ingredient → FlavorDBIngredient)
- Required: `cosine_similarity`, `similarity`, `miskg_ingredient`, `flavordb_name`

### HAS_SUBSTITUTION (Ingredient → Ingredient)
- Required: `ingredient`, `substitution`, `ingredient_original_id`, `substitution_original_id`
