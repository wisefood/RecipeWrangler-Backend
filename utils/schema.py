neo4j_schema = """
Node: Recipe
- Properties:
  - title: STRING
  - duration: FLOAT
  - instructions: LIST(STRING)
  - serves: INTEGER
  - servingsizegrams: FLOAT
  - totalenergykcalperservinghummus: FLOAT
  - totalsodiummgperservinghummus: FLOAT
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

Node: Tag
- Properties:
  - name: STRING
  - embedding: LIST(FLOAT)

Node: Allergen
- Properties:
  - name: STRING

Relationship: (:Recipe)-[:HAS_INGREDIENT {weight: NUMBER, measurement: STRING}]->(:Ingredient)
Relationship: (:Recipe)-[:HAS_TAG]->(:Tag)
Relationship: (:Ingredient)-[:SIMILAR_TO {score: FLOAT}]->(:Ingredient)
Relationship: (:Ingredient)-[:HAS_ALLERGEN]->(:Allergen)


Important Notes:
- Tags are stored as Tag nodes (with property 'name').
- Nutritional, dietary, dish type, technique, region, time, and difficulty filters are handled using Tag nodes, not numeric thresholds, unless explicitly requested.
- Use exact tag names from the list below when filtering recipes.

Available Tags:
Diet Type: Vegan, Vegetarian, Meat-based, Seafood
Nutritional: High Protein, Low Fat, Low Carb, High Fiber, Low Sugar, Keto Friendly, Balanced
Health: Heart Healthy, Low Cholesterol, Low Sodium, Diabetic Friendly, Weight Loss Friendly, Highly Nutritious, General
Time: 15-minutes-or-less, 30-minutes-or-less, 60-minutes-or-less, 4-hours-or-less, 1-day-or-less
Difficulty: 5-ingredients-or-less, Easy
Dish Type: Appetizer, Main Course, Side Dish, Breakfast, Lunch, Dinner, Desserts, Snacks, Brunch, Salads, Soups & Stews, Beverages and Cocktails
Main Ingredient: Chicken, Beef, etc.
Special Dietary: Dairy Free, Gluten Free
Techniques: Boil, Bake, Grill, Roast, Sauté, Steam, Fry, etc.
Region: Greek, Indian, Italian, etc.
"""