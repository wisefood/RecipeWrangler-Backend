# Purpose: Neo4j driver/session helpers and run_query.

import os
from neo4j import GraphDatabase
from typing import Optional
import os

# Set up the connection (override via env vars)
uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
username = os.getenv("NEO4J_USER", "neo4j")
password = os.getenv("NEO4J_PASSWORD", "")

auth = None
if password and password.lower() != "none":
    auth = (username, password)

driver = GraphDatabase.driver(uri, auth=auth)

# Function to run a Cypher query
def run_query(query, parameters=None):
    with driver.session() as session:
        result = session.run(query, parameters)  # Pass parameters directly
        return list(result) #list(result)
