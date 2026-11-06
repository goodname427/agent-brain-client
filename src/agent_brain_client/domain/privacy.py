"""domain / privacy: one owner for each contract and rule."""

import re


def explicit_sharing_restriction(text):
    """Recognize explicit markers/directive prefixes, never body keywords.

    Free-form intent remains an Agent decision. Restriction words inside an
    explanation, quotation or question do not independently change sharing.
    A polite question after an explicit directive still retains that directive.
    """
    if not isinstance(text, str):
        return False
    return bool(
        re.search(
            r"(?im)^[^\S\r\n]*(?:"
            r"\[(?:no[-_ ]?share|project[-_ ]?only|do[-_ ]?not[-_ ]?share)\]|"
            r"(?:请)?(?:(?:禁止|不要)(?:跨项目)?共享"
            r"(?:这(?:段|条|些)?(?:内容|信息|记录|消息)|本(?:段|条)(?:内容|信息|记录|消息))?"
            r"|仅限(?:当前|本)项目(?:使用)?)"
            r"(?=$|[\s:：。.!！,，;；])|"
            r"(?:please[ \t]+)?(?:do not share (?:this|across projects)|keep (?:this )?project[- ]only)"
            r"(?=$|[\s:;,.!]))",
            text,
        )
    )


def private_recall_text(text):
    """Keep explicit privacy/sharing restrictions; body shape is not a verdict."""
    return bool(
        re.search(
            r"(?im)^\s*(?:\[local[-_ ]?only\]|\[no-memory\]|\[private\]|localonly:|不要记忆[:：])",
            text,
        )
        or explicit_sharing_restriction(text)
    )


"""Explicit local-only content markers; shared by capture and recall."""


def private_content(text):
    """Honor explicit local-only instructions without guessing from body shape."""
    return bool(
        re.search(
            r"(?im)^\s*(?:\[local[-_ ]?only\]|\[no-memory\]|\[private\]|localonly:|不要记忆[:：])",
            text,
        )
    )
