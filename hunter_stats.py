#!/usr/bin/env python3
"""
HUNTER SYSTEM SYNC — atualiza o README do perfil com o progresso REAL do GitHub.

Lê a atividade pública da conta via GitHub GraphQL API, converte em
XP / Level / Rank / atributos e reescreve os blocos do README marcados com:

    <!-- HUNTER:<NOME>:START -->  ...  <!-- HUNTER:<NOME>:END -->

Blocos gerados: CORE, PROGRESSION, QUESTLOG.

Uso:
    GH_TOKEN=... GH_LOGIN=Icaro090 python scripts/hunter_stats.py
    python scripts/hunter_stats.py --mock scripts/mock_data.json   # teste offline

Sem dependências externas (somente biblioteca padrão do Python 3.9+).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.github.com/graphql"
README = Path(__file__).resolve().parent.parent / "README.md"
TZ = timezone(timedelta(hours=-4))  # Cuiabá (UTC-4)

# ───────────────────────── Regras do jogo (ajuste à vontade) ─────────────────────────
XP_RULES = {
    "commit": 10,        # cada commit
    "pull_request": 30,  # cada PR aberto
    "review": 20,        # cada revisão de PR
    "issue": 10,         # cada issue aberta
    "private": 5,        # cada contribuição privada (se visível no perfil)
    "repo": 40,          # cada repositório público próprio (não-fork)
    "star": 25,          # cada estrela recebida
    "follower": 10,      # cada seguidor
}
XP_PER_LEVEL_SQUARED = 50  # XP total para o nível L = 50 * (L-1)^2

RANKS = [  # (nível mínimo, rank)
    (1, "E"), (6, "D"), (11, "C"), (21, "B"), (36, "A"), (51, "S"), (80, "NATIONAL"),
]
ACTIVE_WINDOW_DAYS = 90  # repositório com push nesse período conta como "ativo"
IGNORE_IN_QUESTLOG = {"icaro090"}  # repositório do próprio perfil


# ─────────────────────────────── Coleta de dados ───────────────────────────────
def gql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "hunter-system-sync",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False))
    return payload["data"]


PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    name
    followers { totalCount }
    contributionsCollection { contributionYears }
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
                 first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name
        description
        url
        pushedAt
        stargazerCount
        isArchived
        primaryLanguage { name }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""

YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      restrictedContributionsCount
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch(token: str, login: str) -> dict:
    """Busca o perfil e as contribuições de TODOS os anos (XP vitalício nunca diminui)."""
    profile = gql(token, PROFILE_QUERY, {"login": login})["user"]
    if profile is None:
        raise RuntimeError(f"Usuário '{login}' não encontrado.")

    totals = {"commit": 0, "pull_request": 0, "review": 0, "issue": 0, "private": 0}
    days: dict[str, int] = {}
    for year in profile["contributionsCollection"]["contributionYears"]:
        variables = {
            "login": login,
            "from": f"{year}-01-01T00:00:00Z",
            "to": f"{year}-12-31T23:59:59Z",
        }
        cc = gql(token, YEAR_QUERY, variables)["user"]["contributionsCollection"]
        totals["commit"] += cc["totalCommitContributions"]
        totals["pull_request"] += cc["totalPullRequestContributions"]
        totals["review"] += cc["totalPullRequestReviewContributions"]
        totals["issue"] += cc["totalIssueContributions"]
        totals["private"] += cc["restrictedContributionsCount"]
        for week in cc["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = d["contributionCount"]

    return {
        "login": profile["login"],
        "name": profile["name"] or profile["login"],
        "followers": profile["followers"]["totalCount"],
        "repos": profile["repositories"]["nodes"],
        "totals": totals,
        "days": days,
    }


# ─────────────────────────────── Cálculos do jogo ───────────────────────────────
def saturate(x: float, k: float) -> int:
    """Converte qualquer valor positivo em 0–100, crescendo cada vez mais devagar."""
    return round(100 * (1 - math.exp(-x / k))) if x > 0 else 0


def streaks(days: dict[str, int], today: date) -> tuple[int, int]:
    active = {date.fromisoformat(d) for d, c in days.items() if c > 0}
    longest = run = 0
    prev = None
    for d in sorted(active):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d
    # sequência atual: conta a partir de hoje (ou de ontem, se hoje ainda não teve commit)
    cursor = today if today in active else today - timedelta(days=1)
    current = 0
    while cursor in active:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


def compute(data: dict, today: date) -> dict:
    repos = [r for r in data["repos"] if not r["isArchived"]]
    stars = sum(r["stargazerCount"] for r in repos)
    t = data["totals"]

    xp_parts = {
        "Commits": t["commit"] * XP_RULES["commit"],
        "Pull Requests": t["pull_request"] * XP_RULES["pull_request"],
        "Revisões": t["review"] * XP_RULES["review"],
        "Issues": t["issue"] * XP_RULES["issue"],
        "Contrib. privadas": t["private"] * XP_RULES["private"],
        "Repositórios": len(repos) * XP_RULES["repo"],
        "Estrelas": stars * XP_RULES["star"],
        "Seguidores": data["followers"] * XP_RULES["follower"],
    }
    xp = sum(xp_parts.values())

    level = math.floor(math.sqrt(xp / XP_PER_LEVEL_SQUARED)) + 1
    floor_xp = XP_PER_LEVEL_SQUARED * (level - 1) ** 2
    next_xp = XP_PER_LEVEL_SQUARED * level ** 2
    rank = [r for lvl, r in RANKS if level >= lvl][-1]

    current_streak, longest_streak = streaks(data["days"], today)
    last30 = sum(
        1 for i in range(30)
        if data["days"].get((today - timedelta(days=i)).isoformat(), 0) > 0
    )
    year_total = sum(
        c for d, c in data["days"].items()
        if date.fromisoformat(d) > today - timedelta(days=365)
    )

    lang_bytes: dict[str, int] = {}
    for r in repos:
        for edge in r["languages"]["edges"]:
            lang_bytes[edge["node"]["name"]] = lang_bytes.get(edge["node"]["name"], 0) + edge["size"]
    total_bytes = sum(lang_bytes.values()) or 1
    languages = sorted(
        ((name, 100 * size / total_bytes) for name, size in lang_bytes.items()),
        key=lambda x: -x[1],
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVE_WINDOW_DAYS)
    active_repos = [
        r for r in repos
        if datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00")) >= cutoff
    ]

    lifetime_commits = t["commit"] + t["private"]
    attributes = [
        ("🧠 INTELLIGENCE", saturate(lifetime_commits, 300), f"{lifetime_commits} commits na carreira"),
        ("⚡ AGILITY", round(100 * last30 / 30), f"{last30}/30 dias ativos no último mês"),
        ("💪 STRENGTH", saturate(len(repos), 15), f"{len(repos)} repositórios públicos"),
        ("🛡️ ENDURANCE", saturate(longest_streak, 30), f"maior sequência: {longest_streak} dias"),
        ("✨ WISDOM", saturate(len(lang_bytes), 8), f"{len(lang_bytes)} linguagens dominadas"),
        ("🔥 CHARISMA",
         saturate(stars + data["followers"] + t["pull_request"] + t["review"], 40),
         f"{stars}★ · {data['followers']} seguidores · {t['pull_request']} PRs"),
    ]

    return {
        "xp": xp, "xp_parts": xp_parts, "level": level, "rank": rank,
        "floor_xp": floor_xp, "next_xp": next_xp,
        "current_streak": current_streak, "longest_streak": longest_streak,
        "year_total": year_total, "attributes": attributes,
        "languages": languages, "repos": repos, "active_repos": active_repos,
    }


# ─────────────────────────────── Renderização ───────────────────────────────
def bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def render_core(s: dict, today: date) -> str:
    return f"""```bash
╔════════════════════════════════════════╗
║    ICARO SYSTEM v3.0 - HUNTER RANK {s['rank']:<3}║
║         STATUS: AWAKENED 🌟           ║
╚════════════════════════════════════════╝

> Loading neural pathways...
████████████████████ 100%

✔️ Deep Learning activated
✔️ IoT Framework online
✔️ AI Engine running
✔️ Projects loaded ({len(s['repos'])} public · {len(s['active_repos'])} active in {ACTIVE_WINDOW_DAYS}d)
✔️ Dungeon expeditions: {s['year_total']} contributions in the last 365 days

CURRENT STATUS: {"ASCENDING 🚀" if s['current_streak'] else "RESTING 💤 (start a new streak!)"}
LAST SYNC: {today.strftime('%d/%m/%Y')}
```"""


def render_progression(s: dict) -> str:
    span = s["next_xp"] - s["floor_xp"]
    pct = 100 * (s["xp"] - s["floor_xp"]) / span if span else 0
    lines = [
        "```bash",
        "> System Stats Loaded (auto-sync via GitHub Actions)",
        "",
        f"LEVEL: {s['level']}  ·  RANK: {s['rank']}",
        f"XP: {s['xp']:,} / {s['next_xp']:,}  [{bar(pct, 16)}] {pct:.0f}%".replace(",", "."),
        f"🔥 STREAK: {s['current_streak']} dias (recorde: {s['longest_streak']})",
        "",
    ]
    for name, value, hint in s["attributes"]:
        lines.append(f"{name}")
        lines.append(f"{bar(value)} {value}/100 ({hint})")
        lines.append("")
    if s["languages"]:
        lines.append("📜 LANGUAGE MASTERY (por volume de código)")
        for lang, pct_l in s["languages"][:6]:
            lines.append(f"  {lang:<12} [{bar(pct_l, 10)}] {pct_l:4.1f}%")
        lines.append("")
    next_rank = next(((lvl, r) for lvl, r in RANKS if lvl > s["level"]), None)
    if next_rank:
        lines.append(f"NEXT RANK: {next_rank[1]} (level {next_rank[0]})")
    lines.append("```")
    return "\n".join(lines)


def render_questlog(s: dict) -> str:
    rows = ["| Dungeon | Linguagem | Última expedição | Descrição |", "|---|---|---|---|"]
    shown = [r for r in s["repos"] if r["name"].lower() not in IGNORE_IN_QUESTLOG][:6]
    for r in shown:
        lang = (r["primaryLanguage"] or {}).get("name", "—")
        pushed = datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00")).astimezone(TZ)
        desc = (r["description"] or "—").replace("|", "\\|")
        rows.append(f"| [**{r['name']}**]({r['url']}) | {lang} | {pushed:%d/%m/%Y} | {desc} |")
    return "\n".join(rows)


def replace_block(text: str, name: str, content: str) -> str:
    pattern = re.compile(
        rf"(<!-- HUNTER:{name}:START -->)(.*?)(<!-- HUNTER:{name}:END -->)", re.S
    )
    if not pattern.search(text):
        print(f"⚠️  Marcador HUNTER:{name} não encontrado no README — bloco ignorado.")
        return text
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text)


# ─────────────────────────────── Execução ───────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Hunter System Sync")
    parser.add_argument("--mock", help="JSON com dados simulados (teste offline)")
    parser.add_argument("--readme", default=str(README), help="caminho do README.md")
    parser.add_argument("--dry-run", action="store_true", help="só mostra, não grava")
    args = parser.parse_args()

    today = datetime.now(TZ).date()
    if args.mock:
        data = json.loads(Path(args.mock).read_text(encoding="utf-8"))
    else:
        token = os.environ.get("GH_TOKEN")
        login = os.environ.get("GH_LOGIN")
        if not token or not login:
            print("Defina GH_TOKEN e GH_LOGIN.", file=sys.stderr)
            return 1
        data = fetch(token, login)

    stats = compute(data, today)
    readme_path = Path(args.readme)
    text = readme_path.read_text(encoding="utf-8")
    new = replace_block(text, "CORE", render_core(stats, today))
    new = replace_block(new, "PROGRESSION", render_progression(stats))
    new = replace_block(new, "QUESTLOG", render_questlog(stats))

    print(f"Level {stats['level']} · Rank {stats['rank']} · {stats['xp']} XP")
    for k, v in stats["xp_parts"].items():
        print(f"  {k:<18} {v:>6} XP")

    if args.dry_run:
        print(new)
    elif new != text:
        readme_path.write_text(new, encoding="utf-8")
        print("README atualizado.")
    else:
        print("Nada mudou.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
