#from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
from dotenv import load_dotenv

from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.llm import OpenAILLM
from neo4j import GraphDatabase
from neo4j_graphrag.generation import GraphRAG

from recipe_wrangler.utils.neo4j_utils import driver, run_query
from recipe_wrangler.utils.schema import neo4j_schema
from recipe_wrangler.utils.examples import examples

from langchain.tools import tool
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from typing import List, Optional

from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info

from langserve import add_routes
from langchain_core.runnables import Runnable

from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from typing import List, Optional



class SystemInstructionAdapter:
    """
    Adapts a LangChain ChatModel to the interface expected by neo4j_graphrag,
    swallowing `system_instruction` and turning it into a SystemMessage.
    """
    def __init__(self, base_llm):
        self.base_llm = base_llm

    def invoke(
        self,
        prompt: str,
        message_history: Optional[List[BaseMessage]] = None,
        system_instruction: Optional[str] = None,
        **kwargs
    ):
        messages: List[BaseMessage] = []
        if system_instruction:
            messages.append(SystemMessage(content=system_instruction))
        if message_history:
            messages.extend(message_history)
        messages.append(HumanMessage(content=prompt))
        # IMPORTANT: do NOT forward unknown kwargs to the base_llm
        # to avoid passing system_instruction to the client.
        return self.base_llm.invoke(messages)
    

@tool
def query_recipes_open(query_text: str, model: str) -> dict:
    """
    This tool is used every time the user requests a recipe.
    """
    
    if model == "open":
        
        base_llm = ChatOllama(model="mistral:latest") # or qwen3:8b
        wrapped_llm = SystemInstructionAdapter(base_llm)

        retriever = Text2CypherRetriever(
            driver=driver,
            llm=wrapped_llm,        # safe to use the adapter here too
            neo4j_schema=neo4j_schema,
            examples=examples,
        )
        
        rag = GraphRAG(retriever=retriever, llm=wrapped_llm)
    
    if model == 'gpt':
        load_dotenv()
        openai_key = os.getenv("OPENAI_API_KEY")

        t2c_llm = OpenAILLM(model_name="gpt-4o-mini", api_key=openai_key)

        retriever = Text2CypherRetriever(
        driver=driver,
        llm=t2c_llm,
        neo4j_schema=neo4j_schema,
        examples=examples,
        )
        rag = GraphRAG(retriever=retriever, llm=t2c_llm)

        

    
    result = retriever.search(query_text)

    rag_result = rag.search(query_text=query_text)
    #answer = rag_result.answer
    

    cypher = result.metadata.get('cypher')

    structured_results = run_query(cypher)

    similar_recipes = [record['r.title'] for record in structured_results]

    #return cypher
    return similar_recipes, cypher

from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info

@tool
def query_recipes_with_properties_open(query_text: str) -> dict:
    """
    Searches and returns recipes that match the given query criteria, such as 
    specific ingredients, dietary preferences, or nutrition goals.

    Parameters:
        query_text (str): A string describing desired recipe attributes, such as 
                          ingredients (e.g., "chicken, spinach"), dietary type 
                          (e.g., "low-carb", "vegan"), or other filters.

    Returns:
        dict: A dictionary where each key is a recipe title, and each value is another 
              dictionary containing detailed metadata about the recipe, including:
                - 'Instructions' (list of str): Step-by-step preparation guide.
                - 'Duration' (float): Total preparation time in minutes.
                - 'Serves' (int): Number of servings.
                - 'ServingSize' (float): Serving size in grams.
                - 'Calories' (float): Total calories per serving.
                - 'CaloriesFromFat' (float): Calories derived from fat.
                - 'TotalFat' (float): Total fat content (g).
                - 'SaturatedFat' (float): Saturated fat content (g).
                - 'Cholesterol' (float): Cholesterol content (mg).
                - 'Protein' (float): Protein content (g).
                - 'TotalCarbohydrate' (float): Carbohydrate content (g).
                - 'DietaryFiber' (float): Dietary fiber (g).
                - 'Sugars' (float): Sugar content (g).
                - 'Sodium' (float): Sodium content (mg).
                - 'NutriScore' (float): Nutritional quality score (lower is better).
                - 'FSAScore' (float): UK Food Standards Agency health score.
                - 'WhoScore' (float): WHO-based nutrition score.
                - 'Sustainability_per_kg' (float): Environmental impact per kg (lower is better).
    """
  
    result = retriever.search(query_text)

    #rag_result = rag.search(query_text=query_text)
    #answer = rag_result.answer
    
    cypher = result.metadata.get('cypher')

    structured_results = run_query(cypher)

    similar_recipes = [record['r.title'] for record in structured_results]

    recipes_info = {
        title: {k: v for k, v in fetch_recipe_info(title).items() if k != "embedding"}
        for title in similar_recipes
    }
    
    return recipes_info

def query_recipes_Node(state: dict) -> dict:

    query = state['query']

    result = query_recipes_with_properties_open.invoke({
        "query_text": query
    })

    return {
        **state,
        "cypher": result["cypher"],
        "similar_recipes": result["similar_recipes"]
    }
    
"""
# FastAPI app instance
app = FastAPI()

# Request body schema
class QueryRequest(BaseModel):
    query_text: str

# Endpoint definition
@app.post("/query_recipes")
def query_recipes_endpoint(request: QueryRequest):
    try:
        query_text = request.query_text
        result = retriever.search(query_text)
        rag_result = rag.search(query_text=query_text)

        cypher = result.metadata.get('cypher')
        structured_results = run_query(cypher)
        similar_recipes = [record['r.title'] for record in structured_results]

        recipes_info = [
            {**{k: v for k, v in fetch_recipe_info(title).items() if k != "embedding"}, 'title': title}
            for title in similar_recipes
        ]

        return {
            'similar_recipes': recipes_info
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
"""

"""
Run:
uvicorn Text2Cypher_Neo_tool:app --reload

http://127.0.0.1:8000/docs

and:
curl -X POST http://127.0.0.1:8000/query_recipes \
  -H "Content-Type: application/json" \
  -d '{"query_text": "easy chicken curry"}'

"""

"""
from fastapi import FastAPI
from langserve import add_routes
from langchain.tools import tool
from langchain_core.runnables import Runnable

# Create the FastAPI app
app = FastAPI(
    title="Recipe Query API",
    version="1.0",
    description="API to query recipes based on ingredients, dietary filters, etc.",
)

# Add LangServe route for the tool
add_routes(app, query_recipes_with_properties_open, path="/query-recipes")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="localhost", port=8000)
    """
