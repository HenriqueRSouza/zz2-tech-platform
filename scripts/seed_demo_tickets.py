"""Fill a development installation with realistic demo tickets.

    .venv/bin/python scripts/seed_demo_tickets.py --dry-run
    .venv/bin/python scripts/seed_demo_tickets.py              # local store + database
    .venv/bin/python scripts/seed_demo_tickets.py --remove     # take them out again

Analytics reads the local ticket store, and the Tickets page reads the database, so
both receive the same rows. Every demo ticket carries ``DEMO_TAG`` in its notes,
which is how ``--remove`` finds them without touching anything else.

The data is shaped to make the charts say something: volume grows over the
period, a few kits are clearly troublesome, and client names arrive in the
spelling variants that the consolidation step exists to merge.

**Refuses to run when ``ZZ2_ENVIRONMENT`` is ``production``.**
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zz2.core.errors import UpstreamError  # noqa: E402
from zz2.core.text import digits_only  # noqa: E402
from zz2.domain.models import Ticket  # noqa: E402
from zz2.gateways.database import column_list, db, placeholder_list, sql  # noqa: E402
from zz2.repositories.tickets import TicketStoreRepository  # noqa: E402
from zz2.services.analytics import AnalyticsService  # noqa: E402
from zz2.settings import get_settings  # noqa: E402

DEMO_TAG = "[demo-seed]"

TECHS = ["Ana Souza", "Bruno Lima", "Carla Mendes", "Diego Rocha"]

# Each client appears under the variants people actually type.
CLIENTS = [
    ["Multisom", "MULTISOM", "Multi Som"],
    ["Som e Cia", "Som & Cia", "som e cia"],
    ["Auto Center Paulista", "Auto Center Paulista Ltda"],
    ["Car Audio SP", "CarAudio SP"],
    ["Prime Acessórios", "Prime Acessorios"],
    ["Vitória Som Automotivo"],
    ["Garage 77"],
    ["JR Instalações", "JR Instalacoes"],
    ["Premium Car Tech"],
    ["Blindados Morumbi"],
    ["Elite Car Som"],
    ["Cliente final - WhatsApp"],
]

# Kit -> vehicle, with a weight for how often it appears and how often it fails.
KITS = {
    "ITZ-NTG4":    (["Mercedes C200 2013", "Mercedes E250 2014"], 10, 0.30),
    "ITZ-NTG3":    (["Mercedes ML350 2010", "Mercedes C180 2011"], 6, 0.20),
    "IT3-NTG5":    (["Mercedes GLA 2016", "Mercedes C300 2016"], 9, 0.15),
    "IT3-NBT":     (["BMW 320i 2015", "BMW X1 2016"], 9, 0.18),
    "ITZ-CCC":     (["BMW 118i 2010", "BMW X5 2009"], 4, 0.35),
    "IT3-AUDI":    (["Audi A3 2016", "Audi Q3 2017"], 7, 0.12),
    "ITZ-LR15-A":  (["Range Rover Evoque 2016", "Discovery Sport 2017"], 6, 0.40),
    "IT3-PCM31A":  (["Porsche Cayenne 2012", "Porsche Panamera 2013"], 3, 0.25),
    "ZW-RAM":      (["RAM 1500 2019", "RAM 2500 2020"], 5, 0.10),
    "ZW-GM":       (["Chevrolet Tracker 2018", "Chevrolet S10 2019"], 7, 0.08),
    "IT3-VOLVO":   (["Volvo XC60 2015", "Volvo XC90 2016"], 4, 0.15),
    "ITZ-TOY":     (["Toyota Corolla 2017", "Toyota Hilux 2018"], 6, 0.10),
    "ITZ-JGC":     (["Jeep Grand Cherokee 2015"], 3, 0.22),
    "ITZ-SYNC2":   (["Ford Ranger 2016", "Ford Fusion 2015"], 4, 0.14),
}

ISSUES = {
    "No power / black screen": [
        "LVDS cables reversed; reconnected in the correct direction.",
        "DIP switches did not match the diagram; corrected positions.",
        "Blown fuse on the main harness; replaced.",
    ],
    "No audio / incorrect audio source": [
        "Audio method set to AUX; changed to the factory input.",
        "Audio cable not seated; reseated and tested.",
    ],
    "CarPlay / Android Auto will not connect": [
        "Firmware updated; phone pairs normally.",
        "Customer was using a charge-only USB cable; replaced.",
        "Wi-Fi module reseated; wireless CarPlay working.",
    ],
    "Factory controls are not working": [
        "CAN wires swapped; corrected and confirmed knob control.",
    ],
    "Camera problem": [
        "Camera trigger wire not connected to reverse signal.",
        "Camera DIP switch set to off; enabled.",
    ],
    "Intermittent operation / restarting": [
        "Poor ground; moved to chassis ground point.",
        "Interface overheating behind the dash; relocated.",
    ],
    "Installation or manual question": [
        "Sent the manual and wiring diagram.",
        "Explained the switch button sequence.",
    ],
}

ISSUE_WEIGHTS = [16, 12, 22, 8, 10, 9, 14]


def pick_status(rng: random.Random, failure_rate: float, age_days: int) -> str:
    """Older tickets are mostly closed; recent ones are often still open."""
    if age_days < 4 and rng.random() < 0.6:
        return rng.choice(["", "Waiting for customer feedback"])
    if rng.random() < failure_rate:
        return rng.choices(
            ["RMA/Return", "RMA - ADV REP REQ GROUND", "No Solution", "Field Destroy"],
            weights=[5, 2, 3, 1],
        )[0]
    if rng.random() < 0.05:
        return "DISTRIBUTOR"
    return "Resolved"


def pick_date(rng: random.Random, days: int, today: dt.date) -> dt.date:
    """Volume grows towards the present, and weekends are quiet."""
    while True:
        age = int(days * (1 - rng.random() ** 0.6))
        day = today - dt.timedelta(days=age)
        if day.weekday() < 5 or rng.random() < 0.15:
            return day


def generate(count: int, days: int, first_number: int, seed: int) -> list[Ticket]:
    rng = random.Random(seed)  # noqa: S311 -- demo data, not secrets
    today = dt.date.today()
    kit_names = list(KITS)
    kit_weights = [KITS[name][1] for name in kit_names]
    issue_names = list(ISSUES)

    dates = sorted(pick_date(rng, days, today) for _ in range(count))
    tickets = []
    for offset, date in enumerate(dates):
        kit = rng.choices(kit_names, weights=kit_weights)[0]
        vehicles, _, failure_rate = KITS[kit]
        issue = rng.choices(issue_names, weights=ISSUE_WEIGHTS)[0]
        status = pick_status(rng, failure_rate, (today - date).days)
        resolution = rng.choice(ISSUES[issue]) if status == "Resolved" else ""
        tickets.append(Ticket(
            number=str(first_number + offset),
            date=date.isoformat(),
            tech=rng.choices(TECHS, weights=[5, 4, 3, 2])[0],
            client=rng.choice(rng.choices(CLIENTS, weights=[9, 7, 6, 5, 5, 4, 4, 3, 3, 2, 2, 6])[0]),
            product=kit,
            vehicle=rng.choice(vehicles),
            issue=issue,
            resolution=resolution,
            status=status,
            serial=f"ZZ{rng.randint(10_000_000, 99_999_999)}",
            notes=DEMO_TAG,
        ))
    return tickets


def remote_max() -> int:
    rows = db.fetch_all(
        "SELECT ticket_number FROM tickets ORDER BY ticket_number DESC LIMIT 1000")
    return max((int(d) for d in (digits_only(r.get("ticket_number")) for r in rows) if d),
               default=0)


def seed(args: argparse.Namespace) -> None:
    store = TicketStoreRepository()
    existing = store.rows()
    local_max = max((int(d) for d in (digits_only(r.get("ticket")) for r in existing) if d),
                    default=0)
    remote = 0 if args.local_only else remote_max()
    first = max(local_max, remote, 999) + 1

    tickets = generate(args.count, args.days, first, args.seed)
    print(f"{len(tickets)} tickets, #{first}..#{first + len(tickets) - 1}, "
          f"{tickets[0].date} to {tickets[-1].date}")
    if args.dry_run:
        for ticket in tickets[:5]:
            print("  ", ticket.to_store())
        print("dry run: nothing written")
        return

    if not args.local_only:
        rows = [ticket.to_record() for ticket in tickets]
        columns = list(rows[0])
        db.execute_many(
            sql.SQL("INSERT INTO tickets ({}) VALUES ({})").format(
                column_list(columns), placeholder_list(len(columns))),
            ([row[c] for c in columns] for row in rows),
        )
        print(f"database: inserted {len(rows)}")

    store.replace_all(existing + [ticket.to_store() for ticket in tickets],
                      source="demo-seed")
    print(f"local store: {len(existing)} -> {len(existing) + len(tickets)} rows")
    rebuild_view()


def remove(args: argparse.Namespace) -> None:
    store = TicketStoreRepository()
    existing = store.rows()
    kept = [row for row in existing if DEMO_TAG not in str(row.get("notes") or "")]
    print(f"local store: removing {len(existing) - len(kept)} demo rows")
    if not args.local_only:
        print("database: deleting rows tagged", DEMO_TAG)
    if args.dry_run:
        print("dry run: nothing removed")
        return
    if not args.local_only:
        db.execute("DELETE FROM tickets WHERE notes = %s", [DEMO_TAG])
    store.replace_all(kept, source="demo-seed-removed")
    rebuild_view()


def rebuild_view() -> None:
    AnalyticsService().build_view()
    print("analytics view rebuilt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=320)
    parser.add_argument("--days", type=int, default=180, help="spread over the last N days")
    parser.add_argument("--seed", type=int, default=2026, help="same seed, same tickets")
    parser.add_argument("--local-only", action="store_true", help="skip the database")
    parser.add_argument("--remove", action="store_true", help="delete the demo tickets")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if get_settings().is_production:
        print("refusing to run with ZZ2_ENVIRONMENT=production", file=sys.stderr)
        return 2

    try:
        (remove if args.remove else seed)(args)
    except UpstreamError as exc:
        print(f"database request failed: {exc.log_detail or exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
