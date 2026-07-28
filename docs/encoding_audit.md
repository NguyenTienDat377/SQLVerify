# Encoding audit: Skolem vs. VeriEQL (OOPSLA 2024)

Cross-check of Skolem's Z3 encoding against the paper it is based on:

> He, Zhao, Wang, Wang. *VeriEQL: Bounded Equivalence Verification for Complex
> SQL Queries with Integrity Constraints.* Proc. ACM Program. Lang. 8, OOPSLA1,
> Article 132 (April 2024). DOI 10.1145/3649849. Checked in as `3649849.pdf`.

Paper references below: §4.2 (symbolic DB / NULL encoding), Fig. 8–9 (integrity
constraints, pp. 13), Fig. 11 (tuple-count rules, p. 15), Fig. 12 (operator
encoding, p. 16), §4.5 + Eqns 1–2 (bag equivalence, pp. 17–18).

Verdicts: **MATCHES** (faithful to the paper), **DEVIATES** (intentional,
with soundness argument), **N/A** (construct outside the V1 subset and
rejected fail-closed), **BUG** (must be fixed — none open).

The failure mode that matters most is a **false "equivalent"** verdict.
Each DEVIATES row states which direction of error it can introduce.

| # | Paper rule | Code site | Verdict |
|---|---|---|---|
| 1 | Symbolic DB, Del(t) predicate (§4.2, Alg. 1) | `SymbolicDB.exists[t][i]` ≡ ¬Del(tᵢ) — `core/sql_encoder.py` `_build()` | MATCHES |
| 2 | NULL as (b, v) pair (§4.2 "Encoding NULL") | `SymbolicDB.nulls` / `vars`, `SymValue(is_null, value)` | MATCHES |
| 3 | Attributes as uninterpreted functions (§4.2) | Flattened to one Z3 variable per (table, column, row) instead of `attr(t)` UF applications | MATCHES (equivalent formulation: each `attr(tᵢ)` term is named directly; no tuple ever flows through two different attribute functions) |
| 4 | IC-PK Φ₁: PK attributes non-NULL (Fig. 8) | `core/ddl_parser.py:232` forces `nullable=False` on PK columns → enforced via the IC-NN path | MATCHES — **load-bearing dependency**: the Φ₂ encoding (row below) compares raw values without NULL flags and is sound *only because of this line* |
| 5 | IC-PK Φ₂: any two tuples differ on ≥1 PK attribute, ¬(∧ₖ tᵢ.aₖ = tⱼ.aₖ) (Fig. 8) | `domain_constraints()` PK block: `Implies(And(exists[i], exists[j]), Or(differs))` | MATCHES — composite keys handled correctly (per-column inequality would over-constrain; the code comment documents this) |
| 6 | IC-FK: ∀t₁∈R₁ ∃t₂∈R₂. t₁.a₁ = t₂.a₂ (Fig. 8) | `domain_constraints()` FK block | DEVIATES (intentional): a NULL FK cell is exempted (`Implies(And(exists, ¬null), ∃ match)`), matching real SQL `FOREIGN KEY` semantics where NULLs are allowed. The paper's rule is stricter. Effect: Skolem admits *more* databases → "equivalent" verdicts remain sound; counterexamples may use NULL FKs, which real databases accept. No false-equivalent risk. |
| 7 | IC-NN: all tuples non-NULL on attribute (Fig. 8) | `domain_constraints()`: `Implies(row_exists, ¬is_null)` for `nullable=False` columns | MATCHES |
| 8 | IC-Check (Fig. 8/9) | CHECK constraints are parsed by `ddl_parser` but **not encoded** | DEVIATES (documented): symbolic DB admits databases violating CHECK. Direction of error: spurious **divergent** only — a counterexample may violate a CHECK constraint, and the SQLite witness cross-check will NOT catch it (the witness DB is created without CHECK). Never produces a false "equivalent" (more databases = stronger equivalence claim). Listed in CLAUDE.md "V1 known limitations". |
| 9 | IC-Inc / auto-increment (Fig. 8) | Not parsed, not encoded | N/A — construct never claimed; absence admits more databases, same one-sided error direction as #8 |
| 10 | Predicate semantics, three-valued logic + AND/OR/NOT (Fig. 4 grammar `φ ::= … φ∧φ ⎪ φ∨φ ⎪ ¬φ`; Fig. 9, §3.3, §4.4.3) | `_parse_predicate()` builds a `BoolNode` tree; `_eval_tf()` returns the Kleene `(is_true, is_false)` pair (AND → `(∧t, ∨f)`, OR → `(∨t, ∧f)`, NOT → swap); leaf `_pred_tf()`: a comparison with a NULL operand is NULL (both halves false), `IS [NOT] NULL` are the only two-valued leaves, column-vs-column requires both sides non-NULL. `IN (v-list)` desugars to `∨` of equalities | MATCHES — the filter reads `is_true`; `is_false` exists only to make `¬φ` sound |
| 10a | Membership predicate `E⃗ ∈ Q` (Fig. 4 grammar), three-valued | `_parse_in_subquery()` parses `col IN (SELECT c FROM …)` into an `in_subquery` leaf; `encode_query` materializes the body once (`cond.subquery_qf`), and `_pred_tf()` builds the Kleene pair over the body's output bag: `is_true = ∨ᵢ(presentᵢ ∧ ¬xn ∧ ¬rnᵢ ∧ xv=rvᵢ)`, `is_false = ∧ᵢ(¬presentᵢ ∨ (¬xn ∧ ¬rnᵢ ∧ xv≠rvᵢ))`. `NOT IN` = `NOT(IN)` via the tree swap | MATCHES within scope — uncorrelated, single-column body, WHERE only. The NOT IN NULL trap falls out for free (a NULL body cell blocks `is_false`, so `¬(IN)` is never TRUE); empty body → `is_false` vacuous → IN is FALSE / NOT IN is TRUE. **Fail-closed** (row 28): correlated bodies (outer-alias ref → resolve error), scalar subqueries, `EXISTS`, tuple-LHS, multi-column bodies, HAVING position. Fuzzer-checked (`gen_in_subquery`, NULLABLE body columns, SQLite runs the IN natively) |
| 11 | E-Filter (Fig. 12), `σ_φ(Q)=filter(Q, λx.[[φ]]_x=⊤)` (Fig. 5) | `where_all()` = `_eval_tf(tree).is_true`, applied once per final entry inside `build_join_bag()` — after the whole join chain has folded, never during it | MATCHES — output tuple "deleted" iff input deleted or predicate not TRUE (NULL and FALSE both drop the row); WHERE-after-fold is what keeps ON and WHERE faithful at every chain level (row 17) |
| 12 | E-Proj (Fig. 12) | `proj()` copies (b, v) cells positionally; Del status shared with source row | MATCHES |
| 13 | E-Agg (T-Agg, Fig. 11/12): aggregation without GROUP BY yields exactly one tuple | `has_agg` branch: single `OutputTuple` always present (HAVING may drop it); `SUM` of empty/all-NULL input is NULL, `COUNT` is 0 | MATCHES |
| 14 | E-Prod / inner join = σ_φ(Q₁ × Q₂) (Fig. 12, §3.3) | `build_join_bag()`'s matched step: for each accumulated entry and each new-table row, `And(exists, on3v)`; output tuple per surviving combination | MATCHES — ON restricted to one column equality (fail-closed otherwise); generalizes to N tables via the left-deep fold (row 14a) |
| 14a | Binary join composition, Q1⊗Q2 (Fig. 4 grammar `Q ::= … ⎪ Q⊗Q`; Fig. 5 semantics) generalized to an N-table chain | `build_join_bag()` folds `parsed.joins` left-deep, one join at a time, over an accumulated `(present, cellfn)` bag — mirrors the paper's own binary operator nested left-deep, since VeriEQL has no native N-ary join either | MATCHES — any mix of INNER/LEFT/RIGHT/FULL at each chain level (each join's ON may only reference the table being joined or one already in scope — not valid SQL otherwise, so this narrows nothing real). Fuzzer-checked (`differential_test.py`'s 2-join `t1⋈t2⋈z` generator, all 4×4 type combinations) |
| 15 | E-LJoin: inner join + null-extension of unmatched left tuples with T_Null (Fig. 12, §3.3) | Per fold step, `left_ext`: for each accumulated entry with no matching new-table row, emit `(entry.present ∧ ¬∃match, entry.idx ∪ {new_alias: NULL})` | MATCHES — match test is pre-WHERE (correct); WHERE runs once at the very end, so `WHERE right.x = 5` kills a null-extended row (LEFT≡INNER) and `WHERE right.x IS NULL` keeps it (anti-join), at every chain level |
| 16 | Right outer join (§3.3, symmetric) | Per fold step, `right_ext`: for each new-table row with no matching accumulated entry, emit `(new_row.exists ∧ ¬∃match, {every alias accumulated so far: NULL} ∪ {new_alias: j})` | MATCHES — a RIGHT/FULL join null-extends the WHOLE accumulated left relation (paper Fig. 5's `T_Null` is as wide as `Q1`), not just the immediately preceding table, so a RIGHT/FULL deeper in a chain nulls every earlier alias in that output row. Fuzzer-checked (`test_right_join_in_chain_nulls_every_accumulated_alias` in smoke_test.py) |
| 17 | Full outer join (§3.3) | `left_ext`/`right_ext` both true for a FULL step — the union of rows 15 and 16 over the same match test | MATCHES within scope. No longer restricted to a single join (row 14a) — a FULL may appear at any chain level, combined with any mix of INNER/LEFT/RIGHT. No false-equivalent path (fuzzer-checked; SQLite ≥ 3.39 runs FULL and chains natively as an independent oracle) |
| 18 | T-* tuple-count rules (Fig. 11) | Per fold step, entries grow by: matched N·M, +N for a LEFT/FULL step's left-extension, +M for a RIGHT/FULL step's right-extension (N = accumulated bag size, M = new table's row count); GROUP BY → one candidate group per distinct key over the whole bag; ungrouped aggregate → 1 | MATCHES the paper's counts (N·M for products/inner joins, +N for LJoin, +2N for FJoin) per chain level, composing left-deep exactly as the paper's binary operator does |
| 19 | GroupBy = Dedup + Eval + HAVING filter (Fig. 5, §3.3) | Group-leader scheme: row g leads iff alive ∧ no earlier alive row shares its key; contributions gated by key-equality; NULL keys group together (`Or(And(n1,n2), ...)`) per SQL/paper semantics. Dedup runs over `build_join_bag()`'s output directly | MATCHES — group keys and bare SELECT columns may come from any table in the chain, including a null-extended outer side (the NULL-safe key equality merges a null-extended row's NULL key with any matched row's NULL key without restriction) |
| 19a | Static rejection of ill-formed GroupBy queries (§3.2: "a query with a non-aggregated attribute not in the GROUP BY list is not permitted … we perform static analysis to reject such queries") | `encode_query` grouped branch | **BUG — FOUND BY THIS AUDIT, FIXED.** A bare SELECT column not among the GROUP BY keys (e.g. `SELECT sal FROM emp GROUP BY did`) was silently encoded as the group *leader's* value — inventing deterministic semantics for a query PostgreSQL rejects and SQLite resolves arbitrarily, in violation of the project's fail-closed policy. Now raises `ValueError` ("must appear in GROUP BY"). Regression: `tests/paper_cases_test.py::test_bare_select_column_not_in_group_by_rejected`. |
| 20 | HAVING aggregate need not appear in SELECT (§3.2 Ex. 3.1) | `having_clauses()` computes aggregates fresh from group contributions, never matched by alias | MATCHES |
| 21 | Bag equivalence Eqn (1): equal count of non-deleted tuples | `_assert_diverges()` `same_count` — `core/equivalence.py` | MATCHES |
| 22 | Bag equivalence Eqn (2): ∀ non-deleted t∈R₁, multiplicity in R₁ = multiplicity in R₂ | `_assert_diverges()` `mult_ok`, counting only `present` tuples on both sides; (1)+(2) ⇒ (3) per the paper, so no reverse direction needed | MATCHES |
| 23 | Tuple equality: NULL = NULL, attribute names ignored (§4.5) | `_col_eq()` / `_tuple_eq()`; arity mismatch → trivially divergent | MATCHES |
| 24 | List semantics Eqns (3)–(4) / ORDER BY (§4.5, Fig. 5) | ORDER BY accepted but ignored; only bag semantics implemented | DEVIATES (documented): two queries differing only in output *order* are reported equivalent. This matches the paper's own default (bag semantics unless the query sorts). Risk: a consumer relying on row order gets no protection — documented in CLAUDE.md. |
| 25 | Unbounded Int theory for values (§4.2) | Finite window `[-(4·bound + max\|literal\|), +(4·bound + max\|literal\|)]`; symbolic-enum window for TEXT/TIMESTAMP/BOOLEAN; widened past every literal via `note_numeric_literal` (`domain_constraints()` must run *after* `encode_query()` — `equivalence.py` honours this order) | DEVIATES (intentional, documented): "equivalent" is sound only within the window. A divergence requiring a value outside it is missed (false equivalent in the unbounded sense, sound in the bounded sense). Bound widening past all literals removes the common practical failure (`x > 100` vs `x >= 100`). |
| 26 | Strings/dates as ints via uninterpreted functions (§3.1, Ex. 3.4) | Global string interning (`intern_string`), equality-only; ordering comparisons on TEXT/TIMESTAMP rejected | MATCHES (the paper also treats strings as ints; rejecting `<`/`>` on them is fail-closed, stricter than the paper) |
| 27 | Counterexample = model of Φ (Alg. 1 line 8, Fig. 2f) | `_materialize_witness()` + interning reversed to real strings + SQLite replay | MATCHES, **plus a guardrail the paper does not have**: if both queries agree on the witness under SQLite, the verdict is downgraded to `error` instead of trusting Z3 (catches encoder bugs; cannot catch #8 because the witness DB carries no CHECK constraints) |
| 27b | Witness replay's own SQLite→dict conversion (`_rows_to_dicts`, feeding row 27's guardrail and the `query_v1_output`/`query_v2_output` display) | `_rows_to_dicts()` — `core/equivalence.py` | **BUG — FOUND BY PHASE B'S FUZZER, FIXED.** `dict(zip(cols, row))` silently dropped every SELECT column but the last sharing a name (e.g. unaliased `SELECT t.account_id, a.account_id`) — legal SQL that becomes common once queries join multiple tables sharing a column name, as the new join-chain fuzzer generator does. This could collapse two genuinely different output rows into equal tuples, making the row-27 guardrail wrongly downgrade a real `divergent` verdict to `error` (found via `differential_test.py` seed 2782, an outer-join-chain pair) — never a false "equivalent", but a real usability/soundness-guardrail bug in the witness display path, not the Z3 encoding itself. Fixed by disambiguating repeated column names (`id`, `id_2`, …) so every column survives. Regression: `tests/smoke_test.py::test_witness_replay_disambiguates_duplicate_output_column_names`. |
| 27a | `With(Q̃, R⃗, Q)` — CTEs (Fig. 4 grammar; Fig. 5 `[[With]]_D = [[Q]]_D′` where `D′ = D[Rᵢ ↦ [[Qᵢ]]_D]`) | `encode_query` **materializes** each non-recursive CTE: the body is encoded to a `QueryFormula` and bound into the SymbolicDB as a pseudo-table (`register_cte_relation`) that the main query reads like a base table — the paper's `D′` binding. Per-source row counts let a CTE relation differ in size from `bound`; `db.cte_col_types` carries derived column types; witness materialization skips CTE pseudo-tables (they recompute from base rows under SQLite) | MATCHES the paper's approach within scope. A CTE body may be any supported query (aggregating, multi-table, outer-join, or a chain — row 14a). **Scope restriction** (sound, fail-closed): a CTE relation is usable only in FROM / INNER-join positions — if the query has an outer (LEFT/RIGHT/FULL) join ANYWHERE in its chain and ANY alias resolves to a CTE relation, the whole query → `error` (a materialized relation's cells have no null-extension handling; the guard doesn't yet distinguish which alias is on the outer-joined side, so this is conservative — a CTE combined with an outer join elsewhere in the same chain is also rejected, a documented follow-up). CTE-on-CTE and `WITH RECURSIVE` also fail-closed. No false-equivalent path. Fuzzer-checked by `cte_wrap` + `materialize_wrap` (SQLite runs the CTE natively; we materialize) |
| 28 | Unsupported operators (recursive CTEs, CTE-on-CTE, CTE on an outer-join side, correlated / scalar subqueries, `EXISTS`, `IN (SELECT…)` outside WHERE / with tuple-LHS / multi-column body, Distinct, set ops, windows, BETWEEN/LIKE, arithmetic exprs, AVG/MIN/MAX, CROSS/self-join, a join's ON referencing a table not yet in scope, …) | `parse_query` / `_parse_select_ast` / `encode_query` / `_parse_predicate` / `_parse_select_expr` / `build_join_bag` raise `ValueError` → status `error` | N/A — fail-closed by design; never silently dropped, so no false-equivalent path. (OR / NOT / `IN (value-list)` see row 10; uncorrelated `IN (SELECT…)` semi-join see row 10a; non-recursive CTE materialization see row 27a; join chains see row 14a.) |

## Summary

- **One bug found and fixed** (row 19a): grouped queries with a bare SELECT
  column outside the GROUP BY keys were silently given group-leader semantics
  instead of being rejected. Fixed in `core/sql_encoder.py`; now fail-closed.
- **No open BUG rows.** Every other paper rule that applies to the V1 subset
  is either encoded faithfully or rejected fail-closed.
- Three intentional deviations (#6, #8, #25) all err in the *safe* direction
  for the tool's core promise: none can turn a real divergence into a false
  "equivalent" within the bounded domain. #8 and #6 can produce
  counterexamples a constrained real database would reject; #25 bounds the
  meaning of "equivalent" (documented in CLAUDE.md).
- The one silent soundness dependency worth protecting with a test:
  PK uniqueness (#5) compares values without NULL guards and relies on
  `ddl_parser.py` marking PK columns `nullable=False` (#4).
  Covered by `tests/paper_cases_test.py::test_pk_columns_forced_non_null`.

## Empirical validation

- `tests/paper_cases_test.py` — deterministic cases derived from the paper
  (Fig. 6 join semantics, three-valued logic, NULL aggregation, Dedup of NULL
  group keys, composite-PK admissibility, bag multiplicity, domain-window
  widening).
- `tests/differential_test.py` — randomized differential testing: random
  schemas + query pairs in the V1 subset; every `equivalent` verdict is
  attacked with randomly sampled concrete databases executed on SQLite, and
  every `divergent` witness is replayed. Any disagreement fails the run and
  prints the reproducing seed.

Audit campaign results (2026-06-10, post-fix): 18/18 paper cases, 28/28 smoke
tests, and 1,700 fuzz seeds (1,230 @ bound 2, 500 @ bound 3 across seed ranges
0–, 1000–, 5000–, 9000–) with **zero failures** — no false-equivalent verdict
(632 equivalent verdicts × 25–40 concrete DBs each ≈ 16k SQLite cross-checks),
every one of 1,068 divergence witnesses reproduced, no unexpected errors.
