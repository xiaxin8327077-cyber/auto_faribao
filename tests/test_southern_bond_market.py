from datetime import datetime
import importlib

import pytest
import requests


def _module():
    return importlib.import_module("src.southern_bond_market")


def _matrix():
    return {
        "code": "ETS-5BP0000",
        "trade_date": "2026-10-23",
        "deadline_time": "09:46",
        "barometerRateItemList": [
            {"type": "0", "sub_type": "国债", "sub_weather": "2", "bpDataList": [
                {"age_limit": "1Y", "bp": "0.01"},
                {"age_limit": "3Y", "bp": "0"},
                {"age_limit": "5Y", "bp": ""},
                {"age_limit": "7Y", "bp": "-0.15"},
                {"age_limit": "10Y", "bp": "0.75"},
            ]},
            {"type": "1", "sub_type": "AAA", "sub_weather": "3", "bpDataList": [
                {"age_limit": "1M", "bp": "2.78"},
                {"age_limit": "3M", "bp": "-0.92"},
                {"age_limit": "6M", "bp": "-0.47"},
                {"age_limit": "1Y", "bp": "-0.46"},
                {"age_limit": "3Y", "bp": "0.05"},
                {"age_limit": "5Y", "bp": "-0.38"},
            ]},
            {"type": "1", "sub_type": "AA+", "sub_weather": "3", "bpDataList": [
                {"age_limit": "5Y", "bp": "-3.13"},
            ]},
        ],
    }


def test_southern_matrix_preserves_bp_units_small_moves_and_missing_quotes():
    data = _module().parse_barometer(_matrix())
    assert data["available"] is True
    assert data["as_of"] == "2026-10-23"
    assert data["source_id"] == "southernfund"
    assert data["source_updated_at"] == "2026-10-23T09:46:00"
    assert data["change_unit"] == "bp"
    cells = data["rate"]["rows"][0]["cells"]
    assert [cell["color"] for cell in cells] == ["green", "flat", "missing", "red", "green"]
    assert cells[0]["change"] == "0.01"
    assert cells[2]["change"] is None
    assert cells[4]["yield"] is None
    assert data["rate"]["weather"] == "cloud"
    assert data["credit"]["weather"] == "sun_cloud"
    assert data["credit"]["rows"][1]["label"] == "AA+"
    assert data["credit"]["rows"][1]["cells"][-1]["change"] == "-3.13"


@pytest.mark.parametrize("bad_date", ["", "not-a-date", "2026-02-31"])
def test_southern_matrix_rejects_unverifiable_source_date(bad_date):
    raw = _matrix()
    raw["trade_date"] = bad_date
    with pytest.raises(ValueError):
        _module().parse_barometer(raw)


def test_egg_ranges_use_percent_units_and_keep_their_own_timestamp():
    raw = {
        "trade_date": "20261023", "deadline_time": "1792720140051",
        "llz_short_price": "-0.01", "llz_long_price": "0.01",
        "yxz_short_price": "0.03", "yxz_long_price": "0.10",
    }
    data = _module().parse_egg_estimates(raw)
    assert data["as_of"] == "2026-10-23"
    assert data["source_updated_at"] == "2026-10-23T09:49:00"
    assert [item["display"] for item in data["items"]] == [
        "预计碎蛋", "预计收蛋0–3个", "预计收蛋3–6个", "预计收蛋超10个",
    ]
    assert [item["return_pct"] for item in data["items"]] == ["-0.01", "0.01", "0.03", "0.10"]
    assert data["items"][0]["color"] == "green"
    assert data["items"][1]["color"] == "red"


def test_egg_missing_or_nonfinite_values_are_unavailable_not_zero():
    data = _module().parse_egg_estimates({
        "trade_date": "20261023", "deadline_time": "1792720140051",
        "llz_short_price": "", "llz_long_price": "NaN",
        "yxz_short_price": "Infinity", "yxz_long_price": "0",
    })
    assert [item["return_pct"] for item in data["items"]] == [None, None, None, "0"]
    assert [item["display"] for item in data["items"][:3]] == ["暂无预估"] * 3
    assert data["items"][3]["color"] == "flat"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("OC050"):
            return _Response(_matrix())
        return _Response({"code": "ETS-5BP0000", "trade_date": "20261023",
                          "deadline_time": "1792720140051", "llz_long_price": "0.01"})


def test_public_api_uses_a_new_valid_signature_for_each_request():
    import base64
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    session = _Session()
    data = _module().fetch_barometer(session=session)
    assert data["egg_estimates"]["available"] is True
    assert len(session.calls) == 2
    nonces = []
    for url, kwargs in session.calls:
        body = kwargs["data"]
        nonce = body["reqrandom"]
        decoded = unpad(AES.new(b"tkeynfjj08282016", AES.MODE_CBC, iv=b"tkeynfjj08282016")
                        .decrypt(base64.b64decode(body["reqtoken"])), 16).decode()
        assert decoded == nonce
        nonces.append(nonce)
        assert kwargs["headers"]["Referer"] == "https://m.nffund.com/new/index.html"
    assert len(set(nonces)) == 2
    egg_body = session.calls[1][1]["data"]
    assert egg_body["queryTacticsProduct"] == "1"
    assert egg_body["bond_channel"] == "1"


def test_egg_failure_does_not_hide_the_available_intraday_matrix():
    class EggFailure(_Session):
        def post(self, url, **kwargs):
            if url.endswith("OC049"):
                raise requests.Timeout("upstream timeout")
            return super().post(url, **kwargs)

    data = _module().fetch_barometer(session=EggFailure())
    assert data["available"] is True
    assert data["as_of"] == "2026-10-23"
    assert data["egg_estimates"]["available"] is False


def test_business_error_is_not_accepted_as_market_data():
    class Rejected(_Session):
        def post(self, url, **kwargs):
            return _Response({"code": "MOB-COMM-REQRPT", "message": "rejected"})

    with pytest.raises(ValueError, match="MOB-COMM-REQRPT"):
        _module().fetch_barometer(session=Rejected())
