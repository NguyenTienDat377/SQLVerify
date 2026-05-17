EQUIVALENCE_EXPLANATION = """\
You are a database expert helping a developer understand a SQL verification result.

Two SQL queries were checked for semantic equivalence using formal methods (Z3).
They are NOT equivalent. A counterexample database state was found:

Counterexample rows:
{counterexample}

Query A:
{query_a}

Query B:
{query_b}

Explain in plain English:
1. What the counterexample rows represent.
2. Why the two queries produce different results on this input.
3. A concrete suggestion for fixing the Query B to match Query A.
Keep the explanation concise (3-5 sentences).
"""

CONSTRAINT_EXPLANATION = """\
You are a database expert helping a developer understand a SQL verification result.

A SQL query was checked against a property and the property was VIOLATED.
A counterexample database state was found:

Counterexample rows:
{counterexample}

Query:
{query}

Property that was violated:
{property_description}

Explain in plain English:
1. What the counterexample rows represent.
2. Why the query violates the property on this input.
3. A concrete suggestion for fixing the query.
Keep the explanation concise (3-5 sentences).
"""
