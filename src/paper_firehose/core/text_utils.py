"""Shared text processing utilities.

Consolidates text normalization, cleaning, and matching functions used across
the codebase for author names, abstracts, and other text fields.
"""

import re
import html as htmllib
import unicodedata
from typing import Optional, List, Tuple


def strip_jats(text: Optional[str]) -> Optional[str]:
    """Remove JATS/HTML tags and unescape entities in Crossref-style strings.

    JATS (Journal Article Tag Suite) is an XML format used by publishers. Crossref
    and other APIs often return abstracts with JATS tags embedded.

    Args:
        text: Text potentially containing JATS/HTML tags

    Returns:
        Cleaned text with tags removed and entities unescaped, or None if input was None

    Examples:
        >>> strip_jats("<jats:p>Some text</jats:p>")
        'Some text'
        >>> strip_jats("Text with &lt;angle&gt; brackets")
        'Text with <angle> brackets'
    """
    if not text:
        return text

    # Remove <jats:...> and regular HTML tags
    text = re.sub(r"</?jats:[^>]+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)

    # Unescape HTML entities like &lt; &gt; &amp;
    return htmllib.unescape(text).strip()


def clean_abstract_for_db(text: Optional[str]) -> Optional[str]:
    """Conservative sanitizer for abstracts before storing in database.

    Performs comprehensive cleaning:
    - Removes JATS/HTML tags and unescapes entities via strip_jats()
    - Strips stray '<' and '>' characters (common artifact from feeds)
    - Removes leading feed prefixes like "Abstract" and arXiv announce headers
    - Normalizes whitespace and removes zero-width characters

    Args:
        text: Raw abstract text from API or feed

    Returns:
        Cleaned abstract ready for database storage, or None if input was None

    Examples:
        >>> clean_abstract_for_db("Abstract: This is the abstract.")
        'This is the abstract.'
        >>> clean_abstract_for_db("arXiv:2509.09390v1 Announce Type: new Abstract: Text")
        'Text'
    """
    if text is None:
        return None

    # First remove tags and unescape entities
    s = strip_jats(text) or ""

    # Remove zero-width and BOM-like chars
    s = s.replace("\u200B", "").replace("\u200C", "").replace("\u200D", "").replace("\uFEFF", "")

    # Normalize non-breaking spaces
    s = s.replace("\xa0", " ")

    # Remove any remaining angle bracket characters which often leak from markup
    s = s.replace("<", "").replace(">", "")

    # Drop leading arXiv announce header like:
    #   "arXiv:2509.09390v1 Announce Type: new Abstract: ..."
    s = re.sub(r"^\s*arXiv:[^\n]*?(?:Announce\s+Type:\s*\w+\s+)?Abstract:\s*", "", s, flags=re.IGNORECASE)

    # Drop simple leading "Abstract" or "Abstract:" tokens
    s = re.sub(r"^\s*Abstract\s*:?[\s\-–—]*", "", s, flags=re.IGNORECASE)

    # Collapse excessive whitespace
    s = re.sub(r"[\t\r ]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()


def strip_accents(text: str) -> str:
    """Return ASCII-ish text by removing accent marks via Unicode normalization.

    Useful for comparing author names and other text where accents should not
    affect matching.

    Args:
        text: Text potentially containing accented characters

    Returns:
        Text with accent marks removed

    Examples:
        >>> strip_accents("José García")
        'Jose Garcia'
        >>> strip_accents("Müller")
        'Muller'
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def normalize_name(text: str) -> str:
    """Normalize a human name for loose matching.

    Strips accents, punctuation, and converts to lowercase for fuzzy name matching.

    Args:
        text: Human name to normalize

    Returns:
        Normalized name suitable for comparison

    Examples:
        >>> normalize_name("García-López, José")
        'garcia lopez jose'
        >>> normalize_name("John P. Smith")
        'john p smith'
    """
    t = strip_accents(text or "").lower()
    # Keep only letters, spaces, and hyphens
    t = re.sub(r"[^a-z\s\-]", " ", t)
    # Collapse multiple spaces
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_name_parts(name: str) -> Tuple[str, List[str]]:
    """Parse a human name into (lastname, initials[]).

    Handles both "Last, First M" and "First M Last" styles, ignoring accents
    and case for robust parsing.

    Args:
        name: Full name in various formats

    Returns:
        Tuple of (lastname, list of first/middle initials)

    Examples:
        >>> parse_name_parts("Smith, John P.")
        ('smith', ['j', 'p'])
        >>> parse_name_parts("John P. Smith")
        ('smith', ['j', 'p'])
        >>> parse_name_parts("García-López, José")
        ('garcia lopez', ['j'])
    """
    if not name:
        return "", []

    # Preserve comma pattern before normalization for ordering hint
    if "," in name:
        last_raw, _, rest_raw = name.partition(",")
        last = normalize_name(last_raw)
        rest = normalize_name(rest_raw)
        tokens = rest.split()
    else:
        n = normalize_name(name)
        tokens = n.split()
        last = tokens[-1] if tokens else ""
        tokens = tokens[:-1]

    # Extract first letter of each remaining token as initial
    initials = [t[0] for t in tokens if t]
    return last, initials


def names_match(a: str, b: str) -> bool:
    """Heuristic author-name comparator supporting initials and comma forms.

    Compares two author names with fuzzy matching that handles:
    - Different name orderings (Last, First vs First Last)
    - Initials vs full first names
    - Accents and punctuation differences

    Args:
        a: First author name
        b: Second author name

    Returns:
        True if names likely refer to the same person, False otherwise

    Examples:
        >>> names_match("Smith, J. P.", "John P. Smith")
        True
        >>> names_match("J. Smith", "Jane Smith")
        True
        >>> names_match("J. Smith", "John Doe")
        False
    """
    la, ia = parse_name_parts(a)
    lb, ib = parse_name_parts(b)

    # Both must have a last name
    if not la or not lb:
        return False

    # Last names must match
    if la != lb:
        return False

    # If both have initials, at least one must overlap
    if ia and ib and not set(ia).intersection(ib):
        return False

    return True
