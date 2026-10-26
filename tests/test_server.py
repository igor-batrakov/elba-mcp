"""Тесты elba_mcp.py на моке API Эльбы. Запуск из корня репозитория:

    uv run pytest -q
"""

import importlib.util
import io
import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import date, timedelta
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "elba_mcp.py"
ITEM = [{"name": "Доработка сайта", "price": 40000}]
TODAY = date.today().isoformat()


def load():
    spec = importlib.util.spec_from_file_location("server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fake:
    """API Эльбы в памяти: счета, документы, лента оплат."""

    def __init__(self):
        self.bills, self.posts, self.news, self.n = {}, [], [], 0

    def __call__(self, method, path, body=None, query=None):
        p = re.sub(r"^/organizations/[^/]+", "", path)
        if method == "GET" and p == "/bank-accounts":
            return 200, {"bankAccounts": [{"id": "ACC", "closed": False}]}
        if method == "GET" and p.startswith("/contractors/"):
            cid = p.split("/")[-1]
            return 200, {"id": cid, "name": f"ООО Клиент {cid}", "shortName": None}
        if method == "POST" and p == "/bills":
            self.n += 1
            bid = f"B{self.n}"
            self.bills[bid] = {
                "id": bid, "number": str(100 + self.n), "date": body["date"] + "T00:00:00",
                "contractorId": body["contractorId"], "bankAccountId": body.get("bankAccountId"),
                "withNDS": body["withNDS"], "status": "notPaid", "sumsWithNds": False, "withDiscount": False,
                "warehouseItems": [{**r, "sum": r["price"] * r["quantity"], "ndsRate": r.get("ndsRate", "withoutNds")}
                                   for r in body["warehouseItems"]],
            }
            return 201, {"id": bid}
        if method == "GET" and p.startswith("/bills/"):
            return 200, json.loads(json.dumps(self.bills[p.split("/")[-1]]))
        if method == "POST" and p in ("/acts", "/universal-transfer-documents"):
            self.posts.append((p, body))
            return 201, {"id": f"D{len(self.posts)}"}
        if method == "POST" and p == "/document-links":
            return 201, {"id": "L1", "url": "https://elba.example/doc/" + body["documentId"]}
        if method == "GET" and p == "/document-news":
            return 200, {"news": [n for n in self.news if n["id"] >= query["offset"]][:query["limit"]]}
        raise AssertionError(f"неожиданный запрос {method} {path}")

    def pay(self, bid, status, paid, total=None):
        if bid in self.bills:
            self.bills[bid]["status"] = status
            total = sum(i["sum"] for i in self.bills[bid]["warehouseItems"])
        self.news.append({"id": len(self.news), "newsType": "billPaymentNews",
                          "newsContent": {"billId": bid, "billStatus": status, "billSum": total,
                                          "relatedPaymentsSum": paid}})

    def add_bill(self, bid, total, status="notPaid", contractor="C9", day="2026-10-20"):
        self.bills[bid] = {"id": bid, "number": bid, "date": day, "contractorId": contractor, "status": status,
                           "warehouseItems": [{"price": total, "quantity": 1, "sum": total}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ELBA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ELBA_API_KEY", "test")
    s = load()
    s._org = lambda: "ORG"
    f = Fake()
    s._request = f
    return s, f, tmp_path


def journal(tmp_path, org="ORG"):
    return json.loads((tmp_path / f"journal-{org}.json").read_text())


def fails(exc_text):
    def raise_(*a, **k):
        raise OSError(exc_text)
    return raise_


# --- HTTP и подстановки ---

class Resp:
    def __init__(self, status, raw):
        self.status, self._raw = status, raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen_seq(s, monkeypatch, *steps):
    it = iter(steps)

    def fake(req, timeout=None):
        step = next(it)
        if isinstance(step, BaseException):
            raise step
        return step
    monkeypatch.setattr(s.urllib.request, "urlopen", fake)


def test_request_errors_become_elba_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ELBA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ELBA_API_KEY", "test")
    s = load()
    for err in (urllib.error.URLError("dns"), TimeoutError("t"), socket.timeout("t"), Resp(200, b"<html>")):
        urlopen_seq(s, monkeypatch, err)
        with pytest.raises(s.ElbaError) as e:
            s._request("GET", "/x")
        assert "проверьте" not in str(e.value)
    urlopen_seq(s, monkeypatch, TimeoutError("t"))
    with pytest.raises(s.ElbaError, match="документ мог создаться"):
        s._request("POST", "/x", {})
    urlopen_seq(s, monkeypatch, urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"err":1}')))
    with pytest.raises(s.ElbaError, match="HTTP 400"):
        s._request("POST", "/x", {})


def test_query_is_urlencoded(tmp_path, monkeypatch):
    monkeypatch.setenv("ELBA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ELBA_API_KEY", "test")
    s = load()
    seen = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        return Resp(200, b"{}")

    monkeypatch.setattr(s.urllib.request, "urlopen", fake)
    s._request("GET", "/x", query={"name": "ООО «Ромашка» & Ко", "offset": 0, "skip": None})
    assert seen == ["https://elba-api.kontur.ru/v1/x?name=%D0%9E%D0%9E%D0%9E+%C2%AB%D0%A0%D0%BE%D0%BC%D0%B0%D1%88"
                    "%D0%BA%D0%B0%C2%BB+%26+%D0%9A%D0%BE&offset=0"]


def test_bank_account(env):
    s, f, _ = env

    def accounts(lst):
        s._request = lambda *a, **k: (200, {"bankAccounts": lst})

    accounts([{"id": "A1", "closed": False}, {"id": "A2", "closed": True}])
    assert s._bank_account("ORG", None) == "A1"
    accounts([])
    assert s._bank_account("ORG", None) is None
    accounts([{"id": "A1", "closed": False}, {"id": "A2", "closed": False}])
    assert s._bank_account("ORG", "A2") == "A2"
    with pytest.raises(s.ElbaError, match="bank_account_id"):
        s._bank_account("ORG", None)


def test_bill_total_unknown_is_null(env):
    s, f, _ = env
    for items, total in (([{"sum": 100}, {"sum": 250.5}], 350.5), ([{"sum": 100}, {"sum": None}], None),
                         ([{"sum": 100}, {}], None), ([], None), ([{"sum": 0}], 0)):
        s._request = lambda *a, **k: (200, {"id": "B", "warehouseItems": items})
        bill = s.elba_get_bill("B")
        assert bill["total"] == total
        assert ("totalWarning" in bill) == (total is None)


# --- счёт и журнал ---

def test_bill_goes_to_journal(env):
    s, f, tmp = env
    s.elba_create_bill("C1", ITEM, project="site.ru")
    e = journal(tmp)["bills"]["B1"]
    assert (e["number"], e["project"], e["requested"], e["total"], e["date"]) == ("101", "site.ru", 40000, 40000, TODAY)
    assert e["contractor"] == "ООО Клиент C1"
    s.elba_document_link("B1")
    assert journal(tmp)["bills"]["B1"]["link"].endswith("/B1")


def test_duplicate_bill(env):
    s, f, _ = env
    s.elba_create_bill("C1", ITEM)
    with pytest.raises(s.ElbaError, match="allow_duplicate"):
        s.elba_create_bill("C1", ITEM)
    assert len(f.bills) == 1
    s.elba_create_bill("C1", ITEM, allow_duplicate=True)
    s.elba_create_bill("C1", [{"name": "x", "price": 1000, "quantity": 3}])
    s.elba_create_bill("C2", ITEM)
    s.elba_create_bill("C1", ITEM, bill_date=(date.today() + timedelta(days=10)).isoformat())
    assert len(f.bills) == 5


def test_get_fails_after_post_keeps_id(env):
    s, f, tmp = env

    def flaky(method, path, body=None, query=None):
        if method == "GET" and "/bills/" in path:
            raise s.ElbaError("GET: HTTP 503")
        return f(method, path, body, query)

    s._request = flaky
    r = s.elba_create_bill("C6", ITEM)
    assert r["id"] == "B1" and r["created"] and "не создавайте заново" in r["warning"]
    assert journal(tmp)["bills"]["B1"]["requested"] == 40000


def test_journal_failure_keeps_id(env):
    s, f, tmp = env
    (tmp / "journal-ORG.json").write_text("{}")
    r = s.elba_create_bill("C1", ITEM, allow_duplicate=True)
    assert r["total"] == 40000 and "B1" in journal(tmp)["bills"]

    (tmp / "journal-ORG.json").write_text("[]")
    r = s.elba_create_bill("C2", ITEM, allow_duplicate=True)
    assert r["id"] == "B2" and "journalWarning" in r
    with pytest.raises(Exception):  # журнал не прочитать — проверка дублей невозможна, POST не уходит
        s.elba_create_bill("C3", ITEM)
    assert len(f.bills) == 2

    (tmp / "journal-ORG.json").write_text("{}")
    s._track = fails("диск")
    r = s.elba_create_bill("C4", ITEM)
    assert r["id"] == "B3" and "journalWarning" in r


# --- акт и УПД ---

def test_act_from_bill(env):
    s, f, tmp = env
    s.elba_create_bill("C1", ITEM)
    a = s.elba_act_from_bill("B1")
    path, body = f.posts[-1]
    assert path == "/acts" and (body["reasonName"], body["reasonNumber"], body["reasonDate"]) == ("Счёт", "101", TODAY)
    assert (body["contractorId"], body["bankAccountId"], body["withNDS"]) == ("C1", "ACC", False)
    assert body["warehouseItems"] == [{"productName": "Доработка сайта", "unitName": "усл.", "quantity": 1,
                                       "price": 40000}]
    assert journal(tmp)["bills"]["B1"]["act"] == a["id"]
    with pytest.raises(s.ElbaError, match="уже создан"):
        s.elba_act_from_bill("B1")
    s.elba_act_from_bill("B1", allow_duplicate=True)
    assert len(f.posts) == 2


def test_upd_from_bill(env):
    s, f, tmp = env
    s.elba_create_bill("C1", ITEM)
    s.elba_upd_from_bill("B1")
    path, body = f.posts[-1]
    assert path == "/universal-transfer-documents" and body["status"] == "status2"
    assert body["warehouseItems"][0]["productType"] == "service" and "ndsRate" not in body["warehouseItems"][0]
    s.elba_create_bill("C3", [{"name": "Лицензия", "price": 5000}], with_nds=True)
    s.elba_upd_from_bill("B2")
    body = f.posts[-1][1]
    assert body["status"] == "status1" and body["warehouseItems"][0]["ndsRate"] == "nds22"
    assert journal(tmp)["bills"]["B2"]["upd"]


def test_create_upd(env):
    s, f, _ = env
    with pytest.raises(s.ElbaError, match="nds_rate"):
        s.elba_create_upd("C1", ITEM, with_nds=True)
    assert f.posts == []
    s.elba_create_upd("C1", ITEM, with_nds=True, nds_rate="nds5")
    assert f.posts[-1][1]["status"] == "status1" and f.posts[-1][1]["warehouseItems"][0]["ndsRate"] == "nds5"
    s.elba_create_upd("C1", [{"name": "Сервер", "price": 1, "type": "product"}])
    body = f.posts[-1][1]
    assert body["status"] == "status2" and body["warehouseItems"][0]["productType"] == "product"


# --- оплаты ---

def test_payment_news(env):
    s, f, tmp = env
    s.elba_create_bill("C1", ITEM, project="site.ru")
    s.elba_create_bill("C2", ITEM)
    f.pay("OLD", "paid", 999, total=999)
    r = s.elba_payment_news()
    assert r["initialized"] and r["skipped"] == 1 and journal(tmp)["news_offset"] == 1

    f.pay("B1", "paid", 40000)
    f.pay("B2", "partiallyPaid", 15000)
    f.add_bill("X2", 500, status="paid")
    f.pay("X2", "paid", 500)
    r = s.elba_payment_news()["payments"]
    assert [p["billId"] for p in r] == ["B1", "B2", "X2"]
    assert r[0]["project"] == "site.ru" and r[1]["paid"] == 15000
    assert r[2]["inJournal"] is False and r[2]["number"] == "X2" and "X2" not in journal(tmp)["bills"]
    assert journal(tmp)["bills"]["B1"]["status"] == "paid"
    assert s.elba_payment_news() == {"payments": []}


def test_payment_news_survives_enrichment_failure(env):
    s, f, _ = env
    s.elba_payment_news()
    f.pay("Z1", "paid", 10, total=10)

    def null_bill(method, path, body=None, query=None):
        if method == "GET" and path.endswith("/bills/Z1"):
            return 200, None
        return f(method, path, body, query)

    s._request = null_bill
    assert [p["billId"] for p in s.elba_payment_news()["payments"]] == ["Z1"]
    assert s.elba_payment_news() == {"payments": []}


def test_unpaid_debt(env):
    s, f, _ = env
    s.elba_create_bill("C1", ITEM)
    s.elba_create_bill("C2", ITEM)
    s.elba_create_bill("C3", [{"name": "x", "price": 5000}])
    f.pay("B1", "paid", 40000)
    f.pay("B2", "partiallyPaid", 15000)
    u = s.elba_unpaid()
    rows = {b["id"]: b for b in u["bills"]}
    assert set(rows) == {"B2", "B3"} and rows["B2"]["paid"] == 15000 and rows["B3"]["paid"] == 0
    assert u["debt"] == 25000 + 5000 and u["errors"] == [] and rows["B3"]["days"] == 0


def test_unpaid_partial_without_news_is_unknown(env):
    s, f, _ = env
    f.add_bill("Y1", 1000, status="partiallyPaid")
    s.elba_track_bill("Y1")
    assert s.elba_unpaid()["debt"] is None
    f.pay("Y1", "partiallyPaid", 400)
    assert s.elba_unpaid()["debt"] == 600


def test_unpaid_rechecks_paid_and_rejected(env):
    s, f, _ = env
    s.elba_create_bill("C1", ITEM)
    f.pay("B1", "paid", 40000)
    assert s.elba_unpaid()["bills"] == []
    f.pay("B1", "notPaid", 0)  # платёж отвязали — лента расходится с журналом
    assert [b["id"] for b in s.elba_unpaid()["bills"]] == ["B1"]

    s.elba_create_bill("C2", ITEM)  # по B2 новостей нет
    f.bills["B2"]["status"] = "rejected"
    s.elba_get_bill("B2")
    f.bills["B2"]["status"] = "notPaid"  # восстановлен без новости — виден только при recheck_all
    assert [b["id"] for b in s.elba_unpaid()["bills"]] == ["B1"]
    assert [b["id"] for b in s.elba_unpaid(recheck_all=True)["bills"]] == ["B1", "B2"]


def test_unpaid_unknown_sum_not_hidden_by_old(env):
    s, f, tmp = env
    s.elba_create_bill("C1", ITEM)
    f.bills["B1"]["warehouseItems"][0]["sum"] = None
    assert s.elba_unpaid()["debt"] is None and journal(tmp)["bills"]["B1"]["total"] is None


def test_unpaid_uses_fresh_bill(env):
    s, f, tmp = env
    s.elba_create_bill("C1", [{"name": "x", "price": 5000}])
    f.bills["B1"]["warehouseItems"][0].update(price=7000, sum=7000)
    f.bills["B1"].update(number="555", date="2026-10-25T00:00:00")
    s._track = fails("диск")  # запись журнала внутри elba_get_bill не проходит
    b = s.elba_unpaid()["bills"][0]
    assert (b["total"], b["number"], b["date"]) == (7000, "555", "2026-10-25")
    assert journal(tmp)["bills"]["B1"]["total"] == 5000


def test_refresh_syncs_requisites_and_duplicate_sum(env):
    s, f, tmp = env
    s.elba_create_bill("C1", [{"name": "x", "price": 5000}])
    f.bills["B1"].update(number="555", contractorId="C55")
    f.bills["B1"]["warehouseItems"][0].update(price=8000, sum=8000)
    s.elba_track_bill("B1")
    e = journal(tmp)["bills"]["B1"]
    assert (e["number"], e["contractorId"], e["contractor"], e["total"], e["requested"]) == \
        ("555", "C55", "ООО Клиент C55", 8000, 8000)
    with pytest.raises(s.ElbaError, match="allow_duplicate"):
        s.elba_create_bill("C55", [{"name": "x", "price": 8000}])
    s.elba_create_bill("C55", [{"name": "x", "price": 5000}])


def test_contractor_change_drops_old_name(env):
    s, f, _ = env
    s.elba_create_bill("C1", ITEM)

    def no_contractor(method, path, body=None, query=None):
        if "/contractors/" in path:
            raise s.ElbaError("HTTP 500")
        return f(method, path, body, query)

    s._request = no_contractor
    f.bills["B1"]["contractorId"] = "C66"
    t = s.elba_track_bill("B1")
    assert t["contractorId"] == "C66" and "contractor" not in t


def test_organizations_have_separate_journals(env):
    s, f, tmp = env
    s.elba_create_bill("C1", ITEM)
    s.elba_payment_news()
    s._org = lambda: "ORG2"
    assert s.elba_unpaid() == {"bills": [], "debt": 0, "errors": []}
    assert s.elba_payment_news()["initialized"]
    assert "B1" in journal(tmp)["bills"] and journal(tmp, "ORG2")["bills"] == {}


# --- параллельная работа ---

def test_race_creates_one_document(env):
    s, f, _ = env
    s.elba_create_bill("C1", ITEM)

    def slow(method, path, body=None, query=None):
        if method == "POST":
            time.sleep(0.3)
        return f(method, path, body, query)

    s._request = slow

    def run(fn, out):
        try:
            out.append(fn())
        except s.ElbaError as e:
            out.append(str(e))

    for fn, created in ((lambda: s.elba_act_from_bill("B1"), lambda: len(f.posts)),
                        (lambda: s.elba_create_bill("C77", ITEM), lambda: len(f.bills) - 1)):
        out = []
        ts = [threading.Thread(target=run, args=(fn, out)) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert created() == 1 and sum(isinstance(r, str) for r in out) == 1


def test_parallel_processes_keep_all_entries(env):
    s, f, tmp = env
    worker = tmp / "worker.py"
    worker.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('server', {str(SERVER)!r})\n"
        "s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)\n"
        "for i in range(50):\n"
        "    s._track({'id': f'P{sys.argv[1]}-{i}', 'number': str(i), 'date': '2026-10-01'})\n"
    )
    procs = [subprocess.Popen([sys.executable, "-I", str(worker), str(k)],
                              env={"ELBA_STATE_DIR": str(tmp), "ELBA_API_KEY": "test", "ELBA_ORG_ID": "ORG"})
             for k in range(4)]
    assert all(p.wait() == 0 for p in procs)
    assert len(journal(tmp)["bills"]) == 200


def test_no_fcntl_refuses(env):
    s, f, _ = env
    s.fcntl = None
    with pytest.raises(s.ElbaError, match="fcntl"):
        s.elba_create_bill("C1", ITEM)
    assert f.bills == {}
