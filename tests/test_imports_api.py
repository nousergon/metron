"""End-to-end PH1 gate: a stranger's exported CSV round-trips to a correct
Portfolio (holdings) + realized view through the HTTP API."""

from __future__ import annotations

import io
import uuid

import pytest

CSV = """date,type,symbol,quantity,price,amount,fees
2024-01-15,BUY,AAPL,10,150,1500,1
2024-02-15,BUY,MSFT,5,300,1500,1
2024-03-01,DIVIDEND,AAPL,,,4.40,
2024-08-01,SELL,AAPL,4,200,800,1
"""


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _new_portfolio(client, tenant, name="Taxable"):
    r = client.post("/portfolios", json={"name": name}, headers={"X-Tenant-Id": tenant})
    assert r.status_code == 201
    return r.json()["id"]


def _upload(client, tenant, pid, text=CSV):
    return client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("trades.csv", io.BytesIO(text.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )


def test_csv_roundtrips_to_holdings(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _upload(client, tenant, pid)
    assert r.status_code == 200
    body = r.json()
    assert body["rows_parsed"] == 4
    assert body["transactions_inserted"] == 4
    assert body["securities_created"] == 2

    holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    # 10 AAPL bought, 4 sold (FIFO) → 6 left at $150 basis; 5 MSFT at $300.
    assert holdings["AAPL"]["quantity"] == 6
    assert holdings["AAPL"]["avg_cost"] == pytest.approx(150.1, abs=0.05)  # $1 fee folded into basis
    assert holdings["MSFT"]["quantity"] == 5


def test_realized_gain_computed(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    realized = client.get(f"/portfolios/{pid}/realized", headers={"X-Tenant-Id": tenant}).json()
    assert len(realized) == 1
    lot = realized[0]
    assert lot["ticker"] == "AAPL"
    assert lot["quantity"] == 4
    # Sold 4 @ $200 less $1 fee = $799 proceeds; basis 4 @ ~$150.1 = ~$600.4 → ~$198.6 gain.
    assert lot["gain"] == pytest.approx(198.6, abs=0.5)
    assert lot["long_term"] is False


def test_transactions_listed(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    txns = client.get(f"/portfolios/{pid}/transactions", headers={"X-Tenant-Id": tenant}).json()
    assert [t["txn_type"] for t in txns] == ["BUY", "BUY", "DIVIDEND", "SELL"]


def test_reupload_is_idempotent(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    second = _upload(client, tenant, pid)
    assert second.json()["transactions_inserted"] == 0
    assert second.json()["transactions_skipped"] == 4
    txns = client.get(f"/portfolios/{pid}/transactions", headers={"X-Tenant-Id": tenant}).json()
    assert len(txns) == 4  # no duplication


def test_dirty_rows_reported_not_fatal(client, tenant):
    pid = _new_portfolio(client, tenant)
    dirty = "date,type,symbol,quantity,price\n2024-01-15,BUY,AAPL,1,100\nbad,BUY,AAPL,1,100\n"
    r = _upload(client, tenant, pid, dirty)
    assert r.status_code == 200
    assert r.json()["rows_parsed"] == 1
    assert r.json()["rows_skipped"] == 1
    assert r.json()["errors"][0]["ref"] == "line 3"


def test_invalid_csv_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _upload(client, tenant, pid, "symbol,quantity\nAAPL,1\n")  # no date/type columns
    assert r.status_code == 422


def test_import_requires_tenant_ownership(client, tenant):
    pid = _new_portfolio(client, tenant)
    other = str(uuid.uuid4())
    # Another tenant cannot import into — or even see — this portfolio (404, not 403).
    r = _upload(client, other, pid)
    assert r.status_code == 404
    assert client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": other}).status_code == 404


def test_holdings_isolated_per_tenant(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    other = str(uuid.uuid4())
    other_pid = _new_portfolio(client, other, name="Other")
    assert client.get(f"/portfolios/{other_pid}/holdings", headers={"X-Tenant-Id": other}).json() == []


# --- OFX round-trip (the second free-tier ingestion path, same bridge) ---

OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20240802120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<INVSTMTMSGSRSV1><INVSTMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<INVSTMTRS><DTASOF>20240801120000<CURDEF>USD
<INVACCTFROM><BROKERID>example.com<ACCTID>U99999999</INVACCTFROM>
<INVTRANLIST><DTSTART>20240101120000<DTEND>20240801120000
<BUYSTOCK><INVBUY><INVTRAN><FITID>T1<DTTRADE>20240115120000</INVTRAN><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><UNITS>10<UNITPRICE>150.00<COMMISSION>1.00<TOTAL>-1501.00<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INVBUY><BUYTYPE>BUY</BUYSTOCK>
<SELLSTOCK><INVSELL><INVTRAN><FITID>T2<DTTRADE>20240601120000</INVTRAN><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><UNITS>-4<UNITPRICE>200.00<COMMISSION>1.00<TOTAL>799.00<SUBACCTSEC>CASH<SUBACCTFUND>CASH</INVSELL><SELLTYPE>SELL</SELLSTOCK>
</INVTRANLIST></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>
<SECLISTMSGSRSV1><SECLIST><STOCKINFO><SECINFO><SECID><UNIQUEID>037833100<UNIQUEIDTYPE>CUSIP</SECID><SECNAME>Apple Inc<TICKER>AAPL</SECINFO></STOCKINFO></SECLIST></SECLISTMSGSRSV1>
</OFX>
"""


def _upload_ofx(client, tenant, pid, text=OFX):
    return client.post(
        f"/portfolios/{pid}/import/ofx",
        files={"file": ("statement.ofx", io.BytesIO(text.encode()), "application/x-ofx")},
        headers={"X-Tenant-Id": tenant},
    )


def test_ofx_roundtrips_to_holdings(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _upload_ofx(client, tenant, pid)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "ofx"
    assert body["transactions_inserted"] == 2

    holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    # 10 AAPL bought (+$1 fee in basis), 4 sold FIFO → 6 left.
    assert holdings["AAPL"]["quantity"] == 6
    realized = client.get(f"/portfolios/{pid}/realized", headers={"X-Tenant-Id": tenant}).json()
    assert len(realized) == 1 and realized[0]["quantity"] == 4


def test_ofx_then_csv_share_security_master(client, tenant):
    # OFX and CSV both reference AAPL → one global security row, two ingestion paths.
    pid = _new_portfolio(client, tenant)
    ofx_created = _upload_ofx(client, tenant, pid).json()["securities_created"]
    csv_created = _upload(client, tenant, pid, "date,type,symbol,quantity,price\n2024-09-01,BUY,AAPL,1,160\n").json()[
        "securities_created"
    ]
    assert ofx_created == 1 and csv_created == 0  # AAPL master already exists from the OFX import


def test_invalid_ofx_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _upload_ofx(client, tenant, pid, "not an ofx file")
    assert r.status_code == 422
