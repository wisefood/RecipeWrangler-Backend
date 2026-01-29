# Purpose: Prompt templates used by text2cypher.

GUARDRAILS_SYSTEM_PROMPT = """
As an intelligent assistant, your primary objective is to decide whether a given question is related to food recipes or not.
If the question is related to food recipes, output "recipe". Otherwise, output "end".
To make this decision, assess the content of the question and determine if it refers to any recipe, ingredient or diet type.
Provide only the specified output: "recipe" or "end".
"""

TEXT2CYPHER_SYSTEM_PROMPT = (
    "Given an input question, convert it to a Cypher query. No pre-amble."
    "Do not wrap the response in any backticks or anything else. Respond with a Cypher statement only!"
    "Property names are case-sensitive; copy them exactly as they appear in the schema."
)
TEXT2CYPHER_HUMAN_PROMPT = """You are a Neo4j expert. Given an input question, create a syntactically correct Cypher query to run.
Do not wrap the response in any backticks or anything else. Respond with a Cypher statement only!
Property names are case-sensitive. Use the exact casing from the schema (e.g., use Duration, not duration).
Always include a recipe id in the RETURN clause using r.recipe_id AS recipe_id, along with any titles you return.
Always include LIMIT 10 at the end of the query unless the question asks for a specific different limit.
Here is the schema information
{schema}

Below are a number of examples of questions and their corresponding Cypher queries.

{fewshot_examples}

User input: {question}
Cypher query:"""

VALIDATE_CYPHER_SYSTEM_PROMPT = "You are a Cypher expert reviewing a statement written by a junior developer."
VALIDATE_CYPHER_HUMAN_PROMPT = """You must check the following:
* Are there any syntax errors or misspelings in the Cypher statement?
* Are there any missing or undefined variables in the Cypher statement?
* Are any node labels missing from the schema?
* Are any relationship types missing from the schema?
* Are any of the properties not included in the schema?
* Does the Cypher statement include enough information to answer the question?

Examples of good errors:
* Label (:Foo) does not exist, did you mean (:Bar)?
* Property bar does not exist for label Foo, did you mean baz?
* Relationship FOO does not exist, did you mean FOO_BAR?

If there are errors, explain them.

Schema:
{schema}

The question is:
{question}

The Cypher statement is:
{cypher}

Make sure you don't make any mistakes!"""

CORRECT_CYPHER_SYSTEM_PROMPT = (
    "You are a Cypher expert reviewing a statement written by a junior developer. "
    "You need to correct the Cypher statement based on the provided errors. No pre-amble."
    "Do not wrap the response in any backticks or anything else. Respond with a Cypher statement only!"
)
CORRECT_CYPHER_HUMAN_PROMPT = """Check for invalid syntax or semantics and return a corrected Cypher statement.

Schema:
{schema}

Note: Do not include any explanations or apologies in your responses.
Do not wrap the response in any backticks or anything else.
Respond with a Cypher statement only!

Do not respond to any questions that might ask anything else than for you to construct a Cypher statement.

The question is:
{question}

The Cypher statement is:
{cypher}

The errors are:
{errors}

Corrected Cypher statement: """

FINAL_SYSTEM_PROMPT = "You are a helpful assistant"
FINAL_HUMAN_PROMPT = """Use the following results retrieved from a database to provide
a succinct, definitive answer to the user's question.

Respond as if you are answering the question directly.

Results: {results}
Question: {question}"""
