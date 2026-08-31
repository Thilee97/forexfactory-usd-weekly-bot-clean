from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cloudscraper
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont


FF_THISWEEK_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_CALENDAR_URL = "https://www.forexfactory.com/calendar?week={week}"
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

STATE_PATH = Path("state/state.json")
IMAGE_PATH = Path("usd_calendar_week.png")

IMAGE_CURRENCY = "USD"
NOTIFY_IMPACTS = {"High", "Medium"}
REMINDER_MINUTES = (30, 10)
ACTUAL_LOOKBACK_MINUTES = 45
ACTUAL_MAX_ATTEMPTS = 4
WEEKLY_SEND_WEEKDAY = 0
WEEKLY_SEND_HOUR = 7


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
CHAT_ID = require_env("TELEGRAM_CHAT_ID")
FORCE_WEEKLY = os.getenv("FORCE_WEEKLY", "false").lower() == "true"


def telegram_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


# Reply keyboard đã được tắt theo yêu cầu người dùng
# Bot sẽ chỉ gửi text thuần, không kèm nút bấm
MENU_KEYBOARD = None


def send_message(text: str, show_menu: bool = False) -> None:
    data = {"chat_id": CHAT_ID, "text": text}
    if show_menu and MENU_KEYBOARD is not None:
        data["reply_markup"] = json.dumps(MENU_KEYBOARD, ensure_ascii=False)
    r = requests.post(
        telegram_url("sendMessage"),
        data=data,
        timeout=30,
    )
    r.raise_for_status()


def send_photo(path: Path, caption: str, show_menu: bool = False) -> None:
    data = {"chat_id": CHAT_ID, "caption": caption}
    if show_menu and MENU_KEYBOARD is not None:
        data["reply_markup"] = json.dumps(MENU_KEYBOARD, ensure_ascii=False)
    with path.open("rb") as f:
        r = requests.post(
            telegram_url("sendPhoto"),
            data=data,
            files={"photo": (path.name, f, "image/png")},
            timeout=60,
        )
    r.raise_for_status()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"weekly_sent": "", "reminders": {}, "actual_sent": {}, "actual_attempts": {}, "telegram_last_update_id": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"weekly_sent": "", "reminders": {}, "actual_sent": {}, "actual_attempts": {}, "telegram_last_update_id": 0}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def event_id(event: dict[str, Any]) -> str:
    raw = f"{event.get('country','')}|{event.get('title','')}|{event.get('date','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]



def fetch_weekly_events() -> list[dict[str, Any]]:
    """Fetch the official Forex Factory export for the current week."""
    r = requests.get(
        FF_THISWEEK_JSON_URL,
        headers={"User-Agent": "Mozilla/5.0 forexfactory-usd-weekly-bot/1.0"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Forex Factory weekly JSON did not return a list.")

    events: list[dict[str, Any]] = []
    for raw in data:
        if raw.get("country") != IMAGE_CURRENCY:
            continue
        try:
            source_dt = datetime.fromisoformat(raw["date"])
        except Exception:
            continue
        event = dict(raw)
        event["source_dt"] = source_dt
        event["local_dt"] = source_dt.astimezone(LOCAL_TZ)
        event["id"] = event_id(event)
        event["actual"] = ""
        events.append(event)

    events.sort(key=lambda e: e["source_dt"])
    return events


def _impact_from_cell(cell) -> str:
    if cell is None:
        return ""
    parts = [cell.get_text(" ", strip=True), cell.get("title", "")]
    for node in cell.find_all(True):
        parts.append(node.get("title", ""))
        parts.extend(node.get("class", []))
    probe = " ".join(str(x) for x in parts).lower()
    if "high" in probe or "impact-red" in probe:
        return "High"
    if "medium" in probe or "med impact" in probe or "impact-ora" in probe or "impact-orange" in probe:
        return "Medium"
    if "low" in probe or "impact-yel" in probe or "impact-yellow" in probe:
        return "Low"
    return ""


def _page_timezone(soup) -> ZoneInfo:
    page_text = soup.get_text(" ", strip=True)
    match = re.search(r"Calendar Time Zone:\s*([A-Za-z_]+/[A-Za-z_]+)", page_text)
    if match:
        try:
            return ZoneInfo(match.group(1))
        except Exception:
            pass
    return ZoneInfo("America/New_York")


def _next_sunday(base_date):
    days = (6 - base_date.weekday()) % 7
    if days == 0:
        days = 7
    return base_date + timedelta(days=days)


def _parse_row_datetime(row, date_text: str, time_text: str, page_tz: ZoneInfo, target_sunday):
    candidates = [row.get("data-event-datetime", "")]
    for node in row.find_all(True):
        value = node.get("data-event-datetime", "")
        if value:
            candidates.append(value)
    for raw in candidates:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=page_tz)
            local = dt.astimezone(LOCAL_TZ)
            return local, local.strftime("%a %d/%m  %H:%M")
        except Exception:
            pass

    date_match = re.search(r"(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat)?\s*([A-Za-z]{3})\s+(\d{1,2})", date_text or "")
    if date_match:
        mon, day = date_match.groups()
        dates = []
        for year in (target_sunday.year - 1, target_sunday.year, target_sunday.year + 1):
            try:
                dates.append(datetime.strptime(f"{mon} {day} {year}", "%b %d %Y").date())
            except Exception:
                pass
        event_date = min(dates, key=lambda d: abs((d - target_sunday).days)) if dates else target_sunday
    else:
        event_date = target_sunday

    raw_time = (time_text or "").strip()
    normalized = raw_time.replace(" ", "").upper()
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            tm = datetime.strptime(normalized, fmt).time()
            dt = datetime.combine(event_date, tm, tzinfo=page_tz).astimezone(LOCAL_TZ)
            return dt, dt.strftime("%a %d/%m  %H:%M")
        except Exception:
            pass

    dt = datetime.combine(event_date, datetime.min.time(), tzinfo=page_tz).astimezone(LOCAL_TZ)
    label = f"{dt.strftime('%a %d/%m')}  {raw_time or 'Tentative'}"
    return dt, label


def fetch_next_week_events() -> list[dict[str, Any]]:
    """Fetch next week's USD events from the Forex Factory calendar page.

    Forex Factory's export link is `ff_calendar_thisweek.json`; there is no
    `ff_calendar_nextweek.json`, so next week is read from `calendar?week=next`
    only when the user presses the Telegram button.
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    r = scraper.get(FF_CALENDAR_URL.format(week="next"), timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", {"class": "calendar__table"})
    if table is None:
        raise RuntimeError("Could not locate Forex Factory next-week calendar table.")

    page_tz = _page_timezone(soup)
    target_sunday = _next_sunday(datetime.now(page_tz).date())
    current_date = ""
    current_time = ""
    events: list[dict[str, Any]] = []

    for row in table.find_all("tr", {"class": "calendar__row"}):
        def cell_text(cls: str) -> str:
            cell = row.find("td", {"class": cls})
            return cell.get_text(" ", strip=True) if cell else ""

        date_text = cell_text("calendar__date")
        time_text = cell_text("calendar__time")
        if date_text:
            current_date = date_text
        if time_text:
            current_time = time_text

        currency = cell_text("calendar__currency")
        title = cell_text("calendar__event")
        if currency != IMAGE_CURRENCY or not title:
            continue

        local_dt, display_time = _parse_row_datetime(
            row, current_date, current_time, page_tz, target_sunday
        )
        impact_cell = row.find("td", {"class": "calendar__impact"})
        event = {
            "title": title,
            "country": currency,
            "date": local_dt.isoformat(),
            "impact": _impact_from_cell(impact_cell),
            "actual": cell_text("calendar__actual"),
            "forecast": cell_text("calendar__forecast"),
            "previous": cell_text("calendar__previous"),
            "source_dt": local_dt,
            "local_dt": local_dt,
            "display_time": display_time,
        }
        event["id"] = event_id(event)
        events.append(event)

    events.sort(key=lambda e: e["local_dt"])
    if not events:
        raise RuntimeError("Next-week page was read, but no USD events were found.")
    return events

def week_key(events: list[dict[str, Any]]) -> str:
    if not events:
        now = datetime.now(LOCAL_TZ)
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    start = min(e["local_dt"].date() for e in events)
    end = max(e["local_dt"].date() for e in events)
    return f"{start.isoformat()}_{end.isoformat()}"


def ff_week_parameter(events: list[dict[str, Any]]) -> str:
    if not events:
        return datetime.now().strftime("%b%d.%Y").lower()
    first_source_date = min(e["source_dt"] for e in events).date()
    return first_source_date.strftime("%b%d.%Y").lower()


def scrape_current_week() -> list[dict[str, str]]:
    events = fetch_weekly_events()
    week = ff_week_parameter(events)
    url = FF_CALENDAR_URL.format(week=week)

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )
    r = scraper.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", {"class": "calendar__table"})
    if table is None:
        raise RuntimeError("Could not locate Forex Factory calendar table.")

    rows = table.find_all("tr", {"class": "calendar__row"})
    result: list[dict[str, str]] = []

    for row in rows:
        def cell_text(cls: str) -> str:
            cell = row.find("td", {"class": cls})
            return cell.get_text(" ", strip=True) if cell else ""

        currency = cell_text("calendar__currency")
        title = cell_text("calendar__event")
        if not currency or not title:
            continue

        result.append(
            {
                "currency": currency,
                "title": title,
                "actual": cell_text("calendar__actual"),
                "forecast": cell_text("calendar__forecast"),
                "previous": cell_text("calendar__previous"),
            }
        )
    return result


def merge_live_values(events: list[dict[str, Any]], live_rows: list[dict[str, str]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in live_rows:
        groups[(row["currency"], row["title"])].append(row)

    used: dict[tuple[str, str], int] = defaultdict(int)
    for event in events:
        key = (event["country"], event["title"])
        idx = used[key]
        rows = groups.get(key, [])
        if idx < len(rows):
            row = rows[idx]
            event["actual"] = row.get("actual", "")
            if row.get("forecast"):
                event["forecast"] = row["forecast"]
            if row.get("previous"):
                event["previous"] = row["previous"]
            used[key] += 1


def impact_symbol(impact: str) -> str:
    return {"High": "🔴", "Medium": "🟠", "Low": "🟡"}.get(impact, "⚪")


def impact_label(impact: str) -> str:
    return {"High": "HIGH", "Medium": "MED", "Low": "LOW"}.get(impact, impact.upper())


def value_or_dash(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "—"


def parse_numeric(value: str) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([KMBT%]?)", value, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).upper()
    mult = {"": 1, "%": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[unit]
    return number * mult


def comparison_text(actual: str, forecast: str) -> str:
    a = parse_numeric(actual)
    f = parse_numeric(forecast)
    if a is None or f is None:
        return ""
    if a > f:
        return "📈 Actual cao hơn Forecast"
    if a < f:
        return "📉 Actual thấp hơn Forecast"
    return "➖ Actual bằng Forecast"


def font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def truncate(draw: ImageDraw.ImageDraw, text: str, max_width: int, fnt) -> str:
    if draw.textbbox((0, 0), text, font=fnt)[2] <= max_width:
        return text
    suffix = "…"
    while text:
        text = text[:-1]
        if draw.textbbox((0, 0), text + suffix, font=fnt)[2] <= max_width:
            return text + suffix
    return suffix


def render_week_image(events: list[dict[str, Any]], output: Path) -> None:
    width = 1500
    header_h = 125
    columns_h = 58
    row_h = 62
    footer_h = 50
    height = header_h + columns_h + row_h * max(1, len(events)) + footer_h

    img = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(img)

    f_title = font(38, bold=True)
    f_sub = font(22)
    f_head = font(22, bold=True)
    f_row = font(22)
    f_small = font(18)

    if events:
        start = min(e["local_dt"].date() for e in events)
        end = max(e["local_dt"].date() for e in events)
        date_range = f"{start.strftime('%d/%m/%Y')} - {end.strftime('%d/%m/%Y')}"
    else:
        date_range = "Không có dữ liệu"

    draw.rectangle((0, 0, width, header_h), fill=(41, 61, 91))
    draw.text((35, 25), "FOREX FACTORY — USD WEEKLY CALENDAR", font=f_title, fill="white")
    draw.text(
        (37, 78),
        f"Tuần {date_range}  •  Giờ Việt Nam (GMT+7)",
        font=f_sub,
        fill=(225, 232, 242),
    )

    cols = [
        ("Thời gian", 35),
        ("Impact", 220),
        ("Sự kiện", 360),
        ("Actual", 1020),
        ("Forecast", 1160),
        ("Previous", 1310),
    ]

    y = header_h
    draw.rectangle((0, y, width, y + columns_h), fill=(220, 226, 235))
    for name, x in cols:
        draw.text((x, y + 15), name, font=f_head, fill=(35, 45, 60))

    y += columns_h
    for i, event in enumerate(events):
        bg = (255, 255, 255) if i % 2 == 0 else (238, 242, 247)
        draw.rectangle((0, y, width, y + row_h), fill=bg)

        time_label = event.get("display_time") or event["local_dt"].strftime("%a %d/%m  %H:%M")
        draw.text(
            (35, y + 17),
            time_label,
            font=f_row,
            fill=(25, 35, 48),
        )

        impact = event.get("impact", "")
        impact_fill = {
            "High": (196, 48, 43),
            "Medium": (231, 132, 31),
            "Low": (222, 180, 38),
        }.get(impact, (130, 140, 150))

        cx, cy, radius = 278, y + 31, 10
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=impact_fill)

        title = truncate(draw, event.get("title", ""), 625, f_row)
        draw.text((360, y + 17), title, font=f_row, fill=(25, 35, 48))
        draw.text((1020, y + 17), value_or_dash(event.get("actual")), font=f_row, fill=(25, 35, 48))
        draw.text((1160, y + 17), value_or_dash(event.get("forecast")), font=f_row, fill=(25, 35, 48))
        draw.text((1310, y + 17), value_or_dash(event.get("previous")), font=f_row, fill=(25, 35, 48))

        y += row_h

    draw.text(
        (35, height - 36),
        "Nguồn: Forex Factory / Fair Economy  •  Thời gian có thể thay đổi",
        font=f_small,
        fill=(90, 100, 112),
    )
    img.save(output, "PNG", optimize=True)


def send_weekly_image(
    events: list[dict[str, Any]],
    state: dict[str, Any],
    key: str,
    title: str = "📅 LỊCH KINH TẾ USD TRONG TUẦN",
    mark_sent: bool = True,
    include_live: bool = True,
) -> None:
    if include_live:
        try:
            live_rows = scrape_current_week()
            merge_live_values(events, live_rows)
        except Exception as exc:
            print(f"[warning] Live table unavailable for weekly image: {exc}", file=sys.stderr)

    render_week_image(events, IMAGE_PATH)

    start_date = min(e["local_dt"].date() for e in events) if events else datetime.now(LOCAL_TZ).date()
    end_date = max(e["local_dt"].date() for e in events) if events else start_date

    caption = (
        f"{title}\n"
        f"🗓 {start_date.strftime('%d/%m')} - {end_date.strftime('%d/%m/%Y')}\n"
        "🕒 Giờ Việt Nam (GMT+7)\n"
        f"📊 {len(events)} sự kiện USD\n"
        "Nguồn: Forex Factory"
    )
    send_photo(IMAGE_PATH, caption)
    if mark_sent:
        state["weekly_sent"] = key


def send_next_week_image(state: dict[str, Any]) -> None:
    events = fetch_next_week_events()
    key = f"next_{week_key(events)}"
    send_weekly_image(
        events,
        state,
        key,
        title="⏭ LỊCH KINH TẾ USD TUẦN SAU",
        mark_sent=False,
        include_live=False,
    )

def process_reminders(events: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> None:
    reminder_state = state.setdefault("reminders", {})

    for event in events:
        if event.get("impact") not in NOTIFY_IMPACTS:
            continue

        minutes_to = (event["local_dt"] - now).total_seconds() / 60
        eid = event["id"]
        sent = set(reminder_state.get(eid, []))

        for threshold in REMINDER_MINUTES:
            if threshold - 5 < minutes_to <= threshold and threshold not in sent:
                msg = (
                    f"⏰ TIN USD SẮP RA — còn khoảng {threshold} phút\n\n"
                    f"{impact_symbol(event.get('impact',''))} {event.get('impact','')} Impact\n"
                    f"🇺🇸 {event.get('title','')}\n"
                    f"🕒 {event['local_dt'].strftime('%H:%M - %d/%m/%Y')} (GMT+7)\n\n"
                    f"Forecast: {value_or_dash(event.get('forecast'))}\n"
                    f"Previous: {value_or_dash(event.get('previous'))}\n"
                    "Nguồn: Forex Factory"
                )
                send_message(msg)
                sent.add(threshold)
                reminder_state[eid] = sorted(sent)


def process_actuals(events: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> None:
    """Low-request Actual polling.

    The weekly JSON handles normal schedule/reminder work.
    Live Forex Factory HTML is fetched only when a USD High/Medium event:
    - has reached release time,
    - is within 45 minutes after release,
    - has not sent Actual yet,
    - has fewer than 4 prior attempts.

    One page fetch serves all eligible events in that run.
    """
    attempts = state.setdefault("actual_attempts", {})
    actual_sent = state.setdefault("actual_sent", {})

    candidates: list[dict[str, Any]] = []
    for event in events:
        if event.get("impact") not in NOTIFY_IMPACTS:
            continue
        if actual_sent.get(event["id"]):
            continue

        age_minutes = (now - event["local_dt"]).total_seconds() / 60
        if age_minutes < 0 or age_minutes > ACTUAL_LOOKBACK_MINUTES:
            continue

        current_attempts = int(attempts.get(event["id"], 0) or 0)
        if current_attempts >= ACTUAL_MAX_ATTEMPTS:
            continue

        candidates.append(event)

    if not candidates:
        return

    try:
        live_rows = scrape_current_week()
    except Exception as exc:
        print(f"[warning] Could not scrape live Actual values: {exc}", file=sys.stderr)
        for event in candidates:
            attempts[event["id"]] = int(attempts.get(event["id"], 0) or 0) + 1
        return

    merge_live_values(events, live_rows)
    by_id = {e["id"]: e for e in events}

    for candidate in candidates:
        eid = candidate["id"]
        event = by_id[eid]
        attempts[eid] = int(attempts.get(eid, 0) or 0) + 1

        actual = str(event.get("actual") or "").strip()
        if not actual:
            continue

        comparison = comparison_text(actual, str(event.get("forecast") or ""))
        msg = (
            "🚨 KẾT QUẢ TIN USD\n\n"
            f"{impact_symbol(event.get('impact',''))} {event.get('impact','')} Impact\n"
            f"🇺🇸 {event.get('title','')}\n"
            f"🕒 {event['local_dt'].strftime('%H:%M - %d/%m/%Y')} (GMT+7)\n\n"
            f"Actual: {value_or_dash(actual)}\n"
            f"Forecast: {value_or_dash(event.get('forecast'))}\n"
            f"Previous: {value_or_dash(event.get('previous'))}"
        )
        if comparison:
            msg += f"\n\n{comparison}"
        msg += "\n\nNguồn: Forex Factory"

        send_message(msg)
        actual_sent[eid] = actual

def fetch_telegram_updates(last_update_id: int) -> list[dict[str, Any]]:
    params = {
        "offset": last_update_id + 1,
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }
    r = requests.get(telegram_url("getUpdates"), params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {payload}")
    return payload.get("result", [])


def format_event_line(event: dict[str, Any]) -> str:
    return (
        f"{impact_symbol(event.get('impact',''))} "
        f"{event['local_dt'].strftime('%d/%m %H:%M')} — "
        f"{event.get('title','')}"
    )


def send_menu() -> None:
    send_message(
        "📋 FOREX FACTORY USD BOT\n\n"
        "Chọn chức năng bên dưới:\n"
        "📅 Lịch USD tuần này — gửi ảnh lịch USD tuần hiện tại\n"
        "⏭ Tuần sau — gửi ảnh lịch USD của tuần kế tiếp\n"
        "⏰ Tin USD 24h — các tin trong 24 giờ tới\n"
        "🔴 High Impact — chỉ tin USD mức High\n"
        "🔄 Cập nhật ngay — kiểm tra lịch và Actual mới nhất\n"
        "ℹ️ Trạng thái bot — trạng thái dữ liệu/cron\n\n"
        "⏱ Bot chạy bằng GitHub Actions nên nút có thể phản hồi chậm 0–5 phút."
    )


def send_upcoming_24h(events: list[dict[str, Any]], now: datetime) -> None:
    upcoming = [
        e for e in events
        if now <= e["local_dt"] <= now + timedelta(hours=24)
    ]
    if not upcoming:
        send_message("⏰ Không có tin USD nào trong 24 giờ tới.")
        return

    lines = ["⏰ TIN USD TRONG 24 GIỜ TỚI", ""]
    for e in upcoming:
        lines.append(format_event_line(e))
        lines.append(
            f"   Forecast: {value_or_dash(e.get('forecast'))} | "
            f"Previous: {value_or_dash(e.get('previous'))}"
        )
    lines += ["", "🕒 Giờ Việt Nam (GMT+7)", "Nguồn: Forex Factory"]
    send_message("\n".join(lines))


def send_high_impact(events: list[dict[str, Any]], now: datetime) -> None:
    high = [e for e in events if e.get("impact") == "High" and e["local_dt"] >= now - timedelta(hours=12)]
    if not high:
        send_message("🔴 Tuần này không còn tin USD High Impact nào.")
        return

    lines = ["🔴 USD HIGH IMPACT — TUẦN NÀY", ""]
    for e in high:
        lines.append(format_event_line(e))
        lines.append(
            f"   Forecast: {value_or_dash(e.get('forecast'))} | "
            f"Previous: {value_or_dash(e.get('previous'))}"
        )
    lines += ["", "🕒 Giờ Việt Nam (GMT+7)", "Nguồn: Forex Factory"]
    send_message("\n".join(lines))


def send_status(events: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> None:
    upcoming = [e for e in events if e["local_dt"] >= now]
    next_event = upcoming[0] if upcoming else None
    lines = [
        "ℹ️ TRẠNG THÁI BOT",
        "",
        "✅ GitHub Actions: cấu hình chạy mỗi 5 phút",
        "✅ Forex Factory Weekly JSON: đọc được",
        f"📊 Sự kiện USD trong tuần: {len(events)}",
        f"📅 Tuần đã gửi ảnh: {state.get('weekly_sent') or 'Chưa'}",
        f"🕒 Kiểm tra lúc: {now.strftime('%H:%M:%S %d/%m/%Y')} GMT+7",
    ]
    if next_event:
        mins = max(0, int((next_event["local_dt"] - now).total_seconds() // 60))
        lines += [
            "",
            "⏭ Tin USD kế tiếp:",
            format_event_line(next_event),
            f"⏳ Còn khoảng {mins} phút",
        ]
    send_message("\n".join(lines))


def send_refresh(events: list[dict[str, Any]], now: datetime) -> None:
    live_ok = False
    try:
        live_rows = scrape_current_week()
        merge_live_values(events, live_rows)
        live_ok = True
    except Exception as exc:
        print(f"[warning] Manual refresh live scrape failed: {exc}", file=sys.stderr)

    recent = [
        e for e in events
        if now - timedelta(hours=6) <= e["local_dt"] <= now + timedelta(hours=12)
    ]
    lines = [
        "🔄 CẬP NHẬT FOREX FACTORY",
        f"🕒 {now.strftime('%H:%M:%S %d/%m/%Y')} GMT+7",
        f"🌐 Live Actual: {'OK' if live_ok else 'tạm thời không đọc được'}",
        "",
    ]
    if recent:
        for e in recent[:12]:
            lines.append(format_event_line(e))
            if e["local_dt"] <= now:
                lines.append(
                    f"   Actual: {value_or_dash(e.get('actual'))} | "
                    f"Forecast: {value_or_dash(e.get('forecast'))} | "
                    f"Previous: {value_or_dash(e.get('previous'))}"
                )
            else:
                lines.append(
                    f"   Forecast: {value_or_dash(e.get('forecast'))} | "
                    f"Previous: {value_or_dash(e.get('previous'))}"
                )
    else:
        lines.append("Không có tin USD gần thời điểm hiện tại.")
    lines += ["", "Nguồn: Forex Factory"]
    send_message("\n".join(lines))


def process_telegram_menu(
    events: list[dict[str, Any]],
    state: dict[str, Any],
    now: datetime,
    key: str,
) -> None:
    last_id = int(state.get("telegram_last_update_id", 0) or 0)
    try:
        updates = fetch_telegram_updates(last_id)
    except Exception as exc:
        print(f"[warning] Telegram menu polling failed: {exc}", file=sys.stderr)
        return

    for update in updates:
        update_id = int(update.get("update_id", 0))
        if update_id > int(state.get("telegram_last_update_id", 0) or 0):
            state["telegram_last_update_id"] = update_id

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != str(CHAT_ID):
            continue

        command = str(message.get("text") or "").strip()

        try:
            if command in {"/start", "/menu", "📋 Menu"}:
                send_menu()
            elif command == "📅 Lịch USD tuần này":
                send_weekly_image(events, state, key)
            elif command == "⏭ Tuần sau":
                send_next_week_image(state)
            elif command == "⏰ Tin USD 24h":
                send_upcoming_24h(events, now)
            elif command == "🔴 High Impact":
                send_high_impact(events, now)
            elif command == "🔄 Cập nhật ngay":
                send_refresh(events, now)
            elif command == "ℹ️ Trạng thái bot":
                send_status(events, state, now)
        except Exception as exc:
            print(f"[warning] Telegram command failed: {command!r}: {exc}", file=sys.stderr)
            try:
                send_message(
                    "⚠️ Không lấy được dữ liệu cho yêu cầu này ở lần chạy hiện tại. "
                    "Bot vẫn tiếp tục hoạt động; bạn có thể bấm lại sau."
                )
            except Exception:
                pass


def prune_state(state: dict[str, Any], valid_ids: set[str]) -> None:
    state["reminders"] = {
        k: v for k, v in state.get("reminders", {}).items() if k in valid_ids
    }
    state["actual_sent"] = {
        k: v for k, v in state.get("actual_sent", {}).items() if k in valid_ids
    }
    state["actual_attempts"] = {
        k: v for k, v in state.get("actual_attempts", {}).items() if k in valid_ids
    }


def main() -> None:
    now = datetime.now(LOCAL_TZ)
    state = load_state()
    events = fetch_weekly_events()
    key = week_key(events)
    valid_ids = {e["id"] for e in events}
    prune_state(state, valid_ids)

    should_send_weekly = (
        FORCE_WEEKLY
        or (
            now.weekday() == WEEKLY_SEND_WEEKDAY
            and now.hour >= WEEKLY_SEND_HOUR
            and state.get("weekly_sent") != key
        )
    )

    if should_send_weekly:
        print("Sending weekly USD calendar image...")
        send_weekly_image(events, state, key)

    process_telegram_menu(events, state, now, key)
    process_reminders(events, state, now)
    process_actuals(events, state, now)
    save_state(state)

    print(
        f"Done. USD events={len(events)}, week={key}, "
        f"time={now.isoformat(timespec='seconds')}"
    )


if __name__ == "__main__":
    main()
