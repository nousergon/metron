"""Index constituent weight x return decomposition sourcing (SPX/NDX name-level
"why" behind the benchmark gap). See ``source`` for the injectable seam and the
unit-boundary conversion every reader of this artifact must get right.
"""

from portfolio_analytics.index_contributions.source import (
    IndexConstituent,
    IndexContributionsArtifact,
    IndexContributionsSource,
    fetch_index_contributions,
)

__all__ = [
    "IndexConstituent",
    "IndexContributionsArtifact",
    "IndexContributionsSource",
    "fetch_index_contributions",
]
