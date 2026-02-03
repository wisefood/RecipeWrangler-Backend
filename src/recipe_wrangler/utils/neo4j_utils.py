from neo4j import GraphDatabase
from typing import Optional
import os

# Set up the connection
uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")

# Support either NEO4J_AUTH="username/password" or NEO4J_USERNAME + NEO4J_PASSWORD.
neo4j_auth = os.getenv("NEO4J_AUTH")
if neo4j_auth:
    username, password = neo4j_auth.split("/", 1)
else:
    username = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not username or not password:
        raise ValueError(
            "Set NEO4J_AUTH (username/password) or NEO4J_USERNAME + NEO4J_PASSWORD."
        )

driver = GraphDatabase.driver(uri, auth=(username, password))


# Function to run a Cypher query
def run_query(query, parameters=None):
    with driver.session() as session:
        result = session.run(query, parameters)  # Pass parameters directly
        return list(result)  # list(result)
