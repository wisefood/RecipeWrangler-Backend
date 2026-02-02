# Purpose: Neo4j schema string used for text-to-cypher prompting.

neo4j_schema = """
Node: Recipe
- Properties:
  - id: STRING
  - title: STRING
  - duration: FLOAT
  - instructions: LIST(STRING)
  - serves: INTEGER
  - servingsizegrams: FLOAT
  - totalenergykcalperserving: FLOAT
  - totalsodiummgperserving: FLOAT
  - directionsize: INTEGER
  - ingredientssizes: INTEGER
  - totalcarbohydrategperserving: FLOAT
  - totalsaturatedfatgperserving: FLOAT
  - totaldietaryfibergperserving: FLOAT
  - totalfatgperserving: FLOAT
  - sustainabilityperkg: FLOAT (optional)
  - totalenergyfromfatkcalperserving: FLOAT
  - totalsugargperserving: FLOAT
  - totalsustainabilityperserving: FLOAT (optional)
  - fsascore: FLOAT (optional)
  - nutriscore: FLOAT
  - whoscore: FLOAT (optional)
  - totalsustainability: FLOAT (optional)
  - totalcholesterolmgperserving: FLOAT
  - totalproteingperserving: FLOAT

Node: Ingredient
- Properties:
  - name: STRING
  - embedding: LIST(FLOAT)

Node: Allergen
- Properties:
  - name: STRING

Relationship: (:Recipe)-[:HAS_INGREDIENT {weight: NUMBER, measurement: STRING}]->(:Ingredient)
Relationship: (:Ingredient)-[:SIMILAR_TO {score: FLOAT}]->(:Ingredient)
Relationship: (:Ingredient)-[:HAS_ALLERGEN]->(:Allergen)
"""
