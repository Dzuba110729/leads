"""Веб-CRM: дашборд, канбан заявок, ловец лидов. Логин/пароль + cookie-сессия, /api/reset (ТЗ 4.6-4.7).

Рендер — Jinja2 + HTMX (templates/, static/): HTMX-запросы получают партиалы,
обычные GET/redirect — полную страницу. Раздел /catcher — автовыгрузка чатов для
шага 1 «Ловец лидов» (см. .scratch/lead-catcher-auto/spec.md), отдельный от
основного конвейера 2-8.

Доступ защищён логином/паролем (auth.py, таблица db.users) вместо статического
токена в URL — CRM рассчитана на доступ с других устройств через Tailscale,
общий URL-токен для этого небезопасен (светится в истории браузера/логах).
Пользователей создаёт manage_users.py, самостоятельной регистрации нет.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import bot as bot_module
import catcher_db
import catcher_pipeline
import db
from config import CONFIG

logger = logging.getLogger(__name__)

app = FastAPI(title="og1 CRM")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

STATUS_LABELS = {
    "заявка": "Заявка",
    "пробный_день": "Пробный день",
    "документы": "Документы",
    "договор": "Договор",
    "оплачено": "Оплачено",
    "зачислен": "Зачислен",
}

PUBLIC_PATHS = {"/login"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not request.session.get("username"):
        return RedirectResponse(url="/login")
    return await call_next(request)


# SessionMiddleware зарегистрирован ПОСЛЕ require_login — в Starlette порядок
# добавления определяет вложенность (последний добавленный = самый внешний),
# и должен выполниться раньше, чтобы request.session уже был доступен внутри require_login.
app.add_middleware(SessionMiddleware, secret_key=CONFIG.crm_secret_key, same_site="lax")


def _current_username(request: Request) -> str:
    return request.session.get("username", "")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _price_label(price: float | None) -> str:
    return f"{price:,.0f} ₽".replace(",", " ") if price else "по запросу"


def _lead_contact_link(o) -> str | None:
    keys = o.keys() if hasattr(o, "keys") else o
    username = o["lead_username"] if "lead_username" in keys else None
    tg_id = o["lead_tg_id"] if "lead_tg_id" in keys else None
    if username:
        return f"https://t.me/{username}"
    if tg_id:
        return f"tg://user?id={tg_id}"
    return None


def _order_dict(o) -> dict:
    d = dict(o)
    d["price_label"] = _price_label(o["price"])
    d["lead_contact_link"] = _lead_contact_link(o)
    return d


def _order_columns(orders: list) -> list[dict]:
    by_status: dict[str, list] = {status: [] for status in db.ORDER_STATUSES}
    for o in orders:
        by_status.setdefault(o["status"], []).append(o)
    return [
        {
            "status": status,
            "label": STATUS_LABELS.get(status, status),
            "orders": [_order_dict(o) for o in by_status[status]],
        }
        for status in db.ORDER_STATUSES
    ]


# --- логин ------------------------------------------------------------------

@app.get("/login")
async def login_page(request: Request):
    if request.session.get("username"):
        return RedirectResponse(url="/")
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    with db.session() as conn:
        user = db.get_user_by_username(conn, username)
    if user is None or not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "error": "Неверный логин или пароль"}, status_code=401
        )
    request.session["username"] = user["username"]
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# --- дашборд -------------------------------------------------------------------

@app.get("/")
async def dashboard(request: Request):
    with db.session() as conn:
        orders = db.list_open_orders(conn)
        leads_count = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
        catcher_db.migrate(conn)
        new_candidates_count = conn.execute(
            "SELECT COUNT(*) AS n FROM catch_candidates WHERE status = 'new'"
        ).fetchone()["n"]

    open_orders_sum = sum(o["price"] or 0 for o in orders)
    context = {
        "request": request,
        "active": "dashboard",
        "username": _current_username(request),
        "leads_count": leads_count,
        "open_orders_count": len(orders),
        "open_orders_sum_label": _price_label(open_orders_sum) if open_orders_sum else "0 ₽",
        "new_candidates_count": new_candidates_count,
        "recent_orders": [
            {**_order_dict(o), "status": o["status"]} for o in orders[:5]
        ],
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


# --- заявки ----------------------------------------------------------------------

@app.get("/orders")
async def orders_page(request: Request):
    with db.session() as conn:
        orders = db.list_open_orders(conn)
    context = {
        "request": request,
        "active": "orders",
        "username": _current_username(request),
        "columns": _order_columns(orders),
    }
    return templates.TemplateResponse(request, "orders.html", context)


@app.post("/orders/{order_id}/advance")
async def advance(request: Request, order_id: int):
    with db.session() as conn:
        order = db.get_order(conn, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        new_status = db.advance_status(conn, order_id)
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (order["lead_id"],)).fetchone()
        orders = db.list_open_orders(conn)

    if lead is not None and CONFIG.bot_token:
        try:
            bot = bot_module.build_bot()
            await bot_module.notify_status_change(
                bot, lead["tg_id"], f"Статус вашей заявки в og1 обновлён: {new_status}"
            )
            await bot.session.close()
        except Exception:
            logger.exception("failed to notify lead about status change")

    if _is_htmx(request):
        return templates.TemplateResponse(request, "_orders_board.html", {"request": request, "columns": _order_columns(orders)}
        )
    return RedirectResponse(url="/orders", status_code=303)


@app.post("/api/reset")
async def reset(request: Request) -> dict:
    with db.session() as conn:
        db.reset(conn)
    return {"ok": True}


# --- /catcher: автовыгрузка чатов для шага 1 «Ловец лидов» -------------------------------------

def _catcher_candidate_dict(c) -> dict:
    d = dict(c)
    username = d.get("author_username")
    tg_id = d.get("author_tg_id")
    d["lead_link"] = f"https://t.me/{username}" if username else (f"tg://user?id={tg_id}" if tg_id else None)
    return d


@app.get("/catcher")
async def catcher_index(request: Request):
    with db.session() as conn:
        catcher_db.migrate(conn)
        sources = catcher_db.list_sources(conn)
        candidates = [_catcher_candidate_dict(c) for c in catcher_db.list_candidates(conn)]
    context = {
        "request": request,
        "active": "catcher",
        "username": _current_username(request),
        "sources": sources,
        "candidates": candidates,
    }
    return templates.TemplateResponse(request, "catcher.html", context)


@app.post("/catcher/sources")
async def catcher_add_source(request: Request, platform: str = Form(...), url: str = Form(...)):
    if platform not in ("tg", "vk"):
        raise HTTPException(status_code=400, detail="platform must be tg or vk")
    with db.session() as conn:
        catcher_db.migrate(conn)
        catcher_db.add_source(conn, platform, url)
        sources = catcher_db.list_sources(conn)

    if _is_htmx(request):
        return templates.TemplateResponse(request, "_catcher_sources.html", {"request": request, "sources": sources}
        )
    return RedirectResponse(url="/catcher", status_code=303)


@app.post("/catcher/sources/{source_id}/toggle")
async def catcher_toggle_source(request: Request, source_id: int):
    with db.session() as conn:
        catcher_db.migrate(conn)
        source = catcher_db.get_source(conn, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        catcher_db.set_source_enabled(conn, source_id, not source["enabled"])
        source = catcher_db.get_source(conn, source_id)

    if _is_htmx(request):
        return templates.TemplateResponse(request, "_catcher_source_row.html", {"request": request, "s": source}
        )
    return RedirectResponse(url="/catcher", status_code=303)


@app.post("/catcher/sources/{source_id}/fetch")
async def catcher_fetch(request: Request, source_id: int):
    """Синхронный вызов «Выгрузить сейчас» — без фоновой очереди (MVP-решение по спеке)."""
    with db.session() as conn:
        catcher_db.migrate(conn)
        source = catcher_db.get_source(conn, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")

        if source["platform"] == "tg":
            import catcher_tg

            await catcher_tg.fetch_new_messages(conn, source)
        else:
            import catcher_vk

            await asyncio.to_thread(catcher_vk.fetch_new_messages, conn, source)

        catcher_pipeline.process_source(conn, source_id)
        source = catcher_db.get_source(conn, source_id)

    if _is_htmx(request):
        return templates.TemplateResponse(request, "_catcher_source_row.html", {"request": request, "s": source}
        )
    return RedirectResponse(url="/catcher", status_code=303)


@app.post("/catcher/candidates/{candidate_id}/contacted")
async def catcher_mark_contacted(request: Request, candidate_id: int):
    with db.session() as conn:
        catcher_db.migrate(conn)
        catcher_db.mark_contacted(conn, candidate_id)
        candidate = next((c for c in catcher_db.list_candidates(conn) if c["id"] == candidate_id), None)
        candidate = _catcher_candidate_dict(candidate) if candidate else None

    if _is_htmx(request):
        return templates.TemplateResponse(request, "_catcher_candidate_card.html", {"request": request, "c": candidate}
        )
    return RedirectResponse(url="/catcher", status_code=303)
