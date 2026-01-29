# Purpose: Few-shot examples for text-to-cypher prompting.

examples = [
    """USER INPUT: 'Tell me a recipe with chicken under 30 minutes',
    QUERY: MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
    WHERE toLower(i.name) CONTAINS 'chicken' AND r.duration < 30
    RETURN DISTINCT r.title
    """,
    """
    USER INPUT: 'Tell me a recipe with rice and beef',
    QUERY: MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i1:Ingredient),
                 (r)-[:HAS_INGREDIENT]->(i2:Ingredient)
    WHERE toLower(i1.name) CONTAINS 'rice'
      AND toLower(i2.name) CONTAINS 'beef'
    RETURN DISTINCT r.title
    """,
    """
    USER INPUT: 'pizza',
    QUERY: MATCH (r:Recipe)
    WHERE toLower(r.title) CONTAINS 'pizza'
    RETURN DISTINCT r.title
    """,
]

TEXT2CYPHER_FEWSHOT_EXAMPLES = [
    {
        "question": "Tell me a recipe with chicken under 30 minutes",
        "query": """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE toLower(i.name) CONTAINS 'chicken' AND r.Duration < 30
RETURN DISTINCT r.id AS recipe_id, r.title AS title
LIMIT 10""",
    },
    {
        "question": "Tell me a recipe with rice and beef",
        "query": """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i1:Ingredient), (r)-[:HAS_INGREDIENT]->(i2:Ingredient)
WHERE toLower(i1.name) CONTAINS 'rice'
  AND toLower(i2.name) CONTAINS 'beef'
RETURN DISTINCT r.id AS recipe_id, r.title AS title
LIMIT 10""",
    },
    {
        "question": "pizza",
        "query": """MATCH (r:Recipe)
WHERE toLower(r.title) CONTAINS 'pizza'
RETURN DISTINCT r.id AS recipe_id, r.title AS title
LIMIT 10""",
    },
]
