"""Service layer — hydrates portfolio_analytics engine objects from DB rows and runs
analytics. Populated in PH1+ (performance/attribution/risk/income/tax). The engine
itself lives in the ``portfolio-analytics`` package; this layer is the thin glue
between the SQLAlchemy models and the engine's pure functions.
"""
