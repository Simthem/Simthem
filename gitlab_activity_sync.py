#!/usr/bin/env python3
"""
Génère un heatmap SVG de l'activité GitLab (events: push/MR/issues) et le
pousse vers le repo profil GitHub (<pseudo>/<pseudo>) via l'API Contents.

À exécuter sur une machine ayant accès au GitLab privé (VPN/réseau interne).
Le flux est strictement sortant vers GitHub - rien n'entre depuis l'extérieur.

Dépendances : uniquement la stdlib Python 3 (aucun pip install requis).

Variables d'environnement requises :
  GITLAB_URL      ex: https://gitlab.mondomaine.local
  GITLAB_TOKEN    Personal Access Token GitLab, scope `read_api`
  GITHUB_OWNER    pseudo GitHub
  GITHUB_REPO     nom du repo profil (généralement == GITHUB_OWNER)
  GITHUB_TOKEN    Fine-grained PAT GitHub, "Contents: Read and write"
                  limité au seul repo GITHUB_OWNER/GITHUB_REPO

Variables optionnelles :
  SVG_PATH        chemin du fichier SVG dans le repo (défaut: gitlab-activity.svg)
  LOOKBACK_WEEKS  nombre de semaines affichées (défaut: 53, ~1 an)
  GITHUB_BRANCH   branche cible (défaut: main)
  TIMEZONE        fuseau pour découper les jours, ex: Europe/Paris (défaut: UTC)
                  - l'UI GitLab affiche les jours en heure locale du
                  navigateur, alors que l'API renvoie created_at en UTC ;
                  sans ce réglage, les events proches de minuit peuvent se
                  retrouver classés sur le mauvais jour par rapport à l'UI.

Avant première exécution, ajouter ces deux lignes dans le README.md du
repo profil :

  <!--GITLAB-ACTIVITY:START-->
  <!--GITLAB-ACTIVITY:END-->

Exemple de crontab :
  0 6 * * * GITLAB_URL=... GITLAB_TOKEN=... GITHUB_OWNER=... GITHUB_REPO=... \
            GITHUB_TOKEN=... /usr/bin/python3 /opt/scripts/gitlab_activity_sync.py \
            >> /var/log/gitlab_activity_sync.log 2>&1
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MARKER_START = "<!--GITLAB-ACTIVITY:START-->"
MARKER_END = "<!--GITLAB-ACTIVITY:END-->"

# Palette orange GitLab (fond sombre -> couleur de marque -> quasi blanc en
# pointe), pour bien distinguer visuellement ce graphique de celui de GitHub
# (vert) tout en gardant un vrai contraste sur les jours à forte activité.
PALETTE = ["#161b22", "#3a2110", "#7a3e12", "#c1531a", "#ffb380"]


# ---------------------------------------------------------------- GitLab ---

def fetch_gitlab_events(base_url, token, since_date):
    """Récupère les events du compte authentifié (endpoint /events, pas besoin
    de connaître son user_id) avec pagination."""
    events = []
    page = 1
    per_page = 100
    while True:
        url = (
            f"{base_url}/api/v4/events"
            f"?after={since_date}&per_page={per_page}&page={page}"
        )
        req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.loads(resp.read().decode())
        if not batch:
            break
        events.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return events


def aggregate_by_day(events, tz_name="UTC"):
    """Les push events sont pondérés par leur nombre de commits réels,
    le reste (MR, issues, commentaires...) compte pour 1 événement."""
    tz = ZoneInfo(tz_name)
    counts = defaultdict(int)
    for ev in events:
        raw = ev.get("created_at")
        if not raw:
            continue
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(tz)
        d = dt.date().isoformat()
        push_data = ev.get("push_data")
        if push_data:
            counts[d] += max(int(push_data.get("commit_count") or 1), 1)
        else:
            counts[d] += 1
    return counts


def build_grid(counts, weeks):
    """Construit une grille de semaines (colonnes) x jours (lignes, Dim->Sam),
    alignée comme le graphe GitHub, se terminant aujourd'hui."""
    today = date.today()
    end = today
    start = end - timedelta(days=weeks * 7 - 1)
    start -= timedelta(days=(start.weekday() + 1) % 7)  # recule jusqu'au dimanche

    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)

    grid, week = [], []
    for d in days:
        week.append((d, counts.get(d.isoformat(), 0)))
        if len(week) == 7:
            grid.append(week)
            week = []
    if week:
        grid.append(week)
    return grid


def color_for(count):
    """Seuils fixes (nombre absolu de contributions dans la journée), pas de
    quantiles ni de ratio au max - plus prévisible d'un jour sur l'autre."""
    if count == 0:
        return PALETTE[0]
    if count <= 9:
        return PALETTE[1]
    if count <= 19:
        return PALETTE[2]
    if count <= 29:
        return PALETTE[3]
    return PALETTE[4]


def generate_svg(grid, total):
    cell, gap = 11, 3
    left_pad, top_pad = 34, 35
    width = left_pad + len(grid) * (cell + gap) + 4
    height = top_pad + 7 * (cell + gap) + 22

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#0d1117"/>',
        f'<text x="{left_pad}" y="16" fill="#c9d1d9" font-size="12">'
        f'{total} contributions GitLab (12 derniers mois)</text>',
    ]

    last_month = None
    for wi, week in enumerate(grid):
        first_day = week[0][0]
        if first_day.day <= 7 and first_day.month != last_month:
            x = left_pad + wi * (cell + gap)
            parts.append(
                f'<text x="{x}" y="{top_pad - 6}" fill="#8b949e" font-size="9">'
                f'{first_day.strftime("%b")}</text>'
            )
            last_month = first_day.month

    for idx, label in {1: "Mon", 3: "Wed", 5: "Fri"}.items():
        y = top_pad + idx * (cell + gap) + cell - 2
        parts.append(f'<text x="0" y="{y}" fill="#8b949e" font-size="9">{label}</text>')

    for wi, week in enumerate(grid):
        for di, (d, count) in enumerate(week):
            x = left_pad + wi * (cell + gap)
            y = top_pad + di * (cell + gap)
            color = color_for(count)
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" '
                f'fill="{color}"><title>{d.isoformat()}: {count}</title></rect>'
            )

    legend_y = height - 8
    parts.append(f'<text x="{left_pad}" y="{legend_y}" fill="#8b949e" font-size="9">Moins</text>')
    lx = left_pad + 38
    for c in PALETTE:
        parts.append(f'<rect x="{lx}" y="{legend_y - 9}" width="9" height="9" rx="2" fill="{c}"/>')
        lx += 12
    parts.append(f'<text x="{lx + 4}" y="{legend_y}" fill="#8b949e" font-size="9">Plus</text>')

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- GitHub ---

def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gitlab-activity-sync",
    }


def github_get_file(owner, repo, path, token):
    """Retourne (sha, contenu_decodé) ou (None, None) si le fichier n'existe pas."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers=gh_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return data["sha"], base64.b64decode(data["content"]).decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def github_put_file(owner, repo, path, content_bytes, token, message, branch):
    sha, _ = github_get_file(owner, repo, path, token)
    body = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=gh_headers(token), method="PUT"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def update_readme(owner, repo, token, total, svg_path, branch):
    sha, current = github_get_file(owner, repo, "README.md", token)
    if sha is None:
        print("README.md introuvable, étape ignorée.", file=sys.stderr)
        return
    if MARKER_START not in current or MARKER_END not in current:
        print(
            "Marqueurs GITLAB-ACTIVITY absents du README - "
            "ajoute-les une fois manuellement, voir l'en-tête du script.",
            file=sys.stderr,
        )
        return

    today = date.today().isoformat()
    block = (
        f"{MARKER_START}\n"
        f"![Activité GitLab]({svg_path})\n\n"
        f"*{total} contributions GitLab ces 12 derniers mois - "
        f"dernière synchro : {today}*\n"
        f"{MARKER_END}"
    )
    pattern = re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END)
    new_content = re.sub(pattern, lambda _m: block, current, flags=re.DOTALL)

    if new_content == current:
        return  # rien à changer, on évite un commit vide

    body = {
        "message": f"chore: sync gitlab activity ({today})",
        "content": base64.b64encode(new_content.encode()).decode(),
        "sha": sha,
        "branch": branch,
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/README.md"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=gh_headers(token), method="PUT"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# ------------------------------------------------------------------ main ---

def env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Variable d'environnement manquante : {name}")
    return val


def main():
    gitlab_url = env("GITLAB_URL").rstrip("/")
    gitlab_token = env("GITLAB_TOKEN")
    github_owner = env("GITHUB_OWNER")
    github_repo = env("GITHUB_REPO")
    github_token = env("GITHUB_TOKEN")
    svg_path = env("SVG_PATH", "gitlab-activity.svg", required=False)
    branch = env("GITHUB_BRANCH", "main", required=False)
    weeks = int(env("LOOKBACK_WEEKS", "53", required=False))
    tz_name = env("TIMEZONE", "UTC", required=False)

    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    events = fetch_gitlab_events(gitlab_url, gitlab_token, since)
    counts = aggregate_by_day(events, tz_name)
    grid = build_grid(counts, weeks)
    total = sum(c for week in grid for _, c in week)

    svg = generate_svg(grid, total)
    github_put_file(
        github_owner, github_repo, svg_path, svg.encode(), github_token,
        f"chore: update {svg_path} ({date.today().isoformat()})", branch,
    )
    update_readme(github_owner, github_repo, github_token, total, svg_path, branch)

    print(f"OK - {total} contributions synchronisées, {svg_path} mis à jour.")


if __name__ == "__main__":
    main()
