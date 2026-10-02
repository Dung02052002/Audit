"""Text similarity helpers (Prompt Pack v8, prompt #057), context C4 Research.

Shared by source deduplication (#057) and, later, topic deduplication
(#058). The rules were approved by the user on 2026-10-02:

- Text is compared as words: lower case (``casefold``) runs of letters and
  digits in any script, so punctuation and spacing do not matter.
- Short texts (titles, queries, topics) use the Jaccard similarity of their
  word sets. Titles are near-duplicates from 0.9 (``TITLE_THRESHOLD``),
  queries and topics from 0.8 (``SHORT_TEXT_THRESHOLD``).
- Page content uses a 64-bit simhash of 3-word shingles, stored as 16
  lower-case hex digits; two pages are near-duplicates when their
  fingerprints differ in at most 6 bits (``MAX_HAMMING_DISTANCE``). The page
  text itself is not stored.

Everything here is deterministic: the same text always gives the same
fingerprint, on every machine and run.
"""

import hashlib
import re

WORD_PATTERN = re.compile(r"\w+", re.UNICODE)
SHINGLE_SIZE = 3
FINGERPRINT_BITS = 64
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{16}$")
# 6, not the 3 first proposed: measured on 400-1500 word texts, 3 bits missed
# up to 15% of one-word edits, 6 caught all of them, and unrelated texts were
# never closer than 21 bits (user decision 2026-10-02).
MAX_HAMMING_DISTANCE = 6
TITLE_THRESHOLD = 0.9
SHORT_TEXT_THRESHOLD = 0.8


def words(text: str) -> list[str]:
    return [word.casefold() for word in WORD_PATTERN.findall(text)]


def word_jaccard(first: str, second: str) -> float:
    """|A ∩ B| / |A ∪ B| of the word sets; 0.0 when either has no words."""
    a, b = set(words(first)), set(words(second))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def content_fingerprint(text: str) -> str | None:
    """The 64-bit simhash of the text's 3-word shingles, or None without words."""
    tokens = words(text)
    if not tokens:
        return None
    if len(tokens) < SHINGLE_SIZE:
        shingles = {" ".join(tokens)}
    else:
        shingles = {
            " ".join(tokens[i : i + SHINGLE_SIZE])
            for i in range(len(tokens) - SHINGLE_SIZE + 1)
        }
    weights = [0] * FINGERPRINT_BITS
    for shingle in shingles:
        digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(FINGERPRINT_BITS):
            weights[bit] += 1 if value >> bit & 1 else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)
    return f"{fingerprint:016x}"


def hamming_distance(first: str, second: str) -> int:
    """How many bits two fingerprints differ in."""
    for value in (first, second):
        if not FINGERPRINT_PATTERN.match(value):
            raise ValueError(f"fingerprint {value!r} must be 16 lower-case hex digits")
    return (int(first, 16) ^ int(second, 16)).bit_count()


def fingerprint_similarity(first: str, second: str) -> float:
    """1.0 for equal fingerprints, down to 0.0 when every bit differs."""
    return 1 - hamming_distance(first, second) / FINGERPRINT_BITS
