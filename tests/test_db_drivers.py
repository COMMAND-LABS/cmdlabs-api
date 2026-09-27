"""The database drivers production's POSTGRES_URL needs are installed.

SQLAlchemy picks the DBAPI driver from the URL scheme and imports it lazily, so
no module in src/ imports a driver by name — which makes a driver look unused
to a code search. psycopg (v3) was removed as "unused" once and every
production revision then crashed at boot: the POSTGRES_URL secret uses
postgresql+psycopg://. create_engine() loads the driver without connecting,
so this fails the suite instead of the deploy.
"""
import pytest
from sqlalchemy import create_engine


@pytest.mark.parametrize("url", [
    "postgresql://u:p@localhost:1/db",          # psycopg2 (tests, CI migrate)
    "postgresql+psycopg://u:p@localhost:1/db",  # psycopg v3 (production secret)
])
def test_driver_for_scheme_is_installed(url):
    create_engine(url).dispose()
