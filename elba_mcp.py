# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["mcp>=2.3,<3"]
# ///
"""MCP-сервер Контур.Эльбы: контрагенты, счета, акты, УПД, ссылки на документы, оплаты.

Запуск как MCP (stdio):  uvx elba-mcp  или  uv run --script elba_mcp.py
Запуск разово из шелла:   elba-mcp call <инструмент> [args.json]

Ключ — ELBA_API_KEY из окружения или из .env рядом со скриптом (при запуске скриптом).
Организация — ELBA_ORG_ID, иначе единственная доступная по ключу.
Журнал счетов — ELBA_STATE_DIR, иначе ~/.local/state/elba-mcp.
"""

import functools
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: журнал без блокировки
    fcntl = None

from mcp.server.mcpserver import MCPServer

__version__ = "0.1.0"  # единственное место версии: pyproject.toml читает её отсюда

BASE = "https://elba-api.kontur.ru/v1"
HERE = Path(__file__).resolve().parent
DUP_DAYS = 7  # тот же клиент и та же сумма за неделю — почти наверняка повтор, а не новый счёт


class ElbaError(Exception):
    pass


def _env(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == name:
                return v.strip().strip("'\"")
    return None


STATE = Path(_env("ELBA_STATE_DIR") or Path.home() / ".local/state/elba-mcp")


def _request(method: str, path: str, body: dict | None = None, query: dict | None = None):
    key = _env("ELBA_API_KEY")
    if not key:
        raise ElbaError(f"нет ELBA_API_KEY ни в окружении, ни в {HERE / '.env'}")
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Kontur-ApiKey", key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:1000]
        raise ElbaError(f"{method} {path}: HTTP {e.code} {text}") from None
    except (OSError, ValueError) as e:  # сеть, тайм-аут, не-JSON в ответе
        hint = "" if method == "GET" else " — документ мог создаться, проверьте в Эльбе перед повтором"
        raise ElbaError(f"{method} {path}: {type(e).__name__} {e}{hint}") from None


@functools.cache  # ошибки не кэшируются, удачный ответ — на весь процесс
def _org() -> str:
    org = _env("ELBA_ORG_ID")
    if org:
        return org
    _, res = _request("GET", "/organizations", query={"offset": 0, "limit": 100})
    orgs = res["organizations"]
    if len(orgs) != 1:
        raise ElbaError(f"организаций по ключу {len(orgs)}, задайте ELBA_ORG_ID: {orgs}")
    return orgs[0]["id"]


def _wait_task(task: dict, timeout: float = 30) -> dict:
    """Поиск отвечает 200 сразу или 202 с асинхронной операцией — ждём её результат."""
    deadline = time.monotonic() + timeout
    while True:
        _, st = _request("GET", f"/long-running-tasks/{task['id']}")
        if st["status"] == "completed":
            return st["result"]
        if st["status"] == "failed":
            raise ElbaError(f"операция {task['id']} не выполнена: {st.get('errorDescription')}")
        if time.monotonic() > deadline:
            raise ElbaError(f"операция {task['id']} не завершилась за {timeout} с")
        time.sleep(1)


def _bank_account(org: str, given: str | None) -> str | None:
    """Явно заданный или единственный открытый счёт. Без открытых — None, решает Эльба;
    при нескольких — ошибка: молча подставленный основной может оказаться не тем, куда ждут оплату."""
    if given:
        return given
    _, res = _request("GET", f"/organizations/{org}/bank-accounts", query={"offset": 0, "limit": 100})
    opened = [a for a in res["bankAccounts"] if not a["closed"]]
    if len(opened) > 1:
        raise ElbaError(f"открытых банковских счетов {len(opened)}, укажите bank_account_id: {opened}")
    return opened[0]["id"] if opened else None


def _items(items: list[dict], nds_rate: str | None, upd: bool = False) -> list[dict]:
    """nds_rate — ставка по умолчанию для документа с НДС, None — документ без НДС."""
    out = []
    for i, it in enumerate(items, 1):
        if not it.get("name") or it.get("price") is None:
            raise ElbaError(f"позиция {i}: нужны name и price")
        row = {
            "productName": it["name"],
            "unitName": it.get("unit", "усл."),
            "quantity": it.get("quantity", 1),
            "price": it["price"],
        }
        if nds_rate:
            row["ndsRate"] = it.get("nds_rate", nds_rate)
        if upd:
            row["productType"] = it.get("type", "service")
        out.append(row)
    return out


@contextmanager
def _locked(name: str):
    """Блокировка между процессами: сервер запущен в каждой сессии Claude, и работают они параллельно."""
    if not fcntl:  # без блокировки две сессии создадут два одинаковых документа — лучше отказ
        raise ElbaError("нет fcntl (Windows): журнал и защита от дублей без блокировки не поддерживаются")
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / name, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _exclusive():
    """Проверка дублей, создание документа и запись в журнал — одной операцией: иначе две сессии
    одновременно пройдут проверку и создадут два документа. Так же опрос ленты оплат — иначе откат курсора."""
    return _locked("operation.lock")


@contextmanager
def _journal():
    """Журнал счетов организации. Ведётся здесь, потому что API Эльбы не отдаёт список счетов — только по id."""
    path = STATE / f"journal-{_org()}.json"
    with _locked("journal.lock"):
        data = json.loads(path.read_text()) if path.exists() else {}
        data.setdefault("bills", {})
        data.setdefault("news_offset", None)
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)


def _contractor_name(contractor_id: str | None) -> str | None:
    if not contractor_id:
        return None
    try:
        c = _request("GET", f"/organizations/{_org()}/contractors/{contractor_id}")[1]
        return c.get("shortName") or c.get("name")
    except ElbaError:
        return None


def _total(bill: dict) -> float | None:
    """Сумма счёта по позициям; None, если Эльба не вернула сумму хоть одной."""
    sums = [i.get("sum") for i in bill.get("warehouseItems") or []]
    return sum(sums) if sums and None not in sums else None


def _track(bill: dict, project: str | None = None, **extra) -> dict:
    """Записать или обновить счёт в журнале. Пустые поля не затирают известное — кроме сумм:
    у перечитанного счёта (с позициями) они всегда свежие, даже неизвестные."""
    fields = {k: bill.get(k) for k in ("number", "contractorId", "status")}
    fields["date"] = (bill.get("date") or "")[:10] or None
    fields.update(project=project, **extra)
    with _journal() as j:
        entry = j["bills"].setdefault(bill["id"], {})
        cid = fields["contractorId"]
        if cid and (cid != entry.get("contractorId") or "contractor" not in entry):
            entry.pop("contractor", None)  # имя прежнего клиента хуже, чем никакого
            fields["contractor"] = _contractor_name(cid)
        entry.update({k: v for k, v in fields.items() if v is not None})
        if "warehouseItems" in bill:
            entry["total"] = _total(bill)
            rows = bill["warehouseItems"] or []
            # requested — цена × количество, как при создании: по нему ловятся дубли
            if rows and all(r.get("price") is not None and r.get("quantity") is not None for r in rows):
                entry["requested"] = round(sum(float(r["price"]) * float(r["quantity"]) for r in rows), 2)
            else:
                entry.pop("requested", None)
        return {"id": bill["id"], **entry}


def _tracked(bill_id: str) -> dict | None:
    with _journal() as j:
        return j["bills"].get(bill_id)


def _news(org: str, offset: int) -> list[dict]:
    news = []
    while True:
        _, res = _request("GET", f"/organizations/{org}/document-news", query={"offset": offset, "limit": 1000})
        batch = res["news"]
        news += batch
        if len(batch) < 1000:
            return news
        offset = batch[-1]["id"] + 1  # offset — id новости, с которой читать


mcp = MCPServer("elba", version=__version__)


@mcp.tool()
def elba_organizations() -> dict:
    """Организации, доступные по ключу, и их банковские счета."""
    _, res = _request("GET", "/organizations", query={"offset": 0, "limit": 100})
    for o in res["organizations"]:
        _, acc = _request("GET", f"/organizations/{o['id']}/bank-accounts", query={"offset": 0, "limit": 100})
        o["bankAccounts"] = acc["bankAccounts"]
    return res


@mcp.tool()
def elba_templates() -> list[dict]:
    """Шаблоны документов (счета, акты, договоры) — id для template_id в elba_create_bill и elba_create_act."""
    _, res = _request("GET", f"/organizations/{_org()}/document-templates", query={"offset": 0, "limit": 100})
    return res["documentTemplates"]


@mcp.tool()
def elba_find_contractor(inn: str | None = None, name: str | None = None, kpp: str | None = None) -> list[dict]:
    """Найти контрагента в Эльбе по ИНН (надёжнее) и/или названию."""
    flt = {k: v for k, v in {"inn": inn, "name": name, "kpp": kpp}.items() if v}
    if not flt:
        raise ElbaError("укажите inn или name")
    code, res = _request("POST", f"/organizations/{_org()}/contractors/search", {"filter": flt, "limit": 50})
    if code == 202:
        res = _wait_task(res)
    return res["contractors"]


@mcp.tool()
def elba_get_contractor(contractor_id: str) -> dict:
    """Карточка контрагента с контактами."""
    return _request("GET", f"/organizations/{_org()}/contractors/{contractor_id}")[1]


@mcp.tool()
def elba_create_contractor(
    name: str,
    inn: str | None = None,
    kpp: str | None = None,
    short_name: str | None = None,
    address: str | None = None,
    ogrn: str | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
) -> dict:
    """Создать контрагента. Перед созданием проверьте elba_find_contractor по ИНН, чтобы не завести дубль."""
    body = {"name": name, "inn": inn, "kpp": kpp, "shortName": short_name, "address": address, "ogrn": ogrn}
    if contact_name or contact_email or contact_phone:
        body["contacts"] = [{
            "name": contact_name,
            "phone": contact_phone,
            "emails": [contact_email] if contact_email else [],
        }]
    return _request("POST", f"/organizations/{_org()}/contractors", {k: v for k, v in body.items() if v is not None})[1]


@mcp.tool()
def elba_create_bill(
    contractor_id: str,
    items: list[dict],
    bill_date: str | None = None,
    number: str | None = None,
    comment: str | None = None,
    with_nds: bool = False,
    bank_account_id: str | None = None,
    template_id: str | None = None,
    project: str | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Выставить счёт клиенту.

    items — список {"name": "Название услуги", "price": 10000, "quantity": 1, "unit": "усл."};
    quantity и unit необязательны (1 и «усл.»), при with_nds можно задать "nds_rate" (nds22 по умолчанию).
    bill_date — ГГГГ-ММ-ДД, по умолчанию сегодня; number — по умолчанию следующий номер Эльбы.
    template_id — шаблон из elba_templates; сверьте его текст: шаблон-оферта может описывать не тот договор.
    bank_account_id — по умолчанию единственный открытый счёт организации; при нескольких открытых обязателен.
    project — метка для журнала (например сайт клиента), по ней видно, за что счёт.
    Счёт записывается в журнал. Если тому же клиенту на ту же сумму счёт выставлен в пределах недели,
    создание останавливается; allow_duplicate=true — выставить всё равно.
    Возвращает счёт с номером, суммой и статусом.
    """
    with _exclusive():
        return _create_bill(contractor_id, items, bill_date, number, comment, with_nds,
                            bank_account_id, template_id, project, allow_duplicate)


def _create_bill(contractor_id, items, bill_date, number, comment, with_nds,
                 bank_account_id, template_id, project, allow_duplicate) -> dict:
    org = _org()
    bill_date = bill_date or date.today().isoformat()
    rows = _items(items, "nds22" if with_nds else None)
    requested = round(sum(float(r["price"]) * float(r["quantity"]) for r in rows), 2)
    if not allow_duplicate:
        with _journal() as j:
            dups = [
                {"id": bid, **e} for bid, e in j["bills"].items()
                if e.get("contractorId") == contractor_id
                and e.get("requested", e.get("total")) == requested
                and e.get("date")
                and abs((date.fromisoformat(bill_date) - date.fromisoformat(e["date"])).days) <= DUP_DAYS
            ]
        if dups:
            raise ElbaError(f"похожий счёт уже есть в журнале: {dups}. Если это не повтор — allow_duplicate=true")
    body = {
        "date": bill_date,
        "number": number,
        "comment": comment,
        "contractorId": contractor_id,
        "bankAccountId": _bank_account(org, bank_account_id),
        "templateId": template_id,
        "withNDS": with_nds,
        "warehouseItems": rows,
    }
    _, res = _request("POST", f"/organizations/{org}/bills", {k: v for k, v in body.items() if v is not None})
    # дальше счёт уже создан: никакая ошибка не должна скрыть его id, иначе повтор заведёт второй
    try:
        out = elba_get_bill(res["id"])
    except ElbaError as e:
        out = {"id": res["id"], "created": True,
               "warning": f"счёт создан, но не прочитан — не создавайте заново, проверьте elba_get_bill: {e}"}
    try:
        _track({"contractorId": contractor_id, "date": bill_date, **out}, project, requested=requested)
    except Exception as e:
        out["journalWarning"] = f"счёт создан, но не записан в журнал ({e}) — добавьте elba_track_bill"
    return out


@mcp.tool()
def elba_get_bill(bill_id: str) -> dict:
    """Счёт по id: номер, позиции, сумма и статус оплаты (notPaid, paid, partiallyPaid, rejected, overdue).
    total — null, если Эльба не вернула сумму хотя бы одной позиции. Счёт из журнала обновляется в журнале."""
    bill = _request("GET", f"/organizations/{_org()}/bills/{bill_id}")[1]
    bill["total"] = _total(bill)
    if bill["total"] is None:
        bill["totalWarning"] = "Эльба не вернула суммы позиций — итог неизвестен, сверьте счёт по ссылке"
    try:
        if _tracked(bill_id) is not None:
            _track(bill)
    except Exception:
        pass  # журнал — вспомогательный: его сбой не должен скрыть счёт, в том числе только что созданный
    return bill


@mcp.tool()
def elba_track_bill(bill_id: str, project: str | None = None) -> dict:
    """Добавить в журнал счёт, выставленный не через этот сервер (в Эльбе вручную или до появления журнала)."""
    return _track(elba_get_bill(bill_id), project)


@mcp.tool()
def elba_unpaid(recheck_all: bool = False) -> dict:
    """Неоплаченные счета из журнала со статусом из Эльбы и оплаченной частью из ленты оплат.
    Оплаченные и отменённые перечитываются, если лента расходится с журналом; recheck_all=true — все.
    Возвращает счета (номер, клиент, проект, сумма, оплачено, дней с даты счёта, ссылка) и общий долг;
    debt — null, если хоть одна сумма неизвестна или счёт не перечитался."""
    org = _org()
    latest = {n["newsContent"]["billId"]: n["newsContent"]  # последняя новость по счёту — текущая оплата
              for n in _news(org, 0) if n.get("newsType") == "billPaymentNews"}
    with _journal() as j:
        ids = [bid for bid, e in j["bills"].items()
               if recheck_all or e.get("status") not in ("paid", "rejected")
               or (bid in latest and latest[bid]["billStatus"] != e.get("status"))]
    errors, fresh = [], {}
    for bid in ids:
        try:
            fresh[bid] = elba_get_bill(bid)
        except ElbaError as e:
            errors.append({"id": bid, "error": str(e)})
    with _journal() as j:
        entries = {bid: dict(j["bills"][bid]) for bid in ids if bid in j["bills"]}
    rows = []
    for bid, e in entries.items():
        b = fresh.get(bid)
        if b:  # реквизиты — из свежего ответа: запись журнала в elba_get_bill могла не пройти
            if b.get("contractorId") != e.get("contractorId"):
                e.pop("contractor", None)
            e.update(number=b.get("number"), date=(b.get("date") or "")[:10] or None,
                     contractorId=b.get("contractorId"), total=b["total"], status=b.get("status"))
        if e.get("status") not in ("paid", "rejected"):
            rows.append({"id": bid, **e})
    for r in rows:
        p = latest.get(r["id"])
        if r.get("status") == "notPaid":
            r["paid"] = 0
        elif p:
            r["paid"] = p["relatedPaymentsSum"]
        else:  # оплат по счёту в ленте не было: просрочен без оплат — 0, частичная оплата без новости — неизвестно
            r["paid"] = 0 if r.get("status") == "overdue" else None
        if r.get("date"):
            r["days"] = (date.today() - date.fromisoformat(r["date"])).days
    rows.sort(key=lambda r: r.get("date") or "")
    known = not errors and all(r.get("total") is not None and r["paid"] is not None for r in rows)
    return {
        "bills": rows,
        "debt": round(sum(r["total"] - r["paid"] for r in rows), 2) if known else None,
        "errors": errors,
    }


@mcp.tool()
def elba_payment_news() -> dict:
    """Оплаты по счетам с прошлой проверки (лента новостей Эльбы): счёт, статус, сумма счёта и сколько пришло.
    Обновляет статусы в журнале. Первый вызов только ставит отметку в конец ленты — история не выводится."""
    org = _org()
    with _exclusive():
        with _journal() as j:
            start = j.get("news_offset")
            known = {bid: dict(e) for bid, e in j["bills"].items()}
        news = _news(org, start or 0)
        payments = [n["newsContent"] for n in news if n.get("newsType") == "billPaymentNews"]
        out = []
        for p in payments if start is not None else []:
            row = {"billId": p["billId"], "status": p["billStatus"], "sum": p["billSum"],
                   "paid": p["relatedPaymentsSum"]}
            entry = known.get(p["billId"])
            if entry is None:  # счёт не из журнала — номер и клиента берём из Эльбы
                try:
                    bill = _request("GET", f"/organizations/{org}/bills/{p['billId']}")[1]
                    entry = {"number": bill.get("number"), "date": (bill.get("date") or "")[:10],
                             "contractor": _contractor_name(bill.get("contractorId")), "inJournal": False}
                except Exception:  # без номера оплата всё равно выводится
                    entry = {"inJournal": False}
            row.update({k: entry[k] for k in ("number", "date", "contractor", "project", "inJournal") if k in entry})
            out.append(row)
        # курсор двигается последним, когда результат уже собран
        with _journal() as j:
            for p in payments:
                entry = j["bills"].get(p["billId"])
                if entry is not None:
                    entry.update(status=p["billStatus"], total=p["billSum"], paid=p["relatedPaymentsSum"])
            j["news_offset"] = news[-1]["id"] + 1 if news else (start or 0)
    if start is None:
        return {"initialized": True, "skipped": len(news),
                "message": "отметка поставлена в конец ленты; следующий вызов покажет новые оплаты"}
    return {"payments": out}


@mcp.tool()
def elba_create_act(
    contractor_id: str,
    items: list[dict],
    act_date: str | None = None,
    number: str | None = None,
    reason_name: str | None = None,
    reason_number: str | None = None,
    reason_date: str | None = None,
    with_nds: bool = False,
    bank_account_id: str | None = None,
    template_id: str | None = None,
) -> dict:
    """Создать акт выполненных работ. items — как в elba_create_bill.
    Основание (например «Счёт», номер и дата счёта) — reason_name, reason_number, reason_date.
    bank_account_id — как в elba_create_bill. template_id — шаблон акта из elba_templates.
    Акт по выставленному счёту проще сделать elba_act_from_bill."""
    org = _org()
    body = {
        "date": act_date or date.today().isoformat(),
        "number": number,
        "contractorId": contractor_id,
        "bankAccountId": _bank_account(org, bank_account_id),
        "templateId": template_id,
        "withNDS": with_nds,
        "reasonName": reason_name,
        "reasonNumber": reason_number,
        "reasonDate": reason_date,
        "warehouseItems": _items(items, "nds22" if with_nds else None),
    }
    return _request("POST", f"/organizations/{org}/acts", {k: v for k, v in body.items() if v is not None})[1]


def _from_bill(bill_id: str, kind: str, allow_duplicate: bool) -> tuple[dict, dict]:
    """Общая часть акта и УПД по счёту: клиент, банковский счёт, позиции, основание «Счёт № от»."""
    entry = _tracked(bill_id)
    if entry and entry.get(kind) and not allow_duplicate:
        raise ElbaError(f"по счёту {entry.get('number')} уже создан документ {entry[kind]}. "
                        "Если нужен второй — allow_duplicate=true")
    bill = _request("GET", f"/organizations/{_org()}/bills/{bill_id}")[1]
    items = [{k: v for k, v in {
        "productName": i.get("productName"),
        "unitName": i.get("unitName"),
        "quantity": i.get("quantity"),
        "price": i.get("price"),
        "discount": i.get("discount"),
        "ndsRate": i.get("ndsRate") if bill.get("withNDS") else None,
    }.items() if v is not None} for i in bill.get("warehouseItems") or []]
    if not items:
        raise ElbaError(f"в счёте {bill_id} нет позиций")
    body = {
        "contractorId": bill.get("contractorId"),
        "bankAccountId": bill.get("bankAccountId"),
        "sumsWithNDS": bill.get("sumsWithNds"),
        "withDiscount": bill.get("withDiscount"),
        "reasonName": "Счёт",
        "reasonNumber": bill.get("number"),
        "reasonDate": (bill.get("date") or "")[:10] or None,
        "warehouseItems": items,
    }
    return bill, body


def _post_from_bill(bill: dict, kind: str, path: str, body: dict) -> dict:
    res = _request("POST", f"/organizations/{_org()}/{path}", {k: v for k, v in body.items() if v is not None})[1]
    out = {"id": res["id"], "basedOnBill": bill.get("number")}
    try:
        _track(bill, **{kind: res["id"]})
    except Exception as e:
        out["journalWarning"] = f"документ создан, но не отмечен в журнале счёта: {e}"
    return out


@mcp.tool()
def elba_act_from_bill(
    bill_id: str,
    act_date: str | None = None,
    number: str | None = None,
    template_id: str | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Акт по выставленному счёту: позиции, клиент и банковский счёт берутся из счёта, основание — «Счёт № от».
    act_date — ГГГГ-ММ-ДД, по умолчанию сегодня. Второй акт по тому же счёту — только с allow_duplicate=true."""
    with _exclusive():
        bill, body = _from_bill(bill_id, "act", allow_duplicate)
        body.update(date=act_date or date.today().isoformat(), number=number,
                    templateId=template_id, withNDS=bill.get("withNDS"))
        return _post_from_bill(bill, "act", "acts", body)


@mcp.tool()
def elba_create_upd(
    contractor_id: str,
    items: list[dict],
    with_nds: bool = False,
    nds_rate: str | None = None,
    upd_date: str | None = None,
    number: str | None = None,
    reason_name: str | None = None,
    reason_number: str | None = None,
    reason_date: str | None = None,
    bank_account_id: str | None = None,
) -> dict:
    """Создать УПД. items — как в elba_create_bill, плюс "type": "service" (по умолчанию) или "product".
    with_nds — УПД со счётом-фактурой (статус 1), без — только передаточный документ (статус 2).
    nds_rate — ставка при with_nds, обязательна: withoutNds, nds0, nds5, nds10, nds20, nds22
    (у позиции можно переопределить "nds_rate"). УПД по выставленному счёту — elba_upd_from_bill."""
    if with_nds and not nds_rate:
        raise ElbaError("для УПД с НДС укажите nds_rate — ставка не подставляется сама")
    org = _org()
    body = {
        "date": upd_date or date.today().isoformat(),
        "number": number,
        "status": "status1" if with_nds else "status2",
        "contractorId": contractor_id,
        "bankAccountId": _bank_account(org, bank_account_id),
        "reasonName": reason_name,
        "reasonNumber": reason_number,
        "reasonDate": reason_date,
        "warehouseItems": _items(items, nds_rate if with_nds else None, upd=True),
    }
    path = f"/organizations/{org}/universal-transfer-documents"
    return _request("POST", path, {k: v for k, v in body.items() if v is not None})[1]


@mcp.tool()
def elba_upd_from_bill(
    bill_id: str,
    upd_date: str | None = None,
    number: str | None = None,
    product_type: str = "service",
    allow_duplicate: bool = False,
) -> dict:
    """УПД по выставленному счёту: позиции, клиент, банковский счёт и НДС берутся из счёта, основание — «Счёт № от».
    Счёт с НДС даёт УПД со счётом-фактурой (статус 1), без НДС — статус 2.
    product_type — service (по умолчанию) или product для всех позиций. Второй УПД по счёту — allow_duplicate=true."""
    with _exclusive():
        bill, body = _from_bill(bill_id, "upd", allow_duplicate)
        for i in body["warehouseItems"]:
            i["productType"] = product_type
        body.update(date=upd_date or date.today().isoformat(), number=number,
                    status="status1" if bill.get("withNDS") else "status2")
        return _post_from_bill(bill, "upd", "universal-transfer-documents", body)


@mcp.tool()
def elba_document_link(document_id: str, with_signatures: bool = True, qr_payment: bool = True) -> dict:
    """Публичная ссылка на счёт или акт — её отправляют клиенту, по ней документ открывается и скачивается в PDF.
    with_signatures — с печатью и подписью; qr_payment — оплата по QR (только для счетов).
    Ссылка на счёт из журнала сохраняется в журнале."""
    body = {
        "documentId": document_id,
        "published": True,
        "withSignatures": with_signatures,
        "isQrCodeEnabled": qr_payment,
    }
    res = _request("POST", f"/organizations/{_org()}/document-links", body)[1]
    try:
        with _journal() as j:
            entry = j["bills"].get(document_id)
            if entry is not None and res.get("url"):
                entry["link"] = res["url"]
    except Exception:
        pass  # ссылка важнее журнала
    return res


def _cli(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] != "call":
        sys.exit("использование: elba-mcp call <инструмент> [args.json]")
    fn = globals().get(argv[1])
    if not callable(fn) or not argv[1].startswith("elba_"):
        sys.exit(f"нет инструмента {argv[1]}")
    args = json.loads(Path(argv[2]).read_text()) if len(argv) > 2 else {}
    try:
        print(json.dumps(fn(**args), ensure_ascii=False, indent=2))
    except ElbaError as e:
        sys.exit(f"ошибка Эльбы: {e}")


def main() -> None:
    if len(sys.argv) > 1:
        _cli(sys.argv[1:])
    else:
        mcp.run()


if __name__ == "__main__":
    main()
