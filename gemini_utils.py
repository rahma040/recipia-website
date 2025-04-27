import hashlib
import mysql.connector
import google.generativeai as genai
import re
from dotenv import load_dotenv
import os
import logging # Import logging

load_dotenv()

# --- Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

# Configure Gemini only if the key exists
if GEMINI_API_KEY:
    try:
        genai.configure(
            api_key=GEMINI_API_KEY,
            transport="rest", # Recommend keeping REST for Flask compatibility
        )
        logging.info("Gemini configured successfully.")
    except Exception as e:
        logging.error(f"Failed to configure Gemini: {e}")
        GEMINI_API_KEY = None # Disable if configuration fails
else:
    logging.warning("GEMINI_API_KEY not found in environment variables.")

# --- Database Connection ---
def get_db():
    """Establishes a new database connection."""
    try:
        # Ensure required DB environment variables are set
        if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
             logging.error("Database configuration environment variables missing.")
             return None
        db = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            connect_timeout=5 # Add a connection timeout
        )
        return db
    except mysql.connector.Error as err:
        logging.error(f"Database connection error: {err}")
        return None # Return None if connection fails

# --- Hashing Functions ---
def get_list_query_hash(ingredients, diet, exclude, intolerances, strict_mode):
    """Generates hash for the initial list of recipe ideas query."""
    ingredients_norm = str(ingredients or '').lower().strip()
    diet_norm = str(diet or '').lower().strip()
    exclude_norm = str(exclude or '').lower().strip()
    intolerances_norm = str(intolerances or '').lower().strip()
    input_str = f"LIST|{ingredients_norm}|{diet_norm}|{exclude_norm}|{intolerances_norm}|{strict_mode}"
    return hashlib.sha256(input_str.encode('utf-8')).hexdigest()

def get_detail_query_hash(idea_name, ingredients, diet, exclude, intolerances, strict_mode):
    """Generates hash for the detailed recipe query, including the idea name."""
    idea_name_norm = str(idea_name or '').lower().strip()
    ingredients_norm = str(ingredients or '').lower().strip()
    diet_norm = str(diet or '').lower().strip()
    exclude_norm = str(exclude or '').lower().strip()
    intolerances_norm = str(intolerances or '').lower().strip()
    input_str = f"DETAIL|{idea_name_norm}|{ingredients_norm}|{diet_norm}|{exclude_norm}|{intolerances_norm}|{strict_mode}"
    return hashlib.sha256(input_str.encode('utf-8')).hexdigest()


# --- Caching Functions ---
def get_cached_response(query_hash):
    """Fetches cached response text from DB based on query hash."""
    db = get_db()
    result = None
    if not db: return None
    cursor = None
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT generated_text FROM cached_recipes WHERE query_hash = %s", (query_hash,))
        result_row = cursor.fetchone()
        result = result_row['generated_text'] if result_row else None
    except mysql.connector.Error as err:
        logging.error(f"Database error getting cache (hash: {query_hash}): {err}")
        result = None
    finally:
        if cursor: cursor.close()
        if db and db.is_connected(): db.close()
    return result

def cache_response(query_hash, generated_text):
    """Stores generated text response in DB cache."""
    db = get_db()
    if not db: return
    cursor = None
    try:
        cursor = db.cursor()
        sql = """
            INSERT INTO cached_recipes (query_hash, generated_text)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE generated_text = VALUES(generated_text), created_at = CURRENT_TIMESTAMP
        """
        cursor.execute(sql, (query_hash, generated_text))
        db.commit()
        logging.info(f"Cached response for hash: {query_hash[:8]}...")
    except mysql.connector.Error as err:
        logging.error(f"Database error caching response (hash: {query_hash}): {err}")
        try:
            db.rollback()
        except mysql.connector.Error as rb_err:
            logging.error(f"Database error during rollback: {rb_err}")
    finally:
        if cursor: cursor.close()
        if db and db.is_connected(): db.close()


# --- Parsing Functions ---

def parse_recipe_ideas(text):
    """Parses Gemini response for a list of recipe ideas."""
    ideas = []
    # Use greedy (+) match for name
    pattern = re.compile(r"^\s*(?:[\d\.\-\*]+)?\s*([^-\n]+)(?:\s*(?:-?–?-?)\s*(.*))?$", re.MULTILINE)
    matches = pattern.findall(text)

    if not matches:
        lines = text.strip().split('\n')
        if len(lines) > 1:
             for line in lines:
                 line = line.strip()
                 if line and not re.match(r'^(here are|recipe ideas:|certainly|sure|okay|great!)', line, re.IGNORECASE):
                     parts = re.split(r'\s+-\s+|\s+–\s+|\s+--\s+', line, 1)
                     name = parts[0].strip(' *-.\t')
                     description = parts[1].strip() if len(parts) > 1 else "..."
                     if name: ideas.append({"name": name, "description": description})
        else:
             logging.warning(f"Could not parse recipe ideas using regex or fallback. Raw text: {text[:200]}...")
             return {"error": "Could not parse recipe ideas from the response.", "raw_text": text}
    else:
        for match in matches:
            name = match[0].strip()
            description = match[1].strip() if len(match) > 1 and match[1] else "..."
            if name: ideas.append({"name": name, "description": description})

    if not ideas:
         logging.warning(f"No valid ideas found after parsing. Raw text: {text[:200]}...")
         return {"error": "No valid recipe ideas found in the response.", "raw_text": text}

    logging.info(f"Successfully parsed {len(ideas)} recipe ideas.")
    return {"ideas": ideas}

def parse_recipe(text):
    """Parses Gemini response for detailed recipe information."""
    try:
        name_match = re.search(r'Recipe Name\s*:\s*(.+)', text, re.IGNORECASE)
        servings_match = re.search(r'Servings\s*:\s*(\d+)', text, re.IGNORECASE)
        ingredients_header_match = re.search(r'Ingredients\s*:', text, re.IGNORECASE)
        instructions_header_match = re.search(r'Instructions\s*:', text, re.IGNORECASE)
        ingredients_text = ""
        instructions_text = ""

        if ingredients_header_match and instructions_header_match:
            if instructions_header_match.start() > ingredients_header_match.end():
                ingredients_text = text[ingredients_header_match.end() : instructions_header_match.start()].strip()
                instructions_text = text[instructions_header_match.end():].strip()
            else:
                 logging.warning("Instructions header found before Ingredients header.")
                 ingredients_text = text[ingredients_header_match.end():].strip(); instructions_text = ""
        elif ingredients_header_match: ingredients_text = text[ingredients_header_match.end():].strip(); logging.warning("Instructions header missing.")
        elif instructions_header_match: instructions_text = text[instructions_header_match.end():].strip(); logging.warning("Ingredients header missing.")
        else: logging.warning("Ingredients/Instructions headers missing.")

        ingredients_list = [line.strip('-* \t') for line in ingredients_text.split('\n') if line.strip('-* \t')]
        if not ingredients_list and ingredients_text: ingredients_list = [ingredients_text]

        instructions_list = [match.group(1).strip() for match in re.finditer(r'^\s*(?:\d+\.?\s*)?(.+)$', instructions_text, re.MULTILINE) if match.group(1).strip()]
        if not instructions_list and instructions_text: instructions_list = [instructions_text]

        recipe_name = name_match.group(1).strip() if name_match else "Untitled Recipe"
        recipe_servings = servings_match.group(1).strip() if servings_match else "N/A"

        if recipe_name == "Untitled Recipe" and not ingredients_list and not instructions_list:
            raise ValueError("Failed to extract any meaningful recipe parts.")

        return { "name": recipe_name, "servings": recipe_servings, "ingredients": ingredients_list, "instructions": instructions_list }
    except Exception as e:
        logging.error(f"Error parsing detailed recipe: {e}\nRaw Text Start:\n{text[:500]}...")
        return {"error": f"Parsing error: Could not structure the recipe details ({type(e).__name__})."}


# --- Main Generation Functions ---

def generate_recipe_ideas(ingredients, diet, exclude, intolerances, strict_mode):
    """Generates a list of recipe ideas using Gemini."""
    if not GEMINI_API_KEY: return {"error": "Gemini API key not configured."}
    query_hash = get_list_query_hash(ingredients, diet, exclude, intolerances, strict_mode)
    cached_response = get_cached_response(query_hash)
    if cached_response is not None:
        logging.info(f"Using cached recipe idea list for hash: {query_hash[:8]}...")
        return parse_recipe_ideas(cached_response)

    logging.info(f"Generating NEW recipe idea list for hash: {query_hash[:8]}...")

    # === PROMPT FOR IDEAS ===
    strict_instruction = ""
    if strict_mode:
        strict_instruction = f"CRITICAL INSTRUCTION: You MUST generate ideas that ONLY use ingredients from the 'MUST USE INGREDIENTS' list: [{ingredients}]. DO NOT include any recipe ideas that require additional ingredients (spices, oils, liquids etc. are only allowed if listed in 'MUST USE')."
    else:
        strict_instruction = "You may suggest recipe ideas that include complementary ingredients beyond the 'MUST USE' list."

    prompt = f"""
    Generate exactly 5 distinct recipe ideas based on the following criteria.
    For each idea, provide a concise name and a very short (1 sentence) description.

    Constraints:
    - MUST USE INGREDIENTS: {ingredients}
    - DIET TYPE: {diet or 'Any'}
    - EXCLUDE INGREDIENTS: {exclude or 'None'}
    - ALLERGIES/INTOLERANCES: {intolerances or 'None'}

    Strict Mode Rule:
    {strict_instruction}

    Output Format (ONLY this numbered list, no intro/outro text):
    1. Recipe Name 1 - Short description.
    2. Recipe Name 2 - Short description.
    3. Recipe Name 3 - Short description.
    4. Recipe Name 4 - Short description.
    5. Recipe Name 5 - Short description.
    """
    # === END MODIFIED PROMPT ===

    try:
        model = genai.GenerativeModel("gemini-1.5-pro-latest")
        response = model.generate_content(prompt)
        if hasattr(response, 'text') and response.text:
             generated_text = response.text.strip()
        else:
             block_reason = getattr(response, 'prompt_feedback', {}).get('block_reason', 'Unknown')
             logging.error(f"Gemini response empty or blocked for recipe ideas. Reason: {block_reason}")
             return {"error": f"Failed to generate recipe ideas (Blocked or Empty Response - Reason: {block_reason})."}
        logging.debug(f"--- RAW GEMINI IDEAS TEXT START ---\n{generated_text}\n--- RAW GEMINI IDEAS TEXT END ---")
        cache_response(query_hash, generated_text)
        return parse_recipe_ideas(generated_text)
    except Exception as e:
        logging.error(f"Gemini API call/processing failed for recipe ideas: {e}", exc_info=True)
        return {"error": f"Failed to generate recipe ideas due to an unexpected error ({type(e).__name__})."}


def get_or_generate_detailed_recipe(idea_name, ingredients, diet, exclude, intolerances, strict_mode):
    """Gets a detailed recipe for a specific idea name, using cache or generating if needed."""
    if not GEMINI_API_KEY: return {"error": "Gemini API key not configured."}
    query_hash = get_detail_query_hash(idea_name, ingredients, diet, exclude, intolerances, strict_mode)
    cached_response = get_cached_response(query_hash)
    if cached_response is not None:
        logging.info(f"Using cached detailed recipe for '{idea_name}' (hash: {query_hash[:8]}...)")
        return parse_recipe(cached_response)

    logging.info(f"Generating NEW detailed recipe for '{idea_name}' (hash: {query_hash[:8]}...)")

    # === PROMPT FOR DETAIL ===
    strict_instruction = ""
    if strict_mode:
        strict_instruction = f"CRITICAL REQUIREMENT: The 'Ingredients' list for this recipe MUST ONLY contain items derived from the 'Original MUST USE INGREDIENTS' list: [{ingredients}]. DO NOT add any other food items, spices, oils, liquids, garnishes, etc., unless they were explicitly listed in the original 'MUST USE' ingredients. Adherence to this rule is mandatory."
    else:
        strict_instruction = "You may include complementary ingredients beyond the original 'MUST USE' list if appropriate for the recipe."

    prompt = f"""
    Generate a full recipe based on the Recipe Idea Name and the original constraints provided.
    Follow the EXACT output format specified below. Adhere strictly to all constraints.

    RECIPE IDEA NAME: {idea_name}

    Original Constraints:
    - MUST USE INGREDIENTS: {ingredients}
    - DIET TYPE: {diet or 'Any'}
    - EXCLUDE INGREDIENTS: {exclude or 'None'}
    - ALLERGIES/INTOLERANCES: {intolerances or 'None'}

    Strict Mode Rule:
    {strict_instruction}

    Output Format (Use EXACTLY this structure, no extra text):

    Recipe Name: {idea_name}
    Servings: <number, e.g., 4>
    Ingredients:
    - <quantity> <ingredient name>
    - <quantity> <ingredient name>
    ...
    Instructions:
    1. <First step>
    2. <Second step>
    ...
    """
    # === END PROMPT ===

    try:
        model = genai.GenerativeModel("gemini-1.5-pro-latest")
        response = model.generate_content(prompt)
        if hasattr(response, 'text') and response.text:
             generated_text = response.text.strip()
        else:
             block_reason = getattr(response, 'prompt_feedback', {}).get('block_reason', 'Unknown')
             logging.error(f"Gemini response empty or blocked for detailed recipe '{idea_name}'. Reason: {block_reason}")
             return {"error": f"Failed to generate recipe details (Blocked or Empty Response - Reason: {block_reason})."}
        logging.debug(f"--- RAW GEMINI DETAIL TEXT START ---\n{generated_text}\n--- RAW GEMINI DETAIL TEXT END ---")
        cache_response(query_hash, generated_text)
        return parse_recipe(generated_text)
    except Exception as e:
        logging.error(f"Gemini API call/processing failed for detailed recipe '{idea_name}': {e}", exc_info=True)
        return {"error": f"Failed to generate detailed recipe for '{idea_name}' due to an unexpected error ({type(e).__name__})."}

# --- Deprecated/Original function (Optional: Can be removed) ---
# def _generate_single_detailed_recipe(ingredients, diet, exclude, intolerances, strict_mode):
#     """Original function, now likely replaced by the two-step process."""
#     pass