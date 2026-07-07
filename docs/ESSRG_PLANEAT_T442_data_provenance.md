# ESSRG PLANEAT T442 Meal Database - Data Provenance

## Dataset Overview

**Dataset Name:** PLANEAT T442 Meal Database - Hungary Living Lab (ESSRG)
**Source:** European Sustainable Seafood and Responsible Governance (ESSRG), Hungary Living Lab
**File:** `PLANEAT T442 MEAL DB LL ESSRG.xlsx`
**Received:** 2026-06-19
**Region:** Hungary
**Food Composition Base:** CoFID 2021 (Hungarian Food Composition Database)

---

## 1. Raw Data Structure

### File Format
- **Format:** Microsoft Excel (.xlsx) workbook
- **Sheets:** 18 sheets total
  - **Recipe/Meal sheets:** Meals, Dishes, Lists, List of tables
  - **Food composition sheets:** 1.1 Notes, 1.2 Factors, 1.3 Proximates, 1.4 Inorganics, 1.5 Vitamins, 1.6 Vitamin Fractions, 1.7–1.12 (Fatty acids by species), 1.13 Phytosterols, 1.14 Organic Acids

### Sheet Descriptions

#### **Meals Sheet**
- **Records:** 1,005 rows (includes metadata header rows)
- **Active meal records:** 150 unique meal names
- **Columns:** 29 total
  - **Key fields:**
    - `ID`: Meal identifier
    - `Name`: Meal title
    - `Description [OPTIONAL]`: Preparation notes (62 records have descriptions)
    - `Contains animal products?`: Categorical flag (Egg, Dairy, Red meat, Other meat, Plant based)
    - `Type`: Meal category (Breakfast, Lunch, Dinner, Snack)
    - `Autumn`, `Winter`, `Spring`, `Summer`: Seasonal availability flags (Y/N)
    - `ID.1–ID.10`, `Dish #1–Dish #10`: References to up to 10 component dishes per meal

**Seasonality Distribution:**
| Season | Count |
|--------|-------|
| Autumn | 146 meals (97.3%) |
| Winter | 117 meals (78.0%) |
| Spring | 120 meals (80.0%) |
| Summer | 141 meals (94.0%) |

#### **Dishes Sheet**
- **Records:** 1,005 rows (includes metadata header rows)
- **Active dish records:** 222 unique dish names
- **Columns:** 34 total
  - **Key fields:**
    - `ID`: Dish identifier
    - `Name`: Dish name
    - `Recipe [OPTIONAL]`: Preparation instructions (227 records have recipes)
    - `Contains animal products?`: Categorical flag
    - `CoFID ID`, `Food #1–Food #10`: Food item names (from CoFID database)
    - `Quantity (g/ml)`, `Quantity (g/ml).1–.9`: Ingredient quantities in grams or millilitres
- **Ingredient Completeness:**
  - All 222 dishes with names have at least one ingredient
  - Ingredients are already structured with food names and quantities in grams

**Animal Product Distribution (Dishes):**
| Category | Count |
|----------|-------|
| Plant based | 148 (67%) |
| Egg | 22 (10%) |
| Dairy | 21 (9%) |
| Other meat | 17 (8%) |
| Red meat | 14 (6%) |

#### **Food Composition Base: CoFID 2021**
- **Total food items:** 2,887 unique foods
- **Source:** Hungarian Food Composition Database (CoFID) 2021 edition
- **All nutrient values:** Per 100g of food (except alcoholic beverages, which are per 100ml)

##### **1.2 Factors Sheet**
- **Records:** 2,887 foods (rows 3–2889)
- **Columns:** Food Code, Food Name, Description, Group, Previous, Main data references, Footnote, and conversion factors
- **Conversion Factors Available:**
  - Edible proportion (all 2,887 foods)
  - Specific gravity (optional)
  - Total solids (optional)
  - Nitrogen conversion factor
  - Glycerol conversion factor

##### **1.3 Proximates Sheet** (Core nutrients)
- **Columns:** 47 (includes metadata)
- **Nutrient Coverage (per 100g food):**
  - Water (g): 2,889 values
  - Total nitrogen (g): 2,889 values
  - Protein (g): 2,889 values ✓ **Required**
  - Fat (g): 2,889 values ✓ **Required**
  - Carbohydrate (g): 2,887 values ✓ **Required**
  - Energy (kcal): 2,888 values ✓ **Required for Nutri-Score**
  - Energy (kJ): 2,888 values
  - Starch (g): 2,887 values
  - Total sugars (g): 2,887 values ✓ **Required for Nutri-Score**
  - Fiber (NSP or AOAC): 2,884–2,887 values ✓ **Required for Nutri-Score**
  - Saturated fat (g): 2,875 values ✓ **Required for Nutri-Score**
  - Sodium (mg): See Inorganics sheet ✓ **Required for Nutri-Score**
  - Alcohol (g): 1,897 values
  - Individual sugars (Glucose, Fructose, Sucrose, Lactose, Maltose, Galactose): 1,669–2,876 values
  - Cholesterol (mg): 2,874 values

##### **1.4 Inorganics Sheet**
- **Columns:** 19
- **Minerals Available (per 100g food):**
  - Sodium (mg): ✓ **Required for Nutri-Score**
  - Potassium, Calcium, Magnesium, Phosphorus
  - Iron, Copper, Zinc, Manganese
  - Chloride, Selenium, Iodine

##### **1.5 Vitamins Sheet**
- **Columns:** 24
- **Vitamins Available (per 100g food):**
  - Retinol, Carotene, Retinol Equivalent
  - Vitamin D, Vitamin E, Vitamin K1
  - Thiamin, Riboflavin, Niacin, Niacin equivalent
  - Vitamin B6, Vitamin B12, Folate, Pantothenate, Biotin
  - Vitamin C

##### **1.6 Vitamin Fractions Sheet**
- **Columns:** 25
- **Detailed vitamin breakdown:** All-trans-retinol, carotene fractions, tocopherols, tocotrienols, etc.

##### **1.7–1.12 Fatty Acids Sheets**
- **Saturated Fatty Acids (SFA):** 1.7 (per 100g FA), 1.8 (per 100g food) — 34 columns each
  - Individual chain lengths (C4:0 through C25:0)
  - Branched chain variants
- **Monounsaturated Fatty Acids (MUFA):** 1.9–1.10 — 32 columns each
  - cis and trans isomers
  - Individual species (C10:1 through C24:1)
- **Polyunsaturated Fatty Acids (PUFA):** 1.11–1.12 — 43 columns each
  - n-3 and n-6 families
  - EPA (C20:5), DHA (C22:6), and linoleic acid (C18:2)

**Coverage:** Varies by fatty acid type
- Saturated fat per 100g food: 2,875 values
- Monounsaturated fat per 100g food: 2,874 values
- Polyunsaturated fat per 100g food: 2,873 values
- Trans fats: 927–2,408 values

##### **1.13 Phytosterols Sheet**
- **Columns:** 17
- **Plant sterols:** Total phytosterols, beta-sitosterol, brassicasterol, campesterol, avenasterols, stigmasterol

##### **1.14 Organic Acids Sheet**
- **Columns:** 9
- **Acids:** Citric acid, Malic acid

---

## 2. Data Completeness Assessment

### Recipe Data Completeness

| Metric | Count | % |
|--------|-------|---|
| Total meal records | 150 | 100 |
| Meals with description | 62 | 41.3 |
| Meals with seasonality flag | 150 | 100 |
| Meals with ≥1 dish component | 150 | 100 |
| **Total dish records** | **222** | **100** |
| Dishes with recipe instructions | 227 | 102* |
| Dishes with ≥1 ingredient | 222 | 100 |
| Dishes with structured quantities (g/ml) | 222 | 100 |

*Note: Some dishes appear in multiple meals, so the 227 count represents data cells, not unique dishes.

### Nutrient Data Completeness (Core nutrients required for Nutri-Score)

| Nutrient | Values | Coverage |
|----------|--------|----------|
| Energy (kcal) | 2,888 | 99.97 |
| Protein (g) | 2,889 | 100.0 |
| Fat (g) | 2,889 | 100.0 |
| Saturated fat (g) | 2,875 | 99.5 |
| Carbohydrate (g) | 2,887 | 99.93 |
| Total sugars (g) | 2,887 | 99.93 |
| Fiber (NSP or AOAC) | 2,884–2,887 | 99.8–99.93 |
| Sodium (mg) | — | 100.0 (all inorganic columns populated) |

**Nutri-Score Derivation Feasibility:** All core nutrients required for Nutri-Score calculation are available for >99.5% of foods in the composition table.

---

## 3. Licence and Usage Restrictions

- **Licence:** Not explicitly documented in the raw file.
- **Source Metadata:** CoFID 2021 is a Hungarian national food composition database. Usage restrictions should be verified with ESSRG/PLANEAT consortium.
- **Recommendation:** Clarify with ESSRG whether this dataset and CoFID composition table can be included in RecipeWrangler's public or research contexts.

---

## 4. Field Names and Data Entry Conventions

### Meal/Dish Data

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| ID | Integer | 1, 2, 3 | Unique meal/dish identifier |
| Name | String | "Scrambled eggs and toast" | Required |
| Description | String (optional) | "Scrambled eggs with onion..." | Preparation or ingredient notes |
| Contains animal products? | Categorical | Egg, Dairy, Red meat, Other meat, Plant based | Free-text, not normalized |
| Recipe | String (optional) | "Heat oil, sauté, stir..." | Unstructured preparation steps |
| Type | Categorical | Breakfast, Lunch, Dinner, Snack | Meal classification |
| Season flags | Binary | Y/N | Availability in each season |
| Dish # / Food # | String (reference) | "Scrambled eggs", "Whole meal toast" | References another ID or food name |
| Quantity (g/ml) | Numeric | 100, 80, 40 | All measurements in grams or millilitres |

### Food Composition Data

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| Food Code | String | "12-963", "13-316" | CoFID identifier |
| Food Name | String | "Eggs, chicken, whole, scrambled, without milk" | Standardized food name from CoFID |
| Description | String | "8 cans" | Source description (optional) |
| Group | Categorical | DG | Food group classifier |
| Nutrients | Numeric | 100, 0.5, 2.88 | Per 100g food, except beverages |
| Missing values | Various | "N", "NaN", "Tr" | "N" = not analysed, "Tr" = trace, blank = NaN |

---

## 5. Key Findings and Limitations

### Strengths

1. **Complete ingredients list:** All dishes have structured ingredient lists with quantities already in grams.
2. **High-quality food composition base:** CoFID 2021 is a comprehensive national database with 2,887 foods.
3. **Rich nutrient coverage:** Includes not only macronutrients and key micronutrients but also detailed fatty acid profiles, vitamins, and phytosterols.
4. **Nutri-Score derivable:** All required nutrients for Nutri-Score are present in >99.5% of foods.
5. **Metadata rich:** Meals include seasonality, meal type, animal product flags, and optional descriptions.
6. **Well-structured:** Ingredients and quantities are already separated and quantified.

### Limitations

1. **Meal vs. Serving Size:** Meals reference dishes but do **not specify portion counts or total serving size**. This must be inferred or defined during import.
2. **Optional recipes:** Only 227 of 1,005 dishes have explicit recipe instructions (22.6%). Most dishes will require ingredient-level reconstruction.
3. **No URLs or external sources:** Meals and dishes do not include URLs or links to original recipes or institutions.
4. **No images:** No image URLs are provided.
5. **Animal product flags are free-text:** Values like "Egg", "Dairy" are not normalized; some records contain summary statistics instead of individual flags.
6. **No allergen information:** Allergen flags (e.g., nuts, shellfish) are not provided separately; they must be inferred from ingredient codes.
7. **Cooked vs. raw quantities:** No indication whether ingredient quantities are for raw or cooked weights (assumed raw based on typical food composition practice).
8. **Hungarian-centric:** Food names and groupings are optimized for Hungarian cuisine; cross-cultural mappings may be needed.

---

## 6. Integration Pathway

This dataset is **primarily a new recipe dataset** with an embedded **regional composition table**.

### Recommended Next Steps

1. **Confirm food code mappings:** Validate that all CoFID food codes in the Dishes sheet are present in the Factors/Proximates sheets.
2. **Resolve dish-to-ingredient parsing:** Convert the dish-ingredient references into a clean JSON format with structured ingredient lists (1.2 in integration guide).
3. **Define meal portions:** Clarify whether meals represent single servings or multi-serve units; define standard serving counts.
4. **Validate serving size inference:** For meals without explicit serving size metadata, determine a consistent convention.
5. **Parse recipe instructions:** Extract and normalize the 227 available recipes (1.4 in integration guide).
6. **Resolve weights:** Map ingredient quantities to standardized gram weights using the CoFID conversion factors (1.5 in integration guide).
7. **Profile recipes:** Generate nutrition profiles for each meal using the CoFID composition table (1.6 in integration guide).
8. **Load into databases:** Import recipes into PostgreSQL, Neo4j, and Elasticsearch (1.7 in integration guide).

---

## 7. Acceptance Checklist for Integration

- [ ] Licence restrictions verified with ESSRG
- [ ] Food code mappings validated (all CoFID IDs exist in composition table)
- [ ] Dishes converted to normalized JSON format
- [ ] Meal serving size convention defined
- [ ] Recipe instructions cleaned and extracted where available
- [ ] Ingredient weight resolution completed
- [ ] Full nutrition profiles generated against CoFID composition table
- [ ] Low-coverage meals audited
- [ ] Nutritional outliers reviewed
- [ ] Meals loaded into PostgreSQL profile tables
- [ ] Meals loaded into Neo4j recipe graph
- [ ] Meals indexed in Elasticsearch
- [ ] Complete integration report generated

---

## Appendix: CoFID 2021 Metadata

**Food Groups in CoFID:**
- DG (appears frequently in sample records)
- Other group codes assumed to be defined in CoFID documentation (not visible in sample)

**Data References:**
- "MW4, 1978; and Vegetables, Herbs and Spices Supplement, 1991"
- "Main data references" field contains citations to original analyses and published composition data

**Metadata Rows:**
- Rows 1–2 contain metadata (column codes and full names); actual food data begins at row 3.
