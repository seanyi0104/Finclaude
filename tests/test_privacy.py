"""Privacy guard: nothing personal or secret may be committed. Runs with the offline suite and in .githooks/pre-push.

Private files stay on your disk but are git-ignored: your investor profile and IPS, the watchlist and glossary marks,
the forecast log, weekly reports, knowledge cards, CLAUDE.local.md and everything under data/. The checks below read
what git tracks and fail on private paths, secrets, personal e-mail addresses and the words you list in
profile/private_terms.txt (git-ignored; one term per line, lines starting with # are comments).
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / "profile" / "private_terms.txt"

PRIVATE_FILES = [".env", "CLAUDE.local.md", "AGENTS.md",
                 "profile/investor_profile.md", "profile/ips.md", "profile/watchlist.csv", "profile/glossary.json",
                 "profile/holdings.csv", "profile/deals.csv", "profile/funds.csv", "profile/private_terms.txt",
                 "profile/deploy_progress.md", "journal/forecasts.csv", "journal/official_events.csv",
                 "journal/2026-01-05-AAPL.md"]
PRIVATE_DIRS = ["data/", "reports/", "references/", "knowledge/", ".agents/", ".codex/"]
SECRET = re.compile(r"(sk-(ant-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|vck_[A-Za-z0-9]{20,}"
                    r"|AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9-]{10,}|Bearer\s+[A-Za-z0-9._-]{24,})")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ALLOWED_EMAIL = re.compile(r"(?i)(noreply@anthropic\.com|[\w.+-]+@users\.noreply\.github\.com|[\w.+-]+@example\.(com|org))$")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout.decode("utf-8")


def tracked() -> list[str]:
    try:
        return [p for p in git("ls-files", "-z").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")


def tracked_text() -> list[tuple[str, list[str]]]:
    files = []
    for path in tracked():
        if path.endswith((".lock", ".pdf", ".png", ".jpg", ".ico")):
            continue
        try:
            files.append((path, (ROOT / path).read_text(encoding="utf-8").splitlines()))
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    return files


def test_private_files_and_folders_are_not_tracked():
    leaked = [p for p in tracked() if p in PRIVATE_FILES or p.startswith(tuple(PRIVATE_DIRS))
              or (p.startswith("profile/") and ".example." not in p)
              or (p.startswith("journal/") and p != "journal/_template.md")]
    assert not leaked, f"private files are tracked: {leaked}"


def test_private_files_and_folders_are_ignored_so_new_ones_stay_out():
    tracked()  # skips outside a git checkout
    samples = PRIVATE_FILES + [d + "2026-01-05_01-09/News.md" for d in PRIVATE_DIRS]
    result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "--stdin"],
                            input="\n".join(samples).encode("utf-8"), capture_output=True)
    ignored = set(result.stdout.decode("utf-8").split("\n"))
    assert not [s for s in samples if s not in ignored]


def test_templates_stay_tracked():
    paths = set(tracked())
    for template in ("profile/investor_profile.example.md", "profile/ips.example.md", "CLAUDE.local.example.md",
                     "journal/_template.md", ".env.example"):
        assert template in paths, template


def test_env_example_holds_no_values():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            assert value.strip() == "", f".env.example must leave {key} empty"


def test_no_secrets_or_personal_emails_in_tracked_files():
    found = []
    for path, lines in tracked_text():
        for number, line in enumerate(lines, 1):
            if SECRET.search(line):
                found.append(f"{path}:{number}: secret-like value")
            for email in EMAIL.findall(line):
                if not ALLOWED_EMAIL.search(email):
                    found.append(f"{path}:{number}: e-mail {email}")
    assert not found, "\n".join(found)


def private_terms() -> list[str]:
    if not TERMS_FILE.exists():
        pytest.skip("no profile/private_terms.txt (optional, git-ignored)")
    return [t.strip() for t in TERMS_FILE.read_text(encoding="utf-8").splitlines()
            if t.strip() and not t.lstrip().startswith("#")]


def term_pattern(term: str) -> re.Pattern:
    # numbers only match whole numbers (1234 must not hit 12345), words match case-insensitively anywhere
    if re.fullmatch(r"[\d.,]+", term):
        return re.compile(rf"(?<![\d.,]){re.escape(term)}(?![\d])")
    return re.compile(re.escape(term), re.IGNORECASE)


def commit_identities() -> set[str]:
    """Who the current branch's commits and the next commit are signed as (an empty new branch has no log yet)."""
    has_head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", "HEAD"],
                              capture_output=True).returncode == 0
    names = set(git("log", "--format=%an <%ae>%n%cn <%ce>").splitlines()) if has_head else set()
    configured = subprocess.run(["git", "-C", str(ROOT), "config", "--get", "user.email"], capture_output=True)
    return names | {f"git config user.email = {configured.stdout.decode('utf-8').strip()}"}


def test_private_terms_never_appear_in_tracked_files_or_commit_authors():
    patterns = [(t, term_pattern(t)) for t in private_terms()]
    found = [f"{path}:{number}: {term}" for path, lines in tracked_text() for number, line in enumerate(lines, 1)
             for term, pattern in patterns if pattern.search(line)]
    found += [f"commit identity: {who}" for who in commit_identities() for term, pattern in patterns
              if pattern.search(who)]
    assert not found, "\n".join(found)
