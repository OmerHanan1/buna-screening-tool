"""Versioned improvedEng word units, retaining original character offsets."""
import heapq
import re

from buna.classified import tokens as lexical_tokens

WORD_POLICY_VERSION = "literal-operators-equals-less-greater-approx-v1"
OPERATORS = frozenset("=<>≈")
_OPERATOR = re.compile(r"[=<>≈]")


def tokens(text, join_words=None, check=lambda: None):
    words = lexical_tokens(text, join_words, check)
    operators = []
    for index, match in enumerate(_OPERATOR.finditer(text)):
        if index % 2048 == 0:
            check()
        operators.append((match.group(), match.start(), match.end()))
    return list(heapq.merge(words, operators, key=lambda token: token[1]))
