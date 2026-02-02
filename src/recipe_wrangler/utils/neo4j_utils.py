from neo4j import GraphDatabase
from typing import Optional
import os

# Set up the connection
uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
neo4j_auth = os.getenv("NEO4J_AUTH")  # Expected format: "username/password"
if neo4j_auth:
    username, password = neo4j_auth.split("/", 1)
else:
    raise ValueError("NEO4J_AUTH environment variable not set or improperly formatted.")

driver = GraphDatabase.driver(uri, auth=(username, password))


# Function to run a Cypher query
def run_query(query, parameters=None):
    with driver.session() as session:
        result = session.run(query, parameters)  # Pass parameters directly
        return list(result)  # list(result)
