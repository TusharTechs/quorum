"""Demo mode. The checker (run.py) is run by DOGFOOD judges against *their* fresh clone,
so the four auth headers in .dogfood.toml must be identical on every install. These
tokens are therefore public constants, created only when QUORUM_DEMO=1; production mode
refuses to start if any of them exist (see quorum.core.checks.production_guards)."""

DEMO_PASSWORD = "quorum-demo"

DEMO_TOKENS = {
    "organizer": ("organizer@demo.local", "qm_demo_organizer_7f2a91c4e8b3d6a0"),
    "judge_a": ("diego.herrera@example.org", "qm_demo_judge_a_91bc40d2f7e6a813"),
    "judge_b": ("jonas.vogel@example.org", "qm_demo_judge_b_44de17a9c3b85f20"),
    "participant": ("priya1@example.org", "qm_demo_participant_2e88c3b1a4d97e56"),
    "admin": ("admin@demo.local", "qm_demo_admin_0c6e5a8f2b7d9143"),
}

DEMO_LOGINS = ["organizer@demo.local", "admin@demo.local", "diego.herrera@example.org",
               "jonas.vogel@example.org", "priya1@example.org"]


def banner(summary: str) -> str:
    lines = [
        "",
        "=" * 78,
        "Quorum ready  ->  http://localhost:8080     mail (offline) -> http://localhost:8025",
        "DEMO MODE: the tokens below are public test credentials. Never use them in production.",
        summary,
        "",
        "test headers for .dogfood.toml:",
    ]
    labels = {"organizer": "", "judge_a": "(jdg_24 Diego Herrera)", "judge_b": "(jdg_26 Jonas Vogel)",
              "participant": "(priya1@example.org, team tm_01)"}
    for role in ("organizer", "judge_a", "judge_b", "participant"):
        lines.append(f"  {role:<12} Authorization: Bearer {DEMO_TOKENS[role][1]}   {labels[role]}")
    lines += ["", f"browser logins (password '{DEMO_PASSWORD}'): " + ", ".join(DEMO_LOGINS), "=" * 78, ""]
    return "\n".join(lines)
