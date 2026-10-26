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
    msvcrt = None
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from mcp.server.mcpserver import MCPServer

__version__ = "0.1.1"  # единственное место версии: pyproject.toml читает её отсюда

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
        for line in env.read_text(encoding="utf-8").splitlines():
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
    if not fcntl and not msvcrt:  # без блокировки две сессии создадут два одинаковых документа — лучше отказ
        raise ElbaError("нет ни fcntl, ни msvcrt: журнал и защита от дублей без блокировки не поддерживаются")
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / name, "a+") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
            return
        _win_lock(lock)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _win_lock(lock, timeout: float = 300) -> None:
    """msvcrt.LK_LOCK ждёт всего 10 с, а операция с несколькими запросами к API бывает дольше — ждём сами."""
    deadline = time.monotonic() + timeout
    while True:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise ElbaError(f"журнал занят другой сессией дольше {timeout:.0f} с") from None
            time.sleep(0.1)


def _exclusive():
    """Проверка дублей, создание документа и запись в журнал — одной операцией: иначе две сессии
    одновременно пройдут проверку и создадут два документа. Так же опрос ленты оплат — иначе откат курсора."""
    return _locked("operation.lock")


@contextmanager
def _journal():
    """Журнал счетов организации. Ведётся здесь, потому что API Эльбы не отдаёт список счетов — только по id."""
    path = STATE / f"journal-{_org()}.json"
    with _locked("journal.lock"):
        # кодировка явно: на Windows по умолчанию cp1252, и русское имя клиента уронило бы запись
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault("bills", {})
        data.setdefault("news_offset", None)
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
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


def _latest_payments(org: str) -> dict:
    """Последняя новость об оплате по каждому счёту — его текущая оплаченная сумма."""
    return {n["newsContent"]["billId"]: n["newsContent"]
            for n in _news(org, 0) if n.get("newsType") == "billPaymentNews"}


def _paid(entry: dict, latest: dict) -> float | None:
    """Оплаченная часть счёта из журнала: None — узнать нельзя."""
    status = entry.get("status")
    if status == "paid":  # переплата видна по ленте; устаревшая частичная новость меньше суммы счёта
        total, rel = entry.get("total"), latest.get(entry["id"], {}).get("relatedPaymentsSum")
        return rel if total is None else (total if rel is None else max(total, rel))
    if status == "notPaid":
        return 0
    if entry["id"] in latest:
        return latest[entry["id"]]["relatedPaymentsSum"]
    # оплат по счёту в ленте не было: просрочен без оплат — 0, частичная оплата без новости — неизвестно
    return 0 if status == "overdue" else None


def _merge_fresh(entry: dict, bill: dict) -> None:
    """Реквизиты строки — из свежего ответа: запись журнала в elba_get_bill могла не пройти."""
    if bill.get("contractorId") != entry.get("contractorId"):
        entry.pop("contractor", None)
    entry.update(number=bill.get("number"), date=(bill.get("date") or "")[:10] or None,
                 contractorId=bill.get("contractorId"), total=bill["total"], status=bill.get("status"))


def _sum(values: list) -> float | None:
    return round(sum(values), 2) if all(v is not None for v in values) else None


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
    latest = _latest_payments(_org())
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
        if fresh.get(bid):
            _merge_fresh(e, fresh[bid])
        if e.get("status") not in ("paid", "rejected"):
            rows.append({"id": bid, **e})
    for r in rows:
        r["paid"] = _paid(r, latest)
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
def elba_report(
    project: str | None = None,
    contractor_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Сводка по журналу счетов: сколько выставлено, оплачено и не оплачено, сколько счетов закрыто актом или УПД —
    всего и по проектам, плюс список счетов (по нему же счёт находят по номеру).
    Фильтры: project, contractor_id, date_from и date_to (ГГГГ-ММ-ДД, по дате счёта). Отменённые не считаются.
    Статусы — из журнала; счета, у которых лента оплат с журналом расходится, перечитываются из Эльбы.
    Оплаченная часть — из ленты. Суммы null, если какую-то узнать не удалось; errors — счета, что не перечитались.
    byProject — список, project null — счета без проекта."""
    latest = _latest_payments(_org())
    with _journal() as j:
        rows = [{"id": bid, **e} for bid, e in j["bills"].items()]
    errors = []
    for r in rows:
        p = latest.get(r["id"])
        # оплату провели или отвязали, счёт изменили после записи в журнал
        if p and (p["billStatus"] != r.get("status") or p["billSum"] != r.get("total")):
            try:
                _merge_fresh(r, elba_get_bill(r["id"]))
            except ElbaError as e:
                errors.append({"id": r["id"], "error": str(e)})
                r["stale"] = True
    rows = [r for r in rows
            if (r.get("stale") or r.get("status") != "rejected")  # неперечитанный «отменён» мог ожить
            and (project is None or r.get("project") == project)
            and (contractor_id is None or r.get("contractorId") == contractor_id)
            and (not date_from or (r.get("date") or "") >= date_from)
            and (not date_to or (r.get("date") or "9999") <= date_to)]
    for r in rows:
        r["paid"] = None if r.get("stale") else _paid(r, latest)
    rows.sort(key=lambda r: r.get("date") or "")

    def summary(part: list[dict]) -> dict:
        billed, paid = _sum([r.get("total") for r in part]), _sum([r["paid"] for r in part])
        return {"bills": len(part), "billed": billed, "paid": paid,
                "unpaid": round(billed - paid, 2) if billed is not None and paid is not None else None,
                "withAct": sum(1 for r in part if r.get("act")), "withUpd": sum(1 for r in part if r.get("upd"))}

    projects = sorted({r.get("project") for r in rows}, key=lambda p: (p is None, p or ""))
    return {
        "total": summary(rows),
        "byProject": [{"project": p, **summary([r for r in rows if r.get("project") == p])} for p in projects],
        "bills": rows,
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
                entry.update(link=res["url"], linkId=res.get("id"), linkPublished=True)
    except Exception:
        pass  # ссылка важнее журнала
    return res


def _link_id(url: str | None) -> str | None:
    """id ссылки — 32 hex в конце её адреса (…/Documents/<id>); нужен для записей журнала без linkId."""
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1].lower()
    if len(tail) != 32 or any(c not in "0123456789abcdef" for c in tail):
        return None
    return f"{tail[:8]}-{tail[8:12]}-{tail[12:16]}-{tail[16:20]}-{tail[20:]}"


@mcp.tool()
def elba_link_publish(
    document_id: str | None = None,
    link_id: str | None = None,
    published: bool = False,
    with_signatures: bool = True,
    qr_payment: bool = True,
) -> dict:
    """Снять публичную ссылку с публикации (published=false, по умолчанию) или вернуть её (published=true).
    Снимать, когда счёт отменён: иначе клиент откроет и оплатит его по старой ссылке.
    link_id — из elba_document_link; для счёта из журнала достаточно document_id.
    API не отдаёт текущие настройки ссылки, поэтому with_signatures и qr_payment задаются заново."""
    entry = _tracked(document_id) if document_id else None
    link_id = link_id or (entry and (entry.get("linkId") or _link_id(entry.get("link"))))
    if not link_id:
        raise ElbaError("не знаю id ссылки: укажите link_id (его вернул elba_document_link)")
    body = {"isPublished": published, "withSignatures": with_signatures, "isQrCodeEnabled": qr_payment}
    _request("PUT", f"/organizations/{_org()}/document-links/{link_id}", body)
    out = {"linkId": link_id, "published": published}
    if entry is not None:
        try:
            with _journal() as j:
                j["bills"][document_id].update(linkId=link_id, linkPublished=published)
        except Exception as e:
            out["journalWarning"] = f"ссылка изменена, журнал — нет: {e}"
    return out


@mcp.tool()
def elba_repeat_bill(
    bill_id: str,
    bill_date: str | None = None,
    items: list[dict] | None = None,
    comment: str | None = None,
    number: str | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Новый счёт по образцу прошлого: тот же клиент, позиции, НДС, банковский счёт, шаблон и проект.
    items — заменить позиции (формат elba_create_bill). Без items позиции копируются — проверьте названия:
    в них может остаться прошлый месяц. bill_date — по умолчанию сегодня. Дубли ловятся как в elba_create_bill."""
    bill = _request("GET", f"/organizations/{_org()}/bills/{bill_id}")[1]
    src = bill.get("warehouseItems") or []
    if items is not None and bill.get("withNDS") and any(not it.get("nds_rate") for it in items):
        # иначе _items подставит nds22 вместо ставки образца
        rates = {i.get("ndsRate") for i in src}
        if len(rates) != 1 or None in rates:
            raise ElbaError("в образце ставки НДС разные или неизвестны — укажите nds_rate у каждой позиции")
        rate = rates.pop()
        items = [{**it, "nds_rate": it.get("nds_rate") or rate} for it in items]
    if items is None:
        # скидки и цены с НДС внутри elba_create_bill не передаёт — копия разошлась бы с образцом по сумме
        if bill.get("withDiscount") or bill.get("sumsWithNds") or any(i.get("discount") for i in src):
            raise ElbaError("в счёте скидка или цены с НДС внутри — передайте items явно")
        # неизвестное количество или ставка иначе молча станут 1 и nds22
        need = ("productName", "quantity", "price") + (("ndsRate",) if bill.get("withNDS") else ())
        if any(i.get(k) is None for i in src for k in need):
            raise ElbaError(f"Эльба вернула позиции без {', '.join(need)} — передайте items явно")
        items = [{k: v for k, v in {
            "name": i.get("productName"), "unit": i.get("unitName"), "quantity": i.get("quantity"),
            "price": i.get("price"), "nds_rate": i.get("ndsRate") if bill.get("withNDS") else None,
        }.items() if v is not None} for i in src]
    if not items:
        raise ElbaError(f"в счёте {bill_id} нет позиций")
    project = (_tracked(bill_id) or {}).get("project")
    with _exclusive():
        return _create_bill(bill.get("contractorId"), items, bill_date, number, comment, bool(bill.get("withNDS")),
                            bill.get("bankAccountId"), bill.get("templateId"), project, allow_duplicate)


def _fns(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ElbaError(f"ФНС: HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise ElbaError(f"ФНС: {type(e).__name__} {e}") from None


@mcp.tool()
def elba_lookup_inn(inn: str) -> list[dict]:
    """Реквизиты организации или ИП по ИНН из ЕГРЮЛ/ЕГРИП ФНС (egrul.nalog.ru, без ключа):
    полное и краткое название, КПП, ОГРН, руководитель, дата регистрации, дата прекращения деятельности.
    Адреса поиск ФНС не отдаёт. Для elba_create_contractor: name — полное название, short_name — краткое.
    Сверьте с реквизитами от клиента; terminated — деятельность прекращена, счёт такому лицу выставлять нельзя."""
    inn = inn.strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        raise ElbaError("ИНН — 10 цифр у организации, 12 у ИП")
    headers = {"User-Agent": f"elba-mcp/{__version__}", "Accept": "application/json"}
    first = _fns(urllib.request.Request("https://egrul.nalog.ru/", method="POST", headers=headers,
                                        data=urllib.parse.urlencode({"query": inn}).encode()))
    if first.get("captchaRequired") or not first.get("t"):
        raise ElbaError("ФНС просит капчу — повторите позже или возьмите реквизиты у клиента")
    for attempt in range(5):  # результат готовится на стороне ФНС, обычно сразу
        res = _fns(urllib.request.Request(f"https://egrul.nalog.ru/search-result/{first['t']}", headers=headers))
        if "rows" in res:
            break
        time.sleep(0.5)
    else:
        raise ElbaError("ФНС не вернула результат поиска")
    return [{k: v for k, v in {
        "name": r.get("n"), "shortName": r.get("c"), "inn": r.get("i"), "kpp": r.get("p"), "ogrn": r.get("o"),
        "type": "ИП" if r.get("k") == "fl" else "организация", "head": r.get("g"), "registered": r.get("r"),
        "region": r.get("rn"), "terminated": r.get("e"),
    }.items() if v} for r in res["rows"] if r.get("i") == inn]


@mcp.prompt(title="Выставить счёт")
def elba_invoice(client: str, work: str, amount: str) -> str:
    """Порядок выставления счёта клиенту со ссылкой и черновиком письма."""
    return f"""Выстави счёт в Контур.Эльбе. Клиент: {client}. Работы: {work}. Сумма: {amount}.

1. Найди клиента: elba_find_contractor по ИНН, иначе по названию; несколько совпадений — спроси, какой.
   Клиента нет — elba_lookup_inn по ИНН, покажи реквизиты и после подтверждения elba_create_contractor.
2. Шаблон — elba_templates; неясно, какой подходит, — спроси.
3. До создания покажи клиента, позиции, сумму, НДС, дату и шаблон и дождись подтверждения.
4. elba_create_bill с project (сайт или проект клиента); сверь номер и total с суммой.
5. elba_document_link — ссылка для клиента.
6. Письмо со ссылкой — только черновиком, если есть почтовый инструмент; сам не отправляй.
   Адрес — из elba_get_contractor."""


@mcp.prompt(title="Закрыть работы по счёту")
def elba_close_work(bill: str, document: str = "акт") -> str:
    """Акт или УПД по выставленному счёту и ссылка для клиента."""
    return f"""Закрой работы по счёту {bill} документом: {document}.

1. Найди счёт: elba_report (поле bills) по номеру или id; нет в журнале — спроси id и добавь elba_track_bill.
2. elba_get_bill: не оплачен — предупреди об этом.
3. До создания покажи клиента, позиции, сумму, основание (счёт № от) и дату документа (по умолчанию сегодня)
   и дождись подтверждения — всегда, даже если счёт оплачен.
4. Акт — elba_act_from_bill, УПД — elba_upd_from_bill. Уже создан — второй не делай, скажи об этом.
5. elba_document_link на новый документ.
6. Письмо со ссылкой — только черновиком, сам не отправляй."""


@mcp.prompt(title="Напомнить должникам")
def elba_remind_debtors(min_days: int = 7) -> str:
    """Черновики напоминаний по неоплаченным счетам."""
    return f"""Подготовь напоминания по неоплаченным счетам старше {min_days} дн.

1. elba_payment_news — что оплачено с прошлой проверки; elba_unpaid — кто должен.
2. Для каждого счёта с days ≥ {min_days}: контакт — elba_get_contractor; остаток — total − paid;
   ссылка — link из elba_unpaid (нет — elba_document_link).
3. Вежливый черновик письма: номер и дата счёта, остаток, ссылка на оплату. Только черновики, не отправляй.
4. Итог списком: клиент, счёт, остаток, дней, подготовлен ли черновик."""


def _cli(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] != "call":
        sys.exit("использование: elba-mcp call <инструмент> [args.json]")
    fn = globals().get(argv[1])
    if not callable(fn) or not argv[1].startswith("elba_"):
        sys.exit(f"нет инструмента {argv[1]}")
    args = json.loads(Path(argv[2]).read_text(encoding="utf-8")) if len(argv) > 2 else {}
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
