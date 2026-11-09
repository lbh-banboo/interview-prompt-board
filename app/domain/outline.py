import re


def outline(answer):
    # Pure extractive draft: never invent numbers or silently remove caveats.
    parts = [p.strip() for p in re.split(r"(?<=[。；])", answer) if p.strip()]
    return dict(conclusion=parts[0] if parts else answer, points=parts[1:4],
                example="", caution="练习参考，非现场原话；个人事实需本人核验。", full=answer)
