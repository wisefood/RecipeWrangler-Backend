from neo4j import GraphDatabase
from typing import Optional

# Set up the connection
uri = "bolt://localhost:7687"  # Change if using a remote server or different port
# Create the driver instance
driver = GraphDatabase.driver(uri)

# Function to run a Cypher query
def run_query(query, parameters=None):
    with driver.session() as session:
        result = session.run(query, parameters)  # Pass parameters directly
        return list(result) #list(result)
