import dspy
from dotenv import load_dotenv
import os

load_dotenv()
openai_key = os.getenv("OPENAI_API_KEY")

lm_openai = dspy.LM('openai/gpt-4o-mini', api_key=openai_key)

dspy.configure(lm=lm_openai)