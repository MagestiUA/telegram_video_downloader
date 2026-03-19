from google import genai
from google.genai import types
from config.config import settings
import json
import logging
import asyncio
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Обмежувач швидкості запитів (Rate Limiter) з використанням алгоритму Token Bucket.
    Гарантує, що кількість запитів не перевищує заданий ліміт за певний проміжок часу.
    """
    
    def __init__(self, max_requests: int = 10, time_window: int = 60):
        """
        Ініціалізація rate limiter.
        
        Args:
            max_requests: Максимальна кількість запитів
            time_window: Часове вікно в секундах
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []  # Список міток часу запитів
        self.lock = asyncio.Lock()
        
    async def acquire(self):
        """
        Отримати дозвіл на виконання запиту.
        Чекає, якщо ліміт вичерпано.
        """
        async with self.lock:
            current_time = time.time()
            
            # Видаляємо старі запити (поза часовим вікном)
            self.requests = [req_time for req_time in self.requests 
                           if current_time - req_time < self.time_window]
            
            # Перевіряємо, чи є місце для нового запиту
            if len(self.requests) >= self.max_requests:
                # Потрібно почекати
                oldest_request = self.requests[0]
                wait_time = self.time_window - (current_time - oldest_request)
                
                if wait_time > 0:
                    logger.info(
                        f"Rate limit досягнуто ({len(self.requests)}/{self.max_requests}). "
                        f"Очікування {wait_time:.2f} секунд перед викликом API..."
                    )
                    await asyncio.sleep(wait_time)
                    
                    # Після очікування - оновлюємо список
                    current_time = time.time()
                    self.requests = [req_time for req_time in self.requests 
                                   if current_time - req_time < self.time_window]
            
            # Реєструємо новий запит
            self.requests.append(time.time())
            logger.debug(f"API запит дозволено. Поточна кількість: {len(self.requests)}/{self.max_requests}")


# Глобальний інстанс rate limiter (10 запитів на хвилину)
rate_limiter = RateLimiter(max_requests=10, time_window=60)

# Initialize Client
client = genai.Client(api_key=settings.GEMINI_API_KEY)

# Config using types
config = types.GenerateContentConfig(
    temperature=0.1,
    top_p=0.95,
    top_k=40,
    max_output_tokens=1024,
)

MODEL_NAME = "gemma-3-27b-it"

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

3. EPISODE NUMBER DETECTION:
   - Any patterns similar to:
     "01 серія", "1 серія", "серія 1", "серія_03", "серія03", "серія-03"
     MUST be interpreted as the EPISODE number (int).
   - Such patterns MUST NOT be interpreted as a season number under any circumstances.
   - Leading zeros (e.g. "01", "03") should be converted to integers (1, 3).

4. SEASON NUMBER RULES:
   - Only treat a number as a season if it is explicitly indicated by words like:
     "season", "сезон", "s", "season 2", "2 сезон".
   - If season is not explicitly specified, default season to 1 (int), not null.

5. OUTPUT FORMAT:
    return ONLY a valid JSON string. Do not include any markdown formatting or extra text.
    JSON keys: `title` (string), `season` (int), `episode` (int).
    - If season is not specified, default to 1.
    - If no episode number is explicitly detected,
      BUT there are digits immediately preceding the extracted title,
      THEN interpret those digits as the EPISODE number.
    - Such digits MUST NOT be interpreted as a season number.
    - Leading zeros must be ignored (e.g. "01" → 1).
    
    - Digits appearing immediately AFTER the extracted title
      MAY be interpreted as the SEASON number.
    - Such digits MUST NOT be interpreted as an episode number.
    - Only assign a season number if no explicit episode indicator is present.
"""

EPISODE_SYSTEM_PROMPT = """
You are an episode number extractor.
You will be given the anime title, season number, and a text (filename or caption).
Extract ONLY the episode number from the text.
Return ONLY valid JSON: {"episode": <integer>}
If no episode number found, return {"episode": null}.
No markdown, no extra text.
"""

async def extract_episode(text: str, title: str, season: int) -> int | None:
    """
    Extracts only the episode number given known title and season.
    Used in Batch mode.
    """
    try:
        await rate_limiter.acquire()
        contents = (
            f"{EPISODE_SYSTEM_PROMPT}\n\n"
            f"Anime: {title}\nSeason: {season}\nText: {text}"
        )
        response = await client.aio.models.generate_content(
            model=MODEL_NAME, contents=contents, config=config
        )
        clean = response.text.strip()
        if clean.startswith("```json"):
            clean = clean[7:]
        if clean.startswith("```"):
            clean = clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        data = json.loads(clean.strip())
        ep = data.get("episode")
        return int(ep) if ep is not None else None
    except Exception as e:
        logger.error(f"Error extracting episode: {e}")
        return None


async def extract_metadata(text: str) -> dict | None:
    """
    Extracts anime metadata using Google Gemini (New SDK).
    """
    try:
        # Застосовуємо rate limiting перед викликом API
        await rate_limiter.acquire()
        
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
