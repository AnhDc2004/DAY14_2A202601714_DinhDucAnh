"""
Day 14 — AI Evaluation & Benchmarking Pipeline
AICB-P1: AI Practical Competency Program, Phase 1

Key concepts from lecture:
    - Evaluation = Scientific Method for AI (Hypothesis → Experiment → Measure → Conclude → Iterate)
    - 4 nhóm metrics: Task Completion, Answer Quality, RAG-Specific, Business
    - RAG pipeline metrics: Context Recall → Context Precision → Faithfulness → Answer Relevancy
    - LLM-as-Judge: rubric scoring 1-5, detect bias (positional, verbosity, self-preference)
    - Golden dataset: stratified sampling (5 Easy + 7 Medium + 5 Hard + 3 Adversarial)
    - Failure taxonomy: hallucination, irrelevant, incomplete, off_topic, refusal
    - 5 Whys method for root cause analysis
    - CI/CD integration: eval as quality gate (score < threshold = block deploy)
    - Continuous Improvement Loop: Evaluate → Analyze → Improve → Augment → Repeat

Implementation notes (solution):
    - Mọi metric là heuristic word-overlap trên token đã bỏ stopwords.
    - Retrieval metrics (context_recall / context_precision) chỉ mang tính chẩn
      đoán: KHÔNG tham gia overall_score() và KHÔNG đổi pass rule.
    - Không thay đổi signature của bất kỳ class/function nào trong template.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Task 1 — Data Models (Golden Dataset + Evaluation Results)
# ---------------------------------------------------------------------------

@dataclass
class QAPair:
    """
    A question-answer pair for evaluation (part of the Golden Dataset).

    Fields:
        question:        The question to answer.
        expected_answer: The reference/ground-truth answer (expert-written).
        context:            Source context (may be empty string if not applicable).
        metadata:           Optional metadata dict (difficulty, category, etc.).
        retrieved_contexts: List of retrieved chunks (ORDER = retriever rank).
                            Used by the retrieval-side metrics (Task 2b).
    """

    question: str = ""
    expected_answer: str = ""
    context: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    retrieved_contexts: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    """
    Evaluation result for a single Q&A pair.

    Score interpretation (lecture):
        0.8-1.0: Good (monitor, maintain)
        0.6-0.8: Needs work (analyze failures, iterate)
        < 0.6:   Significant issues (deep investigation required)

    Fields:
        qa_pair:        The original QAPair.
        actual_answer:  What the agent actually returned.
        faithfulness:   Float 0-1, how grounded the answer is in context.
        relevance:      Float 0-1, how relevant the answer is to the question.
        completeness:   Float 0-1, how complete the answer is vs expected.
        passed:         True if all three scores >= 0.5.
        failure_type:   None if passed, otherwise one of:
                        "hallucination", "irrelevant", "incomplete", "off_topic".
        context_precision: Float 0-1 or None — quality of retrieval ranking.
        context_recall:    Float 0-1 or None — coverage of expected by context.
                        (Both stay None unless retrieved chunks are supplied;
                         they are NOT part of overall_score().)
    """

    qa_pair: QAPair | None = None
    actual_answer: str = ""
    faithfulness: float = 0.0
    relevance: float = 0.0
    completeness: float = 0.0
    passed: bool = False
    failure_type: str | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    def overall_score(self) -> float:
        """Average of faithfulness, relevance and completeness."""
        return (self.faithfulness + self.relevance + self.completeness) / 3.0


# ---------------------------------------------------------------------------
# Task 2 — RAGAS Evaluator (Simplified word-overlap heuristic)
# ---------------------------------------------------------------------------
# In production, replace with actual RAGAS / DeepEval / TruLens metrics.
# ---------------------------------------------------------------------------

# Common English stopwords are ignored so overlap reflects *content* words,
# not filler (otherwise "is"/"a"/"the" inflate every score).
STOPWORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "as", "by", "and", "or",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokenization, ignoring punctuation and stopwords."""
    if not text:
        return set()
    tokens = re.findall(r"\b\w+\b", text.lower())
    return {t for t in tokens if t not in STOPWORDS}


def _clamp(value: float) -> float:
    """Clamp a raw ratio into the [0.0, 1.0] range."""
    return max(0.0, min(1.0, float(value)))


def _overlap_ratio(source: str, reference: str) -> float:
    """|tokens(source) ∩ tokens(reference)| / |tokens(source)|.

    Returns 1.0 when *source* (the denominator) has no content tokens — an
    empty denominator carries no information to be wrong about.
    """
    source_tokens = _tokenize(source)
    if not source_tokens:
        return 1.0
    reference_tokens = _tokenize(reference)
    return _clamp(len(source_tokens & reference_tokens) / len(source_tokens))


class RAGASEvaluator:
    """
    Evaluates RAG pipeline outputs using RAGAS-inspired heuristics.

    All metrics use word overlap rather than LLM calls for simplicity.
    """

    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        """|answer ∩ context| / |answer| — how grounded the answer is.

        Returns 1.0 for an empty answer, clamped to [0.0, 1.0].
        """
        return _overlap_ratio(answer, context)

    def evaluate_relevance(self, answer: str, question: str) -> float:
        """|answer ∩ question| / |question| — does the answer address the ask.

        Returns 1.0 for an empty question, clamped to [0.0, 1.0].
        """
        return _overlap_ratio(question, answer)

    def evaluate_completeness(self, answer: str, expected: str) -> float:
        """|answer ∩ expected| / |expected| — coverage of the reference answer.

        Returns 1.0 for an empty expected answer, clamped to [0.0, 1.0].
        """
        return _overlap_ratio(expected, answer)

    # -----------------------------------------------------------------------
    # Task 2b — Retrieval-side metrics (evaluate the GET-CONTEXT step)
    # -----------------------------------------------------------------------

    def evaluate_context_recall(self, contexts: list[str], expected: str) -> float:
        """Context Recall — coverage of the expected answer by the UNION of
        retrieved chunks.

            recall = |expected ∩ ⋃ chunks| / |expected|

        Low recall => the retriever missed evidence the answer needs.
        """
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        union_tokens: set[str] = set()
        for chunk in contexts:
            union_tokens |= _tokenize(chunk)

        return _clamp(len(expected_tokens & union_tokens) / len(expected_tokens))

    def evaluate_context_precision(
        self,
        contexts: list[str],
        expected: str,
        relevance_threshold: float = 0.1,
    ) -> float:
        """Context Precision — rank-aware Average Precision (AP@K), like RAGAS.

        1. chunk relevant  <=>  |chunk ∩ expected| / |expected| >= threshold
        2. Precision@k = (#relevant in top-k) / k
        3. AP@K = (1 / #relevant) * Σ_k [ Precision@k · relevant_k ]

        1.0 if expected empty; 0.0 if no chunks or none relevant.
        """
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        # Step 1 — relevance flag per chunk, in retriever rank order.
        flags: list[bool] = []
        for chunk in contexts:
            coverage = len(_tokenize(chunk) & expected_tokens) / len(expected_tokens)
            flags.append(coverage >= relevance_threshold)

        total_relevant = sum(flags)
        if total_relevant == 0:
            return 0.0

        # Steps 2 & 3 — Average Precision @ K.
        hits = 0
        precision_sum = 0.0
        for k, is_relevant in enumerate(flags, start=1):
            if is_relevant:
                hits += 1
                precision_sum += hits / k  # Precision@k

        return _clamp(precision_sum / total_relevant)

    def run_full_eval(
        self,
        answer: str,
        question: str,
        context: str,
        expected: str,
        contexts: list[str] | None = None,
    ) -> EvalResult:
        """Run the three answer-side metrics and, when ``contexts`` is given,
        both retrieval-side metrics.

        passed = all three answer scores >= 0.5.

        failure_type (first match wins, only when failed):
            faithfulness < 0.3  → "hallucination"
            relevance    < 0.3  → "irrelevant"
            completeness < 0.3  → "incomplete"
            otherwise           → "off_topic"
        """
        faithfulness = self.evaluate_faithfulness(answer, context)
        relevance = self.evaluate_relevance(answer, question)
        completeness = self.evaluate_completeness(answer, expected)

        passed = faithfulness >= 0.5 and relevance >= 0.5 and completeness >= 0.5

        failure_type: str | None = None
        if not passed:
            if faithfulness < 0.3:
                failure_type = "hallucination"
            elif relevance < 0.3:
                failure_type = "irrelevant"
            elif completeness < 0.3:
                failure_type = "incomplete"
            else:
                failure_type = "off_topic"

        result = EvalResult(
            qa_pair=QAPair(
                question=question,
                expected_answer=expected,
                context=context,
                retrieved_contexts=list(contexts) if contexts else [],
            ),
            actual_answer=answer,
            faithfulness=faithfulness,
            relevance=relevance,
            completeness=completeness,
            passed=passed,
            failure_type=failure_type,
        )

        # Retrieval metrics are DIAGNOSTIC ONLY — computed after the pass rule
        # so they can never influence passed / overall_score().
        if contexts is not None:
            result.context_recall = self.evaluate_context_recall(contexts, expected)
            result.context_precision = self.evaluate_context_precision(contexts, expected)

        return result


# ---------------------------------------------------------------------------
# Reranking helper (Bonus — Exercise 3.5, boosting Context Precision)
# ---------------------------------------------------------------------------

def rerank_by_overlap(contexts: list[str], query: str) -> list[str]:
    """Minimal lexical reranker: chunks sorted by word overlap with the query,
    most-overlapping first. Stand-in for a real cross-encoder reranker.

    The retrieved SET is unchanged (so Context Recall is unchanged); only the
    ORDER changes, which is what rank-aware Context Precision measures.
    """
    query_tokens = _tokenize(query)
    # sorted() is stable => equal-overlap chunks keep their original rank.
    return sorted(
        contexts,
        key=lambda chunk: len(_tokenize(chunk) & query_tokens),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Task 3 — LLM Judge
# ---------------------------------------------------------------------------

class LLMJudge:
    """Uses an LLM to score AI responses according to a rubric."""

    DEFAULT_SCORE: float = 0.5

    def __init__(self, judge_llm_fn: Callable[[str], str]) -> None:
        self.judge_llm_fn = judge_llm_fn
        # Convenience alias so callers can use either name.
        self.judge_fn = judge_llm_fn

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _build_prompt(question: str, answer: str, rubric: dict[str, Any]) -> str:
        """Judge prompt = question + answer + rubric + strict output format."""
        rubric_lines = "\n".join(
            f"- {criterion}: {description}" for criterion, description in rubric.items()
        )
        criteria = ", ".join(f'"{c}": <score>' for c in rubric) or '"overall": <score>'
        return (
            "You are an impartial evaluation judge.\n"
            "Score the ANSWER against every rubric criterion on a 0.0-1.0 scale.\n"
            "Judge content only — do NOT reward length, ordering or writing style.\n\n"
            f"QUESTION:\n{question}\n\n"
            f"ANSWER:\n{answer}\n\n"
            f"RUBRIC:\n{rubric_lines}\n\n"
            "Return JSON only, in exactly this shape:\n"
            f'{{"scores": {{{criteria}}}, "reasoning": "<short rationale>"}}'
        )

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Pull the first JSON object out of an LLM response, if any."""
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _normalize(value: Any) -> float | None:
        """Coerce a judge score to [0, 1]; also accepts the 1-5 rubric scale."""
        if isinstance(value, bool) or value is None:
            return None
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        if score > 1.0:  # judge answered on the 1-5 rubric scale
            score = score / 5.0
        return _clamp(score)

    # -- public API ---------------------------------------------------------

    def score_response(
        self,
        question: str,
        answer: str,
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        """Score an AI response using the judge LLM.

        Unparseable judge output falls back to 0.5 per criterion.

        Returns:
            {"scores": dict[str, float], "reasoning": str}
        """
        prompt = self._build_prompt(question, answer, rubric)

        try:
            raw = self.judge_llm_fn(prompt)
        except Exception:  # a broken judge must not break the benchmark run
            raw = ""

        reasoning = raw if isinstance(raw, str) else str(raw)
        parsed = self._extract_json(reasoning)

        # Scores may be nested under "scores" or sit at the top level.
        payload: dict[str, Any] = {}
        if parsed is not None:
            candidate = parsed.get("scores", parsed)
            if isinstance(candidate, dict):
                payload = candidate

        scores: dict[str, float] = {}
        for criterion in rubric:
            normalized = self._normalize(payload.get(criterion))
            scores[criterion] = self.DEFAULT_SCORE if normalized is None else normalized

        # No rubric supplied: keep whatever numeric criteria the judge returned.
        if not rubric:
            for criterion, value in payload.items():
                normalized = self._normalize(value)
                if normalized is not None:
                    scores[criterion] = normalized

        return {"scores": scores, "reasoning": reasoning}

    @staticmethod
    def _entry_mean(entry: Any) -> float | None:
        """Mean score of one batch entry ({"scores": {...}} or a flat dict)."""
        if not isinstance(entry, dict):
            return None
        payload = entry.get("scores", entry)
        if not isinstance(payload, dict):
            return None
        values = [
            float(v) for v in payload.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if not values:
            return None
        return sum(values) / len(values)

    def detect_bias(self, scores_batch: list[dict[str, Any]]) -> dict[str, Any]:
        """Detect bias patterns across a batch of judge scores.

            positional_bias: the first-shown response consistently scores higher
            leniency_bias:   average score > 0.8 across all criteria
            severity_bias:   average score < 0.3 across all criteria
        """
        no_bias: dict[str, Any] = {
            "positional_bias": False,
            "leniency_bias": False,
            "severity_bias": False,
        }
        if not scores_batch:
            return no_bias

        per_entry = [self._entry_mean(entry) for entry in scores_batch]
        per_entry = [m for m in per_entry if m is not None]
        if not per_entry:
            return no_bias

        overall_mean = sum(per_entry) / len(per_entry)

        # Positional bias: the first entry outscores the rest by a margin large
        # enough not to be noise (0.1 on a 0-1 scale).
        positional = False
        if len(per_entry) >= 2:
            rest = per_entry[1:]
            positional = per_entry[0] > (sum(rest) / len(rest)) + 0.1

        return {
            "positional_bias": positional,
            "leniency_bias": overall_mean > 0.8,
            "severity_bias": overall_mean < 0.3,
        }


# ---------------------------------------------------------------------------
# Task 4 — Benchmark Runner
# ---------------------------------------------------------------------------
# Eval as CI/CD quality gate. Regression = metric average drop > 0.05.
# ---------------------------------------------------------------------------

REGRESSION_THRESHOLD: float = 0.05


class BenchmarkRunner:
    """Runs a full evaluation benchmark."""

    def run(
        self,
        qa_pairs: list[QAPair],
        agent_fn: Callable[[str], str],
        evaluator: RAGASEvaluator,
    ) -> list[EvalResult]:
        """Run every QA pair through the agent, then through the evaluator."""
        results: list[EvalResult] = []

        for pair in qa_pairs:
            answer = agent_fn(pair.question)

            # An empty retrieved list means "retrieval not measured here", so it
            # is passed as None and both retrieval metrics stay None.
            retrieved = getattr(pair, "retrieved_contexts", None) or None

            result = evaluator.run_full_eval(
                answer,
                pair.question,
                pair.context,
                pair.expected_answer,
                contexts=retrieved,
            )
            # Keep the ORIGINAL pair (with its metadata/id) on the result.
            result.qa_pair = pair
            result.actual_answer = answer
            results.append(result)

        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        """Aggregate report: totals, pass rate, metric averages, failure mix.

        Retrieval averages use only non-None scores, and are None when no
        result carries that metric.
        """
        total = len(results)
        if total == 0:
            return {
                "total": 0,
                "passed": 0,
                "pass_rate": 0.0,
                "avg_faithfulness": 0.0,
                "avg_relevance": 0.0,
                "avg_completeness": 0.0,
                "avg_context_recall": None,
                "avg_context_precision": None,
                "failure_types": {},
            }

        passed = sum(1 for r in results if r.passed)

        recalls = [r.context_recall for r in results if r.context_recall is not None]
        precisions = [
            r.context_precision for r in results if r.context_precision is not None
        ]

        failure_types: dict[str, int] = {}
        for r in results:
            if r.failure_type:
                failure_types[r.failure_type] = failure_types.get(r.failure_type, 0) + 1

        return {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total,
            "avg_faithfulness": sum(r.faithfulness for r in results) / total,
            "avg_relevance": sum(r.relevance for r in results) / total,
            "avg_completeness": sum(r.completeness for r in results) / total,
            "avg_context_recall": (sum(recalls) / len(recalls)) if recalls else None,
            "avg_context_precision": (
                (sum(precisions) / len(precisions)) if precisions else None
            ),
            "failure_types": failure_types,
        }

    def run_regression(self, new_results: list, baseline_results: list) -> dict:
        """Compare a new run against a baseline.

        Regression = a metric average dropped by more than 0.05 vs baseline.
        """

        def _avg(results: list, metric: str) -> float:
            if not results:
                return 0.0
            return sum(getattr(r, metric) for r in results) / len(results)

        metrics = ("faithfulness", "relevance", "completeness")
        report: dict[str, Any] = {}
        regressions: list[str] = []

        for metric in metrics:
            new_avg = _avg(new_results, metric)
            baseline_avg = _avg(baseline_results, metric)
            report[f"new_avg_{metric}"] = new_avg
            report[f"baseline_avg_{metric}"] = baseline_avg
            if (baseline_avg - new_avg) > REGRESSION_THRESHOLD:
                regressions.append(metric)

        report["regressions"] = regressions
        report["passed"] = len(regressions) == 0
        return report

    def identify_failures(
        self,
        results: list[EvalResult],
        threshold: float = 0.5,
    ) -> list[EvalResult]:
        """Return results where ANY answer-side score is below threshold."""
        return [
            r for r in results
            if min(r.faithfulness, r.relevance, r.completeness) < threshold
        ]


# ---------------------------------------------------------------------------
# Task 5 — Failure Analyzer
# ---------------------------------------------------------------------------
# Taxonomy: hallucination / irrelevant / incomplete / off_topic / refusal
# 5 Whys → root cause; clustering → one fix resolves many failures.
# ---------------------------------------------------------------------------

ROOT_CAUSE_RETRIEVAL = "Context is missing or irrelevant — improve retrieval"
ROOT_CAUSE_PROMPT = "Answer does not address the question — improve prompt clarity"
ROOT_CAUSE_GENERATION = (
    "Answer is missing key information — increase context window or improve generation"
)
ROOT_CAUSE_MULTIPLE = "Multiple issues detected — review full pipeline"


class FailureAnalyzer:
    """Analyzes failed evaluation results to find patterns and suggest fixes."""

    def categorize_failures(self, failures: list[EvalResult]) -> dict[str, int]:
        """Count failures by failure_type → {"hallucination": 3, ...}."""
        categories: dict[str, int] = {}
        for failure in failures:
            failure_type = getattr(failure, "failure_type", None)
            if not failure_type:
                continue
            categories[failure_type] = categories.get(failure_type, 0) + 1
        return categories

    def find_root_cause(self, failure: EvalResult) -> str:
        """Map the LOWEST answer-side score to the pipeline stage to fix.

        When two or more metrics tie for lowest, no single stage is to blame
        and the whole pipeline needs review.
        """
        scores = {
            ROOT_CAUSE_RETRIEVAL: failure.faithfulness,
            ROOT_CAUSE_PROMPT: failure.relevance,
            ROOT_CAUSE_GENERATION: failure.completeness,
        }
        lowest = min(scores.values())
        tied = [cause for cause, score in scores.items() if abs(score - lowest) < 1e-9]

        if len(tied) > 1:
            return ROOT_CAUSE_MULTIPLE
        return tied[0]

    def generate_improvement_log(self, failures: list, suggestions: list[str]) -> str:
        """Markdown table: one row per failure, Status always "Open"."""
        lines = [
            "| Failure ID | Type | Root Cause | Suggested Fix | Status |",
            "|------------|------|------------|---------------|--------|",
        ]

        for index, failure in enumerate(failures):
            failure_id = f"F{index + 1:03d}"
            failure_type = getattr(failure, "failure_type", None) or "unknown"
            root_cause = self.find_root_cause(failure)
            if suggestions:
                fix = suggestions[index] if index < len(suggestions) else suggestions[-1]
            else:
                fix = "No suggestion generated"
            lines.append(
                f"| {failure_id} | {failure_type} | {root_cause} | {fix} | Open |"
            )

        return "\n".join(lines)

    def generate_improvement_suggestions(
        self, failures: list[EvalResult]
    ) -> list[str]:
        """Prioritized, concrete improvement actions (>= 3 when failures exist)."""
        if not failures:
            return []

        categories = self.categorize_failures(failures)
        suggestions: list[str] = []

        playbook = {
            "hallucination": (
                "Add a grounding/hallucination checker that drops claims absent "
                "from the retrieved context, and require a citation per claim"
            ),
            "irrelevant": (
                "Tighten the system prompt and add query rewriting so the answer "
                "addresses the asked question instead of the nearest topic"
            ),
            "incomplete": (
                "Increase top_k and chunk size in the RAG pipeline to reduce "
                "context fragmentation, and add few-shot examples of complete answers"
            ),
            "off_topic": (
                "Add intent detection/routing before retrieval so out-of-domain "
                "questions go to a dedicated branch"
            ),
            "refusal": (
                "Loosen over-strict guardrails and whitelist in-scope topics so "
                "answerable questions are not refused"
            ),
        }

        # Biggest failure cluster first.
        for failure_type, _count in sorted(
            categories.items(), key=lambda kv: kv[1], reverse=True
        ):
            if failure_type in playbook:
                suggestions.append(playbook[failure_type])

        # Score-driven suggestions (independent of the taxonomy label).
        def _avg(metric: str) -> float:
            return sum(getattr(f, metric) for f in failures) / len(failures)

        if _avg("faithfulness") < 0.5:
            suggestions.append(
                "Rework chunking/embeddings: low average faithfulness means the "
                "generator is writing from the wrong evidence"
            )
        if _avg("completeness") < 0.5:
            suggestions.append(
                "Add answer-plan or checklist prompting so multi-part questions "
                "return every required element"
            )

        recalls = [
            f.context_recall for f in failures
            if getattr(f, "context_recall", None) is not None
        ]
        if recalls and (sum(recalls) / len(recalls)) < 0.6:
            suggestions.append(
                "Add a reranker and hybrid (BM25 + vector) retrieval: context "
                "recall is too low for the generator to succeed"
            )

        # Baseline actions so the list is always actionable (>= 3 items).
        fallbacks = [
            "Add the failing cases to the golden dataset so the fix is verified "
            "by regression on the next run",
            "Wire evaluation into CI/CD as a quality gate that blocks deploys "
            "when a metric average drops by more than 0.05",
            "Calibrate the LLM judge against human labels on the failing cases "
            "before trusting its scores",
        ]
        for suggestion in fallbacks:
            if len(suggestions) >= 3:
                break
            suggestions.append(suggestion)

        # Deduplicate, preserving priority order.
        seen: set[str] = set()
        unique: list[str] = []
        for suggestion in suggestions:
            if suggestion not in seen:
                seen.add(suggestion)
                unique.append(suggestion)
        return unique


# ---------------------------------------------------------------------------
# Entry point for manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    qa_pairs = [
        QAPair(
            question="What is RAG?",
            expected_answer="RAG stands for Retrieval-Augmented Generation, which combines retrieval with text generation.",
            context="RAG is a technique that retrieves relevant documents and uses them to ground LLM generation.",
            metadata={"difficulty": "easy", "category": "definition"},
        ),
        QAPair(
            question="What is the capital of France?",
            expected_answer="Paris is the capital of France.",
            context="France is a country in Western Europe. Its capital city is Paris.",
            metadata={"difficulty": "easy", "category": "factual"},
        ),
        QAPair(
            question="Explain backpropagation and why it matters for training",
            expected_answer="Backpropagation is an algorithm for training neural networks by computing gradients efficiently, enabling deep learning models to learn from errors.",
            context="Neural networks learn through gradient descent. Backpropagation efficiently computes these gradients layer by layer.",
            metadata={"difficulty": "medium", "category": "explanation"},
        ),
        QAPair(
            question="Should I use RAG or fine-tuning for my chatbot?",
            expected_answer="It depends on the use case: RAG is better for frequently updated knowledge, fine-tuning for consistent style/behavior. Consider cost, latency, and data freshness.",
            context="RAG retrieves external documents at inference time. Fine-tuning modifies model weights during training.",
            metadata={"difficulty": "hard", "category": "comparison"},
        ),
        QAPair(
            question="What is the meaning of life?",
            expected_answer="This question is outside the scope of this system. I can help with AI and technology questions.",
            context="This is an AI assistant specialized in technology topics.",
            metadata={"difficulty": "adversarial", "category": "out_of_scope"},
        ),
    ]

    evaluator = RAGASEvaluator()
    runner = BenchmarkRunner()

    def mock_agent(question: str) -> str:
        """Simple mock agent for testing. Replace with your actual agent."""
        return f"Based on my knowledge: {question[:30]}... The answer involves key concepts."

    results = runner.run(qa_pairs, mock_agent, evaluator)
    report = runner.generate_report(results)
    print("=== Benchmark Report ===")
    for k, v in report.items():
        print(f"  {k}: {v}")

    failures = runner.identify_failures(results, threshold=0.5)
    print(f"\n=== Failures ({len(failures)}) ===")
    analyzer = FailureAnalyzer()

    categories = analyzer.categorize_failures(failures)
    print("Failure Categories:", categories)

    for f in failures:
        cause = analyzer.find_root_cause(f)
        print(f"  Root cause: {cause}")

    suggestions = analyzer.generate_improvement_suggestions(failures)
    print("\nImprovement Suggestions:")
    for s in suggestions:
        print(f"  - {s}")

    log = analyzer.generate_improvement_log(failures, suggestions)
    print("\n=== Improvement Log ===")
    print(log)