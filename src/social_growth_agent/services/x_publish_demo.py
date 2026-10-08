"""Opt-in live demo: publish ONE harmless post to X through the real pipeline.

This is the only code path in the project that creates a real post. It is triple
gated and never runs by accident:

1. ``RUN_LIVE_X_PUBLISH_TESTS=1``;
2. ``CONFIRM_LIVE_X_PUBLISH=YES``;
3. a typed ``publish`` on the terminal, after the exact text has been printed.

Credentials being present is never enough. Nothing is ever deleted afterwards.

    RUN_LIVE_X_PUBLISH_TESTS=1 CONFIRM_LIVE_X_PUBLISH=YES \\
      PUBLISHER_PROVIDER=x DATABASE_URL=postgresql://... \\
      uv run python -m social_growth_agent.services.x_publish_demo

It creates a run with the deterministic fake agents and mock research (so the demo
costs nothing but the one post), submits the harmless line as a human edit so it goes
through re-critique like any other content, approves the result, requests publication,
runs one worker cycle, prints the status and post URL, and then shows that a second
request for the same candidate is refused.
"""

import os
import sys

from social_growth_agent.agents.fakes import build_fake_llm
from social_growth_agent.config import AppSettings
from social_growth_agent.errors import PublicationExistsError
from social_growth_agent.graph import Dependencies
from social_growth_agent.models import ReviewAction, ReviewDecision, utc_now
from social_growth_agent.persistence import publications as pubs
from social_growth_agent.persistence.migrate import upgrade
from social_growth_agent.policies import content_sha256
from social_growth_agent.providers.mocks import MockResearchProvider
from social_growth_agent.services.demo import demo_account
from social_growth_agent.services.publications import PublicationService
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_publisher_worker, build_runtime

GATE_ENV = "RUN_LIVE_X_PUBLISH_TESTS"
CONFIRM_ENV = "CONFIRM_LIVE_X_PUBLISH"
TYPED_CONFIRMATION = "publish"


def post_text() -> str:
    """One harmless line, with a timestamp so it is never duplicate content."""
    return (
        "Testing an approval-gated publishing pipeline "
        f"(automated test post, {utc_now().isoformat(timespec='seconds')})"
    )


def gates_open() -> tuple[bool, str]:
    if os.environ.get(GATE_ENV) != "1":
        return False, f"{GATE_ENV} is not 1: refusing to publish anything"
    if os.environ.get(CONFIRM_ENV) != "YES":
        return False, f"{CONFIRM_ENV} is not YES: refusing to publish anything"
    return True, ""


def main() -> int:
    allowed, reason = gates_open()
    if not allowed:
        print(reason, file=sys.stderr)
        return 2
    settings = AppSettings()
    if settings.database_url is None:
        print("Set DATABASE_URL to a scratch PostgreSQL database.", file=sys.stderr)
        return 2
    if settings.publisher_provider != "x":
        print("Set PUBLISHER_PROVIDER=x for the live demo.", file=sys.stderr)
        return 2

    content = post_text()
    print("This will create ONE real post on the account whose X_PUBLISH_* credentials")
    print("are configured. It is never deleted afterwards. The exact text is:\n")
    print(f"    {content}\n")
    typed = input(f"Type '{TYPED_CONFIRMATION}' to publish it, anything else to abort: ").strip()
    if typed != TYPED_CONFIRMATION:
        print("aborted; nothing was published")
        return 1

    upgrade(settings.database_url.get_secret_value())
    runtime = build_runtime(
        settings,
        deps=Dependencies(llm=build_fake_llm(), research_provider=MockResearchProvider()),
        executor=InlineExecutor(),
    )
    try:
        return _publish_once(runtime, settings, content)
    finally:
        runtime.close()


def _publish_once(runtime: Runtime, settings: AppSettings, content: str) -> int:
    run_id, candidate_id = _approved_candidate(runtime, content)
    service = PublicationService(runtime.db)
    publication = service.request(run_id, candidate_id, requested_by="x-publish-demo")
    print(f"intent {publication.id} is {publication.status.value}; calling X once")

    worker = build_publisher_worker(settings, runtime.db)
    report = worker.run_once()
    with runtime.db.transaction() as session:
        view = pubs.publication_view(session, pubs.get_publication_row(session, publication.id))
    print(f"\nstatus: {view.status.value}")
    print(f"post id: {view.provider_post_id}")
    print(f"post url: {view.provider_post_url}")
    if view.failure_category:
        print(f"failure: {view.failure_category} - {view.failure_message}")
    print(f"attempts: {[(a.attempt, a.outcome) for a in view.attempts]}")

    try:
        service.request(run_id, candidate_id)
    except PublicationExistsError as exc:
        print(f"\na second publish request is refused (409): {exc}")
    else:  # pragma: no cover - would be a bug in the idempotency guarantee
        print("\nBUG: a second publication intent was created", file=sys.stderr)
        return 1
    print(f"platform calls made: {report.calls_made} (exactly one)")
    return 0 if view.status.value in ("published", "unknown", "failed") else 1


def _approved_candidate(runtime: Runtime, content: str) -> tuple[str, str]:
    """A normal run (offline agents): the harmless line goes in as a human edit, is
    re-critiqued like any other content, and is then approved. Nothing about the review
    guarantees is bypassed for the demo."""
    account, strategy = demo_account()
    run = runtime.service.create_run(account, strategy)
    [pending] = [p for p in runtime.service.pending_reviews() if p.run_id == run.id]
    runtime.service.submit_review(
        run.id,
        ReviewDecision(
            action=ReviewAction.EDIT,
            candidate_id=pending.candidates[0].id,
            edited_content=content,
            reviewer="x-publish-demo",
        ),
    )
    [after_edit] = [p for p in runtime.service.pending_reviews() if p.run_id == run.id]
    edited = next(c for c in after_edit.candidates if c.content == content)
    summary = runtime.service.submit_review(
        run.id,
        ReviewDecision(
            action=ReviewAction.APPROVE, candidate_id=edited.id, reviewer="x-publish-demo"
        ),
    )
    print(f"run {run.id} is {summary.status}; approved candidate {edited.id}")
    print(f"content hash: {content_sha256(content)[:12]}")
    return run.id, edited.id


if __name__ == "__main__":
    sys.exit(main())
