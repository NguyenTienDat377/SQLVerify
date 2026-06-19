# Third-Party Notices

SQLVerify's core equivalence-checking engine is built on the following published research. Copies are included under their respective open licenses for reference; see each entry for attribution and license terms.

## VeriEQL

He, Y., Zhao, P., Wang, X., & Wang, Y. (2024). VeriEQL: Bounded Equivalence
Verification for Complex SQL Queries with Integrity Constraints.
_Proc. ACM Program. Lang._, 8(OOPSLA1), 1071–1099.
https://doi.org/10.1145/3649849

Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Local copy: [docs/references/veriEQL-2024.pdf](docs/references/veriEQL-2024.pdf)

SQLVerify's `core/sql_encoder.py` and `core/equivalence.py` implement the
symbolic-tuple encoding and bag-equality algorithm (Algorithm 1) described
in this paper.

## Research references (not yet implemented)

### SpotIt

Klopfenstein, R., He, Y., Tremante, A., Wang, Y., Narodytska, N., & Wu, H. (2026).
SpotIt: Evaluating Text-to-SQL Evaluation with Formal Verification.
_Published as a conference paper at ICLR 2026._
https://arxiv.org/abs/2510.26840

Licensed under CC BY-SA 4.0.

Referenced for planned future work — SQLVerify's V1 encoder does not yet
implement SpotIt's date/string/CASE-ITE extensions. Tracked on the project
roadmap under "VeriEQL encoder widening."
