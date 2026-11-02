#!/usr/bin/env python3
"""
Génère un aperçu local du heatmap (via gitlab_activity_sync.py) SANS jamais
appeler l'API GitHub. Deux modes :

  1. Données factices, aucun accès réseau requis :
       python3 test_gitlab_activity_svg.py --fake

  2. Données GitLab réelles, mais écriture uniquement en local :
       GITLAB_URL=https://gitlab.mondomaine.local GITLAB_TOKEN=xxx \
       python3 test_gitlab_activity_svg.py
"""

import argparse
import json
import os
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gitlab_activity_sync as gas


def fake_counts(weeks):
    """Simule une activité plausible : jours actifs ~75% en semaine, calme
    le week-end, avec quelques pics - juste pour valider le rendu visuel."""
    counts = {}
    today = date.today()
    for i in range(weeks * 7):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:  # week-end
            if random.random() < 0.2:
                counts[d.isoformat()] = random.randint(1, 3)
            continue
        if random.random() < 0.75:
            counts[d.isoformat()] = random.randint(1, 12)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true",
                         help="données factices, aucun accès réseau")
    parser.add_argument("--weeks", type=int,
                         default=int(os.environ.get("LOOKBACK_WEEKS", 53)))
    parser.add_argument("--out", default="preview.svg")
    parser.add_argument("--debug", action="store_true",
                         help="affiche la répartition des events par type (action_name / target_type)")
    parser.add_argument("--check-date", default=None, metavar="YYYY-MM-DD",
                         help="affiche les events bruts pour cette date précise, "
                              "pour comparer avec l'UI GitLab")
    parser.add_argument("--tz", default=os.environ.get("TIMEZONE", "UTC"),
                         help="fuseau pour découper les jours, ex: Europe/Paris "
                              "(défaut: variable TIMEZONE, sinon UTC)")
    args = parser.parse_args()
    tz = ZoneInfo(args.tz)

    events = []
    if args.fake:
        print("Mode factice - aucune requête réseau.")
        counts = fake_counts(args.weeks)
    else:
        gitlab_url = os.environ.get("GITLAB_URL")
        gitlab_token = os.environ.get("GITLAB_TOKEN")
        if not gitlab_url or not gitlab_token:
            sys.exit(
                "GITLAB_URL / GITLAB_TOKEN manquants - "
                "exporte-les, ou lance avec --fake."
            )
        since = (date.today() - timedelta(weeks=args.weeks)).isoformat()
        print(f"Interrogation de {gitlab_url} depuis {since}... (fuseau: {args.tz})")
        events = gas.fetch_gitlab_events(gitlab_url, gitlab_token, since)
        print(f"{len(events)} events bruts récupérés.")
        counts = gas.aggregate_by_day(events, args.tz)

    if args.debug and events:
        actions = Counter(ev.get("action_name") for ev in events)
        targets = Counter(ev.get("target_type") for ev in events)
        print("\nRépartition par action_name :")
        for k, v in actions.most_common():
            print(f"  {k!r}: {v}")
        print("Répartition par target_type :")
        for k, v in targets.most_common():
            print(f"  {k!r}: {v}")
        print()

    if args.check_date:
        matches = []
        for ev in events:
            raw = ev.get("created_at")
            if not raw:
                continue
            dt_local = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(tz)
            if dt_local.date().isoformat() == args.check_date:
                matches.append((dt_local, ev))
        print(f"\n{len(matches)} event(s) pour {args.check_date} en heure locale ({args.tz}) :")
        for dt_local, ev in matches:
            print(f"--- {dt_local.isoformat()} local  |  {ev.get('created_at')} brut (UTC) ---")
            print(json.dumps(ev, indent=2, ensure_ascii=False))
        computed = counts.get(args.check_date, 0)
        print(f"\n=> compté par aggregate_by_day() pour ce jour : {computed}\n")

    grid = gas.build_grid(counts, args.weeks)
    total = sum(c for week in grid for _, c in week)
    max_day = max(counts.values(), default=0)
    active_days = len(counts)

    svg = gas.generate_svg(grid, total)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(svg)

    print(f"Total: {total} | jours actifs: {active_days} | max/jour: {max_day}")
    print(f"SVG écrit dans ./{args.out} - à ouvrir dans un navigateur pour vérifier.")
    print("(Aucun appel GitHub n'a été effectué.)")


if __name__ == "__main__":
    main()
