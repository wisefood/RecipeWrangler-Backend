# Neo4j Docker Setup with APOC and Data Import

This repository contains the necessary files to set up a Neo4j Docker container and a pre-loaded database from a `neo4j.dump` file.

## Files

- `Dockerfile`: Defines the Docker image and database import.
- `docker-compose.yml`: Configures and runs the Docker container.
- `neo4j.dump`: The database dump file to be imported.

## Prerequisites

- Docker Compose installed. Follow the instructions [here](https://docs.docker.com/compose/install/).

## Setup Instructions

### 1. Clone the Repository

Clone the repository or download the files and place them in a directory with the following structure:
```
your-project-directory
├── Dockerfile
├── docker-compose.yml
└── neo4j.dump
```

### 2. Build and Run the Docker Container

Navigate to the directory containing the `docker-compose.yml` file and run the following commands:


```
docker compose build
docker compose up -d
```

### 3. Access Neo4j Browser
Open your web browser and go to http://localhost:7474. You will be prompted to log in with the default username `neo4j` and the default password `neo4j`. Then you will be asked to set up a new password.


### 4. Stopping the Container
To stop the container, run:
```
docker-compose down
```

### Notes
The database will be loaded from the neo4j.dump file.
The container is configured to restart automatically (restart: always).
The dump is baked into the image at `/import/neo4j.dump`, so mounting an empty volume over `/import` will hide it.

### Cypher query examples:
"question": "Tell me a recipe with chicken under 30 minutes",
"query": 
    """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
       WHERE toLower(i.name) CONTAINS 'chicken' AND r.Duration < 30 
       RETURN DISTINCT r.title"""

"question": "Tell me a high-protein dairy free recipe with chicken under 1 hour.",
"query": 
    """
    MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    WHERE toLower(i.name) CONTAINS 'chicken' AND r.Duration < 60
    WITH r.title AS title, collect(t.name) AS tags
    WHERE 'High Protein' IN tags AND 'Dairy Free' IN tags
    RETURN DISTINCT title
    """
)

"question": "Tell me a dairy free salad with seafood",
"query": 
    """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient),(r)-[:HAS_TAG]->(t:Tag)
        WITH r.title AS title, collect(t.name) AS tags
        WHERE 'Seafood' IN tags AND 'Salads' IN tags AND 'Dairy Free' IN tags
        RETURN DISTINCT title"""

"question": "Tell me an Indian vegan desert with the best WhoScore",
"query": 
    """MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
        MATCH (r)-[:HAS_TAG]->(t:Tag)
        WITH r.title AS title, collect(t.name) AS tags, r.WhoScore AS WhoScore
        WHERE 'Vegan' IN tags AND 'Indian' IN tags
        ORDER BY WhoScore DESC
        RETURN DISTINCT title
        LIMIT 1"""

