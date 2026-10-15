"""``sga-discovery``: content discovery, manual export and retention from the shell.

    uv run sga-discovery sources                               # registry; calls nothing
    uv run sga-discovery plan --account acct_1                 # caps and max cost; calls nothing
    uv run sga-discovery run --account acct_1 --by philipp     # one bounded session
    uv run sga-discovery show disc_...                         # a stored session
    uv run sga-discovery opportunities disc_...                # its ranked opportunities
    uv run sga-discovery start-run opp_... --intent news_commentary \\
        --account-file account.json --by philipp               # a run, to awaiting_review
    uv run sga-discovery export RUN CANDIDATE --by philipp     # approved text to post by hand
    uv run sga-discovery register RUN CANDIDATE https://x.com/you/status/123 --by philipp
    uv run sga-discovery purge 1900000000000000101 --by philipp
    uv run sga-discovery purge-expired                         # the X retention window

It uses the same configuration and services as the API. Commands that change anything
(run, start-run, export, register, purge) need the strategy review token, read from
``SGA_REVIEW_TOKEN`` and checked against ``STRATEGY_REVIEW_TOKEN`` exactly as the API's
bearer token is (it is never printed or stored). ``purge-expired`` is the scheduled
retention sweep and needs only database access. ``start-run`` runs the workflow in this
process until it waits for review; review and publishing stay in the API. Nothing here
ever posts, replies, mentions, follows, likes or sends a message.

Exit codes: 0 success, 1 refused or not found, 2 configuration error.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ValidationError

from social_growth_agent.config import AppSettings
from social_growth_agent.discovery.registry import load_registry
from social_growth_agent.errors import ConfigurationError, ReviewAuthError, SocialGrowthError
from social_growth_agent.models import Account, ContentIntent
from social_growth_agent.models.discovery import (
    DiscoveryRequest,
    PurgeRequest,
    StartRunFromOpportunity,
)
from social_growth_agent.models.strategy import ReviewAuthMethod
from social_growth_agent.persistence.db import redacted
from social_growth_agent.persistence.migrate import current_revision, head_revision
from social_growth_agent.services.discovery import (
    DiscoveryService,
    ExportBody,
    ManualPublicationBody,
)
from social_growth_agent.services.enrichment import EnrichmentBody, enrich_primary_source
from social_growth_agent.services.runs import InlineExecutor
from social_growth_agent.services.runtime import Runtime, build_runtime

TOKEN_ENV = "SGA_REVIEW_TOKEN"
MUTATING = frozenset({"run", "start-run", "export", "register", "purge", "enrich"})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sga-discovery", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="List the source registry (no database, no calls).")
    plan = sub.add_parser("plan", help="Show what a session would call. Calls nothing.")
    plan.add_argument("--account", required=True)
    plan.add_argument("--source", action="append", help="Registry source id (repeatable).")
    run = sub.add_parser("run", help="Run one bounded discovery session.")
    run.add_argument("--account", required=True)
    run.add_argument("--source", action="append", help="Registry source id (repeatable).")
    run.add_argument("--refresh", action="store_true", help="Ignore the cache window.")
    run.add_argument("--by", required=True, help="Who requested it (recorded).")
    show = sub.add_parser("show", help="Show a stored session and its fetches.")
    show.add_argument("session_id")
    opps = sub.add_parser("opportunities", help="List a session's ranked opportunities.")
    opps.add_argument("session_id")
    enrich = sub.add_parser(
        "enrich", help="Explicitly fetch a selected primary article; no model calls."
    )
    enrich.add_argument("opportunity_id")
    enrich.add_argument("--item", required=True)
    enrich.add_argument("--url", required=True)
    enrich.add_argument("--by", required=True)
    enrich.add_argument(
        "--refresh", action="store_true", help="Explicitly request a new evidence version."
    )
    start = sub.add_parser("start-run", help="Start a run from an opportunity.")
    start.add_argument("opportunity_id")
    start.add_argument("--intent", required=True, choices=[i.value for i in ContentIntent])
    start.add_argument("--account-file", required=True, type=Path, help="Account JSON.")
    start.add_argument("--by", required=True, help="The run's reviewer.")
    export = sub.add_parser("export", help="Export an approved candidate to post by hand.")
    export.add_argument("run_id")
    export.add_argument("candidate_id")
    export.add_argument("--by", required=True)
    register = sub.add_parser("register", help="Register a post you published by hand.")
    register.add_argument("run_id")
    register.add_argument("candidate_id")
    register.add_argument("post", help="The post's x.com URL or its id.")
    register.add_argument("--by", required=True)
    purge = sub.add_parser("purge", help="Remove the text of deleted platform items.")
    purge.add_argument("provider_item_ids", nargs="+")
    purge.add_argument("--platform", default="x")
    purge.add_argument("--by", required=True)
    sub.add_parser("purge-expired", help="Purge X text past DISCOVERY_X_RETENTION_DAYS.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = AppSettings()
    except (ConfigurationError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "sources":
        try:
            registry = load_registry(settings.discovery_registry_file)
        except ConfigurationError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        for spec in registry.sources:
            state = "enabled" if spec.enabled else "disabled"
            print(f"{spec.id:<24} {spec.kind.value:<12} {state:<9} {spec.publisher}")
        return 0
    if settings.database_url is None:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    url = settings.database_url.get_secret_value()
    revision = current_revision(url)
    if revision != head_revision():
        print(
            f"{redacted(url)} is at revision {revision}; run `sga-db upgrade` first",
            file=sys.stderr,
        )
        return 2
    try:
        runtime = build_runtime(settings, executor=InlineExecutor())
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        return _dispatch(runtime, args)
    except ReviewAuthError as exc:
        print(f"refused: {exc} (set {TOKEN_ENV})", file=sys.stderr)
        return 2
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except (SocialGrowthError, ValidationError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.close()


def _dispatch(runtime: Runtime, args: argparse.Namespace) -> int:
    discovery = runtime.discovery
    assert discovery is not None
    auth = _auth(runtime) if args.command in MUTATING else ReviewAuthMethod.REVIEW_TOKEN
    match args.command:
        case "plan":
            _emit(discovery.plan(_request(args, "plan")))
        case "run":
            _emit(discovery.run_session(_request(args, args.by), auth=auth))
        case "show":
            _emit(discovery.get_session(args.session_id))
        case "opportunities":
            _opportunities(discovery, args.session_id)
        case "enrich":
            _emit(
                enrich_primary_source(
                    discovery,
                    args.opportunity_id,
                    EnrichmentBody(
                        item_id=args.item,
                        url=args.url,
                        requested_by=args.by,
                        refresh=args.refresh,
                    ),
                    auth=auth,
                )
            )
        case "start-run":
            account = Account.model_validate_json(args.account_file.read_text(encoding="utf-8"))
            body = StartRunFromOpportunity(
                account=account, intent=ContentIntent(args.intent), reviewer=args.by
            )
            _emit(discovery.start_run(args.opportunity_id, body, auth=auth))
        case "export":
            body_e = ExportBody(exported_by=args.by)
            _emit(discovery.export(args.run_id, args.candidate_id, body_e, auth=auth))
        case "register":
            body_m = ManualPublicationBody(post=args.post, registered_by=args.by)
            _emit(
                discovery.register_manual_publication(
                    args.run_id, args.candidate_id, body_m, auth=auth
                )
            )
        case "purge":
            body_p = PurgeRequest(
                platform=args.platform,
                provider_item_ids=list(args.provider_item_ids),
                requested_by=args.by,
            )
            _emit(discovery.purge(body_p, auth=auth))
        case "purge-expired":
            _emit(discovery.purge_expired())
    return 0


def _auth(runtime: Runtime) -> ReviewAuthMethod:
    token = os.environ.get(TOKEN_ENV, "").strip()
    return runtime.review_auth.authenticate(f"Bearer {token}" if token else None)


def _request(args: argparse.Namespace, requested_by: str) -> DiscoveryRequest:
    return DiscoveryRequest(
        account_id=args.account,
        sources=args.source,
        refresh=bool(getattr(args, "refresh", False)),
        requested_by=requested_by,
    )


def _opportunities(discovery: DiscoveryService, session_id: str) -> None:
    for opp in discovery.opportunities(session_id):
        intents = ", ".join(i.value for i in opp.suggested_intents)
        grok = " grok-eligible" if opp.grok.eligible else ""
        print(f"#{opp.rank} {opp.score:.3f} {opp.id} [{opp.status.value}]{grok}")
        print(f"    {opp.headline}")
        print(f"    intents: {intents}")
        for reason in opp.reasons[:3]:
            print(f"    - {reason}")


def _emit(view: BaseModel) -> None:
    print(json.dumps(view.model_dump(mode="json"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
