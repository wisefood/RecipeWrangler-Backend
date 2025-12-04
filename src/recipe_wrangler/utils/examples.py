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
    USER INPUT: 'Tell me a Greek salad',
    QUERY: MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Greek' IN tags AND 'Salads' IN tags
    RETURN DISTINCT r.title
    """,

    """USER INPUT: 'Tell me a high-protein dairy free recipe with chicken under 1 hour.',
    QUERY: MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
    WHERE toLower(i.name) CONTAINS 'chicken' AND r.duration < 60
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'High Protein' IN tags AND 'Dairy Free' IN tags
    RETURN DISTINCT r.title
    """,

    """USER INPUT: 'Tell me a dairy free salad with seafood',
    QUERY: MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Seafood' IN tags AND 'Salads' IN tags AND 'Dairy Free' IN tags
    RETURN DISTINCT r.title
    """,

    """USER INPUT: 'Give me a high-protein vegan recipe that takes less than 30 minutes.',
    QUERY: MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'High Protein' IN tags AND 'Vegan' IN tags AND r.duration < 30
    RETURN DISTINCT r.title
    """,

    """USER INPUT: 'Which Indian dessert has the highest WhoScore?',
    QUERY: MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Indian' IN tags AND 'Desserts' IN tags
    RETURN r.title
    ORDER BY r.whoscore DESC NULLS LAST
    LIMIT 1
    """,

    """USER INPUT: 'Find me a gluten-free seafood main course for dinner.',
    QUERY: MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Gluten Free' IN tags AND 'Seafood' IN tags AND 'Main Course' IN tags AND 'Dinner' IN tags
    RETURN DISTINCT r.title
    """,
    
    """
    USER INPUT: 'Find recipes with at least 100 grams of chicken and tagged as low carb.',
    QUERY: MATCH (r:Recipe)-[rel:HAS_INGREDIENT]->(i:Ingredient)
    WHERE toLower(i.name) CONTAINS 'chicken' AND rel.weight >= 100
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Low Carb' IN tags
    RETURN DISTINCT r.title
    """,

    """
    USER INPUT: 'Tell me a high protein recipe with chicken',
    QUERY: MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
    WHERE toLower(i.name) CONTAINS 'chicken'
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    WHERE t.name = 'High Protein'
    RETURN DISTINCT r.title
    LIMIT 5
    """,

    """
    USER INPUT: 'pizza',
    QUERY: MATCH (r:Recipe)
    WHERE toLower(r.title) CONTAINS 'pizza'
    RETURN DISTINCT r.title
    """,

    """USER INPUT: 'Show me quick vegan breakfast recipes',
    QUERY: MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Vegan' IN tags AND 'Breakfast' IN tags AND r.duration <= 20
    RETURN DISTINCT r.title
    """,

    """
    USER INPUT: 'Tell me a low-fat recipe with chicken and rice',
    QUERY: WITH ['chicken','rice'] AS required
    MATCH (r:Recipe)
    WHERE ALL(ing IN required WHERE EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
        WHERE toLower(i.name) CONTAINS toLower(ing)
    })
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    WHERE t.name = 'Low Fat'
    RETURN DISTINCT r.title
    LIMIT 25
    """,

    """
    USER INPUT: 'Show me low-carb mains that use beef or chicken',
    QUERY: MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
    WHERE ANY(x IN ['beef','chicken'] WHERE toLower(i.name) CONTAINS x)
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    WITH r, collect(DISTINCT t.name) AS tags
    WHERE 'Low Carb' IN tags AND 'Main Course' IN tags
    RETURN DISTINCT r.title
    LIMIT 25
    """
]
