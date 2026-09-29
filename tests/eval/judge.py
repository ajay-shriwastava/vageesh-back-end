"""
Shared LLM-as-judge helper for Vageesh eval tests.

Uses claude-haiku-4-5-20251001 as the judge — cheap per call, sufficient for
pass/fail evaluation against explicit criteria.
"""
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

_JUDGE_SYSTEM = (
    "You are a strict output evaluator. "
    "Evaluate the given output exactly against the stated criterion. "
    "First line of your response must be PASS or FAIL (all caps). "
    "Second line must be a single sentence explaining your decision."
)


async def llm_judge(output: str, criterion: str, context: str = "") -> tuple[bool, str]:
    """
    Run an LLM-as-judge evaluation against a single criterion.

    Args:
        output:    The agent output to evaluate.
        criterion: The specific, testable criterion to check.
        context:   Optional source material the output was derived from
                   (e.g. input headlines, report text). Helps the judge
                   detect hallucinations and groundedness issues.

    Returns:
        (passed, reason) — passed is True when the criterion is met.
    """
    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)

    parts = [f"Criterion: {criterion}"]
    if context:
        parts.append(f"Context (source material):\n{context}")
    parts.append(f"Output to evaluate:\n{output}")

    result = await llm.ainvoke([
        SystemMessage(content=_JUDGE_SYSTEM),
        HumanMessage(content="\n\n".join(parts)),
    ])
    text = result.content if hasattr(result, "content") else str(result)
    passed = text.strip().upper().startswith("PASS")
    return passed, text.strip()
