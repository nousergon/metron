"""The migration graph has exactly one head.

Two PRs that each add a migration off the same parent merge without a textual
conflict (different file names), and the result deploys ``alembic upgrade head``
against a graph with two heads, which fails. This test turns that silent,
deploy-time failure into a red check on whichever branch creates the second head.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_has_exactly_one_head():
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"alembic has {len(heads)} heads {heads}; chain the newer migration's down_revision onto the other"
