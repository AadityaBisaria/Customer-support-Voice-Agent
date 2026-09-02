"""Per-utterance Hindi/English ratio detection.

Pure functions, no pipecat imports. A token counts as Hindi if it contains
Devanagari codepoints (Sarvam's codemix mode writes Hindi words in native
script) or if its Latin form is a known romanized-Hindi word (covers
transcripts or tiers that romanize).
"""

import re

# Devanagari block. U+0900..U+097F covers letters, matras, and signs.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Word tokens: runs of word characters OR anything in the Devanagari block.
# Python's \w excludes combining marks (matras), which would otherwise split
# a word like "रिफंड" into several tokens and inflate the count.
_TOKEN = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)

# Romanized Hindi words that are unambiguous against English. Words that
# collide with common English tokens ("to", "do", "me", "the", "main", "us",
# "par", "ya") are deliberately absent: a false English->Hindi hit skews the
# ratio far more than a missed Hindi word, because Devanagari catches most
# Hindi anyway.
# fmt: off
ROMAN_HINDI_WORDS = frozenset(
    {
        # verbs / auxiliaries
        "hai", "hain", "tha", "thi", "hoga", "hogi", "honge", "hua", "hui", "hue",
        "raha", "rahi", "rahe", "gaya", "gayi", "gaye", "karo", "karna", "karke",
        "karein", "kiya", "kijiye", "dijiye", "dena", "denge", "diya", "lena",
        "liya", "lijiye", "milega", "milegi", "mila", "bhejo", "bheja", "bhejna",
        "batao", "bataiye", "bataye", "bolo", "boliye", "dekho", "dekhiye",
        "suniye", "chahiye", "chahta", "chahti", "samjha", "samjhi", "samajh",
        "aana", "aaya", "aayega", "aayegi", "jana", "jaana", "jayega", "hona",
        # pronouns / possessives
        "mujhe", "mujhko", "humko", "hume", "humein", "mera", "meri", "mere",
        "aap", "aapka", "aapki", "aapke", "apka", "apki", "apke", "tum",
        "tumhara", "tumhari", "tera", "teri", "uska", "uski", "unka", "unki",
        "iska", "iski", "yeh", "woh", "inka", "sabka",
        # question words
        "kya", "kyun", "kyu", "kaise", "kaisa", "kaisi", "kab", "kahan", "kaha",
        "kidhar", "kaun", "kitna", "kitne", "kitni", "kounsa", "kaunsa",
        # common particles / adverbs / nouns
        "nahi", "nahin", "haan", "ji", "theek", "thik", "accha", "achha", "acha",
        "lekin", "magar", "aur", "abhi", "kal", "aaj", "parso", "jaldi", "thoda",
        "thodi", "zyada", "jyada", "bahut", "bohot", "bilkul", "sirf", "wala",
        "wali", "wale", "matlab", "paisa", "paise", "rupaye", "rupay", "wapas",
        "wapis", "andar", "bahar", "yahan", "wahan", "koi", "kuch", "sab",
        "sabhi", "dhanyawad", "shukriya", "namaste", "namaskar", "bhai",
        "bhaiya", "pata", "maloom", "malum", "din", "hafta", "mahina", "saath",
        "baad", "pehle", "pahle", "dobara", "phir", "fir", "toh", "bhi",
        "mein", "meraa", "krna", "karwana", "chalega", "chalegi",
    }
)
# fmt: on


def hindi_ratio(text: str) -> float | None:
    """Fraction of word tokens that are Hindi, or None when there are no words.

    ``None`` (rather than 0.0) keeps empty or punctuation-only turns from
    dragging a rolling estimate toward English.
    """
    tokens = [t for t in _TOKEN.findall(text) if not t.isdigit()]
    if not tokens:
        return None

    hindi = 0
    for token in tokens:
        if _DEVANAGARI.search(token) or token.lower() in ROMAN_HINDI_WORDS:
            hindi += 1
    return hindi / len(tokens)
