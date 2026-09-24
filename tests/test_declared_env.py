"""The box's declared environment (metron-ops-I340, metron-ops-I281).

`EXTERNAL_DEMO_RELEASED` used to live only in a hand-edited `.env` on the box, so the
compliance gate's input had no tracked home and a rebuild would silently drop it. And
the retired `ANTHROPIC_API_KEY` sat in the same file long after the 2026-08-29 ruling.
`infrastructure/declared_env.sh` makes `infrastructure/declared-flags.env` the one place
those values are set; deploy-on-merge.sh runs it on every deploy. These tests run the
real script against temp env files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parent.parent / "infrastructure"
SCRIPT = INFRA / "declared_env.sh"
DECLARED = INFRA / "declared-flags.env"
BEGIN = "# >>> declared-flags (managed by deploy-on-merge.sh from infrastructure/declared-flags.env — do not edit) >>>"
END = "# <<< declared-flags <<<"
SECRET = "sk-ant-api03-NEVER-LOG-THIS-VALUE"


def _run(declared: Path, target: Path, *others: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(declared), str(target), *map(str, others)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def declared(tmp_path: Path) -> Path:
    p = tmp_path / "declared-flags.env"
    p.write_text("# comment\n\nFLAG_A=false\nFLAG_B=true\n-OLD_KEY\n")
    return p


def test_block_is_written_and_hand_set_lines_are_removed_from_both_files(declared, tmp_path):
    target = tmp_path / "metron.env"
    other = tmp_path / "ops.env"
    target.write_text("DATABASE_URL=postgres://x\nFLAG_A=true\n")
    other.write_text("export FLAG_B=false\nSNAPTRADE_ID=abc\n")

    r = _run(declared, target, other)

    assert r.returncode == 0, r.stdout + r.stderr
    assert target.read_text() == (f"DATABASE_URL=postgres://x\n{BEGIN}\nFLAG_A=false\nFLAG_B=true\n{END}\n")
    assert other.read_text() == "SNAPTRADE_ID=abc\n"
    assert "removed hand-set line 'FLAG_A=true'" in r.stdout
    assert "removed hand-set line 'export FLAG_B=false'" in r.stdout


def test_retired_key_is_removed_everywhere_and_its_value_never_logged(declared, tmp_path):
    target = tmp_path / "metron.env"
    other = tmp_path / "ops.env"
    target.write_text(f"OLD_KEY={SECRET}\nKEEP=1\n")
    other.write_text(f"  export OLD_KEY={SECRET}\n")

    r = _run(declared, target, other)

    assert r.returncode == 0, r.stdout + r.stderr
    assert SECRET not in target.read_text()
    assert SECRET not in other.read_text()
    assert SECRET not in r.stdout + r.stderr
    assert r.stdout.count("removed retired OLD_KEY (value not logged)") == 2


def test_absent_retired_key_and_absent_other_file_are_logged_not_errors(declared, tmp_path):
    target = tmp_path / "metron.env"
    target.write_text("KEEP=1\n")

    r = _run(declared, target, tmp_path / "missing.env")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "retired OLD_KEY absent" in r.stdout
    assert "missing.env: absent, nothing to scan" in r.stdout
    assert not (tmp_path / "missing.env").exists()


def test_missing_target_is_created(declared, tmp_path):
    target = tmp_path / "metron.env"

    r = _run(declared, target)

    assert r.returncode == 0, r.stdout + r.stderr
    assert target.read_text() == f"{BEGIN}\nFLAG_A=false\nFLAG_B=true\n{END}\n"


def test_second_run_is_byte_identical(declared, tmp_path):
    """Every deploy runs this; a block that grew or moved on each run would be drift."""
    target = tmp_path / "metron.env"
    other = tmp_path / "ops.env"
    target.write_text("A=1\nFLAG_A=true\nOLD_KEY=x")  # no trailing newline
    other.write_text("B=2\n")

    assert _run(declared, target, other).returncode == 0
    first = (target.read_bytes(), other.read_bytes())
    assert _run(declared, target, other).returncode == 0

    assert (target.read_bytes(), other.read_bytes()) == first
    assert target.read_text().count(BEGIN) == 1


def test_flipping_a_declared_value_replaces_the_managed_line(tmp_path):
    """The external-demo release is a one-line PR against declared-flags.env."""
    decl = tmp_path / "d.env"
    target = tmp_path / "metron.env"
    decl.write_text("FLAG_A=false\n")
    assert _run(decl, target).returncode == 0
    decl.write_text("FLAG_A=true\n")

    assert _run(decl, target).returncode == 0

    assert target.read_text() == f"{BEGIN}\nFLAG_A=true\n{END}\n"


@pytest.mark.parametrize(
    "body",
    [
        "lower_case=1\n",
        "-bad-name\n",
        "NOT A DECLARATION\n",
        "# only comments\n\n",
    ],
)
def test_malformed_declaration_fails_the_deploy_and_touches_nothing(tmp_path, body):
    decl = tmp_path / "d.env"
    target = tmp_path / "metron.env"
    decl.write_text(body)
    target.write_text("FLAG_A=true\n")

    r = _run(decl, target)

    assert r.returncode == 2
    assert target.read_text() == "FLAG_A=true\n"


def test_the_tracked_declaration_keeps_the_demo_unreleased_and_the_key_retired(tmp_path):
    """The real file: flipping either flag is a reviewed change, never a side effect."""
    lines = [ln.strip() for ln in DECLARED.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert "EXTERNAL_DEMO_RELEASED=false" in lines
    assert "DISPLAY_LICENCE_CONFIRMED=false" in lines
    assert "-ANTHROPIC_API_KEY" in lines

    target = tmp_path / "metron.env"
    r = _run(DECLARED, target)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "EXTERNAL_DEMO_RELEASED=false" in target.read_text()


def test_deploy_on_merge_applies_the_declaration_to_both_env_files():
    deploy = (INFRA / "deploy-on-merge.sh").read_text()
    assert (
        'bash "$REPO/infrastructure/declared_env.sh" "$REPO/infrastructure/declared-flags.env" "$REPO/.env" "$ENVF"'
    ) in deploy
    # After SSM hydration (so the hydrated block is also scanned) and before units restart.
    assert deploy.index("hydrated ${HYDRATED} var(s)") < deploy.index("declared_env.sh")
    assert deploy.index("declared_env.sh") < deploy.index('DEPLOY_STAGE="unit install"')
