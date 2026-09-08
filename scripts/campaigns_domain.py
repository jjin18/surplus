"""
scripts/campaigns_domain.py : set up and check the campaign sending domain.

The gate itself lives in backend/campaigns_send.py; this is a thin CLI over it,
deliberately holding no knowledge of its own. Everything it prints is computed
from that module, so a record you paste from here cannot drift from the rules
the send path actually enforces.

    # The DNS to publish, for the subdomain you will send from:
    python -m scripts.campaigns_domain records campaigns.surpluslayer.com

    # ...with the SPF include filled in and DMARC reports going somewhere:
    python -m scripts.campaigns_domain records campaigns.surpluslayer.com \
        --provider ses --dmarc-report-to dmarc@surpluslayer.com

    # The warmup ramp as dates, from the day you first send:
    python -m scripts.campaigns_domain calendar --start 2026-09-09

    # Is the environment complete enough to send anything at all?
    python -m scripts.campaigns_domain check

PUBLISH ON A SUBDOMAIN. A domain holds one SPF record, so pasting SPF onto an
apex that already carries mail replaces what is there and breaks every existing
mailbox on it. `check` will say so if you point it at an apex it recognises as
already in use.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from backend import campaigns_send as snd

# Hosts known to carry real mail or a live site. Sending from one of these is
# not a style preference: it puts the deliverability of support@ and every
# human mailbox on the same domain behind a cold outreach campaign.
IN_USE = ("surpluslayer.com", "www.surpluslayer.com", "event.surpluslayer.com",
          "join.surpluslayer.com")


def _warn_if_in_use(domain: str) -> None:
    domain = domain.strip().lower().lstrip("@")
    if domain in IN_USE:
        print(f"warning: {domain} already carries mail or a live site.\n"
              f"         Publishing SPF here REPLACES the existing record and\n"
              f"         breaks every mailbox on it. Send from a subdomain\n"
              f"         (campaigns.{domain}) instead.\n", file=sys.stderr)


def cmd_records(args) -> int:
    _warn_if_in_use(args.domain)
    try:
        records = snd.dns_records(args.domain,
                                  dmarc_report_to=args.dmarc_report_to,
                                  provider=args.provider)
    except snd.NotConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for record in records:
        print(f"{record['purpose']:<8} {record['type']:<4} {record['host']}")
        print(f"         value: {record['value']}")
        print(f"         why:   {record['note']}\n")
    return 0


def cmd_calendar(args) -> int:
    start = date.fromisoformat(args.start) if args.start else date.today()
    rows = snd.warmup_schedule()
    print(f"{'day':>4} {'date':>12} {'cap':>7} {'cumulative':>11}")
    for row in rows:
        when = start + timedelta(days=row["day"] - 1)
        print(f"{row['day']:>4} {when.isoformat():>12} "
              f"{row['cap']:>7} {row['cumulative']:>11}")
    last_day, last_cap = snd.WARMUP_RAMP[-1]
    print(f"\nramp ends {start + timedelta(days=last_day - 1)}, "
          f"then {last_cap}/day.")
    return 0


def cmd_check(args) -> int:
    try:
        identity = snd.load_identity()
    except snd.NotConfigured as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return 1

    _warn_if_in_use(identity.domain)
    print(f"from:       {identity.from_name} <{identity.from_address}>")
    print(f"domain:     {identity.domain}")
    print(f"postal:     {identity.postal_address}")
    print(f"unsub:      {identity.unsubscribe}")
    print(f"reply-to:   {identity.reply_to or '(none)'}")
    print(f"inbound:    {'declared working' if identity.accepts_replies else 'not declared'}")
    if not identity.accepts_replies:
        print("            -> the footer will route opt-outs to the unsubscribe\n"
              "               URL only, and will NOT offer 'reply STOP'. That is\n"
              "               correct until an MX exists and a test reply arrives.")
    print("\nidentity is complete; the gate will not refuse on configuration.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[1])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("records", help="DNS to publish for a sending domain")
    p.add_argument("domain")
    p.add_argument("--provider", default="",
                   help=f"one of: {', '.join(sorted(snd.PROVIDER_SPF))}")
    p.add_argument("--dmarc-report-to", default="")
    p.set_defaults(func=cmd_records)

    p = sub.add_parser("calendar", help="the warmup ramp as dates")
    p.add_argument("--start", default="", help="first sending day, YYYY-MM-DD")
    p.set_defaults(func=cmd_calendar)

    p = sub.add_parser("check", help="is the sending identity complete?")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
