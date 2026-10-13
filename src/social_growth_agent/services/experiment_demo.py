"""Phase 8 demo: pre-registered experiments on SYNTHETIC data, six scenarios.

    DATABASE_URL=postgresql://... uv run python -m social_growth_agent.services.experiment_demo

Offline and free: no model, research or platform call. Every number below comes from
generated data labelled SYNTHETIC; none of it is evidence about real posts, and no
synthetic result can be adopted. The scenarios:

1. positive effect (hook: question vs number), including one real assignment through the
   service with the fake agents, to show the treatment enforced as a hard rule;
2. null effect (length: 180-260 vs 80-140 characters);
3. insufficient data at the maximum duration;
4. differential attrition (the variant's posts are published far less often);
5. missing impressions (rates cannot be computed for many snapshots);
6. early unblinding (outcomes looked at mid-way: exploratory only, never adoptable).

Use a scratch database: the demo runs ``sga-db upgrade`` on it.
"""

import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from pydantic import SecretStr

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import AdoptionRefusedError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import (
    Account,
    ContentStrategy,
    HookType,
    LengthGuidance,
    new_id,
    utc_now,
)
from social_growth_agent.models.experiment import (
    ArmSpec,
    ExperimentAnalysis,
    ExperimentDesign,
    ExperimentFactor,
    Treatment,
)
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.persistence.views import ExperimentView
from social_growth_agent.providers.mocks import MockPublisher, MockResearchProvider
from social_growth_agent.services.experiment_seed import (
    Outcome,
    SeedOutcome,
    backdate_start,
    rate_outcome,
    seed_experiment_runs,
)
from social_growth_agent.services.experiments import (
    AdoptBody,
    CreateExperimentBody,
    ExperimentActionBody,
    ExperimentRunBody,
    UnblindBody,
)
from social_growth_agent.services.review_auth import ReviewAuth
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime

REVIEWER = "demo-reviewer"
Print = Callable[[str], None]


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    experiment: ExperimentView
    analysis: ExperimentAnalysis
    adopt_refusal: str | None


def build(settings: AppSettings) -> Runtime:
    deps = Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider())
    return build_runtime(settings, deps=deps, executor=InlineExecutor(), publisher=MockPublisher())


def hook_design(**overrides: object) -> ExperimentDesign:
    return ExperimentDesign.model_validate(
        {
            "factor": ExperimentFactor.HOOK_TYPE,
            "hypothesis": "Question hooks earn a higher engagement rate than number hooks.",
            "arms": [
                ArmSpec(
                    label="number", is_control=True, treatment=Treatment(hook_type=HookType.NUMBER)
                ),
                ArmSpec(label="question", treatment=Treatment(hook_type=HookType.QUESTION)),
            ],
            "planned_n_per_arm": 30,
            "bootstrap_resamples": 2000,
            "synthetic": True,
            **overrides,
        }
    )


def length_design(**overrides: object) -> ExperimentDesign:
    return ExperimentDesign.model_validate(
        {
            "factor": ExperimentFactor.LENGTH,
            "hypothesis": "Longer posts (180-260 characters) change the engagement rate.",
            "arms": [
                ArmSpec(
                    label="short",
                    is_control=True,
                    treatment=Treatment(length=LengthGuidance(min_chars=80, max_chars=140)),
                ),
                ArmSpec(
                    label="long",
                    treatment=Treatment(length=LengthGuidance(min_chars=180, max_chars=260)),
                ),
            ],
            "planned_n_per_arm": 30,
            "bootstrap_resamples": 2000,
            "synthetic": True,
            **overrides,
        }
    )


class Demo:
    def __init__(self, runtime: Runtime, auth: ReviewAuthMethod, out: Print) -> None:
        assert runtime.strategies is not None and runtime.experiments is not None
        self.runtime = runtime
        self.strategies = runtime.strategies
        self.experiments = runtime.experiments
        self.auth = auth
        self.out = out

    def account(self, handle: str) -> Account:
        account = Account(id=new_id("acct"), handle=handle, niche="AI engineering")
        strategy = ContentStrategy(
            id=new_id("strat"),
            account_id=account.id,
            pillars=["llm evaluation"],
            tone="practical",
            target_audience="engineers shipping LLM features",
            preferred_hooks=[HookType.NUMBER],
        )
        self.strategies.register_version(strategy, reviewer=REVIEWER, auth=self.auth, note=None)
        self.strategies.activate(
            strategy.id,
            1,
            expected_active_version_id=None,
            reviewer=REVIEWER,
            auth=self.auth,
            note=None,
        )
        return account

    def open(self, account: Account, design: ExperimentDesign, days_ago: int) -> ExperimentView:
        x = self.experiments.create(
            CreateExperimentBody(account_id=account.id, design=design, reviewer=REVIEWER),
            auth=self.auth,
        )
        action = ExperimentActionBody(reviewer=REVIEWER)
        self.experiments.approve(x.id, action, auth=self.auth)
        self.experiments.start(x.id, action, auth=self.auth)
        backdate_start(self.runtime.db, x.id, utc_now() - timedelta(days=days_ago))
        return x

    def seed(
        self,
        x: ExperimentView,
        account: Account,
        count: int,
        outcome: Outcome,
        *,
        days_ago: float,
        spacing_hours: float,
        rng: random.Random,
    ) -> None:
        seed_experiment_runs(
            self.runtime.db,
            x.id,
            account=account,
            count=count,
            outcome=outcome,
            start=utc_now() - timedelta(days=days_ago),
            spacing=timedelta(hours=spacing_hours),
            rng=rng,
        )

    def conclude(self, name: str, x: ExperimentView) -> ScenarioResult:
        concluded = self.experiments.conclude(
            x.id, ExperimentActionBody(reviewer=REVIEWER), auth=self.auth
        )
        analysis = self.experiments.analysis(x.id)
        refusal = None
        variant = next(a.label for a in concluded.arms if not a.is_control)
        try:
            self.experiments.adopt(
                x.id, AdoptBody(variant=variant, reviewer=REVIEWER), auth=self.auth
            )
        except AdoptionRefusedError as exc:
            refusal = str(exc)
        self.show(concluded, analysis, refusal)
        return ScenarioResult(name, concluded, analysis, refusal)

    def show(self, x: ExperimentView, analysis: ExperimentAnalysis, refusal: str | None) -> None:
        out = self.out
        for f in analysis.funnel:
            out(
                f"    funnel {f.arm:<9} assigned={f.assigned} generated={f.generated} "
                f"reviewed={f.reviewed} approved={f.approved} published={f.published} "
                f"eligible={f.snapshot_eligible} analysable={f.analysable}"
            )
        for o in analysis.outcomes or []:
            out(
                f"    outcome {o.arm:<9} n={o.n} pooled rate={o.pooled_rate} median={o.median_rate}"
            )
        for e in analysis.effects or []:
            out(
                f"    {e.variant} - {e.control}: pooled difference {e.pooled_difference} "
                f"[{e.ci_low}, {e.ci_high}] at {e.confidence_level}; median difference "
                f"{e.median_difference} [{e.median_ci_low}, {e.median_ci_high}]"
            )
        flags = ", ".join(f.value for f in analysis.flags)
        assert x.result is not None
        out(
            f"    VERDICT {x.result.verdict.value} ({x.result.evidence_class.value}); "
            f"flags: {flags}"
        )
        out(f"    adoptable: {x.result.adoptable}; adopt refused: {refusal}")


def run_scenarios(
    runtime: Runtime, auth: ReviewAuthMethod, out: Print = print
) -> list[ScenarioResult]:
    demo = Demo(runtime, auth, out)
    results = []

    out("== 1. positive effect (SYNTHETIC): question vs number hooks")
    account = demo.account("@phase8_positive")
    x = demo.open(account, hook_design(), days_ago=40)
    fx = runtime.experiments
    assert fx is not None
    for line in fx.get(x.id).feasibility.warnings or ["no feasibility warning"]:
        out(f"    feasibility: {line}")
    created = fx.create_run(x.id, ExperimentRunBody(account=account, reviewer=REVIEWER), auth=auth)
    detail = runtime.service.get_run(created.run.id)
    hooks = sorted({c.hook_type for c in detail.candidates})
    out(
        f"    real assignment through the service: run {created.run.id} -> arm "
        f"{created.assignment.arm}; status {detail.run.status.value}; candidates declare {hooks}"
    )
    rng = random.Random(8001)
    demo.seed(
        x,
        account,
        64,
        rate_outcome({"number": 0.04, "question": 0.06}),
        days_ago=38,
        spacing_hours=12,
        rng=rng,
    )
    masked = fx.analysis(x.id)
    out(
        f"    while running, masked fields: {len(masked.masked_fields)}; outcomes={masked.outcomes}"
    )
    usage = fx.usage(x.id)
    for arm in usage.per_arm:
        out(
            f"    usage {arm.arm}: {arm.runs} runs, estimated at run pricing "
            f"{arm.at_run_pricing_total} (fake agents and seeded runs are unbilled)"
        )
    p = usage.projected
    out(
        f"    projection: {p.remaining_runs} runs left under the cap, mean per finished run "
        f"{p.mean_cost_per_run}, projected {p.projected_remaining}; "
        f"missing prices: {p.missing_prices or 'none'}"
    )
    results.append(demo.conclude("positive", x))

    out("\n== 2. null effect (SYNTHETIC): long vs short posts")
    account = demo.account("@phase8_null")
    x = demo.open(account, length_design(), days_ago=40)
    demo.seed(
        x,
        account,
        64,
        rate_outcome({"short": 0.05, "long": 0.05}),
        days_ago=38,
        spacing_hours=12,
        rng=random.Random(8003),
    )
    results.append(demo.conclude("null", x))

    out("\n== 3. insufficient data at the maximum duration (SYNTHETIC)")
    account = demo.account("@phase8_insufficient")
    x = demo.open(account, hook_design(max_duration_days=7), days_ago=20)
    demo.seed(
        x,
        account,
        24,
        rate_outcome({"number": 0.04, "question": 0.08}),
        days_ago=19,
        spacing_hours=6,
        rng=random.Random(8003),
    )
    results.append(demo.conclude("insufficient_data", x))

    out("\n== 4. differential attrition (SYNTHETIC)")
    account = demo.account("@phase8_attrition")
    x = demo.open(account, hook_design(), days_ago=40)
    base = rate_outcome({"number": 0.04, "question": 0.06})

    def attrition(label: str, index: int, r: random.Random) -> SeedOutcome:
        outcome = base(label, index, r)
        if label == "question" and index % 2:
            return SeedOutcome(approved=False, published=False)
        return outcome

    demo.seed(x, account, 140, attrition, days_ago=38, spacing_hours=6, rng=random.Random(8004))
    results.append(demo.conclude("attrition", x))

    out("\n== 5. missing impressions (SYNTHETIC)")
    account = demo.account("@phase8_impressions")
    x = demo.open(account, hook_design(), days_ago=40)
    rated = rate_outcome({"number": 0.04, "question": 0.06})

    def no_impressions(label: str, index: int, r: random.Random) -> SeedOutcome:
        outcome = rated(label, index, r)
        if index % 10 < 3 and outcome.metrics is not None:
            return SeedOutcome(metrics={**outcome.metrics, "impressions": None})
        return outcome

    demo.seed(x, account, 90, no_impressions, days_ago=38, spacing_hours=9, rng=random.Random(8005))
    results.append(demo.conclude("missing_impressions", x))

    out("\n== 6. early unblinding (SYNTHETIC)")
    account = demo.account("@phase8_unblinded")
    x = demo.open(account, hook_design(), days_ago=40)
    rng = random.Random(8006)
    outcome = rate_outcome({"number": 0.04, "question": 0.06})
    demo.seed(x, account, 24, outcome, days_ago=38, spacing_hours=12, rng=rng)
    fx.unblind(
        x.id, UnblindBody(reviewer=REVIEWER, reason="stakeholder asked for a preview"), auth=auth
    )
    interim = fx.analysis(x.id)
    for e in interim.effects or []:
        out(
            f"    interim after unblinding: {e.variant} - {e.control} {e.pooled_difference} "
            f"[{e.ci_low}, {e.ci_high}], verdict {e.verdict}"
        )
    demo.seed(x, account, 40, outcome, days_ago=26, spacing_hours=12, rng=rng)
    results.append(demo.conclude("early_unblinding", x))

    return results


def main() -> int:
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database (see docs/DATABASE.md).")
        return 2
    upgrade(settings.database_url.get_secret_value())
    token = secrets.token_urlsafe(24)  # never printed
    settings = settings.model_copy(
        update={
            "llm_provider": "fake",
            "research_provider": "mock",
            "publisher_provider": "mock",
            "strategy_proposer": "disabled",
            "strategy_review_token": SecretStr(token),
        }
    )
    runtime = build(settings)
    auth = ReviewAuth(token=SecretStr(token)).authenticate(f"Bearer {token}")
    print("Every number below is SYNTHETIC demo data, not evidence about real posts.\n")
    run_scenarios(runtime, auth)
    print("\nNothing was published, adopted or activated by any experiment.")
    runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
