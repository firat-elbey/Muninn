"""Deterministic typed relationships over a plain-Markdown bundle.

The authored notes remain the source of truth. This module derives a bounded
one-hop index from explicit typed-link lines and ordinary Markdown links. It
recognizes relation intent from ordinary English only when the named entity
resolves to one unique title or alias.

Edges use one canonical orientation: a person is the subject and the related
company or meeting is the object. This removes source-page direction from
retrieval semantics while preserving the source note for inspection.
"""

from __future__ import annotations

import re
import string
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .store import (
    Bundle,
    Note,
    _all_markdown_links,
    _is_escaped,
    _mask_link_graph_source,
    _mask_nonassertive_markdown,
    _mask_wikilink_context,
    _mask_wikilink_metadata,
)

RELATION_TYPES = frozenset(
    {"attended", "works_at", "founded", "invested_in", "advises"}
)
_WIKILINK = re.compile(
    r"\[\[((?=[^\]|#]*[^\s\]|#])[^\]|#]+)"
    r"(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]"
)
_BOUNDARY = re.compile(r"[.!?\n]")
_NONSPACE = re.compile(r"\S")
_MARKDOWN_BLOCK_LINE = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}(?:[ \t]|$)|[-*+](?:[ \t]+|$)|"
    r"\d{1,9}[.)][ \t]+|>|`{3,}|~{3,}|<(?:/?(?:address|article|aside|"
    r"blockquote|body|div|dl|fieldset|figure|footer|form|h[1-6]|header|"
    r"html|main|nav|ol|p|pre|script|section|style|table|textarea|ul)\b|"
    r"!--|\?|!\[CDATA\[|![A-Z]))",
    re.IGNORECASE,
)


def _wikilink_matches(text: str):
    """Yield unescaped wikilinks only."""

    return (
        match
        for match in _WIKILINK.finditer(text)
        if not _is_escaped(text, match.start())
    )


_FOUNDED = re.compile(
    r"\b(?:founded|co[ -]?(?:created|founded)|founder|founders|incorporated|"
    r"launched|started the company)\b",
    re.IGNORECASE,
)
_INVESTED = re.compile(
    r"\b(?:invest(?:ed|s|or|ors|ing|ment|ments)?(?:\s+in)?|"
    r"back(?:ed|s|er|ers|ing)?|funding from|funded by|raised from|"
    r"seed round|series [a-z]|led the seed|led the round|"
    r"led .{0,30}(?:series [a-z]|seed|round|investment)|participated in|"
    r"capital|wrote (?:a |the )?check|first check|portfolio|cap table|"
    r"term sheet)\b",
    re.IGNORECASE,
)
_ADVISES = re.compile(
    r"\b(?:advis(?:e|es|ed|er|ers|or|ors|ing|ory)|guidance|"
    r"consult(?:s|ed|ing|ant)?|advisory (?:board|role|capacity|engagement|"
    r"partnership|contract|relationship))\b",
    re.IGNORECASE,
)
_RECEIVED_ADVICE_FROM = re.compile(r"\breceived advice from\b", re.IGNORECASE)
_GAVE_ADVICE_TO = re.compile(
    r"\b(?:gave|provided|offered) advice to\b", re.IGNORECASE
)
_WORKS_AT = re.compile(
    r"\b(?:works?|worked|working|employ(?:s|ed|ee|ees|ment)?|hired|joined|"
    r"ceo|cto|coo|cfo|cmo|cro|engineer|developer|designer|director|"
    r"head of|leads?|manages?|role at|position at|team at|tenure at|"
    r"stint at|currently at|previously at)\b",
    re.IGNORECASE,
)
_ATTENDANCE_LANGUAGE = re.compile(
    r"\b(?:attend\w*|participants?|present|took part|joined|"
    r"in the room|show(?:ed)? up|went to)\b",
    re.IGNORECASE,
)
_RELATION_SEGMENT_BOUNDARY = re.compile(
    r"\s*(?:[,;](?:\s*\b(?:and|but|while|whereas)\b)?|"
    r"\b(?:and|because|but|while|whereas)\b|"
    r"\bso(?=\s+(?:that|he|she|they|it|we|i)\b))\s*",
    re.IGNORECASE,
)
_MAX_RELATION_CONTEXT_CHARS = 1024
_PERSON_SELF = r"<source>"
_ORG_SELF = r"<source>"
_ORG_POSSESSIVE = r"<source>(?:['’]s|['’])?"
_MEETING_SELF = r"<source>"
_MEETING_POSSESSIVE = r"<source>(?:['’]s|['’])?"
_EXECUTIVE_ROLE_TEXT = (
    r"ceo|cto|coo|cfo|cmo|cro|chief [a-z]+ officer|president|director|manager"
)
_NON_ASSERTIVE_RELATION_CONTEXT = re.compile(
    r"\b(?:alleged(?:ly)?|claim(?:s|ed|ing)?|den(?:y|ies|ied|ying)|"
    r"disput(?:e|es|ed|ing)|doubt(?:s|ed|ing)?|false|falsely|incorrect|"
    r"if|maybe|neither|never|nor|not|perhaps|possibly|purported(?:ly)?|"
    r"question(?:s|ed|ing)?|"
    r"refut(?:e|es|ed|ing)|"
    r"reportedly|rumou?rs?|speculat(?:e|es|ed|ing|ion)|"
    r"suppos(?:e|es|ed|ing|edly)|uncertain|unclear|"
    r"unconfirmed|unknown|untrue|whether)\b|"
    r"\bno\s+(?:credible\s+)?(?:confirmation|evidence|proof|record)\b",
    re.IGNORECASE,
)
_NON_ASSERTIVE_LEAD_SEGMENT = re.compile(
    r"^\s*(?:allegedly|if(?:\s+.+)?|it\s+is\s+disputed|"
    r"purportedly|reportedly|speculation\s+aside|supposedly)\s*$",
    re.IGNORECASE,
)
_NON_ASSERTIVE_ATTRIBUTION = re.compile(
    r"^\s*(?:according\s+to\b|"
    r"it\s+(?:was|is|has\s+been)\s+"
    r"(?:alleged|asserted|believed|claimed|indicated|noted|reported|"
    r"rumou?red|said|stated|understood|written)"
    r"(?:\s+(?:in|by|on|through|within)\s+(?:an?|the)?\s*"
    r"[A-Za-z0-9][A-Za-z0-9 ./-]{0,48})?\s+that\b|"
    r"(?:an?|the)\s+(?:analyst(?:\s+note)?|article|"
    r"(?:[A-Za-z0-9][A-Za-z0-9'’_-]*\s+)?fact[ -]?check|filing|newspaper|"
    r"news\s+report|report|source|board\s+memo|memo(?:randum)?|"
    r"partner\s+note|internal\s+note|document)\s+"
    r"(?:alleg(?:e|es|ed)|assert(?:s|ed)?|believ(?:e|es|ed)|"
    r"claim(?:s|ed)?|indicat(?:e|es|ed)|not(?:e|es|ed)|report(?:s|ed)?|"
    r"say(?:s)?|said|stat(?:e|es|ed)|writ(?:e|es)|wrote)\b|"
    r"(?:(?!(?:the|it)\b)"
    r"(?-i:[A-Z][A-Za-z'’_-]*(?:\s+[A-Z][A-Za-z'’_-]*){0,3})|"
    r"he|she|they)\s+"
    r"(?:alleg(?:e|es|ed)|assert(?:s|ed)?|believ(?:e|es|ed)|"
    r"claim(?:s|ed)?|indicat(?:e|es|ed)|not(?:e|es|ed)|report(?:s|ed)?|"
    r"say(?:s)?|said|stat(?:e|es|ed)|writ(?:e|es)|wrote)\b)",
    re.IGNORECASE,
)
_WROTE_CHECK_ASSERTION = re.compile(
    r"\bwrote\s+(?:(?!\bwrote\b)[\s\S]){1,120}?\s+"
    r"(?:an?\s+|the\s+)?check\b",
    re.IGNORECASE,
)
_PUNCTUATED_PASSIVE_ATTRIBUTION = re.compile(
    r"^\s*it\s+(?:was|is|has\s+been)\s+"
    r"(?:alleged|asserted|believed|claimed|indicated|noted|reported|"
    r"rumou?red|said|stated|understood|written)\b"
    r"[\s\S]{0,160}?,\s+that\b",
    re.IGNORECASE,
)
_PASSIVE_ATTRIBUTION_LEAD = re.compile(
    r"^\s*it\s+(?:was|is|has\s+been)\s+"
    r"(?:alleged|asserted|believed|claimed|indicated|noted|reported|"
    r"rumou?red|said|stated|understood|written)\b",
    re.IGNORECASE,
)
_NON_ASSERTIVE_MODAL_RELATION = re.compile(
    r"\b(?:may|might|could|would|should|can|will)\s+"
    r"(?:not\s+)?(?:have\s+)?(?:been\s+)?"
    r"(?:help(?:ed|ing)?\s+(?:to\s+)?|be\s+)?"
    r"(?:co[ -]?found(?:ed|ing)?|found(?:ed|ing)?|establish(?:ed|ing)?|"
    r"creat(?:e|ed|ing)|incorporat(?:e|ed|ing)|launch(?:ed|ing)?|"
    r"start(?:ed|ing)?|back(?:ed|ing)?|fund(?:ed|ing)?|"
    r"financ(?:e|ed|ing)|invest(?:ed|ing)?|advis(?:e|ed|ing)|"
    r"counsel(?:ed|ing)?|consult(?:ed|ing)?|mentor(?:ed|ing)?|"
    r"work(?:ed|ing)?|join(?:ed|ing)?|attend(?:ed|ing)?)\b|"
    r"\b(?:plan(?:s|ned)?|intend(?:s|ed)?|aim(?:s|ed)?|hope(?:s|d)?|"
    r"expect(?:s|ed)?|propos(?:e|es|ed)|schedul(?:e|es|ed))\s+"
    r"(?:not\s+)?to\s+(?:help\s+(?:to\s+)?)?"
    r"(?:co[ -]?found|found|establish|create|incorporate|launch|start|"
    r"back|fund|finance|invest|advise|counsel|consult|mentor|work|join|"
    r"attend)\b",
    re.IGNORECASE,
)
_COUNTERFACTUAL_RELATION = re.compile(
    r"^\s*(?:(?:(?:even|indeed|only|perhaps|possibly)\s*,?\s*)?"
    r"(?:had|should|were)\b|"
    r"(?:assume|assuming|imagine|suppose|supposing)\b)",
    re.IGNORECASE,
)
_EMPLOYMENT_ENDING_CLAUSE = re.compile(
    r"^\s*(?:(?:<source>|he|she|they)\s+)?"
    r"(?:also\s+|later\s+|then\s+)?"
    r"(?:left|departed|resigned|retired|stopped\s+working|"
    r"no\s+longer\s+works?)\b"
    r"(?:"
    r"\s+(?:there|(?:from\s+)?(?:the\s+)?"
    r"(?:company|employer|employment|job|position|role|staff|team))"
    r"(?:\s+(?:in|on|by|after|before|because\s+of|due\s+to)\s+"
    r"[^,;.!?]{1,64})?|"
    r"(?:\s+after\s+(?:the\s+)?(?:acquisition|merger)\b"
    r"[^,;.!?]{0,48})|"
    r"(?:\s+(?:in|on|by|after|before)\s+"
    r"(?:(?:(?:early|mid(?:dle)?|late)[ -]+)?"
    r"(?:(?:19|20)\d{2}|\d{1,2}(?:st|nd|rd|th)?|"
    r"(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)|"
    r"(?:the\s+)?(?:following|next|previous)\s+(?:day|month|year)|"
    r"\d+\s+(?:days?|months?|years?)))\b[^,;.!?]{0,48})?"
    r")\s*$",
    re.IGNORECASE,
)
_ASSERTIVE_NOT = re.compile(r"\bnot\s+(?:just|merely|only)\b", re.IGNORECASE)
_ASSERTIVE_CERTAINTY = re.compile(
    r"\b(?:there\s+is\s+)?no\s+question\s+that\b",
    re.IGNORECASE,
)
_DO_NEGATION = re.compile(r"\bdid\s+not\b", re.IGNORECASE)
_BASE_COORDINATED_RELATION = re.compile(
    r"^\s*(?:(?:also|later|then)\s+)?"
    r"(?:advise|back|counsel|create|employ|establish|"
    r"finance|found|fund|hire|incorporate|invest|launch|mentor|start|work)\b",
    re.IGNORECASE,
)
_TARGET_POSSESSIVE = re.compile(
    r"<target>\s*['’](?:s\b|(?=\s|$))",
    re.IGNORECASE,
)
_GAPPED_RELATION_PREDICATE = re.compile(
    r"^(?:(?:also|later|then)\s+)?"
    r"(?:(?:did|does|do)\s+(?:not\s+)?|(?:not|never)\s+)?"
    r"(?:was|were|is|are|employs?|employed|hired|received|got|"
    r"works?|worked|working|invest(?:s|ed|ing)|"
    r"advis(?:e|es|ed|ing)|counsel(?:s|ed)|consult(?:s|ed)|mentor(?:s|ed)|"
    r"back(?:s|ed)|fund(?:s|ed)|financ(?:es|ed)|"
    r"attend(?:s|ed|ing)|participat(?:es|ed|ing)|joined|went|"
    r"co[ -]?founded|founded|established|created|incorporated|launched|"
    r"started)\b",
    re.IGNORECASE,
)
_PERSON_ANTECEDENT_INTRODUCTION = re.compile(
    r"\b(?:introduced|interviewed|met|named|profiled|mentioned|"
    r"spoke\s+(?:to|with)|asked|called|emailed|hired|thanked|visited|"
    r"worked\s+with|stood\s+(?:beside|near|with))\s+"
    r"[A-Z][A-Za-z'’_-]*(?:\s+[A-Z][A-Za-z'’_-]*){0,3}\b"
)
_PERSON_ANTECEDENT_FULL_NAME = re.compile(
    r"\b[A-Z][A-Za-z'’_-]+\s+[A-Z][A-Za-z'’_-]+\b"
)
_PERSON_TO_ORG_FOUNDED = re.compile(
    rf"(?:{_PERSON_SELF}\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:help(?:s|ed|ing)?\s+(?:to\s+)?found|co[ -]?(?:created|founded)|"
    r"founded|"
    r"established|created|incorporated|launched|"
    r"started)\s+"
    r"(?:the\s+)?<target>|"
    rf"{_PERSON_SELF}\s+(?:is|was|became)\s+(?:an?\s+|the\s+)?"
    r"(?:co[ -]?)?founder\s+(?:of|at)\s+<target>|"
    r"<target>\s+(?:was\s+)?(?:co[ -]?)?founded\s+by\s+<source>)",
    re.IGNORECASE,
)
_ORG_TO_PERSON_FOUNDED = re.compile(
    rf"(?:{_ORG_SELF}\s+(?:was\s+)?(?:co[ -]?(?:created|founded)|founded|established|"
    r"created|incorporated|launched|started)\s+"
    r"(?:in\s+\d{4}\s+)?by\s+<target>|"
    rf"<target>\s+(?:also\s+|later\s+|then\s+)?"
    rf"(?:help(?:s|ed|ing)?\s+(?:to\s+)?found|co[ -]?(?:created|founded)|founded|"
    rf"established|created|incorporated|launched|started)\s+{_ORG_SELF}|"
    rf"{_ORG_POSSESSIVE}\s+(?:founders?|co[ -]?founders?)\s+"
    r"(?:include|includes|included|are|were)\s+<target>|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:is|was|became)\s+(?:an?\s+|the\s+)?(?:co[ -]?)?founder"
    rf"(?:\s+(?:of|at)\s+{_ORG_SELF})?\s*$)",
    re.IGNORECASE,
)
_PERSON_TO_ORG_INVESTED = re.compile(
    rf"(?:{_PERSON_SELF}\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:invest(?:s|ed|ing)\s+(?:money\s+|capital\s+)?in|"
    r"back(?:s|ed)|fund(?:s|ed)|financ(?:es|ed))\s+(?:the\s+)?<target>|"
    rf"{_PERSON_SELF}\s+(?:is|was|became)\s+(?:an?\s+|the\s+)?"
    r"(?:seed\s+|lead\s+|angel\s+|early\s+)?(?:investor|backer)\s+"
    r"(?:in|of)\s+<target>|"
    r"<target>\s+(?:was\s+)?(?:backed|funded|financed)\s+by\s+<source>|"
    r"<target>\s+(?:received|got)\s+(?:an?\s+)?"
    r"(?:(?:pre[ -]?seed|seed|series\s+[a-z]|strategic|venture|equity)\s+)?"
    r"(?:investment|funding|financing|capital)\s+from\s+<source>|"
    r"<target>\s+raised\s+(?:money|capital|funds?|funding|financing|"
    r"investment)\s+from\s+<source>|"
    r"<source>\s+wrote\s+<target>\s+(?:an?\s+|the\s+)?check)",
    re.IGNORECASE,
)
_ORG_TO_PERSON_INVESTED = re.compile(
    rf"(?:{_ORG_SELF}\s+(?:was\s+)?(?:backed|funded|financed)\s+by\s+"
    r"<target>|"
    rf"{_ORG_SELF}\s+(?:received|got)\s+(?:an?\s+)?"
    r"(?:(?:pre[ -]?seed|seed|series\s+[a-z]|strategic|venture|equity)\s+)?"
    r"(?:investment|funding|financing|capital)\s+from\s+<target>|"
    rf"{_ORG_SELF}\s+raised\s+(?:money|capital|funds?|funding|financing|"
    r"investment)\s+from\s+<target>|"
    rf"<target>\s+(?:also\s+|later\s+|then\s+)?(?:invest(?:s|ed|ing)\s+"
    rf"(?:money\s+|capital\s+)?in|back(?:s|ed)|fund(?:s|ed)|"
    rf"financ(?:es|ed))\s+{_ORG_SELF}|"
    rf"{_ORG_POSSESSIVE}\s+(?:seed\s+|lead\s+|angel\s+|early\s+)?"
    r"(?:investors?|backers?)\s+(?:include|includes|included|are|were)\s+"
    r"<target>|"
    r"<target>\s+wrote\s+<source>\s+(?:an?\s+|the\s+)?check|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:is|was|became)\s+(?:an?\s+|the\s+)?"
    r"(?:seed\s+|lead\s+|angel\s+|early\s+)?(?:investor|backer)"
    rf"(?:\s+(?:in|of)\s+{_ORG_SELF})?\s*$)",
    re.IGNORECASE,
)
_PERSON_TO_ORG_ADVISES = re.compile(
    rf"(?:{_PERSON_SELF}\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:advis(?:es|ed|ing)|counsel(?:s|ed|ing)|consult(?:s|ed|ing)|"
    r"mentor(?:s|ed|ing))\s+(?:for\s+|to\s+)?<target>|"
    rf"{_PERSON_SELF}\s+(?:gave|provided|offered)\s+"
    r"(?:advice|guidance|counsel|direction|recommendations?)\s+"
    r"(?:to|for)\s+<target>|"
    rf"{_PERSON_SELF}\s+(?:gave|provided|offered)\s+<target>\s+"
    r"(?:(?:expert|strategic|professional)\s+)?"
    r"(?:advice|guidance|counsel|direction|recommendations?)|"
    r"<target>\s+(?:was\s+)?(?:advised|counseled|mentored)\s+by\s+<source>|"
    r"<target>\s+(?:received|got)\s+"
    r"(?:(?:expert|strategic|professional)\s+)?"
    r"(?:advice|guidance|counsel|recommendations?)\s+from\s+<source>)",
    re.IGNORECASE,
)
_ORG_TO_PERSON_ADVISES = re.compile(
    rf"(?:{_ORG_SELF}\s+(?:was\s+)?(?:advised|counseled)\s+by\s+<target>|"
    rf"{_ORG_SELF}\s+(?:received|got)\s+"
    r"(?:(?:expert|strategic|professional)\s+)?"
    r"(?:advice|guidance|counsel|recommendations?|advisory\s+help)\s+"
    r"from\s+<target>|"
    rf"<target>\s+(?:gave|provided|offered)\s+{_ORG_SELF}\s+"
    r"(?:(?:expert|strategic|professional)\s+)?"
    r"(?:advice|guidance|counsel|direction|recommendations?)|"
    rf"<target>\s+(?:also\s+|later\s+|then\s+)?(?:advis(?:es|ed|ing)|"
    rf"counsel(?:s|ed|ing)|consult(?:s|ed|ing)|mentor(?:s|ed|ing))\s+"
    rf"(?:for\s+|to\s+)?{_ORG_SELF}|"
    rf"{_ORG_POSSESSIVE}\s+(?:advisers?|advisors?|consultants?)\s+"
    r"(?:include|includes|included|are|were)\s+<target>|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:is|was|became|serves?\s+as)\s+(?:an?\s+|the\s+)?"
    r"(?:adviser|advisor|consultant|mentor)"
    rf"(?:\s+(?:to|for)\s+{_ORG_SELF})?\s*$)",
    re.IGNORECASE,
)
_PERSON_TO_ORG_ADVISORY_BOARD = re.compile(
    r"(?:<source>\s+(?:sat|sits?|served|serves)\s+on\s+(?:the\s+)?"
    r"(?:<target>\s*(?:['’]s|['’])\s+advisory\s+board|"
    r"advisory\s+board\s+of\s+<target>)|"
    r"<source>\s+(?:is|was|became)\s+(?:an?\s+|the\s+)?member\s+of\s+"
    r"(?:the\s+)?(?:<target>\s*(?:['’]s|['’])\s+advisory\s+board|"
    r"advisory\s+board\s+of\s+<target>))",
    re.IGNORECASE,
)
_PERSON_TO_ORG_LED_ROUND = re.compile(
    r"(?:<source>\s+led\s+(?:the\s+)?<target>\s*(?:['’]s|['’])\s+"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round|<source>\s+led\s+(?:the\s+)?"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round\s+(?:in|for|of)\s+(?:the\s+)?<target>|"
    r"<source>\s+(?:is|was|became)\s+(?:the\s+)?lead\s+investor\s+"
    r"(?:in|for|of)\s+<target>\s*(?:['’]s|['’])\s+"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round)",
    re.IGNORECASE,
)
_PERSON_TO_ORG_EXECUTIVE_ROLE = re.compile(
    rf"<source>\s+(?:(?:is|was|became)\s+|serv(?:e|es|ed|ing)\s+as\s+)"
    rf"(?:an?\s+|the\s+)?"
    rf"(?P<role>{_EXECUTIVE_ROLE_TEXT})\s+(?:at|for|of)\s+<target>",
    re.IGNORECASE,
)
_ORG_TO_PERSON_EXECUTIVE_ROLE = re.compile(
    rf"(?:<source>(?:['’]s|['’])?\s+"
    rf"(?P<role>{_EXECUTIVE_ROLE_TEXT})\s+(?:is|was)\s+<target>|"
    rf"(?:the\s+)?(?P<of_role>{_EXECUTIVE_ROLE_TEXT})\s+of\s+<source>\s+"
    rf"(?:is|was)\s+<target>|"
    rf"<target>\s+(?:is|was|became)\s+(?:an?\s+|the\s+)?"
    rf"(?P<reverse_role>{_EXECUTIVE_ROLE_TEXT})\s+(?:at|for|of)\s+<source>)",
    re.IGNORECASE,
)
_ORG_TO_PERSON_ADVISORY_BOARD = re.compile(
    r"(?:<target>\s+(?:sat|sits?|served|serves)\s+on\s+(?:the\s+)?"
    r"<source>(?:['’]s|['’])?\s+advisory\s+board|"
    r"<source>(?:['’]s|['’])?\s+advisory\s+board\s+"
    r"(?:includes?|included|has|had)\s+<target>)",
    re.IGNORECASE,
)
_ORG_TO_PERSON_LED_ROUND = re.compile(
    r"(?:<target>\s+led\s+(?:the\s+)?<source>(?:['’]s|['’])?\s+"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round|<source>(?:['’]s|['’])?\s+"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round\s+(?:is|was)\s+led\s+by\s+<target>|"
    r"(?:the\s+)?"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round\s+(?:in|for|of)\s+<source>\s+(?:is|was)\s+led\s+by\s+<target>|"
    r"<target>\s+(?:is|was|became)\s+(?:the\s+)?lead\s+investor\s+"
    r"(?:in|for|of)\s+<source>(?:['’]s|['’])?\s+"
    r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|series\s+[a-z])\s+)?"
    r"round)",
    re.IGNORECASE,
)
_PERSON_TO_ORG_WORKS_AT = re.compile(
    rf"(?:{_PERSON_SELF}\s+(?:(?:also|currently|formerly|later|previously|then)\s+)?"
    r"(?:works?|worked|working|(?:has|had)\s+worked|used\s+to\s+work)\s+"
    r"(?:at|for)\s+<target>|"
    rf"{_PERSON_SELF}\s+(?:(?:is|was|became)\s+|"
    r"serv(?:e|es|ed|ing)\s+as\s+)(?:an?\s+|the\s+)?"
    rf"(?:{_EXECUTIVE_ROLE_TEXT}|engineer|developer|designer|employee|"
    r"staff\s+member|team\s+member)\s+(?:at|for|of)\s+"
    r"<target>|"
    rf"{_PERSON_SELF}\s+joined\s+<target>(?:\s+as\s+(?:an?\s+|the\s+)?"
    r"(?:employee|staff\s+member|team\s+member))?|"
    rf"{_PERSON_SELF}\s+(?:was|is)\s+(?:employed|hired)\s+by\s+<target>|"
    r"<target>\s+(?:employs?|employed|hired)\s+<source>)",
    re.IGNORECASE,
)
_ORG_TO_PERSON_WORKS_AT = re.compile(
    rf"(?:{_ORG_SELF}\s+(?:employs?|employed|hired)\s+<target>|"
    rf"{_ORG_POSSESSIVE}\s+"
    r"(?:ceo|cto|coo|cfo|cmo|cro|chief\s+[a-z]+\s+officer|"
    r"president|director|manager)\s+(?:is|was)\s+<target>|"
    rf"<target>\s+(?:(?:also|currently|formerly|later|previously|then)\s+)?"
    r"(?:works?|worked|working|(?:has|had)\s+worked|used\s+to\s+work)\s+"
    rf"(?:at|for)\s+{_ORG_SELF}|"
    rf"{_ORG_POSSESSIVE}\s+(?:staff|employees?|team(?:\s+members?)?)\s+"
    r"(?:include|includes|included|are|were)\s+<target>|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:is|was|became|joined)\s+(?:an?\s+|the\s+)?"
    r"(?:employee|staff\s+member|team\s+member)"
    rf"(?:\s+(?:at|of)\s+{_ORG_SELF})?\s*$)",
    re.IGNORECASE,
)
_CURRENT_PERSON_TO_ORG_WORKS_AT = re.compile(
    rf"(?:<source>\s+(?:(?:also|currently|later|then)\s+)?"
    r"(?:works?|working)\s+(?:at|for)\s+<target>|"
    rf"<source>\s+(?:is\s+|serv(?:e|es|ing)\s+as\s+)"
    rf"(?:an?\s+|the\s+)?(?:{_EXECUTIVE_ROLE_TEXT}|engineer|developer|"
    r"designer|employee|staff\s+member|team\s+member)\s+"
    r"(?:at|for|of)\s+<target>|"
    r"<source>\s+is\s+employed\s+by\s+<target>|"
    r"<target>\s+employs?\s+<source>)",
    re.IGNORECASE,
)
_PAST_PERSON_TO_ORG_WORKS_AT = re.compile(
    rf"(?:<source>\s+(?:(?:also|formerly|later|previously|then)\s+)?"
    r"(?:had\s+worked|worked|used\s+to\s+work)\s+"
    r"(?:at|for)\s+<target>|"
    rf"<source>\s+(?:was\s+|served\s+as\s+)(?:an?\s+|the\s+)?"
    rf"(?:{_EXECUTIVE_ROLE_TEXT}|engineer|developer|designer|employee|"
    r"staff\s+member|team\s+member)\s+(?:at|for|of)\s+<target>|"
    r"<source>\s+was\s+employed\s+by\s+<target>|"
    r"<target>\s+employed\s+<source>)",
    re.IGNORECASE,
)
_CURRENT_ORG_TO_PERSON_WORKS_AT = re.compile(
    rf"(?:<source>\s+employs?\s+<target>|"
    rf"<source>(?:['’]s|['’])?\s+(?:{_EXECUTIVE_ROLE_TEXT})\s+"
    r"is\s+<target>|"
    r"<target>\s+(?:(?:also|currently|later|then)\s+)?"
    r"(?:works?|working)\s+"
    r"(?:at|for)\s+<source>|"
    r"<source>(?:['’]s|['’])?\s+(?:staff|employees?|"
    r"team(?:\s+members?)?)\s+(?:include|includes|are)\s+<target>|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?is\s+"
    r"(?:an?\s+|the\s+)?(?:employee|staff\s+member|team\s+member)"
    r"(?:\s+(?:at|of)\s+<source>)?)",
    re.IGNORECASE,
)
_PAST_ORG_TO_PERSON_WORKS_AT = re.compile(
    rf"(?:<source>\s+employed\s+<target>|"
    rf"<source>(?:['’]s|['’])?\s+(?:{_EXECUTIVE_ROLE_TEXT})\s+"
    r"was\s+<target>|"
    r"<target>\s+(?:(?:also|formerly|later|previously|then)\s+)?"
    r"(?:had\s+worked|worked|used\s+to\s+work)\s+"
    r"(?:at|for)\s+<source>|"
    r"<source>(?:['’]s|['’])?\s+(?:staff|employees?|"
    r"team(?:\s+members?)?)\s+(?:included|were)\s+<target>|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?was\s+"
    r"(?:an?\s+|the\s+)?(?:employee|staff\s+member|team\s+member)"
    r"(?:\s+(?:at|of)\s+<source>)?)",
    re.IGNORECASE,
)
_PERSON_TO_MEETING_ATTENDED = re.compile(
    rf"(?:{_PERSON_SELF}\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:attend(?:s|ed|ing)|participat(?:es|ed|ing)\s+in|joined|went\s+to|"
    r"was\s+present\s+at)"
    r"\s+(?:the\s+)?<target>|"
    r"<target>\s+(?:was\s+)?attended\s+by\s+<source>)",
    re.IGNORECASE,
)
_MEETING_TO_PERSON_ATTENDED = re.compile(
    rf"(?:{_MEETING_SELF}\s+(?:was\s+)?attended\s+by\s+<target>|"
    rf"<target>\s+(?:also\s+|later\s+|then\s+)?(?:attend(?:s|ed|ing)|"
    rf"participat(?:es|ed|ing)\s+in|joined|went\s+to|was\s+present\s+at)\s+(?:the\s+)?"
    rf"{_MEETING_SELF}|"
    rf"{_MEETING_POSSESSIVE}\s+(?:records?\s+)?attendance\s+by\s+<target>|"
    rf"{_MEETING_SELF}\s+(?:reports?|records?|states?)\s+attendance\s+by\s+"
    r"<target>|"
    rf"{_MEETING_POSSESSIVE}\s+(?:attendees?|participants?)\s+"
    r"(?:include|includes|included|are|were)\s+<target>|"
    r"\b(?:record|entry|page)\s+(?:reports?|records?|states?)\s+"
    r"attendance\s+by\s+<target>\s*$|"
    r"<target>\s+as\s+(?:an?\s+)?(?:attendee|participant)s?\s*$|"
    rf"{_MEETING_SELF}\s+(?:lists?|listed|records?|recorded|names?|named)\s+"
    r"<target>\s+as\s+present|"
    r"<target>\s+(?:also\s+|later\s+|then\s+)?"
    r"(?:attend(?:s|ed|ing)|was\s+(?:another\s+|an?\s+)?"
    r"(?:attendee|participant))(?:\s+as\s+well)?\s*$)",
    re.IGNORECASE,
)
_OUTBOUND_QUERY_FORMS = (
    (
        re.compile(
            r"^where (?:(?:does|did) (.{1,120}?) "
            r"(?:(?:currently|presently|previously|formerly) )?work|"
            r"has (.{1,120}?) worked|(?:is|was) (.{1,120}?) "
            r"(?:(?:currently|presently|previously|formerly) )?employed|"
            r"(?:has|had) (.{1,120}?) been employed|"
            r"(?:is|was) (.{1,120}?) "
            r"(?:(?:currently|presently|previously|formerly) )?working|"
            r"(?:has|had) (.{1,120}?) been working|"
            r"did (.{1,120}?) hold (?:an? )?(?:role|position|job))"
            r"(?: currently| presently| previously| formerly)?\??$",
            re.IGNORECASE,
        ),
        frozenset({"works_at"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:companies|startups|businesses|firms|funds|deals) "
            r"(?:(?:did|does) (.{1,120}?) "
            r"(?:invest in|back|fund|finance)|has (.{1,120}?) "
            r"(?:invested in|backed|funded|financed))\??$",
            re.IGNORECASE,
        ),
        frozenset({"invested_in"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:companies|startups|businesses|firms) "
            r"(?:(?:did|does) (.{1,120}?) "
            r"(?:found|establish|create|form|incorporate|launch|start|set up)|"
            r"has (.{1,120}?) (?:founded|established|created|formed|"
            r"incorporated|launched|started|set up))\??$",
            re.IGNORECASE,
        ),
        frozenset({"founded"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:companies|startups|businesses|firms) "
            r"(?:(?:did|does) (.{1,120}?) "
            r"(?:advise|counsel|mentor|guide|coach|consult (?:for|with))|"
            r"has (.{1,120}?) (?:advised|counseled|mentored|guided|coached|"
            r"consulted (?:for|with)))\??$",
            re.IGNORECASE,
        ),
        frozenset({"advises"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:meetings|sessions|events) "
            r"(?:(?:did|does) (.{1,120}?) "
            r"(?:attend|join|go to|participate in)|has (.{1,120}?) "
            r"(?:attended|joined|gone to|participated in))\??$",
            re.IGNORECASE,
        ),
        frozenset({"attended"}),
        "out",
        frozenset({"meeting"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:companies|startups|businesses|firms) "
            r"(?:was|were) (?:co[ -]?founded|founded|established|created|"
            r"formed|incorporated|launched|started) by (.{1,120}?)\??$",
            re.IGNORECASE,
        ),
        frozenset({"founded"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:companies|startups|businesses|firms|funds) "
            r"(?:was|were) (?:backed|funded|financed) by (.{1,120}?)\??$",
            re.IGNORECASE,
        ),
        frozenset({"invested_in"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:companies|startups|businesses|firms) "
            r"(?:was|were) (?:advised|counseled|mentored) by (.{1,120}?)\??$",
            re.IGNORECASE,
        ),
        frozenset({"advises"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:employers|companies|startups|businesses|firms) "
            r"(?:employed|hired) (.{1,120}?)\??$",
            re.IGNORECASE,
        ),
        frozenset({"works_at"}),
        "out",
        frozenset({"company", "fund"}),
    ),
    (
        re.compile(
            r"^(?:what|which) (?:meetings|sessions|events) "
            r"(?:was|were) attended by (.{1,120}?)\??$",
            re.IGNORECASE,
        ),
        frozenset({"attended"}),
        "out",
        frozenset({"meeting"}),
    ),
)
_INBOUND_QUERY_FORMS = (
    (
        re.compile(
            r"(?<!\w)(?:attend\w*|joined) (?:the )?<seed> "
            r"(?:meeting|session|event)",
            re.IGNORECASE,
        ),
        frozenset({"attended"}),
    ),
    (
        re.compile(
            r"(?<!\w)(?:attend\w*(?: (?:at|for|in))?|"
            r"participants? present (?:at|for)|present (?:at|for)|"
            r"present during|(?:was|were) there at|sat in on|"
            r"(?:was|were) counted as present (?:at|for)|"
            r"(?:was|were) among (?:the )?(?:attendees?|participants?) "
            r"(?:at|in|of)|took part (?:at|in|during)|"
            r"participat\w* (?:at|in)|joined in at|came along to|went to|"
            r"joined|(?:were )?in attendance at|"
            r"in the room (?:at|for|during)) (?:the )?<seed>|"
            r"(?:attend\w*|joined) (?:the )?<seed> (?:meeting|session|event)|"
            r"(?:attend\w*|participat\w* in|appear\w* at) (?:the )?"
            r"(?:meeting|session|event) <seed>|"
            r"(?:appear\w*|show(?:ed)? up) at (?:the )?<seed> "
            r"(?:meeting|session|event)|"
            r"(?:appear\w*|show(?:ed)? up) (?:at|for) (?:the )?<seed>|"
            r"(?:drop(?:ped|ping|s)?|stop(?:ped|ping|s)?) by (?:the )?<seed>|"
            r"<seed> include(?:s|d)? (?:people |individuals )?as "
            r"(?:attendees?|participants?)|"
            r"(?:attendees?|participants?) (?:at|in|of) <seed>|"
            r"<seed>(?: s)? (?:attendees?|participants?)(?!\w)",
            re.IGNORECASE,
        ),
        frozenset({"attended"}),
    ),
    (
        re.compile(
            r"(?<!\w)(?:(?:originally )?(?:help(?:s|ed|ing)? (?:to )?found|"
            r"founded|co[ -]?founded|established|created|"
            r"co[ -]?created|formed|incorporated|began|launched|originated|"
            r"initiated|started|set up) "
            r"(?:(?:the )?(?:company|startup|business|firm) )?<seed>"
            r"(?: (?:from the outset|at inception|as (?:a|the) "
            r"(?:company|startup|business|firm)))?|"
            r"(?:responsible for|credited with) "
            r"(?:founding|co[ -]?founding|establishing|creating|forming|"
            r"incorporating|launching|starting|setting up) (?:the )?<seed>|"
            r"set (?:the )?<seed> up|"
            r"got (?:the )?<seed> started|"
            r"(?:was|were) behind (?:the )?(?:creation|founding|launch) of "
            r"<seed>|(?:was|were) <seed> (?:founded|co[ -]?founded|"
            r"established|created|formed|incorporated|launched|started)"
            r"(?: by)?|"
            r"<seed> have as (?:its|their) (?:founder|founders)|"
            r"(?:founder|founders) behind <seed>|"
            r"(?:founders?|co[ -]?founders?|creators?|originators?) "
            r"of <seed>|brought (?:the )?<seed> into (?:existence|being)|"
            r"<seed>(?: s)? (?:founders?|co[ -]?founders?|creators?|"
            r"originators?))(?!\w)",
            re.IGNORECASE,
        ),
        frozenset({"founded"}),
    ),
    (
        re.compile(
            r"(?<!\w)(?:invest(?:s|ed|ing)? (?:money |capital )?in|"
            r"(?:investors?|backers?) (?:in|of)|funded|"
            r"bankroll(?:s|ed|ing)?(?: (?:the )?"
            r"(?:company|startup|business|firm))?|"
            r"help(?:s|ed|ing)? (?:to )?fund(?: (?:the )?"
            r"(?:company|startup|business|firm))?|"
            r"put (?:up )?(?:money|capital|funds?) (?:into|in|for)|"
            r"put (?:money|capital|funding|funds?) behind|"
            r"(?:supplied|provided|gave) (?:the )?money behind|"
            r"(?:furnish(?:es|ed|ing)?|inject(?:s|ed|ing)?) "
            r"(?:money|capital|funding|funds?) (?:into|in|to|for)|"
            r"made (?:an? )?(?:investments?|"
            r"(?:(?:equity|financial|monetary|venture|capital|investment) )+"
            r"(?:contributions?|investments?)) (?:to|in)|"
            r"(?:provided|supplied|gave) (?:an? |the )?"
            r"(?:(?:equity|financial|monetary|venture|investment) )*"
            r"(?:financing|backing|funding|funds?|capital|investment) "
            r"(?:to|in|for)|"
            r"(?:provided|supplied|gave) (?:an? |the )?"
            r"(?:(?:equity|financial|monetary|venture|capital|investment) )+"
            r"contributions? (?:to|in|for)|"
            r"(?:committed|contributed) (?:an? )?"
            r"(?:(?:equity|financial|monetary|venture|investment) )*"
            r"(?:funding|funds?|capital|investment) (?:to|in|for)|"
            r"(?:committed|contributed) (?:an? )?"
            r"(?:(?:equity|financial|monetary|venture|capital|investment) )+"
            r"contributions? (?:to|in|for)|"
            r"(?:purchased|acquired|bought) (?:an? )?"
            r"(?:(?:ownership|equity|financial) )?"
            r"(?:stake|interest|shares?) (?:in|of)|"
            r"took (?:an? )?(?:ownership|equity|financial) "
            r"(?:stake|interest|shares?) (?:in|of)|"
            r"took (?:an? )?(?:equity )?stakes? (?:in|of)|"
            r"financ(?:e|es|ed|ing)|wrote (?:a |the )?check (?:to|for)) "
            r"<seed>|(?:funded|back(?:s|ed|ing)?) (?:the )?"
            r"(?:company|startup|business|firm) <seed>|"
            r"(?:provided|supplied|gave) <seed> "
            r"(?:with |(?:its|their) )?"
            r"(?:(?:seed|venture|investment|equity) )?"
            r"(?:financing|backing|funding|funds?|capital)|"
            r"financ(?:e|es|ed|ing) (?:the )?"
            r"(?:company|startup|business|firm) <seed>|"
            r"invest(?:s|ed|ing)? (?:money |capital )?in (?:the )?"
            r"(?:company|startup|business|firm) <seed>|"
            r"wrote <seed> (?:a |the )?check|"
            r"wrote (?:the )?first check (?:to|for) <seed>|"
            r"(?:the )?lead investor (?:in|for|of) <seed>(?: s)? "
            r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|"
            r"series [a-z]) )?round|"
            r"led <seed>(?: s)? (?:(?:pre[ -]?seed|seed|funding|financing|"
            r"investment|series [a-z]) )?round|"
            r"(?:an? )?(?:early|initial|seed|angel|lead) "
            r"backers? (?:in|of) <seed>|"
            r"led (?:the )?(?:seed round|funding round|financing round|"
            r"investment round|series [a-z]) (?:for|in|of) <seed>|"
            r"participat(?:e|es|ed|ing) in (?:the )?"
            r"(?:funding|financing|investment) of <seed>|"
            r"<seed> rais(?:e|es|ed|ing) (?:money|capital|funds?|funding|"
            r"financing|investment) from|"
            r"(?:was|were) among <seed>(?: s)? (?:investors?|backers?)|"
            r"(?:contributed capital|provided funding) as investors? "
            r"in <seed>|participat(?:e|es|ed|ing) in <seed>(?: s)? "
            r"(?:funding|financing|investment|seed) round|"
            r"back(?:s|ed|ers?)? <seed>"
            r"(?: with (?:money|capital|funds?))?|"
            r"<seed>(?: s)? (?:investors?|backers?)(?!\w)",
            re.IGNORECASE,
        ),
        frozenset({"invested_in"}),
    ),
    (
        re.compile(
            r"(?<!\w)(?:(?:advis(?:e|es|ed|ing)|counsel(?:s|ed|ing)?|"
            r"mentor(?:s|ed|ing)?|guid(?:e|es|ed|ing)|"
            r"coach(?:es|ed|ing)?) (?:the )?"
            r"(?:company |startup |business |firm )?<seed>|"
            r"(?:(?:serves?|served|acted) as|(?:were|are)) (?:an? )?"
            r"(?:advisers?|advisors?|consultants?|counselors?) (?:to|for) "
            r"<seed>|(?:advisers?|advisors?|consultants?|counselors?) "
            r"(?:to|for) <seed>|(?:gave|giv(?:e|es|ing)|provid(?:e|es|ed|ing)|"
            r"offer(?:s|ed|ing)?) "
            r"(?:(?:expert|strategic|professional) )?"
            r"(?:advice|guidance|direction|recommendations?|input|counsel) "
            r"(?:to|for) <seed>|(?:gave|giv(?:e|es|ing)|"
            r"provid(?:e|es|ed|ing)|offer(?:s|ed|ing)?) <seed> "
            r"(?:(?:expert|strategic|professional) )?"
            r"(?:advice|guidance|direction|recommendations?|input|counsel)|"
            r"(?:was|were) retained as (?:an? )?"
            r"(?:adviser|advisor|consultant|counselor) by <seed>|"
            r"(?:was|were) <seed> advis(?:e|ed) by|"
            r"(?:was|were) (?:a |the )?sounding board for <seed>|"
            r"<seed> receiv(?:e|es|ed|ing) "
            r"(?:advice|guidance|counsel|recommendations?) from|"
            r"(?:acts?|acted|serves?|served) in (?:an? )?"
            r"advisory capacity (?:to|for) <seed>|"
            r"(?:serves?|served) <seed> in (?:an? )?advisory "
            r"(?:role|capacity)|"
            r"(?:acts?|acted) as (?:an? )?(?:mentor|consultant) to <seed>|"
            r"(?:provid(?:e|es|ed|ing)|offer(?:s|ed|ing)?) advisory services to "
            r"<seed>|consult(?:s|ed|ing)? (?:for|to|with) (?:the )?"
            r"(?:(?:company|startup|business|firm) )?<seed>|"
            r"members? of <seed>(?: s)? advisory board|"
            r"(?:the )?advisory board members? of <seed>|"
            r"(?:on )?(?:the )?advisory board of <seed>|"
            r"(?:sat|sits?|serves?|served) on <seed>(?: s)? advisory board|"
            r"(?:on )?<seed>(?: s)? advisory board|"
            r"<seed>(?: s)? (?:advisers?|advisors?|consultants?))(?!\w)",
            re.IGNORECASE,
        ),
        frozenset({"advises"}),
    ),
    (
        re.compile(
            r"(?<!\w)(?:(?:work(?:s|ed|ing)? (?:at|for)|"
            r"used to work (?:at|for)|"
            r"(?:(?:has|have|had) been|(?:was|were)) employed by|"
            r"(?:were )?employ(?:ed|ees?) "
            r"(?:at|by|of)|(?:staff|team members?|colleagues?|personnel|"
            r"employees?) (?:at|of|for)|staff employed by|"
            r"(?:role|position|team|tenure|stint) at|"
            r"(?:had|held) (?:an? )?(?:roles?|positions?|jobs?) at|"
            r"(?:occup(?:y|ies|ied|ying)) (?:an? )?"
            r"(?:roles?|positions?|jobs?) at|"
            r"held (?:an? )?(?:roles?|positions?|jobs?) with|"
            r"worked under|"
            r"(?:were )?on the payroll of|"
            r"(?:earned|drew) (?:a )?salar(?:y|ies) from|"
            r"worked professionally at) "
            r"<seed>|on (?:the )?(?:team|staff) (?:at|of) <seed>|"
            r"served on (?:the )?(?:team|staff) (?:at|of) <seed>|"
            r"(?:was|were) part of <seed>(?: s)? (?:team|staff|workforce)|"
            r"(?:made up|comprised) <seed>(?: s)? (?:team|staff|workforce)|"
            r"belong(?:s|ed|ing)? to <seed>(?: s)? (?:team|staff|workforce)|"
            r"work(?:s|ed|ing)? on <seed>(?: s)? (?:team|staff)|"
            r"join(?:s|ed|ing)? <seed>(?: s)? (?:team|staff|workforce)|"
            r"(?:was|were) on <seed>(?: s)? payroll|"
            r"<seed> employ(?:s|ed)?|<seed> have on (?:its|their) payroll|"
            r"(?:serves?|served) as (?:an? )?employee of <seed>|"
            r"join(?:s|ed|ing)? <seed>|"
            r"on <seed>(?: s)? (?:team|staff)|"
            r"members? of <seed>(?: s)? team|"
            r"<seed>(?: s)? (?:staff|team|employees?|workforce))(?!\w)",
            re.IGNORECASE,
        ),
        frozenset({"works_at"}),
    ),
)
_SEED_FIRST_QUERY_FORMS = (
    (
        re.compile(
            r"(?:by whom (?:was|were) <seed> attended|"
            r"who was <seed> attended by)",
            re.IGNORECASE,
        ),
        frozenset({"attended"}),
    ),
    (
        re.compile(
            r"(?:<seed> (?:was|were) (?:founded|co[ -]?founded|established|"
            r"created|formed|incorporated|launched|started) by whom|"
            r"who was <seed> (?:founded|co[ -]?founded|established|created|"
            r"formed|incorporated|launched|started) by)",
            re.IGNORECASE,
        ),
        frozenset({"founded"}),
    ),
    (
        re.compile(
            r"(?:(?:by whom was|who was) <seed> "
            r"(?:backed|funded|financed)(?: by)?)",
            re.IGNORECASE,
        ),
        frozenset({"invested_in"}),
    ),
    (
        re.compile(
            r"from whom did <seed> (?:rais(?:e|es)|get|receive) "
            r"(?:money|capital|funding|funds?|financing|investment)",
            re.IGNORECASE,
        ),
        frozenset({"invested_in"}),
    ),
    (
        re.compile(
            r"(?:who|whom) did <seed> (?:receive|get) "
            r"(?:money|capital|funding|funds?|financing|investment) from",
            re.IGNORECASE,
        ),
        frozenset({"invested_in"}),
    ),
    (
        re.compile(
            r"(?:from whom did <seed> (?:receive|get) "
            r"(?:advice|guidance|counsel|recommendations?)|"
            r"<seed> (?:received|got) "
            r"(?:advice|guidance|counsel|recommendations?) from whom)",
            re.IGNORECASE,
        ),
        frozenset({"advises"}),
    ),
    (
        re.compile(
            r"(?:<seed> (?:was|were) advis(?:e|ed) by whom|"
            r"by whom was <seed> (?:advised|counseled|mentored)|"
            r"who was <seed> (?:advised|counseled|mentored) by|"
            r"(?:who|whom) did <seed> consult)",
            re.IGNORECASE,
        ),
        frozenset({"advises"}),
    ),
    (
        re.compile(
            r"(?:(?:who|whom) did <seed> (?:employ|hire)|"
            r"(?:who|whom) has <seed> hired|"
            r"who was (?:employed|hired) by <seed>|"
            r"by whom was <seed> (?:staffed|served))",
            re.IGNORECASE,
        ),
        frozenset({"works_at"}),
    ),
    (
        re.compile(
            r"who (?:is|was) <seed>(?: s)? (?:ceo|cto|coo|cfo|cmo|cro|"
            r"chief [a-z]+ officer|president|director|manager)",
            re.IGNORECASE,
        ),
        frozenset({"works_at"}),
    ),
    (
        re.compile(
            r"who (?:is|was) (?:the )?"
            r"(?:ceo|cto|coo|cfo|cmo|cro|chief [a-z]+ officer|"
            r"president|director|manager) (?:at|for|of) <seed>",
            re.IGNORECASE,
        ),
        frozenset({"works_at"}),
    ),
)
_QUESTION_INTENT = re.compile(
    r"(?:\?|^\s*(?:who|which|what|where|name|list|identify|show|find|tell|"
    r"do|can|could|would|please|i|out|for|from|give)\b|"
    r"\b(?:which|what) people\b)",
    re.IGNORECASE,
)
_FALSE_INVESTMENT = re.compile(
    r"\binvest\w*\s+(?:time|effort|energy|attention)\b", re.IGNORECASE
)
_QUERY_PUNCTUATION = re.compile(r"[^\w+]+", re.UNICODE)
_EMPLOYMENT_COMPOSITE_SIDE = (
    r"(?:worked (?:at|for)|worked as (?:staff|staff members?|employees?) "
    r"(?:at|for)|(?:were )?employed (?:at|by)|served on (?:the )?staff of|"
    r"served as employees?|held (?:positions?|jobs?) at|"
    r"had employment roles? at|(?:were )?on (?:the )?payroll of|"
    r"(?:were )?members? of (?:the )?workforce|(?:were )?employees? of)"
)
_FOUNDING_COMPOSITE_SIDE = (
    r"(?:founded|helped found|co[ -]?founded|established|created|launched|"
    r"started|(?:were )?founders? of)"
)
_COMPOSITE_AFFILIATION = re.compile(
    rf"\b(?:(?:{_EMPLOYMENT_COMPOSITE_SIDE}) or "
    rf"(?:{_FOUNDING_COMPOSITE_SIDE})|"
    rf"(?:{_FOUNDING_COMPOSITE_SIDE}) or "
    rf"(?:{_EMPLOYMENT_COMPOSITE_SIDE})) <seed>|"
    rf"(?:(?:{_EMPLOYMENT_COMPOSITE_SIDE}) <seed> or "
    rf"(?:{_FOUNDING_COMPOSITE_SIDE}) <seed>|"
    rf"(?:{_FOUNDING_COMPOSITE_SIDE}) <seed> or "
    rf"(?:{_EMPLOYMENT_COMPOSITE_SIDE}) <seed>)(?!\w)",
    re.IGNORECASE,
)
_MEETING_ONLY_ATTENDANCE = re.compile(
    r"\b(?:came to|were at|was at) <seed>(?!\w)", re.IGNORECASE
)
_QUERY_COORDINATOR = re.compile(r"\b(?:and|or)\b", re.IGNORECASE)
_EXECUTIVE_QUERY_DETAIL = re.compile(
    rf"(?:who (?:is|was) <seed>(?: s)? "
    rf"(?P<seed_role>{_EXECUTIVE_ROLE_TEXT})|"
    rf"who (?:is|was) (?:the )?(?P<of_role>{_EXECUTIVE_ROLE_TEXT}) "
    rf"(?:at|for|of) <seed>)",
    re.IGNORECASE,
)
_QUERY_CLAUSE_BOUNDARY = re.compile(
    r"(?:[\n:;]+|[.!?]+|--+|[\u2014\u2013]+)\s*"
    r"(?=(?:who|which|what|where|name|list|identify|show|find|tell|"
    r"do you|can you|could you|would you|will you|by whom|please)\b)",
    re.IGNORECASE,
)
_INBOUND_WH = (
    r"(?:who(?: if anyone)?|which people|which individuals|"
    r"what people|what individuals)"
)
_INBOUND_AUXILIARY = (
    r"(?:(?:is|was)(?: (?:a|an|the))?|(?:are|were)(?: the)?|"
    r"(?:do|does|did|has|have|had)|(?:can|could) be)"
)
_INBOUND_PREAMBLE = (
    r"(?:(?:do you (?:remember|know|recall)|"
    r"(?:can|could|would|will) you (?:please )?(?:tell|show|remind) me|"
    r"(?:can|could|would|will) you (?:please )?(?:let me know|say)|"
    r"do we know|i (?:am trying to remember|cannot recall|"
    r"need to know)|out of curiosity|for reference|"
    r"please (?:tell|show|remind) me|(?:tell|show|remind) me) )?"
)
_INBOUND_QUERY_LEAD = re.compile(
    rf"(?:by whom|{_INBOUND_PREAMBLE}(?:{_INBOUND_WH}"
    rf"(?: (?:currently|presently))?(?: {_INBOUND_AUXILIARY})?|"
    rf"who (?:is|was|are|were) (?:the )?"
    rf"(?:people|person|individuals?|persons?)(?: (?:who|that))?))|"
    rf"(?:(?:can|could|would|will) you (?:please )?|please )?"
    rf"(?:name|list|identify|show|find)(?: me)?(?: all| the)?"
    rf"(?: who| (?:everyone|anyone|those|person|people|individuals|persons|"
    rf"member|members|participant|participants)"
    rf"(?: (?:who|that)(?: {_INBOUND_AUXILIARY})?)?)?|"
    rf"(?:please )?(?:tell|give) me (?:the )?names? of (?:the )?"
    rf"(?:people|persons|individuals) who|"
    rf"(?:please )?give me (?:the )?names? of",
    re.IGNORECASE,
)
_INBOUND_QUERY_TAIL = re.compile(
    r"(?:again|please|again please|please again|for me(?: please)?|"
    r"if known|if you know|if you remember|if you recall|"
    r"if any|that you know of|over time|previously|in the past|"
    r"i forget|i cannot remember|"
    r"i do not remember|i don t remember)",
    re.IGNORECASE,
)
_REQUEST_CLAUSE_LEAD = re.compile(
    r"^\s*(?:who|which|what|where|name|list|identify|show|find|tell|"
    r"do you|do we|can you|could you|would you|will you|by whom|"
    r"please|i|out of|for reference|from whom|give)\b",
    re.IGNORECASE,
)
_CONVERSATIONAL_FRAME = re.compile(
    r"^\s*(?:quick question|one question|i have (?:a|one) question|"
    r"can you help(?: me)?(?: with this)?|what a day|please listen|"
    r"who knows|do you understand|tell me something|"
    r"(?:do you (?:remember|know|recall)|"
    r"(?:can|could|would|will) you (?:please )?(?:tell|show|remind) me|"
    r"(?:can|could|would|will) you (?:please )?(?:let me know|say)|"
    r"do we know|i (?:am trying to remember|cannot recall|need to know)|"
    r"out of curiosity|for reference|"
    r"please (?:tell|show|remind) me|(?:tell|show|remind) me)|"
    r"please)[.!?:]?\s*$",
    re.IGNORECASE,
)


def _normalized_role(value: str) -> str:
    """Return one stable qualifier for an executive role."""

    normalized = " ".join(value.casefold().split())
    aliases = {
        "chief executive officer": "ceo",
        "chief technology officer": "cto",
        "chief operating officer": "coo",
        "chief financial officer": "cfo",
        "chief marketing officer": "cmo",
        "chief revenue officer": "cro",
    }
    return aliases.get(normalized, normalized.replace(" ", "_"))


def _query_qualifier(templated: str, edge_types: frozenset[str]) -> str:
    """Return a detail that must match a qualifying relationship edge."""

    if edge_types == frozenset({"works_at"}):
        match = _EXECUTIVE_QUERY_DETAIL.fullmatch(templated)
        if match is not None:
            role = match.group("seed_role") or match.group("of_role")
            return _normalized_role(role)
    if edge_types == frozenset({"advises"}) and "advisory board" in templated:
        return "advisory_board"
    if (
        edge_types == frozenset({"invested_in"})
        and re.search(r"\b(?:led|lead investor)\b", templated)
        and re.search(r"\bround\b", templated)
    ):
        return "led_round"
    return ""


def _query_temporal(templated: str, edge_types: frozenset[str]) -> str:
    """Return an explicit current or past employment constraint."""

    if edge_types != frozenset({"works_at"}):
        return ""
    if re.search(
        r"\b(?:formerly|in\s+the\s+past|previously|used\s+to)\b",
        templated,
        re.IGNORECASE,
    ):
        return "past"
    if re.search(
        r"\b(?:(?:was|were)\s+working|had\s+been\s+working)\s+"
        r"(?:at|for)\b",
        templated,
        re.IGNORECASE,
    ):
        return "past"
    if re.search(
        r"^where\s+(?:(?:was|were)\s+.{1,120}?\s+working|"
        r"had\s+.{1,120}?\s+been\s+working)\b",
        templated,
        re.IGNORECASE,
    ):
        return "past"
    current_forms = (
        r"\bdoes\b.{0,120}\bwork\b",
        r"\bwork(?:s|ing)?\s+(?:at|for)\b",
        r"\bis\b.{0,120}\bemployed\b",
        r"\bemploys\b",
        r"\b(?:currently|presently)\b",
        r"^where\s+(?:is|are)\s+.{1,120}?\s+working\b",
        r"\bdoes\s+<seed>\s+(?:employ|hire)\b",
        rf"\bwho\s+is\s+<seed>(?:\s+s)?\s+(?:{_EXECUTIVE_ROLE_TEXT})\b",
        (
            rf"\bwho\s+is\s+(?:the\s+)?(?:{_EXECUTIVE_ROLE_TEXT})\s+"
            r"(?:at|for|of)\s+<seed>(?:\s|$)"
        ),
        (
            r"\bwho\s+(?:is|are)\b.{0,80}\b"
            r"(?:employee|employees|staff|team|workforce)\b"
            r".{0,80}<seed>(?:\s|$)"
        ),
        (
            r"\bwho\s+are\s+<seed>(?:\s+s)?\s+"
            r"(?:employees|staff|team|workforce)\b"
        ),
        (
            r"\bwho\s+(?:is|are)\s+on\s+<seed>(?:\s+s)?\s+"
            r"(?:payroll|staff|team)\b"
        ),
    )
    if any(re.search(pattern, templated, re.IGNORECASE) for pattern in current_forms):
        return "current"
    unbounded_forms = (
        r"\b(?:has|have)\b.{0,120}\bworked\b",
        r"\b(?:has|have)\s+been\s+employed\b",
    )
    if any(re.search(pattern, templated, re.IGNORECASE) for pattern in unbounded_forms):
        return ""
    past_forms = (
        r"\bused\s+to\s+work\b",
        r"\b(?:formerly|previously|in\s+the\s+past)\b",
        r"^where\s+did\b.{0,120}\bwork\b",
        r"^where\s+was\b.{0,120}\bemployed\b",
        r"^where\s+had\b.{0,120}\bbeen\s+employed\b",
        r"\bwho\s+was\s+employed\b",
        rf"\bwho\s+was\s+<seed>(?:\s+s)?\s+(?:{_EXECUTIVE_ROLE_TEXT})\b",
        (
            rf"\bwho\s+was\s+(?:the\s+)?(?:{_EXECUTIVE_ROLE_TEXT})\s+"
            r"(?:at|for|of)\s+<seed>(?:\s|$)"
        ),
        r"(?<!\bhas\s)(?<!\bhave\s)\bworked\b",
    )
    if any(re.search(pattern, templated, re.IGNORECASE) for pattern in past_forms):
        return "past"
    return ""


@dataclass(frozen=True)
class RelationEdge:
    """One canonical typed edge with public provenance."""

    subject: str
    object: str
    relation: str
    source: str
    order: int
    qualifier: str = ""
    temporal: str = ""


@dataclass(frozen=True)
class RelationRequest:
    """One bounded relationship query after seed resolution."""

    seed: str
    edge_types: frozenset[str]
    direction: str
    answer_types: frozenset[str]
    qualifier: str = ""
    temporal: str = ""


@dataclass(frozen=True)
class RelationHit:
    """One answer path and the edge that supports it."""

    path: str
    seed: str
    relation: str
    direction: str
    source: str
    hop_count: int = 1
    qualifier: str = ""
    temporal: str = ""


def note_type(note: Note) -> str:
    """Return the declared type, with a conservative path fallback."""

    declared = str(note.meta.get("type", "")).strip().lower()
    if declared:
        return declared
    prefix = note.path.split("/", 1)[0].lower()
    return {
        "people": "person",
        "companies": "company",
        "funds": "fund",
        "meetings": "meeting",
    }.get(prefix, "")


def _identity_index(bundle: Bundle) -> dict[str, str | None]:
    cached = getattr(bundle, "_relationship_identity_index_cache", None)
    if cached is not None:
        return cached
    result = dict(bundle._name_index())
    bundle._relationship_identity_index_cache = result
    return result


def _query_identity_index(
    bundle: Bundle,
    include_stale: bool = False,
) -> tuple[dict[str, str | None], int]:
    """Return normalized query identities and their maximum token length."""

    cache_name = (
        "_relationship_query_identity_index_historical_cache"
        if include_stale
        else "_relationship_query_identity_index_cache"
    )
    cached = getattr(bundle, cache_name, None)
    if cached is not None:
        return cached
    candidates: dict[str, list[str]] = {}
    maximum = 0
    superseded = set(bundle.superseded_by())
    for path, note in bundle.notes.items():
        if not include_stale and (
            path in superseded or note.status() == "deprecated"
        ):
            continue
        aliases = note.meta.get("aliases", [])
        aliases = aliases if isinstance(aliases, list) else [aliases]
        basename = path.rsplit("/", 1)[-1].removesuffix(".md")
        for value in [
            basename,
            note.title,
            *[str(alias) for alias in aliases if alias],
        ]:
            key = " ".join(
                _QUERY_PUNCTUATION.sub(" ", value.casefold()).split()
            )
            if not key:
                continue
            candidates.setdefault(key, []).append(path)
            maximum = max(maximum, len(key.split()))
    identities = {
        key: paths[0] if len(set(paths)) == 1 else None
        for key, paths in candidates.items()
    }
    result = (identities, maximum)
    setattr(bundle, cache_name, result)
    return result


def _matches_complete_inbound_form(pattern: re.Pattern, text: str) -> bool:
    """Return whether a relation phrase completes one bounded question."""

    for match in pattern.finditer(text):
        lead = text[: match.start()].strip()
        tail = text[match.end() :].strip()
        if (
            (not tail or _INBOUND_QUERY_TAIL.fullmatch(tail))
            and _INBOUND_QUERY_LEAD.fullmatch(lead)
        ):
            return True
    return False


def _query_seed_candidates(
    identities: dict[str, str | None], normalized: str, maximum_words: int
) -> tuple[tuple[str | None, tuple[tuple[int, int], ...]], ...]:
    """Return entity candidates for relation-aware seed resolution."""

    mentions: list[tuple[int, int, str | None]] = []
    tokens = tuple(re.finditer(r"\S+", normalized))
    if len(tokens) > 128:
        return ()
    for start_index, start_match in enumerate(tokens):
        stop = min(len(tokens), start_index + maximum_words)
        for end_index in range(start_index + 1, stop + 1):
            value = " ".join(
                token.group(0) for token in tokens[start_index:end_index]
            )
            if value not in identities:
                continue
            mentions.append(
                (
                    start_match.start(),
                    tokens[end_index - 1].end(),
                    identities[value],
                )
            )
    mentions = [
        mention
        for mention in mentions
        if not any(
            other[0] <= mention[0]
            and mention[1] <= other[1]
            and (other[0], other[1]) != (mention[0], mention[1])
            for other in mentions
        )
    ]
    output = []
    for path in sorted({path for _, _, path in mentions}, key=lambda item: str(item)):
        path_mentions = [item for item in mentions if item[2] == path]
        maximal = [
            mention
            for mention in path_mentions
            if not any(
                other[0] <= mention[0]
                and mention[1] <= other[1]
                and (other[0], other[1]) != (mention[0], mention[1])
                for other in path_mentions
            )
        ]
        spans = tuple(sorted({(start, end) for start, end, _ in maximal}))
        if spans:
            output.append((path, spans))
    return tuple(output)


def _template_seed(normalized: str, spans: tuple[tuple[int, int], ...]) -> str:
    """Replace every maximal mention of one resolved seed."""

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.extend((normalized[cursor:start], "<seed>"))
        cursor = end
    parts.append(normalized[cursor:])
    return re.sub(r"\bthe (?=<seed>)", "", "".join(parts))


def _template_seed_variants(
    normalized: str,
    spans: tuple[tuple[int, int], ...],
) -> tuple[str, ...]:
    """Preserve relation words when an entity title is also a relation noun."""

    variants = {_template_seed(normalized, spans)}
    variants.update(_template_seed(normalized, (span,)) for span in spans)
    return tuple(sorted(variants))


def _query_clauses(cue: str) -> tuple[str, ...]:
    """Return bounded clauses that may independently contain one question."""

    clauses = tuple(
        part.strip()
        for part in _QUERY_CLAUSE_BOUNDARY.split(cue)
        if part.strip()
    )
    return clauses or (cue.strip(),)


def _parse_relationship_clause(
    bundle: Bundle,
    identities: dict[str, str | None],
    maximum_identity_words: int,
    cue: str,
) -> RelationRequest | None:
    """Parse one clause after the caller has bounded conversational framing."""

    text = " ".join(cue.strip().split())
    for pattern, edge_types, direction, answer_types in _OUTBOUND_QUERY_FORMS:
        match = pattern.fullmatch(text)
        if not match:
            continue
        identity = next(group for group in match.groups() if group is not None)
        raw = re.sub(r"^(?:the|a|an)\s+", "", identity, flags=re.IGNORECASE)
        normalized_raw = " ".join(
            _QUERY_PUNCTUATION.sub(" ", raw.casefold()).split()
        )
        seed = identities.get(normalized_raw)
        if seed and note_type(bundle.notes[seed]) == "person":
            return RelationRequest(
                seed,
                edge_types,
                direction,
                answer_types,
                temporal=_query_temporal(text, edge_types),
            )
    if not _QUESTION_INTENT.search(text):
        return None
    normalized = " ".join(_QUERY_PUNCTUATION.sub(" ", text.casefold()).split())
    if _FALSE_INVESTMENT.search(text):
        return None
    allowed = (
        frozenset({"attended"}),
        frozenset({"founded"}),
        frozenset({"invested_in"}),
        frozenset({"advises"}),
        frozenset({"works_at"}),
        frozenset({"works_at", "founded"}),
    )
    requests = set()
    for seed, spans in _query_seed_candidates(
        identities,
        normalized,
        maximum_identity_words,
    ):
        for templated in _template_seed_variants(normalized, spans):
            surface_matches = tuple(
                edge_types
                for pattern, edge_types in _INBOUND_QUERY_FORMS
                if _matches_complete_inbound_form(pattern, templated)
            ) + tuple(
                edge_types
                for pattern, edge_types in _SEED_FIRST_QUERY_FORMS
                if pattern.fullmatch(templated)
            )
            meeting_only = _matches_complete_inbound_form(
                _MEETING_ONLY_ATTENDANCE, templated
            )
            composite = _matches_complete_inbound_form(
                _COMPOSITE_AFFILIATION, templated
            )
            if seed is None:
                if surface_matches or meeting_only or composite:
                    return None
                continue
            seed_type = note_type(bundle.notes[seed])
            direct_matches = tuple(
                edge_types
                for edge_types in surface_matches
                if (
                    edge_types == frozenset({"attended"})
                    and seed_type == "meeting"
                )
                or (
                    edge_types != frozenset({"attended"})
                    and seed_type in {"company", "fund"}
                )
            )
            matched = frozenset().union(
                *direct_matches,
                (
                    frozenset({"attended"})
                    if meeting_only and seed_type == "meeting"
                    else frozenset()
                ),
                (
                    frozenset({"works_at", "founded"})
                    if composite
                    else frozenset()
                ),
            )
            coordinators = [
                item.casefold()
                for item in _QUERY_COORDINATOR.findall(templated)
            ]
            if coordinators and not (coordinators == ["or"] and composite):
                continue
            if matched not in allowed:
                continue
            if matched == frozenset({"attended"}):
                if seed_type != "meeting":
                    continue
            elif seed_type not in {"company", "fund"}:
                continue
            requests.add(
                RelationRequest(
                    seed,
                    matched,
                    "in",
                    frozenset({"person"}),
                    _query_qualifier(templated, matched),
                    _query_temporal(templated, matched),
                )
            )
    if len(requests) != 1:
        return None
    return next(iter(requests))


def parse_relationship_query(
    bundle: Bundle,
    cue: str,
    *,
    include_stale: bool = False,
) -> RelationRequest | None:
    """Recognize one bounded relation request with one unique entity seed."""

    identities, maximum_identity_words = _query_identity_index(
        bundle,
        include_stale,
    )
    requests = set()
    for clause in _query_clauses(cue):
        request = _parse_relationship_clause(
            bundle,
            identities,
            maximum_identity_words,
            clause,
        )
        if request is not None:
            requests.add(request)
        elif _REQUEST_CLAUSE_LEAD.search(clause) and not _CONVERSATIONAL_FRAME.fullmatch(
            clause
        ):
            return None
    if len(requests) != 1:
        return None
    return next(iter(requests))


def _mask_direct_quotes(text: str) -> str:
    """Mask direct quotations while preserving offsets and line breaks."""

    if not any(character in text for character in ('"', "'", "“", "‘")):
        return text
    characters = list(text)
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    escaped = [False] * len(text)
    backslashes = 0
    for position, character in enumerate(text):
        if character == "\\":
            backslashes += 1
            continue
        escaped[position] = (
            backslashes % 2 == 1 and character in string.punctuation
        )
        backslashes = 0
    closing_positions = {
        closing: [
            position
            for position, character in enumerate(text)
            if character == closing
            and not escaped[position]
            and not (
                closing == "'"
                and position + 1 < len(text)
                and text[position + 1].isalnum()
            )
        ]
        for closing in set(pairs.values())
    }
    index = 0
    while index < len(text):
        opening = text[index]
        closing_character = pairs.get(opening)
        if closing_character is None or (
            opening in {'"', "'"}
            and index > 0
            and (
                escaped[index]
                or (opening == "'" and text[index - 1] in "])")
                or (opening == "'" and text[index - 1].isalnum())
            )
        ):
            index += 1
            continue
        candidates = closing_positions[closing_character]
        candidate_index = bisect_right(candidates, index)
        if candidate_index >= len(candidates):
            index += 1
            continue
        closing = candidates[candidate_index]
        for position in range(index, closing + 1):
            if characters[position] not in "\r\n":
                characters[position] = " "
        index = closing + 1
    return "".join(characters)


def _implicit_source_lead(source: Note) -> re.Pattern | None:
    """Return a bounded leading self-reference for non-person notes."""

    source_type = note_type(source)
    if source_type in {"company", "fund"}:
        noun = r"(?:company|startup|business|firm)"
        document = r"company\s+(?:page|record)"
    elif source_type == "meeting":
        noun = r"(?:meeting|session|event|demo\s+day)"
        document = r"(?:meeting\s+record|page|record|entry)"
    else:
        return None
    return re.compile(
        rf"^\s*(?:(?P<possessive>its|the\s+(?:{noun}|{document})['’]s)|"
        rf"(?P<nominative>it|the\s+(?:{noun}|{document})))\b",
        re.IGNORECASE,
    )


def _starts_with_source_identity(source: Note, sentence: str) -> bool:
    """Return whether a sentence names the source as its leading subject."""

    pattern = rf"^\s*{_source_identity_pattern(source)}"
    if re.match(pattern, sentence) is not None:
        return True
    return any(
        value.endswith((".", "!", "?"))
        and re.match(pattern, sentence.rstrip() + value[-1]) is not None
        for value in (
            source.title,
            *(
                source.meta.get("aliases", [])
                if isinstance(source.meta.get("aliases", []), list)
                else [source.meta.get("aliases", "")]
            ),
        )
        if value
    )


def _mentions_alternate_antecedent(
    bundle: Bundle,
    identities: dict[str, str | None],
    source: Note,
    sentence: str,
) -> bool:
    """Return whether a source-led sentence also introduces another entity."""

    source_type = note_type(source)
    for link in _all_markdown_links(sentence):
        path = _resolve_markdown_target(bundle, source, link.destination)
        target = bundle.notes.get(path or "")
        target_type = note_type(target) if target is not None else ""
        if (
            source_type in {"company", "fund"}
            and target_type in {"company", "fund"}
        ) or (source_type == "meeting" and target_type == "meeting"):
            return True
    for match in _wikilink_matches(sentence):
        name = " ".join(match.group(1).casefold().split())
        target_path = identities.get(name)
        target = bundle.notes.get(target_path or "")
        if target is None or target.path == source.path:
            continue
        target_type = note_type(target)
        if (
            source_type in {"company", "fund"}
            and target_type in {"company", "fund"}
        ) or (source_type == "meeting" and target_type == "meeting"):
            return True
    remaining = _strip_links(sentence)
    remaining = re.sub(_source_identity_pattern(source), "", remaining)
    aliases = source.meta.get("aliases", [])
    aliases = aliases if isinstance(aliases, list) else [aliases]
    for value in (source.title, *aliases):
        stripped = str(value).rstrip(".!?")
        if stripped != value:
            remaining = re.sub(
                rf"^\s*{re.escape(stripped)}(?=\s|$)",
                "",
                remaining,
                count=1,
                flags=re.IGNORECASE,
            )
    implicit = _implicit_source_lead(source)
    if implicit is not None:
        remaining = implicit.sub("", remaining, count=1)
    if re.search(
        r"\b(?:acquired|bought|followed|merged\s+with|replaced|succeeded)\s+"
        r"[A-Z]",
        remaining,
    ):
        return True
    if source_type in {"company", "fund"}:
        suffix = (
            r"(?:API|Bank|Capital|Company|Corp|Corporation|Fund|Group|Holdings|"
            r"Inc|Labs|LLC|Ltd|Partners|Systems|Technologies|University|"
            r"Ventures)"
        )
    else:
        suffix = (
            r"(?:Colloquium|Conference|Demo\s+Day|Event|Forum|Meeting|Review|"
            r"Session|Summit|Workshop)"
        )
    return (
        re.search(
            rf"\b[A-Z][A-Za-z0-9'’_-]*(?:\s+[A-Z][A-Za-z0-9'’_-]*)*\s+"
            rf"{suffix}\b|\b{suffix}\b",
            remaining,
        )
        is not None
    )


def _implicit_source_safety_by_sentence(
    text: str,
    boundaries: tuple[re.Match, ...],
    source: Note,
    bundle: Bundle,
    identities: dict[str, str | None],
) -> dict[tuple[int, int], bool]:
    """Compute the non-person pronoun chain once in document order."""

    pattern = _implicit_source_lead(source)
    if pattern is None:
        return {}
    spans = []
    left = 0
    for boundary in boundaries:
        spans.append((left, boundary.start()))
        left = boundary.end()
    if left < len(text):
        spans.append((left, len(text)))

    output = {}
    chain_is_safe = False
    for left, right in spans:
        sentence = text[left:right]
        stripped = sentence.strip()
        if not stripped or re.fullmatch(r"#{1,6}\s+.*", stripped):
            continue
        starts_implicitly = pattern.match(sentence) is not None
        output[(left, right)] = chain_is_safe and starts_implicitly
        alternate = _mentions_alternate_antecedent(
            bundle,
            identities,
            source,
            sentence,
        )
        if _starts_with_source_identity(source, sentence) or (
            starts_implicitly and chain_is_safe
        ):
            chain_is_safe = not alternate
        else:
            chain_is_safe = False
    return output


def _bind_implicit_source_lead(sentence: str, source: Note) -> str:
    """Replace one already-validated leading self-reference with a marker."""

    pattern = _implicit_source_lead(source)
    if pattern is None:
        return sentence
    match = pattern.match(sentence)
    if match is None:
        return sentence
    replacement = "<source>'s" if match.group("possessive") else "<source>"
    return replacement + sentence[match.end() :]


def _bind_direct_source_object(sentence: str, source: Note) -> str:
    """Bind an unqualified source-type object in one direct target assertion."""

    if not sentence.lstrip().startswith(("<target>", "<anchor>")):
        return sentence
    source_type = note_type(source)
    if source_type in {"company", "fund"}:
        noun = r"(?:company|startup|business|firm)"
    elif source_type == "meeting":
        noun = r"(?:meeting|session|event|demo\s+day)"
    else:
        return sentence
    return re.sub(
        rf"\b(?:the\s+)?{noun}\s*$",
        "<source>",
        sentence,
        count=1,
        flags=re.IGNORECASE,
    )


def _bind_person_shorthand(
    sentence: str,
    source: Note,
    established_safely: bool,
) -> str:
    """Bind a first-name subject after the note has established its full name."""

    if note_type(source) != "person":
        return sentence
    if re.search(_source_identity_pattern(source), sentence, re.IGNORECASE):
        return sentence
    words = source.title.split()
    if len(words) < 2:
        return sentence
    match = re.match(rf"^\s*{re.escape(words[0])}\s+", sentence, re.IGNORECASE)
    if match is None or _GAPPED_RELATION_PREDICATE.match(
        sentence[match.end() :]
    ) is None:
        return sentence
    if not established_safely:
        return sentence
    return "<source> " + sentence[match.end() :]


def _mentions_linked_person(
    sentence: str,
    source: Note,
    bundle: Bundle,
    identities: dict[str, str | None],
) -> bool:
    """Return whether a sentence links another possible person antecedent."""

    paths = []
    for link in _all_markdown_links(sentence):
        paths.append(_resolve_markdown_target(bundle, source, link.destination))
    for match in _wikilink_matches(sentence):
        paths.append(_resolve_wikilink_target(source, match.group(1), identities))
    return any(
        path != source.path
        and path in bundle.notes
        and note_type(bundle.notes[path]) == "person"
        for path in paths
    )


def _person_subject_antecedent_is_safe(
    previous_sentence: str,
    source: Note,
    bundle: Bundle,
    identities: dict[str, str | None],
) -> bool:
    """Return whether a subject pronoun can refer to the source person."""

    if note_type(source) != "person" or not previous_sentence.strip():
        return False
    identity = _source_identity_pattern(source)
    source_match = re.match(rf"^\s*{identity}\b", previous_sentence, re.IGNORECASE)
    if source_match is None:
        return False
    if _mentions_linked_person(previous_sentence, source, bundle, identities):
        return False
    remainder = _strip_links(previous_sentence[source_match.end() :])
    for match in re.finditer(r"\b[A-Z][A-Za-z'’_-]*\b", remainder):
        name = " ".join(match.group(0).casefold().split())
        if name in {"a", "an", "the"}:
            continue
        path = identities.get(name)
        if path is not None and note_type(bundle.notes[path]) != "person":
            continue
        prefix = remainder[: match.start()]
        if re.search(
            r"\b(?:drove|flew|moved|relocated|returned|travelled|traveled|went)"
            r"\s+(?:back\s+)?(?:from|through|to)\s+(?:the\s+)?$",
            prefix,
            re.IGNORECASE,
        ):
            continue
        return False
    return not (
        _PERSON_ANTECEDENT_INTRODUCTION.search(remainder)
        or re.search(
            r"\b[A-Z][A-Za-z'’_-]+(?:\s+[A-Z][A-Za-z'’_-]+)+\b",
            remainder,
        )
    )


def _bind_person_subject_pronoun(
    sentence: str,
    source: Note,
    antecedent_is_safe: bool,
) -> str:
    """Bind one sentence-leading pronoun to a validated antecedent."""

    if note_type(source) != "person" or not antecedent_is_safe:
        return sentence
    pronoun = re.match(r"^\s*(?:he|she|they)\s+", sentence, re.IGNORECASE)
    if pronoun is None:
        return sentence
    if _GAPPED_RELATION_PREDICATE.match(sentence[pronoun.end() :]) is None:
        return sentence
    return "<source> " + sentence[pronoun.end() :]


def _bind_labeled_person_subject_pronoun(
    sentence: str,
    source: Note,
) -> str:
    """Bind a pronoun after a source-name label and colon."""

    if note_type(source) != "person":
        return sentence
    labeled = re.match(
        rf"^\s*{_source_identity_pattern(source)}\s*:\s*"
        r"(?:he|she|they)\s+",
        sentence,
        re.IGNORECASE,
    )
    if labeled is None or _GAPPED_RELATION_PREDICATE.match(
        sentence[labeled.end() :]
    ) is None:
        return sentence
    return "<source> " + sentence[labeled.end() :]


def _bind_person_object_pronoun(
    sentence: str,
    source: Note,
    antecedent_is_safe: bool,
) -> str:
    """Bind a tight passive object pronoun on a person note."""

    if note_type(source) != "person":
        return sentence
    pattern = re.compile(
        r"^(?:<target>|<anchor>)\s+(?:(?:was\s+)?(?:co[ -]?founded|founded|"
        r"established|created|incorporated|launched|started|backed|funded|"
        r"financed|advised|counseled|mentored|attended)\s+by|"
        r"(?:employs?|employed|hired))\s+(?:him|her|them)\s*$",
        re.IGNORECASE,
    )
    if pattern.fullmatch(sentence.strip()) is None or not antecedent_is_safe:
        return sentence
    return re.sub(r"\b(?:him|her|them)\b", "<source>", sentence, flags=re.IGNORECASE)


def _is_hard_line_break(line: str) -> bool:
    """Return whether a source line ends in a CommonMark hard break."""

    if line.endswith("  "):
        return True
    stripped = line.rstrip(" \t")
    backslashes = len(stripped) - len(stripped.rstrip("\\"))
    return bool(backslashes % 2)


def _remove_hard_break_backslash(text: str) -> str:
    """Remove the non-rendered slash from a CommonMark hard line break."""

    def replace(match: re.Match) -> str:
        slashes = match.group("slashes")
        if len(slashes) % 2:
            return slashes[:-1] + " "
        return match.group(0)

    return re.sub(
        r"(?P<slashes>\\+)[ \t]*\r?\n",
        replace,
        text,
    )


def _mask_hard_break_backslash(text: str) -> str:
    """Mask a rendered hard-break slash while preserving every offset."""

    def replace(match: re.Match) -> str:
        slashes = match.group("slashes")
        if len(slashes) % 2:
            slashes = slashes[:-1] + " "
        return slashes + match.group("spacing") + match.group("ending")

    return re.sub(
        r"(?P<slashes>\\+)(?P<spacing>[ \t]*)(?P<ending>\r?\n)",
        replace,
        text,
    )


def _hard_break_continues_scope(line: str, source: Note | None) -> bool:
    """Preserve only a syntactically incomplete assertion."""

    visible = _classification_prose(line.rstrip(" \t\\"))
    attribution = _NON_ASSERTIVE_ATTRIBUTION.match(visible)
    if attribution is None:
        attribution = _PASSIVE_ATTRIBUTION_LEAD.match(visible)
    if attribution is not None:
        remainder = visible[attribution.end() :]
        relation_patterns = (
            _FOUNDED,
            _INVESTED,
            _ADVISES,
            _WORKS_AT,
            _ATTENDANCE_LANGUAGE,
        )
        if not any(pattern.search(remainder) for pattern in relation_patterns):
            return True
    if source is None:
        return False
    if (
        re.fullmatch(
            rf"\s*{_source_identity_pattern(source)}\s*",
            visible,
            re.IGNORECASE,
        )
        is not None
    ):
        return True
    source_type = note_type(source)
    if source_type == "person":
        patterns = (
            _PERSON_TO_ORG_FOUNDED,
            _PERSON_TO_ORG_INVESTED,
            _PERSON_TO_ORG_ADVISES,
            _PERSON_TO_ORG_WORKS_AT,
            _PERSON_TO_MEETING_ATTENDED,
        )
    elif source_type in {"company", "fund"}:
        patterns = (
            _ORG_TO_PERSON_FOUNDED,
            _ORG_TO_PERSON_INVESTED,
            _ORG_TO_PERSON_ADVISES,
            _ORG_TO_PERSON_WORKS_AT,
        )
    elif source_type == "meeting":
        patterns = (_MEETING_TO_PERSON_ATTENDED,)
    else:
        return False
    marked = _replace_link_markup(
        line.rstrip(" \t\\"),
        replacement="<target>",
    )
    bound = re.sub(
        _source_identity_pattern(source),
        "<source>",
        marked,
        flags=re.IGNORECASE,
    ).strip()
    if any(pattern.fullmatch(bound) for pattern in patterns):
        return False
    candidates = []
    if "<target>" not in bound:
        candidates.append(f"{bound} <target>")
    if "<target>" in bound and "<source>" not in bound:
        candidates.append(f"{bound} <source>")
    return any(
        pattern.fullmatch(candidate)
        for candidate in candidates
        for pattern in patterns
    )


def _is_paragraph_line_break(
    text: str,
    index: int,
    source: Note | None = None,
) -> bool:
    """Return whether one newline continues one semantic sentence."""

    line_start = text.rfind("\n", 0, index) + 1
    current = text[line_start:index].rstrip("\r")
    next_end = text.find("\n", index + 1)
    if next_end < 0:
        next_end = len(text)
    following = text[index + 1 : next_end].rstrip("\r")
    if not current.strip() or not following.strip():
        return False
    if (
        _MARKDOWN_BLOCK_LINE.match(current)
        or _MARKDOWN_BLOCK_LINE.match(following)
    ):
        return False
    if _is_hard_line_break(current):
        return _hard_break_continues_scope(current, source)
    return True


def _sentence_boundaries(text: str, source: Note | None = None) -> tuple[re.Match, ...]:
    """Return prose boundaries while ignoring punctuation inside links."""

    protected = sorted(
        [
            (link.start, link.end)
            for link in _all_markdown_links(text)
        ]
        + [match.span() for match in _wikilink_matches(text)]
    )
    source_spans = (
        tuple(
            match.span()
            for match in re.finditer(_source_identity_pattern(source), text)
        )
        if source is not None
        else ()
    )
    output = []
    span_index = 0
    source_span_index = 0
    for boundary in _BOUNDARY.finditer(text):
        if boundary.group(0) == "\n" and _is_paragraph_line_break(
            text,
            boundary.start(),
            source,
        ):
            continue
        while (
            span_index < len(protected)
            and protected[span_index][1] <= boundary.start()
        ):
            span_index += 1
        if span_index < len(protected):
            protected_start, protected_end = protected[span_index]
            if protected_start <= boundary.start() < protected_end:
                continue
        if boundary.group(0) == ".":
            next_match = _NONSPACE.search(text, boundary.end())
            next_character = next_match.group(0) if next_match is not None else ""
            while (
                source_span_index < len(source_spans)
                and source_spans[source_span_index][1] <= boundary.start()
            ):
                source_span_index += 1
            in_source = None
            if source_span_index < len(source_spans):
                source_start, source_end = source_spans[source_span_index]
                if source_start <= boundary.start() < source_end:
                    in_source = (source_start, source_end)
            if in_source is not None and (
                boundary.start() < in_source[1] - 1
                or next_character.islower()
            ):
                continue
            abbreviation = re.search(
                r"\b(?:Dr|Mr|Mrs|Ms|Prof|Rep|Sen|St)$",
                text[max(0, boundary.start() - 5) : boundary.start()],
                re.IGNORECASE,
            )
            if abbreviation is not None and next_character.isupper():
                continue
            if (
                boundary.start() > 0
                and boundary.end() < len(text)
                and text[boundary.start() - 1].isdigit()
                and text[boundary.end()].isdigit()
            ):
                continue
        output.append(boundary)
    return tuple(output)


@dataclass(frozen=True)
class _LinkContext:
    """Local prose and sentence-level qualifiers for one endpoint."""

    local: str
    attributed: bool
    coordinated: tuple[str, ...]
    inherited_nonassertive: bool = False
    interrogative: bool = False
    employment_ended: bool = False


def _replace_link_markup(
    text: str,
    *,
    preserve_labels: bool = False,
    replacement: str = "",
) -> str:
    """Replace Markdown and wiki link markup in one offset-safe pass."""

    replacements = [
        (
            link.start,
            link.end,
            link.label if preserve_labels else replacement,
        )
        for link in _all_markdown_links(text)
    ]
    replacements.extend(
        (
            match.start(),
            match.end(),
            match.group(1) if preserve_labels else replacement,
        )
        for match in _wikilink_matches(text)
    )
    parts = []
    cursor = 0
    for start, end, rendered in sorted(replacements):
        if start < cursor:
            continue
        parts.extend((text[cursor:start], rendered))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _strip_links(text: str) -> str:
    return _replace_link_markup(text)


def _link_labels(text: str) -> str:
    """Replace link markup with visible labels for prose classification."""

    return _replace_link_markup(text, preserve_labels=True)


def _is_punctuation(character: str) -> bool:
    return bool(character) and unicodedata.category(character).startswith("P")


def _strip_emphasis_delimiters(text: str) -> str:
    """Remove paired CommonMark-like emphasis runs without joining words."""

    characters = list(text)
    delimiters: list[dict[str, object]] = []
    index = 0
    while index < len(text):
        marker = text[index]
        if marker not in "*_" or _is_escaped(text, index):
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] == marker:
            end += 1
        previous = text[index - 1] if index else "\n"
        following = text[end] if end < len(text) else "\n"
        left_flanking = not following.isspace() and (
            not _is_punctuation(following)
            or previous.isspace()
            or _is_punctuation(previous)
        )
        right_flanking = not previous.isspace() and (
            not _is_punctuation(previous)
            or following.isspace()
            or _is_punctuation(following)
        )
        can_open = left_flanking
        can_close = right_flanking
        if marker == "_":
            can_open = can_open and (not right_flanking or _is_punctuation(previous))
            can_close = can_close and (not left_flanking or _is_punctuation(following))
        delimiters.append(
            {
                "marker": marker,
                "start": index,
                "end": end,
                "remaining": end - index,
                "can_open": can_open,
                "can_close": can_close,
            }
        )
        index = end

    openers: dict[str, list[dict[str, object]]] = {"*": [], "_": []}
    for delimiter in delimiters:
        marker = str(delimiter["marker"])
        if delimiter["can_close"] and openers[marker]:
            opener = openers[marker][-1]
            width = min(int(opener["remaining"]), int(delimiter["remaining"]))
            opener_end = int(opener["end"])
            closing_start = int(delimiter["start"])
            for position in range(opener_end - width, opener_end):
                characters[position] = ""
            for position in range(closing_start, closing_start + width):
                characters[position] = ""
            opener["remaining"] = int(opener["remaining"]) - width
            delimiter["remaining"] = int(delimiter["remaining"]) - width
            if not opener["remaining"]:
                openers[marker].pop()
        if delimiter["can_open"] and delimiter["remaining"]:
            openers[marker].append(delimiter)
    return "".join(characters)


def _leading_block_marker_end(text: str, start: int) -> int | None:
    """Return the end of one block marker at *start*, including its spacing."""

    if start >= len(text):
        return None
    cursor = start
    if text[cursor] == "#":
        while cursor < len(text) and text[cursor] == "#":
            cursor += 1
        if 1 <= cursor - start <= 6 and (
            cursor == len(text) or text[cursor].isspace()
        ):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            return cursor
        return None
    if text[cursor] in "-+*":
        cursor += 1
    elif text[cursor].isdecimal():
        while cursor < len(text) and text[cursor].isdecimal():
            cursor += 1
        if cursor - start > 9 or cursor == len(text) or text[cursor] not in ".)":
            return None
        cursor += 1
    else:
        return None
    if cursor == len(text) or not text[cursor].isspace():
        return None
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _strip_leading_markdown_block_markers(text: str) -> str:
    """Remove consecutive leading block markers without repeated slicing."""

    cursor = 0
    consumed = False
    while True:
        marker_start = cursor
        while marker_start < len(text) and text[marker_start].isspace():
            marker_start += 1
        marker_end = _leading_block_marker_end(text, marker_start)
        if marker_end is None:
            return text[marker_start:] if consumed else text
        consumed = True
        cursor = marker_end


def _strip_leading_markdown_prose_syntax(text: str) -> str:
    """Remove alternating block and unmatched emphasis syntax in one scan."""

    cursor = 0
    preserved_whitespace: list[str] = []
    prefer_emphasis = True
    while True:
        whitespace_start = cursor
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        whitespace = text[whitespace_start:cursor]
        marker_end = (
            None
            if prefer_emphasis
            and cursor < len(text)
            and text[cursor] in "*_"
            else _leading_block_marker_end(text, cursor)
        )
        if marker_end is not None:
            preserved_whitespace.clear()
            cursor = marker_end
            prefer_emphasis = True
            continue
        if (
            cursor < len(text)
            and text[cursor] in "*_"
            and not _is_escaped(text, cursor)
        ):
            marker = text[cursor]
            preserved_whitespace.append(whitespace)
            cursor += 1
            while cursor < len(text) and text[cursor] == marker:
                cursor += 1
            prefer_emphasis = False
            continue
        return "".join((*preserved_whitespace, whitespace, text[cursor:]))


def _classification_prose(text: str) -> str:
    """Return prose without leading block or emphasis syntax."""

    visible = _link_labels(text)
    visible = _strip_leading_markdown_block_markers(visible)
    visible = _strip_emphasis_delimiters(visible)
    visible = _strip_leading_markdown_prose_syntax(visible)
    return re.sub(r"(?<!\\)(?:\*+|_+)(\s*)$", r"\1", visible)


_POST_RELATION_PURPOSE = re.compile(
    r"(?<=<target>)(?:\s*,)?\s+(?:in\s+order\s+to|so\s+as\s+to|to)\b",
    re.IGNORECASE,
)


def _relation_assertion_scope(text: str) -> str:
    """Exclude a purpose clause that follows an asserted endpoint."""

    purpose = _POST_RELATION_PURPOSE.search(text)
    return text[: purpose.start()] if purpose is not None else text


def _replace_links(
    sentence: str,
    left: int,
    occurrences: list[tuple[int, int, str | None]],
    current: tuple[int, int, str | None],
) -> str:
    """Replace links without changing which endpoint is under inspection."""

    parts = []
    cursor = 0
    for occurrence in occurrences:
        start, end, _ = occurrence
        local_start = start - left
        local_end = end - left
        parts.append(sentence[cursor:local_start])
        parts.append("<target>" if occurrence == current else "<anchor>")
        cursor = local_end
    parts.append(sentence[cursor:])
    return "".join(parts)


def _segment_starts_with_source(source: Note, segment: str) -> bool:
    return (
        re.match(
            rf"^\s*{_source_identity_pattern(source)}"
            r"(?!['’])(?=$|[\s,;:])",
            segment,
            re.IGNORECASE,
        )
        is not None
    )


_LIST_MEMBER_MODIFIER = re.compile(
    r"(?:as\s+(?:an?\s+|the\s+)?[A-Za-z][\w-]*"
    r"(?:\s+[A-Za-z][\w-]*){0,4}|"
    r"\([A-Za-z][A-Za-z0-9 /&-]{0,48}\))",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d{1,9}[.)])\s+")


def _is_modified_list_member(segment: str) -> bool:
    """Return whether a segment contains only a link and its role modifier."""

    modifier = _strip_links(segment).strip()
    return (
        _LIST_MEMBER_MODIFIER.fullmatch(modifier) is not None
        and _ATTENDANCE_LANGUAGE.search(modifier) is None
    )


def _compact_context_view(
    text: str,
    offsets: list[int],
) -> tuple[str, dict[int, int]]:
    """Collapse whitespace once and map selected source offsets."""

    requested = sorted(set(offsets))
    mapped: dict[int, int] = {}
    parts: list[str] = []
    requested_index = 0
    whitespace_pending = False
    for index, character in enumerate(text):
        if character.isspace():
            while (
                requested_index < len(requested)
                and requested[requested_index] == index
            ):
                mapped[requested[requested_index]] = len(parts)
                requested_index += 1
            whitespace_pending = True
            continue
        if whitespace_pending and parts:
            parts.append(" ")
        whitespace_pending = False
        while (
            requested_index < len(requested)
            and requested[requested_index] == index
        ):
            mapped[requested[requested_index]] = len(parts)
            requested_index += 1
        parts.append(character)
    while requested_index < len(requested):
        mapped[requested[requested_index]] = len(parts)
        requested_index += 1
    return "".join(parts), mapped


def _nearest_marked_segment(marked: list[int], current: int) -> int | None:
    """Return the nearest marked segment without copying the marked set."""

    if not marked:
        return None
    insertion = bisect_left(marked, current)
    candidates = []
    if insertion:
        candidates.append(marked[insertion - 1])
    if insertion < len(marked):
        candidates.append(marked[insertion])
    return min(
        candidates,
        key=lambda index: (abs(index - current), index > current, index),
    )


def _contexts_for_sentence(
    text: str,
    left: int,
    right: int,
    occurrences: list[tuple[int, int, str | None]],
    source: Note,
    implicit_source_is_safe: bool,
    person_shorthand_is_safe: bool,
    person_object_pronoun_is_safe: bool,
    previous_sentence: str,
    bundle: Bundle,
    identities: dict[str, str | None],
) -> dict[tuple[int, int], _LinkContext]:
    """Build every link context in one sentence in linearithmic time."""

    sentence = _mask_hard_break_backslash(text[left:right])
    local_occurrences = [
        (start - left, end - left) for start, end, _ in occurrences
    ]
    segment_boundaries = []
    occurrence_index = 0
    for boundary in _RELATION_SEGMENT_BOUNDARY.finditer(sentence):
        while (
            occurrence_index < len(local_occurrences)
            and local_occurrences[occurrence_index][1] <= boundary.start()
        ):
            occurrence_index += 1
        inside_link = (
            occurrence_index < len(local_occurrences)
            and local_occurrences[occurrence_index][0]
            <= boundary.start()
            < local_occurrences[occurrence_index][1]
        )
        if not inside_link:
            segment_boundaries.append(boundary)

    segments: list[tuple[int, int, str]] = []
    separators: list[str] = []
    cursor = 0
    for boundary in segment_boundaries:
        segments.append((cursor, boundary.start(), sentence[cursor:boundary.start()]))
        separators.append(boundary.group(0))
        cursor = boundary.end()
    segments.append((cursor, len(sentence), sentence[cursor:]))
    segment_starts = [start for start, _, _ in segments]
    context_offsets: list[list[int]] = [[] for _ in segments]
    for local_start, local_end in local_occurrences:
        segment_index = max(
            0,
            bisect_right(segment_starts, local_start) - 1,
        )
        segment_start, segment_end, _ = segments[segment_index]
        if local_end <= segment_end:
            context_offsets[segment_index].extend(
                (local_start - segment_start, local_end - segment_start)
            )
    context_views = [
        _compact_context_view(segment, offsets)
        for (_, _, segment), offsets in zip(
            segments,
            context_offsets,
            strict=True,
        )
    ]
    segment_content_bounds = []
    for _, _, segment in segments:
        first = _NONSPACE.search(segment)
        content_start = first.start() if first is not None else len(segment)
        content_end = len(segment)
        while content_end > content_start and segment[content_end - 1].isspace():
            content_end -= 1
        segment_content_bounds.append((content_start, content_end))
    classification_segments = [
        _classification_prose(segment) for _, _, segment in segments
    ]
    classification_sentence = (
        classification_segments[0]
        if len(classification_segments) == 1
        else _classification_prose(sentence)
    )
    punctuated_attribution = (
        _PUNCTUATED_PASSIVE_ATTRIBUTION.search(classification_sentence)
        is not None
    )
    segment_attributed = []
    for segment in classification_segments:
        attribution = _NON_ASSERTIVE_ATTRIBUTION.search(segment)
        wrote_check = _WROTE_CHECK_ASSERTION.search(segment)
        direct_wrote_check = (
            attribution is not None
            and wrote_check is not None
            and attribution.start() <= wrote_check.start() < attribution.end()
        )
        segment_attributed.append(
            attribution is not None and not direct_wrote_check
        )
    if punctuated_attribution and segment_attributed:
        segment_attributed[0] = True
    attributed_through = []
    attribution_active = False
    for index, attributed in enumerate(segment_attributed):
        if index > 0 and re.search(
            r";|\b(?:but|whereas|while)\b",
            separators[index - 1],
            re.IGNORECASE,
        ):
            attribution_active = False
        attribution_active = attribution_active or attributed
        attributed_through.append(attribution_active)
    attributed_from_right = [False] * len(segments)
    attribution_active = False
    for index in range(len(segments) - 1, -1, -1):
        if index < len(separators) and re.search(
            r";|\b(?:but|whereas|while)\b",
            separators[index],
            re.IGNORECASE,
        ):
            attribution_active = False
        attribution_active = attribution_active or segment_attributed[index]
        attributed_from_right[index] = attribution_active
    inherited_nonassertive = []
    nonassertive_active = False
    nonassertive_lead_pending = False
    requires_base_form = False
    for index, (_, _, segment) in enumerate(segments):
        contrast = index > 0 and re.search(
            r";|\b(?:but|whereas|while)\b",
            separators[index - 1],
            re.IGNORECASE,
        )
        source_subject = _segment_has_source_subject(
            source,
            segment,
            allow_implicit=True,
        )
        if index > 0 and (
            contrast or (source_subject and not nonassertive_lead_pending)
        ):
            nonassertive_active = False
            nonassertive_lead_pending = False
            requires_base_form = False
        normalized = _ASSERTIVE_CERTAINTY.sub(
            "",
            _ASSERTIVE_NOT.sub("", classification_segments[index]),
        )
        normalized_scope = _relation_assertion_scope(
            _replace_link_markup(normalized, replacement="<target>")
        )
        lead_segment = (
            index == 0
            and _NON_ASSERTIVE_LEAD_SEGMENT.fullmatch(normalized) is not None
        )
        local_nonassertive = (
            _NON_ASSERTIVE_RELATION_CONTEXT.search(normalized_scope) is not None
        )
        if nonassertive_active and not local_nonassertive:
            lead_scope = nonassertive_lead_pending
            continues_scope = lead_scope or (
                _GAPPED_RELATION_PREDICATE.match(normalized.strip()) is not None
            )
            if requires_base_form:
                continues_scope = (
                    _BASE_COORDINATED_RELATION.match(normalized) is not None
                )
            if lead_scope and source_subject:
                nonassertive_lead_pending = False
            if not continues_scope:
                nonassertive_active = False
                requires_base_form = False
        inherited_nonassertive.append(
            nonassertive_active and not local_nonassertive
        )
        if local_nonassertive:
            nonassertive_active = True
            nonassertive_lead_pending = lead_segment
            requires_base_form = _DO_NEGATION.search(normalized) is not None

    relation_patterns = (
        _FOUNDED,
        _INVESTED,
        _ADVISES,
        _RECEIVED_ADVICE_FROM,
        _GAVE_ADVICE_TO,
        _WORKS_AT,
        _ATTENDANCE_LANGUAGE,
    )
    marked = [
        index
        for index, (_, _, segment) in enumerate(segments)
        if not _is_modified_list_member(segment)
        and any(pattern.search(segment) for pattern in relation_patterns)
    ]
    marked_set = set(marked)
    barriers = [
        not (
            re.fullmatch(
                r"(?:also|too|as\s+well)?",
                _strip_links(segment).strip(),
                re.IGNORECASE,
            )
            is not None
            or _is_modified_list_member(segment)
        )
        for _, _, segment in segments
    ]
    barrier_prefix = [0]
    for barrier in barriers:
        barrier_prefix.append(barrier_prefix[-1] + int(barrier))

    carry_before = []
    source_carries = False
    for index, (_, _, segment) in enumerate(segments):
        carry_before.append(source_carries)
        without_links = _strip_links(segment).strip()
        if not without_links:
            continue
        if _segment_has_source_subject(
            source,
            segment,
            allow_implicit=index == 0 and implicit_source_is_safe,
        ):
            source_carries = True
            continue
        contrast_follows = index < len(separators) and re.search(
            r"\b(?:but|whereas|while)\b",
            separators[index],
            re.IGNORECASE,
        )
        if contrast_follows and _segment_starts_with_source(source, segment):
            source_carries = True
        elif not _GAPPED_RELATION_PREDICATE.match(without_links):
            source_carries = False

    output = {}
    antecedent_safety: dict[str, bool] = {}
    anchor_contexts: dict[int, str] = {}
    targets_by_segment: list[set[str]] = [set() for _ in segments]
    for candidate, (candidate_start, candidate_end) in zip(
        occurrences,
        local_occurrences,
        strict=True,
    ):
        if candidate[2] is None:
            continue
        segment_index = max(
            0,
            bisect_right(segment_starts, candidate_start) - 1,
        )
        if candidate_end <= segments[segment_index][1]:
            targets_by_segment[segment_index].add(candidate[2])
    for occurrence, (local_start, local_end) in zip(
        occurrences,
        local_occurrences,
        strict=True,
    ):
        if occurrence[2] is None:
            continue
        current = max(0, bisect_right(segment_starts, local_start) - 1)
        current_start, current_end, current_text = segments[current]
        if local_end > current_end:
            current = 0
            current_start, _, current_text = segments[current]
        relative_start = local_start - current_start
        relative_end = local_end - current_start
        content_start, content_end = segment_content_bounds[current]
        no_content_before = relative_start <= content_start
        no_content_after = relative_end >= content_end
        before_tail = current_text[
            max(content_start, relative_start - 160) : relative_start
        ].strip()
        after_length = max(0, content_end - relative_end)
        after_link = (
            current_text[relative_end:content_end].strip()
            if after_length <= 80
            else ""
        )
        current_is_bare_link = no_content_before and no_content_after
        current_is_list_head = (
            no_content_after
            and re.search(
                r"\b(?:lists?|names?|identifies?|records?|states?|notes?|reports?)"
                r"(?:\s+that)?\s*$",
                before_tail,
                re.IGNORECASE,
            )
            is not None
        )
        current_is_modified_member = (
            no_content_before
            and after_length <= 80
            and _LIST_MEMBER_MODIFIER.fullmatch(after_link) is not None
        )
        may_inherit = (
            current_is_bare_link
            or current_is_list_head
            or current_is_modified_member
        )
        current_has_relation = (
            current in marked_set and not current_is_modified_member
        )
        if current_has_relation or not marked or not may_inherit:
            chosen = current
        else:
            candidate = _nearest_marked_segment(marked, current)
            assert candidate is not None
            low, high = sorted((candidate, current))
            barrier_count = barrier_prefix[high] - barrier_prefix[low + 1]
            chosen = current if barrier_count else candidate

        chosen_start, _, chosen_text = segments[chosen]
        if chosen == current:
            compact_text, compact_offsets = context_views[chosen]
            raw_relative_start = local_start - chosen_start
            raw_relative_end = local_end - chosen_start
            if (
                raw_relative_start not in compact_offsets
                or raw_relative_end not in compact_offsets
            ):
                compact_text, compact_offsets = _compact_context_view(
                    chosen_text,
                    [raw_relative_start, raw_relative_end],
                )
            relative_start = compact_offsets[raw_relative_start]
            relative_end = compact_offsets[raw_relative_end]
            radius = _MAX_RELATION_CONTEXT_CHARS // 2
            context_start = max(0, relative_start - radius)
            context_end = min(len(compact_text), relative_end + radius)
            local_context = (
                compact_text[context_start:relative_start]
                + "<target>"
                + compact_text[relative_end:context_end]
            )
        else:
            if chosen not in anchor_contexts:
                anchor_context = _replace_link_markup(
                    chosen_text,
                    replacement="<anchor>",
                )
                anchor_context = " ".join(anchor_context.split())
                if len(anchor_context) > _MAX_RELATION_CONTEXT_CHARS:
                    relation_match = min(
                        (
                            match
                            for pattern in relation_patterns
                            if (match := pattern.search(anchor_context)) is not None
                        ),
                        key=lambda match: match.start(),
                        default=None,
                    )
                    focus = relation_match.start() if relation_match else 0
                    radius = _MAX_RELATION_CONTEXT_CHARS // 2
                    anchor_context = anchor_context[
                        max(0, focus - radius) : focus + radius
                    ]
                anchor_contexts[chosen] = anchor_context
            local_context = anchor_contexts[chosen]
        local_context = _remove_hard_break_backslash(local_context)
        if (
            chosen_start
            and carry_before[chosen]
            and _GAPPED_RELATION_PREDICATE.match(local_context.strip())
        ):
            local_context = "<source> " + local_context
        elif implicit_source_is_safe:
            local_context = _bind_implicit_source_lead(local_context, source)
        local_context = _bind_direct_source_object(local_context, source)
        local_context = _bind_labeled_person_subject_pronoun(
            local_context,
            source,
        )
        subject_antecedent = previous_sentence
        if (
            chosen > 0
            and carry_before[chosen]
            and re.search(
                r"\b(?:but|whereas|while)\b",
                separators[chosen - 1],
                re.IGNORECASE,
            )
        ):
            _, _, prior_text = segments[chosen - 1]
            prior_targets = targets_by_segment[chosen - 1]
            if not prior_targets or prior_targets == {occurrence[2]}:
                subject_antecedent = prior_text
        if subject_antecedent not in antecedent_safety:
            antecedent_safety[subject_antecedent] = (
                _person_subject_antecedent_is_safe(
                    subject_antecedent,
                    source,
                    bundle,
                    identities,
                )
            )
        local_context = _bind_person_subject_pronoun(
            local_context,
            source,
            antecedent_safety[subject_antecedent],
        )
        local_context = _bind_person_shorthand(
            local_context,
            source,
            person_shorthand_is_safe,
        )
        local_context = _bind_person_object_pronoun(
            local_context,
            source,
            person_object_pronoun_is_safe,
        )

        coordinated = ()
        if len(occurrences) == 1:
            sentence_context = _replace_links(
                sentence,
                left,
                occurrences,
                occurrence,
            )
            bound_sentence = re.sub(
                _source_identity_pattern(source),
                "<source>",
                sentence_context,
            )
            coordinated = _coordinated_relations(
                bound_sentence,
                note_type(source),
            )
        employment_ended = False
        if chosen + 1 < len(segments) and re.fullmatch(
            r"\s*(?:[,;]|(?:[,;]\s*)?(?:and|but))\s*",
            separators[chosen],
            re.IGNORECASE,
        ):
            next_segment = _replace_link_markup(
                segments[chosen + 1][2],
                preserve_labels=True,
            )
            bound_next_segment = re.sub(
                _source_identity_pattern(source),
                "<source>",
                next_segment,
                flags=re.IGNORECASE,
            )
            employment_ended = (
                _EMPLOYMENT_ENDING_CLAUSE.fullmatch(bound_next_segment)
                is not None
                and "[" not in segments[chosen + 1][2]
            )
        chosen_attributed = (
            attributed_through[chosen] or attributed_from_right[chosen]
        )
        output[(occurrence[0], occurrence[1])] = _LinkContext(
            " ".join(local_context.split()),
            chosen_attributed,
            coordinated,
            inherited_nonassertive[chosen],
            text[right : right + 1] == "?",
            employment_ended,
        )
    return output


def _relation_heading_context(source: Note, heading: str) -> str | None:
    """Return a bounded assertion for one explicit relation-list heading."""

    plain = _link_labels(heading).strip().removesuffix(":").strip()
    if _segment_has_source_subject(source, plain):
        return f"{plain} <target>"
    bound = re.sub(_source_identity_pattern(source), "<source>", plain)
    if note_type(source) in {"company", "fund"}:
        if re.fullmatch(
            r"<source>(?:['’]s|['’])\s+(?:founders?|co[ -]?founders?)\s+"
            r"(?:include|includes|included|are|were)",
            bound,
            re.IGNORECASE,
        ):
            return "<source>'s founders include <target>"
        if re.fullmatch(
            r"<source>(?:['’]s|['’])\s+"
            r"(?:advisers?|advisors?|consultants?)\s+"
            r"(?:include|includes|included|are|were)",
            bound,
            re.IGNORECASE,
        ):
            return "<source>'s advisers include <target>"
        if re.fullmatch(
            r"<source>(?:['’]s|['’])\s+"
            r"(?:seed\s+|lead\s+|angel\s+|early\s+)?"
            r"(?:investors?|backers?)\s+"
            r"(?:include|includes|included|are|were)",
            bound,
            re.IGNORECASE,
        ):
            return "<source>'s investors include <target>"
        if re.fullmatch(
            r"(?:(?:current|active|external|strategic)\s+)?"
            r"(?:advisers?|advisors?|consultants?)\s+(?:to|for|of)\s+"
            r"(?:the\s+)?<source>",
            bound,
            re.IGNORECASE,
        ):
            return "<source>'s advisers include <target>"
        if re.fullmatch(
            r"(?:(?:current|initial)\s+)?(?:lead\s+investors?|lead\s+backers?)"
            r"\s+(?:in|for|of)\s+(?:the\s+)?<source>(?:['’]s|['’])?\s+"
            r"(?:(?:pre[ -]?seed|seed|funding|financing|investment|"
            r"series\s+[a-z])\s+)?round",
            bound,
            re.IGNORECASE,
        ):
            return "<target> is the lead investor in <source>'s seed round"
    if note_type(source) == "person":
        if re.fullmatch(
            r"(?:companies|organizations|startups|businesses)\s+"
            r"(?:co[ -]?)?founded\s+by\s+<source>",
            bound,
            re.IGNORECASE,
        ):
            return "<target> was founded by <source>"
        if re.fullmatch(
            r"(?:companies|organizations|startups|businesses)\s+"
            r"(?:advised|counseled|mentored)\s+by\s+<source>",
            bound,
            re.IGNORECASE,
        ):
            return "<target> was advised by <source>"
    return None


def _relation_heading_list_contexts(
    text: str,
    occurrences: list[tuple[int, int, str | None]],
    source: Note,
) -> dict[tuple[int, int], _LinkContext]:
    """Bind list members to an immediately preceding relation heading."""

    output: dict[tuple[int, int], _LinkContext] = {}
    occurrence_index = 0
    active_heading: tuple[str, bool] | None = None
    relation_patterns = (
        _FOUNDED,
        _INVESTED,
        _ADVISES,
        _WORKS_AT,
        _ATTENDANCE_LANGUAGE,
    )
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        line_end = offset + len(content)
        stripped = content.strip()
        while (
            occurrence_index < len(occurrences)
            and occurrences[occurrence_index][1] <= offset
        ):
            occurrence_index += 1
        is_list_item = _LIST_MARKER.match(content) is not None
        if is_list_item and active_heading is not None:
            member = _LIST_MARKER.sub("", content, count=1)
            line_occurrences = []
            index = occurrence_index
            while index < len(occurrences) and occurrences[index][0] < line_end:
                occurrence = occurrences[index]
                if offset <= occurrence[0] and occurrence[1] <= line_end:
                    line_occurrences.append(occurrence)
                index += 1
            if len(line_occurrences) != 1 or not (
                not _strip_links(member).strip()
                or _is_modified_list_member(member)
            ):
                offset += len(line)
                continue
            start, end, _target = line_occurrences[0]
            local, attributed = active_heading
            output[(start, end)] = _LinkContext(
                " ".join(local.split()),
                attributed,
                (),
            )
        elif stripped.endswith(":") and any(
            pattern.search(stripped) for pattern in relation_patterns
        ):
            context = _relation_heading_context(source, stripped)
            active_heading = (
                (
                    context,
                    _NON_ASSERTIVE_ATTRIBUTION.search(_link_labels(stripped))
                    is not None,
                )
                if context is not None
                else None
            )
        elif stripped:
            active_heading = None
        offset += len(line)
    return output


def _relationship_contexts(
    text: str,
    occurrences: list[tuple[int, int, str | None]],
    source: Note,
    bundle: Bundle,
    identities: dict[str, str | None],
) -> dict[tuple[int, int], _LinkContext]:
    """Build all endpoint contexts with one structural scan of each sentence."""

    boundaries = _sentence_boundaries(text, source)
    implicit_safety = _implicit_source_safety_by_sentence(
        text,
        boundaries,
        source,
        bundle,
        identities,
    )
    source_identity_ends = tuple(
        match.end()
        for match in re.finditer(
            _source_identity_pattern(source),
            text,
            re.IGNORECASE,
        )
    )
    object_source_identity_ends = tuple(
        match.end()
        for match in re.finditer(_source_identity_pattern(source), text)
    )
    antecedent_characters = list(text)
    for start, end, _target in occurrences:
        for index in range(start, end):
            if antecedent_characters[index] not in "\r\n":
                antecedent_characters[index] = " "
    antecedent_prose = "".join(antecedent_characters)
    person_antecedent_blockers = tuple(
        sorted(
            {
                match.start()
                for pattern in (
                    _PERSON_ANTECEDENT_INTRODUCTION,
                    _PERSON_ANTECEDENT_FULL_NAME,
                )
                for match in pattern.finditer(antecedent_prose)
            }
        )
    )
    allowed_sentence_words = {
        "he",
        "her",
        "his",
        "it",
        "she",
        "the",
        "their",
        "these",
        "they",
        "this",
    }
    disallowed_capitalized_starts = tuple(
        match.start()
        for match in re.finditer(r"\b[A-Z][A-Za-z0-9'’_-]*\b", text)
        if match.group(0).casefold() not in allowed_sentence_words
    )
    starts = [boundary.start() for boundary in boundaries]
    ends = [boundary.end() for boundary in boundaries]
    grouped: dict[tuple[int, int], list[tuple[int, int, str | None]]] = {}
    for occurrence in occurrences:
        start, end, _ = occurrence
        prior = bisect_left(starts, start) - 1
        left = ends[prior] if prior >= 0 else 0
        following = bisect_left(starts, end)
        right = starts[following] if following < len(starts) else len(text)
        grouped.setdefault((left, right), []).append(occurrence)
    output = {}
    for (left, right), sentence_occurrences in grouped.items():
        source_index = bisect_right(source_identity_ends, left) - 1
        person_shorthand_is_safe = False
        if source_index >= 0:
            source_end = source_identity_ends[source_index]
            person_shorthand_is_safe = bisect_left(
                disallowed_capitalized_starts,
                source_end,
            ) == bisect_left(disallowed_capitalized_starts, left)
        object_source_index = bisect_right(object_source_identity_ends, left) - 1
        person_object_pronoun_is_safe = True
        if object_source_index >= 0:
            object_source_end = object_source_identity_ends[object_source_index]
            person_object_pronoun_is_safe = bisect_left(
                person_antecedent_blockers,
                object_source_end,
            ) == bisect_left(person_antecedent_blockers, left)
        previous_sentence = ""
        if left:
            boundary_index = bisect_left(ends, left)
            previous_left = ends[boundary_index - 1] if boundary_index > 0 else 0
            previous_right = starts[boundary_index]
            previous_sentence = text[previous_left:previous_right]
        output.update(
            _contexts_for_sentence(
                text,
                left,
                right,
                sentence_occurrences,
                source,
                implicit_safety.get((left, right), False),
                person_shorthand_is_safe,
                person_object_pronoun_is_safe,
                previous_sentence,
                bundle,
                identities,
            )
        )
    output.update(_relation_heading_list_contexts(text, occurrences, source))
    return output


def _collapse_redundant_relationship_occurrences(
    text: str,
    occurrences: list[tuple[int, int, str | None]],
) -> list[tuple[int, int, str | None]]:
    """Collapse adjacent repetitions that add no prose or graph information."""

    output = []
    previous: tuple[int, int, str | None] | None = None
    for occurrence in occurrences:
        separator = text[previous[1] : occurrence[0]] if previous is not None else ""
        if (
            previous is not None
            and occurrence[2] is not None
            and occurrence[2] == previous[2]
            and "\n" not in separator
            and "\r" not in separator
            and not separator.strip()
        ):
            previous = occurrence
            continue
        output.append(occurrence)
        previous = occurrence
    return output


def _build_source_identity_pattern(note: Note) -> str:
    """Build a bounded regex for a source note's declared identities."""

    aliases = note.meta.get("aliases", [])
    aliases = aliases if isinstance(aliases, list) else [aliases]
    values = {note.title, *[str(alias) for alias in aliases if alias]}
    phrases = []
    for value in sorted(values, key=len, reverse=True):
        words = value.split()
        if words:
            phrases.append(r"\s+".join(re.escape(word) for word in words))
    pattern = rf"(?<!\w)(?:{'|'.join(phrases)})(?!\w)" if phrases else r"(?!)"
    return pattern


def _source_identity_pattern(note: Note) -> str:
    """Return one cached identity regex for the loaded note instance."""

    aliases = note.meta.get("aliases", [])
    aliases = aliases if isinstance(aliases, list) else [aliases]
    identity_key = tuple(
        sorted(
            {note.title, *[str(alias) for alias in aliases if alias]},
            key=lambda value: (-len(value), value),
        )
    )
    if (
        note._relationship_identity_pattern is not None
        and note._relationship_identity_key == identity_key
    ):
        return note._relationship_identity_pattern
    pattern = _build_source_identity_pattern(note)
    note._relationship_identity_key = identity_key
    note._relationship_identity_pattern = pattern
    return pattern


def _segment_has_source_subject(
    source: Note,
    segment: str,
    *,
    allow_implicit: bool = False,
) -> bool:
    """Return whether a preceding segment starts with the source as subject."""

    bound = re.sub(_source_identity_pattern(source), "<source>", segment)
    source_type = note_type(source)
    if source_type == "person":
        subject = r"<source>"
    elif source_type in {"company", "fund"}:
        implicit = r"|it|the\s+(?:company|startup|business|firm)"
        subject = rf"(?:<source>{implicit if allow_implicit else ''})"
    elif source_type == "meeting":
        implicit = r"|it|the\s+(?:meeting|session|event|demo\s+day)"
        subject = rf"(?:<source>{implicit if allow_implicit else ''})"
    else:
        return False
    predicate = (
        r"(?:was|were|is|are|received|got|employs?|employed|hired|"
        r"works?|worked|working|departed|left|resigned|retired|"
        r"advis(?:es|ed)|counsel(?:s|ed)|consult(?:s|ed)|mentor(?:s|ed)|"
        r"back(?:s|ed)|fund(?:s|ed)|financ(?:es|ed)|invest(?:s|ed|ing)?|"
        r"attend(?:s|ed)|"
        r"co[ -]?founded|founded|established|created|incorporated|launched|"
        r"started)\b"
    )
    return re.match(rf"^\s*{subject}\s+{predicate}", bound, re.IGNORECASE) is not None


_COORDINATED_RELATION_VERB = re.compile(
    r"co[ -]?founded|founded|established|created|incorporated|launched|"
    r"started|backed|funded|financed|invest(?:ed)?(?:\s+in)?|"
    r"advis(?:e|ed)|counsel(?:ed)?|"
    r"mentor(?:ed)?",
    re.IGNORECASE,
)
_COORDINATED_RELATION_SEQUENCE = (
    r"(?:co[ -]?founded|founded|established|created|incorporated|launched|"
    r"started|backed|funded|financed|advised|counseled|mentored)"
    r"(?:\s*(?:,\s*)?(?:and\s+)?"
    r"(?:co[ -]?founded|founded|established|created|incorporated|launched|"
    r"started|backed|funded|financed|advised|counseled|mentored))+"
)
_ORG_COORDINATED_PASSIVE = re.compile(
    rf"<source>\s+(?:was|were)\s+"
    rf"(?P<predicates>{_COORDINATED_RELATION_SEQUENCE})\s+by\s+<target>",
    re.IGNORECASE,
)
_PERSON_COORDINATED_ACTIVE = re.compile(
    rf"<source>\s+(?P<predicates>{_COORDINATED_RELATION_SEQUENCE})\s+"
    r"(?:the\s+)?<target>",
    re.IGNORECASE,
)
_SAFE_COORDINATED_PREDICATES = re.compile(
    rf"^(?:\s*(?:{_COORDINATED_RELATION_VERB.pattern}|"
    r"was|were|is|are|did|does|do|has|have|had|been|"
    r"not|never|only|also|later|then|neither|nor|"
    r"and|but|while|whereas|[,;])\s*)+$",
    re.IGNORECASE,
)


def _coordinated_relations(
    context: str,
    source_type: str = "",
) -> tuple[str, ...]:
    """Return every relation in one shared-endpoint coordination."""

    match = None
    if source_type in {"", "company", "fund"}:
        match = _ORG_COORDINATED_PASSIVE.search(context)
    if match is None and source_type in {"", "person"}:
        match = _PERSON_COORDINATED_ACTIVE.search(context)
    if match is None:
        pattern = (
            r"<source>\s+(?P<predicates>[^.!?\n]{1,160}?)\s+by\s+<target>"
            if source_type in {"company", "fund"}
            else r"<source>\s+(?P<predicates>[^.!?\n]{1,160}?)\s+"
            r"(?:the\s+)?<target>"
        )
        candidate = re.search(pattern, context, re.IGNORECASE)
        if candidate is not None:
            predicates = candidate.group("predicates").strip()
            verbs = tuple(_COORDINATED_RELATION_VERB.finditer(predicates))
            if len(verbs) >= 2 and _SAFE_COORDINATED_PREDICATES.fullmatch(
                predicates
            ):
                match = candidate
    if match is None:
        return ()
    output = []
    prior_end = 0
    nonassertive = False
    for verb in _COORDINATED_RELATION_VERB.finditer(match.group("predicates")):
        qualifier = match.group("predicates")[prior_end : verb.start()]
        prior_end = verb.end()
        qualifier = _ASSERTIVE_NOT.sub("", qualifier)
        if re.search(
            r"\b(?:but|whereas|while)\b",
            qualifier,
            re.IGNORECASE,
        ):
            nonassertive = False
        if _NON_ASSERTIVE_RELATION_CONTEXT.search(qualifier):
            nonassertive = True
        if nonassertive:
            continue
        value = verb.group(0).casefold()
        if value in {"backed", "funded", "financed"} or value.startswith(
            "invest"
        ):
            relation = "invested_in"
        elif value.startswith(("advis", "counsel", "mentor")):
            relation = "advises"
        else:
            relation = "founded"
        if relation not in output:
            output.append(relation)
    return tuple(output)


def _infer_relation_qualifier(
    source: Note,
    target: Note,
    context: _LinkContext,
    relation: str,
) -> str:
    """Return a bounded detail needed by a more specific query."""

    assertion_context = _ASSERTIVE_CERTAINTY.sub(
        "",
        _ASSERTIVE_NOT.sub("", context.local),
    )
    bound = re.sub(
        _source_identity_pattern(source),
        "<source>",
        assertion_context.replace("<anchor>", "<target>"),
    )
    source_type = note_type(source)
    target_type = note_type(target)
    if relation == "works_at":
        pattern = (
            _PERSON_TO_ORG_EXECUTIVE_ROLE
            if source_type == "person" and target_type in {"company", "fund"}
            else _ORG_TO_PERSON_EXECUTIVE_ROLE
        )
        match = pattern.search(bound)
        if match is not None:
            role = (
                match.groupdict().get("role")
                or match.groupdict().get("of_role")
                or match.groupdict().get("reverse_role")
            )
            if role:
                return _normalized_role(role)
    if relation == "advises" and (
        (
            source_type == "person"
            and target_type in {"company", "fund"}
            and _PERSON_TO_ORG_ADVISORY_BOARD.search(bound)
        )
        or (
            source_type in {"company", "fund"}
            and target_type == "person"
            and _ORG_TO_PERSON_ADVISORY_BOARD.search(bound)
        )
    ):
        return "advisory_board"
    if relation == "invested_in" and (
        (
            source_type == "person"
            and target_type in {"company", "fund"}
            and _PERSON_TO_ORG_LED_ROUND.search(bound)
        )
        or (
            source_type in {"company", "fund"}
            and target_type == "person"
            and _ORG_TO_PERSON_LED_ROUND.search(bound)
        )
    ):
        return "led_round"
    return ""


def _infer_relation_temporal(
    source: Note,
    target: Note,
    context: _LinkContext,
    relation: str,
) -> str:
    """Classify only explicit current or past employment language."""

    if relation != "works_at":
        return ""
    if context.employment_ended:
        return "past"
    assertion_context = _ASSERTIVE_CERTAINTY.sub(
        "",
        _ASSERTIVE_NOT.sub("", context.local),
    )
    bound = re.sub(
        _source_identity_pattern(source),
        "<source>",
        assertion_context.replace("<anchor>", "<target>"),
        flags=re.IGNORECASE,
    )
    if note_type(source) == "person" and note_type(target) in {"company", "fund"}:
        current_pattern = _CURRENT_PERSON_TO_ORG_WORKS_AT
        past_pattern = _PAST_PERSON_TO_ORG_WORKS_AT
    elif note_type(source) in {"company", "fund"} and note_type(target) == "person":
        current_pattern = _CURRENT_ORG_TO_PERSON_WORKS_AT
        past_pattern = _PAST_ORG_TO_PERSON_WORKS_AT
    else:
        return ""
    if current_pattern.search(bound):
        return "current"
    if past_pattern.search(bound):
        return "past"
    return ""


def _infer_relations(
    source: Note,
    target: Note,
    context: _LinkContext,
) -> tuple[str, ...]:
    source_type = note_type(source)
    target_type = note_type(target)
    assertion_context = _ASSERTIVE_CERTAINTY.sub(
        "",
        _ASSERTIVE_NOT.sub("", context.local),
    )
    assertion_prose = _classification_prose(assertion_context)
    assertion_scope = _relation_assertion_scope(assertion_prose)
    if context.attributed or context.inherited_nonassertive or context.interrogative:
        return ()
    if _COUNTERFACTUAL_RELATION.search(assertion_prose):
        return ()
    bound_context = re.sub(
        _source_identity_pattern(source),
        "<source>",
        assertion_prose.replace("<anchor>", "<target>"),
    )
    bound_scope = re.sub(
        _source_identity_pattern(source),
        "<source>",
        assertion_scope.replace("<anchor>", "<target>"),
    )
    if _NON_ASSERTIVE_MODAL_RELATION.search(bound_scope):
        return ()
    if _NON_ASSERTIVE_RELATION_CONTEXT.search(assertion_scope):
        without_negation = re.sub(
            r"\b(?:never|not)\b",
            "",
            assertion_scope,
            flags=re.IGNORECASE,
        )
        if context.coordinated and not _NON_ASSERTIVE_RELATION_CONTEXT.search(
            without_negation
        ):
            return context.coordinated
        return ()
    if _TARGET_POSSESSIVE.search(assertion_prose):
        if source_type == "person" and target_type in {"company", "fund"}:
            output = []
            if _PERSON_TO_ORG_ADVISORY_BOARD.search(bound_context):
                output.append("advises")
            if _PERSON_TO_ORG_LED_ROUND.search(bound_context):
                output.append("invested_in")
            return tuple(output)
        return ()
    if source_type == "person" and target_type in {"company", "fund"}:
        directional_patterns = (
            ("founded", _PERSON_TO_ORG_FOUNDED),
            ("invested_in", _PERSON_TO_ORG_LED_ROUND),
            ("invested_in", _PERSON_TO_ORG_INVESTED),
            ("advises", _PERSON_TO_ORG_ADVISORY_BOARD),
            ("advises", _PERSON_TO_ORG_ADVISES),
            ("works_at", _PERSON_TO_ORG_EXECUTIVE_ROLE),
            ("works_at", _PERSON_TO_ORG_WORKS_AT),
        )
    elif source_type in {"company", "fund"} and target_type == "person":
        directional_patterns = (
            ("founded", _ORG_TO_PERSON_FOUNDED),
            ("invested_in", _ORG_TO_PERSON_LED_ROUND),
            ("invested_in", _ORG_TO_PERSON_INVESTED),
            ("advises", _ORG_TO_PERSON_ADVISORY_BOARD),
            ("advises", _ORG_TO_PERSON_ADVISES),
            ("works_at", _ORG_TO_PERSON_EXECUTIVE_ROLE),
            ("works_at", _ORG_TO_PERSON_WORKS_AT),
        )
    elif source_type == "person" and target_type == "meeting":
        directional_patterns = (("attended", _PERSON_TO_MEETING_ATTENDED),)
    elif source_type == "meeting" and target_type == "person":
        directional_patterns = (("attended", _MEETING_TO_PERSON_ATTENDED),)
    else:
        directional_patterns = ()
    output = []
    if (
        source_type in {"company", "fund"}
        and target_type == "person"
    ) or (
        source_type == "person"
        and target_type in {"company", "fund"}
    ):
        output.extend(context.coordinated)
    for relation, pattern in directional_patterns:
        if pattern.search(bound_context) and relation not in output:
            output.append(relation)
    return tuple(output)


def _resolve_markdown_target(bundle: Bundle, source: Note, raw: str) -> str | None:
    candidate = bundle._resolved_markdown_target_path(source, raw)
    if candidate in bundle.notes and candidate != source.path:
        return candidate
    return None


def _resolve_wikilink_target(
    source: Note, raw: str, identities: dict[str, str | None]
) -> str | None:
    for name in Bundle._wikilink_lookup_names(raw):
        target = identities.get(name)
        if target and target != source.path:
            return target
    return None


def _canonical_edge(
    source: Note,
    target: Note,
    relation: str,
    order: int,
    qualifier: str = "",
    temporal: str = "",
) -> RelationEdge | None:
    source_type = note_type(source)
    target_type = note_type(target)
    if relation == "attended":
        if source_type == "person" and target_type == "meeting":
            subject, object_path = source.path, target.path
        elif source_type == "meeting" and target_type == "person":
            subject, object_path = target.path, source.path
        else:
            return None
    elif source_type == "person" and target_type in {"company", "fund"}:
        subject, object_path = source.path, target.path
    elif source_type in {"company", "fund"} and target_type == "person":
        subject, object_path = target.path, source.path
    else:
        return None
    return RelationEdge(
        subject,
        object_path,
        relation,
        source.path,
        order,
        qualifier,
        temporal,
    )


def relationship_edges(
    bundle: Bundle,
    *,
    include_stale: bool = False,
) -> tuple[RelationEdge, ...]:
    """Return the cached current or historical relationship index."""

    cache_name = (
        "_relationship_edges_historical_cache"
        if include_stale
        else "_relationship_edges_cache"
    )
    cached = getattr(bundle, cache_name, None)
    if cached is not None:
        return cached
    identities = _identity_index(bundle)
    edges = []
    seen = set()
    order = 0
    superseded = set() if include_stale else set(bundle.superseded_by())
    for path in sorted(bundle.notes):
        source = bundle.notes[path]
        if (
            not include_stale
            and (source.status() == "deprecated" or path in superseded)
        ):
            continue
        explicit = {
            (edge["target"], str(edge["relation"]).strip().lower())
            for edge in source.typed_links
        }
        for item in source.typed_links:
            relation = str(item["relation"]).strip().lower()
            target = bundle.notes.get(item["target"])
            if (
                relation not in RELATION_TYPES
                or target is None
                or (
                    not include_stale
                    and (
                        target.status() == "deprecated"
                        or target.path in superseded
                    )
                )
            ):
                continue
            order += 1
            edge = _canonical_edge(source, target, relation, order)
            if edge and (
                edge.subject,
                edge.object,
                edge.relation,
                edge.qualifier,
                edge.temporal,
            ) not in seen:
                seen.add(
                    (
                        edge.subject,
                        edge.object,
                        edge.relation,
                        edge.qualifier,
                        edge.temporal,
                    )
                )
                edges.append(edge)
        if (
            "[" not in source.body
        ):
            continue
        prose_text = _mask_direct_quotes(
            _mask_nonassertive_markdown(_mask_link_graph_source(source.body))
        )
        markdown_links = _all_markdown_links(prose_text)
        text = _mask_wikilink_context(prose_text)
        wikilink_text = _mask_wikilink_metadata(text)
        occurrences = []
        for link in markdown_links:
            target_path = _resolve_markdown_target(
                bundle,
                source,
                link.destination,
            )
            occurrences.append((link.start, link.end, target_path))
        for match in _wikilink_matches(wikilink_text):
            target_path = _resolve_wikilink_target(source, match.group(1), identities)
            occurrences.append((match.start(), match.end(), target_path))
        occurrences.sort()
        occurrences = _collapse_redundant_relationship_occurrences(
            text,
            occurrences,
        )
        contexts = _relationship_contexts(
            text,
            occurrences,
            source,
            bundle,
            identities,
        )
        for start, end, target_path in occurrences:
            target = bundle.notes.get(target_path or "")
            if (
                target is None
                or (
                    not include_stale
                    and (
                        target.status() == "deprecated"
                        or target.path in superseded
                    )
                )
            ):
                continue
            relations = _infer_relations(
                source,
                target,
                contexts[(start, end)],
            )
            for relation in relations:
                qualifier = _infer_relation_qualifier(
                    source,
                    target,
                    contexts[(start, end)],
                    relation,
                )
                temporal = _infer_relation_temporal(
                    source,
                    target,
                    contexts[(start, end)],
                    relation,
                )
                if (target.path, relation) in explicit and not qualifier and not temporal:
                    continue
                order += 1
                edge = _canonical_edge(
                    source,
                    target,
                    relation,
                    order,
                    qualifier,
                    temporal,
                )
                if edge and temporal and (target.path, relation) in explicit:
                    for existing_index in range(len(edges) - 1, -1, -1):
                        existing = edges[existing_index]
                        if (
                            existing.source == source.path
                            and existing.subject == edge.subject
                            and existing.object == edge.object
                            and existing.relation == edge.relation
                            and not existing.qualifier
                            and not existing.temporal
                        ):
                            edges.pop(existing_index)
                            seen.discard(
                                (
                                    existing.subject,
                                    existing.object,
                                    existing.relation,
                                    existing.qualifier,
                                    existing.temporal,
                                )
                            )
                            break
                if edge and (
                    edge.subject,
                    edge.object,
                    edge.relation,
                    edge.qualifier,
                    edge.temporal,
                ) not in seen:
                    seen.add(
                        (
                            edge.subject,
                            edge.object,
                            edge.relation,
                            edge.qualifier,
                            edge.temporal,
                        )
                    )
                    edges.append(edge)
    result = tuple(edges)
    setattr(bundle, cache_name, result)
    return result


def relationship_hits(
    bundle: Bundle,
    cue: str,
    *,
    include_stale: bool = False,
    excluded_paths: set[str] | frozenset[str] = frozenset(),
) -> tuple[RelationHit, ...]:
    """Return exact one-hop answers in deterministic extraction order."""

    request = parse_relationship_query(
        bundle,
        cue,
        include_stale=include_stale,
    )
    excluded = set(excluded_paths)
    if request is None or request.seed in excluded:
        return ()
    output = []
    seen = set()
    for edge in relationship_edges(bundle, include_stale=include_stale):
        if {edge.source, edge.subject, edge.object} & excluded:
            continue
        if edge.relation not in request.edge_types:
            continue
        if request.qualifier and edge.qualifier != request.qualifier:
            continue
        if request.temporal == "past" and edge.temporal != "past":
            continue
        if request.temporal == "current" and edge.temporal == "past":
            continue
        if request.direction == "in" and edge.object == request.seed:
            path = edge.subject
        elif request.direction == "out" and edge.subject == request.seed:
            path = edge.object
        else:
            continue
        note = bundle.notes.get(path)
        if note is None or note_type(note) not in request.answer_types or path in seen:
            continue
        seen.add(path)
        output.append(
            RelationHit(
                path=path,
                seed=request.seed,
                relation=edge.relation,
                direction=request.direction,
                source=edge.source,
                qualifier=edge.qualifier,
                temporal=edge.temporal,
            )
        )
    return tuple(output)
