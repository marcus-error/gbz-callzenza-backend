import re

def clean_metadata_headers(text: str) -> str:
    """Strips metadata headers, role annotations, markdown asterisks, and bracketed instruction text."""
    if not text:
        return ""
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'\([^\)]*\)', '', text)
    # Strip markdown formatting
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'[`#~]', '', text)
    text = re.sub(r'^(?:agent|bot|rep|assistant|அலெக்ஸ்|அலேக்ஸ்|[A-Za-z]{2,20})\s*:\s*', '', text, flags=re.I)
    return text.strip()


def clean_corrupt_tamil(text: str) -> str:
    """Corrects visual glyph extraction errors from legacy/corrupt Tamil PDF font encodings
    where vowel sign combining marks are extracted as 0, 1, dot, or slash characters."""
    if not text:
        return ""

    # Strip zero-width spacing and layout control characters that break string matches
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u00ad", "")

    # Specific replacements FIRST to prevent general rule visual collisions (e.g. .ல -> லெ inside அ.லைக்ஸ்)
    text = text.replace("அ.லக்ஸ்", "அலெக்ஸ்")
    text = text.replace("அ.லைக்ஸ்", "அலெக்ஸ்")
    text = text.replace("அலக்ஸ்", "அலெக்ஸ்")
    text = text.replace("அலைக்ஸ்", "அலெக்ஸ்")
    text = text.replace("கொ0லை", "காலை")
    text = text.replace("கொலை", "காலை")
    text = text.replace("சோலோர்", "சோலார்")
    text = text.replace("விடை", "விடை")

    # Full set of Tamil consonants to capture all combinations:
    consonants = "கஙசஞடணதநநபமயரலவழளறனஷஸஹக்ஷ"
    for c in consonants:
        text = text.replace(f"0{c}", f"{c}ை")
        text = text.replace(f"1{c}", f"{c}ை")

    # 2. Handle visual prefix '/' (commonly representing visual vowel signs like 'ோ' or 'ே')
    text = text.replace("/ஹோ", "ஹோ")
    text = text.replace("/ஹொ", "ஹொ")
    text = text.replace("/ஹ", "ஹோ")
    text = text.replace("/ச", "சே")
    text = text.replace("/வ", "வே")
    text = text.replace("/ப", "போ")
    text = text.replace("/க", "கோ")
    text = text.replace("/த", "தோ")
    text = text.replace("/ந", "நோ")
    text = text.replace("/ம", "மொ")
    text = text.replace("/ல", "லோ")
    text = text.replace("/ய", "யோ")
    text = text.replace("/ப", "பே")
    text = text.replace("/த", "தே")
    text = text.replace("/ஏ", "ஏ")
    text = text.replace("/கோ", "கோ")

    # 3. Handle visual prefix '.' (commonly representing visual vowel sign 'ெ')
    for c in consonants:
        text = text.replace(f".{c}", f"{c}ெ")

    # 4. Specific replacements for recurring legacy font glyph visual mismatches:
    text = text.replace("வினோ", "வினா")
    text = text.replace("வினொ", "வினா")
    text = text.replace("முன்பொ", "முன்பா")
    text = text.replace("வொழ்த்து", "வாழ்த்து")
    text = text.replace("வோழ்த்து", "வாழ்த்து")
    text = text.replace("இயைல்", "இயல்")
    text = text.replace("பொக", "பாக")
    text = text.replace("பயைன்", "பயன்")
    text = text.replace("பொன்ற", "போன்ற")
    text = text.replace("வொடிக்", "வாடிக்")
    text = text.replace("வோடி", "வாடி")
    text = text.replace("உரயோடைல்", "உரையாடல்")

    # Strip any stray visual slashes from the prefix of words
    text = re.sub(r'/([^\s])', r'\1', text)

    # 5. Dictionary-based spelling correction to fix OCR/glyph errors for natural text-to-speech pronunciation
    spelling_corrections = {
        "பெயைர்": "பெயர்",
        "நொன்": "நான்",
        "ஹொம்": "ஹோம்",
        "செேொலைொர்": "சோலார்",
        "சேேொலைொர்": "சோலார்",
        "அழைைக்கிறன்": "அழைக்கிறேன்",
        "அழைைக்கிறேன்": "அழைக்கிறேன்",
        "நீங் கள்": "நீங்கள்",
        "வொடிக்0கயைொளர்": "வாடிக்கையாளர்",
        "வாடிக்கைக்காரர்களுக்கு": "வாடிக்கையாளர்களுக்கு",
        "பதிலைளித்தொல்": "பதிலளித்தால்",
        "மகிழ்ச்சேி": "மகிழ்ச்சி",
        "சேரைிபொர்க்க": "சரிபார்க்க",
        "சரிபொர்க்க": "சரிபார்க்க",
        "இலைவசே": "இலவச",
        "இரைண்டு": "இரண்டு",
        "கு0றவொன": "குறைவான",
        "முடிந்துவிடும்": "முடிந்துவிடும்",
        "கவலைையைை": "கவலையை",
        "புரைிந்து": "புரிந்து",
        "புரைiந்து": "புரிந்து",
        "புரைiந்துகெொள்கிறன்": "புரிந்துகொள்கிறேன்",
        "புரிந்துகெொள்கிறன்": "புரிந்துகொள்கிறேன்",
        "பிரச்சனை": "பிரச்சனை",
        "பிரைச்சேனையும்": "பிரச்சனையும்",
        "அழைைப்பும்": "அழைப்பும்",
        "பகிர்ந்துகெொள்கிறொம்": "பகிர்ந்துகொள்கிறோம்",
        "தெரைிவிக்கிறொம்": "தெரிவிக்கிறோம்",
        "தெரைiவிக்கிறோம்": "தெரிவிக்கிறோம்",
        "தெரைiவிக்கிறொன்": "தெரிவிக்கிறேன்",
        "அறியை": "அறிய",
        "விரும்புகிறொம்": "விரும்புகிறோம்",
        "மின்சாரக்": "மின்சார",
        "மின்சார": "மின்சார",
        "மின்சோரைக்": "மின்சார",
        "மின்சோர": "மின்சார",
        "மின்சேொரைக்": "மின்சார",
        "சேரைியைொக": "சரியாக",
        "சரியைாக": "சரியாக",
        "நல்லைது": "நல்லது",
        "கொ0லை": "காலை",
        "செேொந்தக்கொரைர்களுக்கு": "சொந்தக்காரர்களுக்கு",
        "சூரைியை": "சூரிய",
        "சேக்தி": "சக்தி",
        "சூரியை": "சூரிய",
        "பனல்கள": "பேனல்களை",
        "பனல்க0ள": "பேனல்களை",
        "பேனல்களை": "பேனல்களை",
        "ஆ/லைொசே0ன": "ஆலோசனை",
        "ஆலோசனை": "ஆலோசனை",
        "விளம்பரை": "விளம்பர",
        "கட்டணத்0தக்": "கட்டணத்தைக்",
        "கட்டணத்தை": "கட்டணத்தை",
        "அதிகரைிக்கவும்": "அதிகரிக்கவும்",
        "அதிகரிக்கவும்": "அதிகரிக்கவும்",
        "சேிலை": "சில",
        "வி0ரைவொன": "விரைவான",
        "விடை": "விடை",
        "கேள்விகளை": "கேள்விகளை",
        "வினொ": "வினா",
        "வினோ": "வினா",
        "வினொக்கள்": "வினாக்கள்",
        "வினோக்கள்": "வினாக்கள்",
        "ஆமொம்": "ஆமாம்",
        "நியைொயைமொன": "நியாயமான",
        "கவலைைதொன்": "கவலைதான்",
        "கவலைைதொன": "கவலைதான்"
    }

    for word_err, word_corr in spelling_corrections.items():
        text = text.replace(word_err, word_corr)

    return text


def extract_single_dialogue_line(text: str, is_greeting: bool = False) -> str:
    """Removes script headers, rules, option annotations and returns the first single spoken sentence."""
    if not text:
        return ""

    # Strip annotations in brackets or parenthesis first
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'\([^\)]*\)', '', text)

    # Filter out header/rules lines based on keywords
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    cleaned_lines = []
    
    # Common metadata keywords to filter out entire lines:
    header_keywords = [
        "ஆட்சேபனை", "ஆட்சேப0ன", "வழிகாட்டல்", "வழிகாட்டி",
        "கிரிப்ட்", "திறவுகோல்", "விதிமுறை", "விதிமுறைகள்", "அலெக்ஸ் ஆர்வம்", "அலேக்ஸ் ஆர்வம்",
        "பதிப்பு", "உரையாடல்", "சோலார் எக்ஸ்பர்ட்ஸ்", "பதில்", "முடிவு",
        "நிபந்தனை", "நிபந்த0ன", "நிறவு", "தகுதி"
    ]
    
    for line in lines:
        # 1. Skip if it matches visual heading pattern /க1, /க2, etc. or /k1, /k2, including Ko variations (e.g. கோ4)
        if re.match(r'^/?(?:க|கோ)\d+', line) or re.match(r'^/?(?:k|ko)\d+', line, re.I):
            continue
        
        # 2. Skip if is an instruction block ending with a colon
        if (line.endswith(":") or line.endswith("：")) and not re.match(r'^(?:agent|bot|assistant|[A-Za-z]{2,20})\s*:', line, re.I):
            continue
            
        # 3. Skip based on global metadata keywords only if the line is short (header) and doesn't look like spoken dialogue
        if len(line) < 80 and any(keyword in line for keyword in header_keywords) and not (line.endswith(".") or line.endswith("!") or line.endswith("?") or line.endswith("சரியா?")):
            continue
            
        cleaned_lines.append(line)
        
    text = " ".join(cleaned_lines).strip()

    # Prioritize slicing greeting text chronologically to remove rules descriptions
    if is_greeting:
        greeting_keywords = ["காலை வணக்கம்", "கொலை வணக்கம்", "கொ0லை வணக்கம்", "வணக்கம்"]
        for keyword in greeting_keywords:
            if keyword in text:
                text = text[text.find(keyword):]
                break
        return text
    
    # Remove any stray role prefixes
    text = re.sub(r'^(?:customer|prospect|user|client|agent|script agent|bot|rep|assistant|அலேக்ஸ்|அலெக்ஸ்|[A-Za-z]{2,20}(?:\s+[A-Za-z]{2,20})?)\s*:\s*', '', text, flags=re.IGNORECASE)
    
    # Split by sentence markers (. ! ? or tamil equivalents) to take only the first sentence
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if sentences:
        first_sentence = sentences[0].strip()
        # If it's too short (1-2 words), combine it with the next sentence if available
        if len(first_sentence.split()) < 3 and len(sentences) > 1:
            return first_sentence + " " + sentences[1].strip()
        return first_sentence

    return text


def extract_spoken_dialogue(text: str, is_greeting: bool = False) -> str:
    """Extracts spoken lines from role-based structured chunks (e.g. Agent: / AI: / <AgentName>:)
    and chops off any trailing customer responses or guidelines."""
    if not text:
        return ""

    text = clean_corrupt_tamil(text)

    # 1. If explicit role marker is found
    m = re.search(r'(?i)(?:அலேக்ஸ்|அலெக்ஸ்|அ\.லைக்ஸ்|அ\.லக்ஸ்|Agent|Greeting|வாழ்த்து|துவக்கம்|[A-Za-z]{2,20}(?:\s+Voice)?)\s*:\s*(.*)', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # 2. Chop off any trailing customer lines, replies, or guidelines (with or without colon)
    # Using \b word boundary for English terms, but literal matching for Tamil/Unicode terms
    split_pattern = r'(?i)\b(?:customer|prospect|user|client)\b|(?:வாடிக்கையாளர்|வாடிக்கையைொளர்|வாடிக்கைக்காரர்|பதிலளித்தால்)'
    text = re.split(split_pattern, text)[0].strip()
    text = re.sub(split_pattern + r'.*$', '', text, flags=re.DOTALL).strip()

    # 3. Extract first single dialogue sentence and strip rules/headers
    text = re.sub(r'(?i)(?:அலெக்ஸ்|அலேக்ஸ்|agent|assistant)\s*:\s*', '', text)  # Double safety sweep
    text = extract_single_dialogue_line(text, is_greeting=is_greeting)
    return text

