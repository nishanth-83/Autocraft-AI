"""
Multilingual Natural Language Understanding (English, Tamil, and Thanglish)
for AUTOCRAFT AI's offline deterministic self-planning engine.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Unicode block range for Tamil script: U+0B80..U+0BFF
TAMIL_CHAR_REGEX = re.compile(r"[\u0B80-\u0BFF]")

# Common Thanglish lexical markers
THANGLISH_MARKERS = re.compile(
    r"\b(pannu|punga|pannunga|panuga|open\s+panni|create\s+panni|type\s+pannu|"
    r"eluthu|ezhuthu|thirakkavum|thira|puthu|pudhu|kootu|kalikavum|alikkavum|"
    r"azhikkavum|delete\s+pannu|thedunga|thedu|apram|adhukapram|athuku\s+apram|"
    r"pannitu|panni|ullae|la\s+poi)\b",
    re.IGNORECASE,
)

# Tamil -> canonical English token replacements for deterministic rule matching
TAMIL_LEXICON_MAP: List[Tuple[str, str]] = [
    ("நோட்பேட்டில்", "in notepad"),
    ("நோட்பேடில்", "in notepad"),
    ("நோட்பேட்", "notepad"),
    ("கால்குலேட்டரில்", "in calculator"),
    ("கால்குலேட்டர்", "calculator"),
    ("பெயிண்ட்", "paint"),
    ("ஃபைல் எக்ஸ்ப்ளோரர்", "file explorer"),
    ("கோப்பு உலாவி", "file explorer"),
    ("விஎஸ் கோட்", "vscode"),
    ("பிரவுசர்", "browser"),
    ("உலாவி", "browser"),
    ("குரோம்", "chrome"),
    ("எட்ஜ்", "edge"),
    ("புதிய கோப்புறை", "new folder"),
    ("கோப்புறை உருவாக்கு", "create folder"),
    ("புதிய கோப்பு", "new file"),
    ("கோப்பு உருவாக்கு", "create file"),
    ("கோப்பை படி", "read file"),
    ("படி", "read"),
    ("கோப்பை நீக்கு", "delete file"),
    ("நீக்கு", "delete"),
    ("அழி", "delete"),
    ("பெயர் மாற்று", "rename"),
    ("நகர்த்து", "move"),
    ("நகலெடு", "copy"),
    ("ஒழுங்குபடுத்து", "organize folder"),
    ("பட்டியலிடு", "list directory"),
    ("திறந்து", "open and"),
    ("திறக்கவும்", "open"),
    ("திற", "open"),
    ("என்று எழுது", "write"),
    ("எழுது", "write"),
    ("டைப் செய்", "type"),
    ("கணக்கிடு", "calculate"),
    ("கூட்டு", "add"),
    ("கழி", "subtract"),
    ("பெருக்கு", "multiply"),
    ("வகு", "divide"),
    ("தேடு", "search"),
    ("கிளிக் செய்", "click"),
    ("ஸ்க்ரோல் செய்", "scroll"),
    ("கீழே உருட்டு", "scroll down"),
    ("மேலே உருட்டு", "scroll up"),
]

# Thanglish -> canonical English phrase normalizations
THANGLISH_NORMALIZATIONS: List[Tuple[re.Pattern[str], str]] = [
    # "notepad open panni hello world nu type pannu"
    (
        re.compile(
            r"notepad\s+open\s+panni\s+['\"]?(.+?)['\"]?\s+(?:nu|endru)\s+(?:type|write|eluthu|ezhuthu)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r'open notepad and type "\1"',
    ),
    # "notepad la <text> nu type pannu"
    (
        re.compile(
            r"notepad\s+(?:la|illa)\s+['\"]?(.+?)['\"]?\s+(?:nu|endru)?\s*(?:type|write|eluthu|ezhuthu)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r'open notepad and type "\1"',
    ),
    # "calculator open panni 25 + 75 calculate pannu"
    (
        re.compile(
            r"calculator\s+(?:open\s+panni\s+|la\s+)?([0-9+\-*/().\s]+)\s*(?:calculate|kootu|compute|portu)?\s*pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"calculate \1 in calculator",
    ),
    # "paint open pannu"
    (
        re.compile(r"paint\s+open\s+pannu(?:nga)?", re.IGNORECASE),
        "open paint",
    ),
    # "file explorer la <path> open pannu"
    (
        re.compile(r"(?:file\s+)?explorer\s+(?:la\s+)?([^\s]+)\s+open\s+pannu(?:nga)?", re.IGNORECASE),
        r"open file explorer at \1",
    ),
    # "puthu folder <name> create pannu" or "<name> folder create pannu"
    (
        re.compile(
            r"(?:puthu|pudhu|new)?\s*folder\s+([A-Za-z0-9_\-./]+)\s+(?:create|uruvaaku)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"create folder \1",
    ),
    (
        re.compile(
            r"([A-Za-z0-9_\-./]+)\s+(?:nu\s+)?(?:puthu\s+|pudhu\s+)?folder\s+(?:create|open|uruvaaku)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"create folder \1",
    ),
    # "<filename> file la <content> nu write pannu"
    (
        re.compile(
            r"([A-Za-z0-9_\-./]+\.[A-Za-z0-9]+)\s+(?:file\s+)?(?:la\s+)?['\"]?(.+?)['\"]?\s+(?:nu\s+)?(?:write|save|create|eluthu)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r'create file \1 with content "\2"',
    ),
    # "file <filename> delete pannu" or "<filename> delete pannu"
    (
        re.compile(
            r"(?:file\s+|folder\s+)?([A-Za-z0-9_\-./]+)\s+(?:file\s+)?(?:delete|remove|ali|azhi)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"delete file \1",
    ),
    # "<filename> file read pannu"
    (
        re.compile(
            r"([A-Za-z0-9_\-./]+)\s+(?:file\s+)?(?:read|padi)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"read file \1",
    ),
    # "browser la <url> open pannu"
    (
        re.compile(
            r"(?:browser|chrome|edge)\s+(?:la\s+|open\s+panni\s+)?(https?://[^\s]+|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}[^\s]*)\s+open\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"open browser to \1",
    ),
    # "browser la <query> nu search pannu"
    (
        re.compile(
            r"(?:browser|google|bing)\s+la\s+['\"]?(.+?)['\"]?\s+(?:nu\s+)?(?:search|thedu)\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r'search browser for "\1"',
    ),
    # "<folder> organize pannu"
    (
        re.compile(
            r"(?:folder\s+)?([A-Za-z0-9_\-./]+)\s+organize\s+pannu(?:nga)?",
            re.IGNORECASE,
        ),
        r"organize folder \1",
    ),
]


def detect_language(text: str) -> str:
    """
    Detect whether the instruction is written in Tamil script ('ta'),
    Thanglish ('thanglish'), or English ('en').
    """
    if not text or not text.strip():
        return "en"
    if TAMIL_CHAR_REGEX.search(text):
        return "ta"
    if THANGLISH_MARKERS.search(text):
        return "thanglish"
    return "en"


def split_compound_clauses(goal: str) -> List[str]:
    """
    Split a multi-step natural language instruction into ordered atomic clauses,
    preserving quoted strings and keeping tightly coupled desktop actions
    (like 'open notepad and type hello' or 'open browser and go to a URL') together.
    """
    if not goal or not goal.strip():
        return []

    quotes: List[str] = []

    def _mask(match: re.Match[str]) -> str:
        quotes.append(match.group(0))
        return f"__QUOTE_{len(quotes) - 1}__"

    masked = re.sub(r"(['\"])(.*?)\1", _mask, goal.strip())

    step_connectors = [
        r"\s+and\s+then\s+",
        r"\s+then\s+",
        r"\s+after\s+that\s+",
        r"\s+afterwards\s+",
        r"\s+next\s+",
        r"\s+மற்றும்\s+பிறகு\s+",
        r"\s+பிறகு\s+",
        r"\s+அப்புறம்\s+",
        r"\s+அதன்\s+பிறகு\s+",
        r"\s+athuku\s+apram\s+",
        r"\s+adhukapram\s+",
        r"\s+apram\s+",
        r"\s+pannitu\s+",
    ]
    for conn in step_connectors:
        masked = re.sub(conn, " ; ", masked, flags=re.IGNORECASE)

    action_verbs = (
        r"(create|make|write|read|delete|remove|rename|move|copy|organize|list|"
        r"open|launch|calculate|compute|navigate|visit|browse|search|click|scroll|fill)"
    )
    masked = re.sub(
        rf"\s+and\s+(?={action_verbs}\b)",
        " ; ",
        masked,
        flags=re.IGNORECASE,
    )

    masked = re.sub(
        r"(open|launch)\s+notepad\s*;\s*(write|type)\b",
        r"\1 notepad and \2",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"(open|launch)\s+calculator\s*;\s*(calculate|compute)\b",
        r"\1 calculator and \2",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"(open|launch)\s+paint\s*;\s*(draw|save)\b",
        r"\1 paint and \2",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"(open|launch)\s+(?:(?:google|microsoft)\s+)?(browser|chrome|edge)\s*;\s*(open|visit|navigate|go\s+to)\b",
        r"\1 \2 and \3",
        masked,
        flags=re.IGNORECASE,
    )

    raw_parts = [p.strip() for p in masked.split(";") if p.strip()]
    restored_parts: List[str] = []
    for part in raw_parts:
        for idx, q in enumerate(quotes):
            part = part.replace(f"__QUOTE_{idx}__", q)
        restored_parts.append(part.strip())

    return restored_parts


def normalize_clause_to_english(clause: str) -> str:
    """
    Convert Tamil or Thanglish clauses into canonical English forms for deterministic rule matching,
    while preserving filenames, paths, numbers, and quoted literals.
    """
    text = clause.strip()

    if TAMIL_CHAR_REGEX.search(text):
        # Pattern: நோட்பேட் திறந்து "<text>" என்று எழுது
        m_np_quoted = re.search(
            r"(?:நோட்பேட்டில்|நோட்பேடில்|நோட்பேட்).*?['\"](.+?)['\"].*?(?:எழுது|டைப்)",
            text,
        )
        if m_np_quoted:
            return f'open notepad and type "{m_np_quoted.group(1).strip()}"'

        m_np_unquoted = re.search(
            r"(?:நோட்பேட்டில்|நோட்பேடில்|நோட்பேட்)\s*(?:திறந்து)?\s*(.+?)\s*(?:என்று\s+)?(?:எழுது|டைப்)",
            text,
        )
        if m_np_unquoted:
            return f'open notepad and type "{m_np_unquoted.group(1).strip()}"'

        # Pattern: கால்குலேட்டரில் 25 மற்றும் 75 கூட்டு
        m_calc_add = re.search(
            r"கால்குலேட்ட.*?(\d+(?:\.\d+)?)\s*(?:மற்றும்|\+)\s*(\d+(?:\.\d+)?)\s*(?:கூட்டு|கணக்கிடு)",
            text,
        )
        if m_calc_add:
            return f"calculate {m_calc_add.group(1)} + {m_calc_add.group(2)} in calculator"

        m_calc_expr = re.search(
            r"கால்குலேட்ட.*?([0-9+\-*/().\s]+)",
            text,
        )
        if m_calc_expr and any(op in m_calc_expr.group(1) for op in ("+", "-", "*", "/")):
            return f"calculate {m_calc_expr.group(1).strip()} in calculator"

        if "பெயிண்ட்" in text and ("திற" in text or "open" in text.lower()):
            return "open paint"

        m_folder = re.search(
            r"(?:புதிய\s+)?கோப்புறை\s+([A-Za-z0-9_\-./]+)\s*உருவாக்கு|([A-Za-z0-9_\-./]+)\s+கோப்புறை\s+உருவாக்கு",
            text,
        )
        if m_folder:
            folder_name = m_folder.group(1) or m_folder.group(2)
            return f"create folder {folder_name}"

        m_file = re.search(
            r"([A-Za-z0-9_\-./]+\.[A-Za-z0-9]+)\s*(?:கோப்பில்|கோப்பு)\s*['\"](.+?)['\"]\s*(?:என்று\s+)?(?:எழுது|உருவாக்கு)",
            text,
        )
        if m_file:
            return f'create file {m_file.group(1)} with content "{m_file.group(2)}"'

        m_del = re.search(
            r"([A-Za-z0-9_\-./]+)\s*(?:கோப்பை|கோப்பு)?\s*(?:நீக்கு|அழி)",
            text,
        )
        if m_del:
            return f"delete file {m_del.group(1)}"

        m_url = re.search(
            r"(https?://[^\s]+|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}[^\s]*)",
            text,
        )
        if m_url and any(w in text for w in ("உலாவி", "குரோம்", "பிரவுசர்", "செல்", "திற")):
            return f"open browser to {m_url.group(1)}"

        for ta_word, en_word in TAMIL_LEXICON_MAP:
            text = text.replace(ta_word, f" {en_word} ")
        text = re.sub(r"\s+", " ", text).strip()

    for pattern, replacement in THANGLISH_NORMALIZATIONS:
        if pattern.search(text):
            text = pattern.sub(replacement, text)
            break

    return text.strip()
