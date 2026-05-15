"""
explainer/explain.py

Public API for generating LLM explanations of verification results.

Usage:
    from explainer.explain import explain_result
    explanation = await explain_result(result, sql_v1, sql_v2)
"""

from __future__ import annotations

import json

from core.models import VerificationResult
from explainer.prompts import EQUIVALENCE_EXPLANATION
from explainer.providers import ExplainerError, get_provider


async def explain_result(
    result: VerificationResult,
    sql_v1: str,
    sql_v2: str,
    provider_name: str | None = None,
) -> str:
    """
    Generate a plain-English explanation for a divergent VerificationResult.

    Only generates explanations for 'divergent' status — equivalent/unknown
    results don't need LLM explanation (the status message is self-explanatory).

    Args:
        result:        VerificationResult from check_equivalence().
        sql_v1:        Original trusted query string.
        sql_v2:        AI-generated query string.
        provider_name: Override provider (defaults to EXPLAINER_PROVIDER env var).

    Returns:
        Plain-text explanation string, or a fallback message on error.
    """
    if result.status != "divergent":
        return ""

    # Format the counterexample DB as readable JSON for the prompt
    counterexample_str = json.dumps(result.counterexample_db, indent=2)

    prompt = EQUIVALENCE_EXPLANATION.format(
        counterexample=counterexample_str,
        query_a=sql_v1,
        query_b=sql_v2,
    )

    try:
        provider = get_provider(provider_name)
        return await provider.explain(prompt)
    except ExplainerError as e:
        # Never let explainer failure break the verification result
        return f"(Explanation unavailable: {e})"