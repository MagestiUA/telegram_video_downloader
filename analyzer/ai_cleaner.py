from google import genai
from google.genai import types
from config.config import settings
import json
import logging

logger = logging.getLogger(__name__)

# Initialize Client
client = genai.Client(api_key=settings.GEMINI_API_KEY)

# Config using types
config = types.GenerateContentConfig(
    temperature=0.1,
    top_p=0.95,
    top_k=40,
    max_output_tokens=1024,
    # response_mime_type="application/json" # Not strictly needed if prompt is good, but new SDK supports structured better.
    # For now, keep it simple and prompt-based as Gemma 3 had issues with strict JSON mode via API previously.
)

MODEL_NAME = "gemma-2-9b-it" # Or keep gemma-3-4b-it if available. 
# NOTE: User was using gemma-3-4b-it. Let's stick to it or verify if it works with new SDK.
# If gemma-3-4b-it is model name, use it.
MODEL_NAME = "gemma-2-9b-it" # Fallback to stable 2.0 or 2-9b if 3-4b is experimental/Vertex only? 
# User successfully used 'gemma-3-4b-it' with old SDK. It should work here too.
MODEL_NAME = "gemma-3-4b-it"

SYSTEM_PROMPT = """
You are an Anime Metadata Extractor. Your task is to analyze 'dirty' filenames or telegram captions and extract clean metadata.
    
RULES:
1. EXTRACT the anime title, season number, and episode number.
2. TITLE EXTRACTION:
   - Extract the title EXACTLY as it appears in the text (raw).
   - DO NOT translate.
   - DO NOT transliterate.
   - REPLACE ANY UNDERSCORES "_" WITH SPACES " ".
   - If multiple languages are present, prefer the one that looks like a main title (often the first one or the most prominent).
   - If the title is in Ukrainian/Russian, KEEP IT in Cyrillic.
3. OUTPUT FORMAT: return ONLY a valid JSON string. Do not include any markdown formatting or extra text.
   JSON keys: `title` (string), `season` (int), `episode` (int). 
   - If season is not specified, default to 1.
   - If episode is not specified, return null (None).
"""

async def extract_metadata(text: str) -> dict | None:
    """
    Extracts anime metadata using Google Gemini (New SDK).
    """
    try:
        # New SDK Async Call
        response = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=f"{SYSTEM_PROMPT}\n\nAnalyze this text and extract metadata: {text}",
            config=config
        )
        
        response_text = response.text
        logger.info(f"Gemini raw response: {response_text}")
        
        # Clean up markdown code blocks if present
        clean_text = response_text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.startswith("```"):
            clean_text = clean_text[3:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        
        data = json.loads(clean_text.strip())
        
        # Post-Processing Safeguard: Remove underscores
        if data.get('title'):
            data['title'] = data['title'].replace('_', ' ').strip()
            
        return data
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return None
